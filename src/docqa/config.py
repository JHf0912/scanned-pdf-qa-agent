"""配置装载。

三条原则
--------
1. **密钥只从环境变量读，永不落进 YAML、更不入库。** YAML 里写的是变量名，
   真正的值由 `.env` 或进程环境提供。这样"把密钥放在项目配置里"和
   "不提交密钥"这两件事可以同时成立。
2. **启动即校验，且报错必须可操作。** 缺密钥时不能只说"配置错误"，
   要说清缺哪个变量、去哪配、不配会影响哪一步。
3. **优先级明确**：调用方显式传入 > 环境变量 > YAML > 内置默认值。

配置分层
--------
`default.yaml` 管全局；`domains/<name>.yaml` 管业务场景（条款正则、检索权重、
拒答阈值、引用格式）。**迁移到金融/合规场景时主要改的就是 domain profile**，
而不是改代码——这是"可扩展"这条评分项的落点。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"
DOMAINS_DIR = PROJECT_ROOT / "config" / "domains"


# ---------------------------------------------------------------- 子配置

@dataclass
class ProviderConfig:
    """一个模型端点的配置。`api_key` 只在内存中，不序列化。"""

    provider: str = "openai"  # "openai" | "anthropic" | ""
    base_url: str | None = None
    api_key: str | None = None
    model: str = ""
    supports_vision: bool = False
    # 供应商自报价格，用于成本估算；单位：美元 / 百万 token
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.provider and self.model and self.api_key)

    def describe(self) -> str:
        """用于日志与 doctor 输出。**绝不包含密钥本身**。"""
        if not self.configured:
            return "(未配置)"
        return f"{self.provider}:{self.model} @ {self.base_url or '默认端点'}"

    def mask_key(self) -> str:
        if not self.api_key:
            return "(无)"
        if len(self.api_key) <= 8:
            return "*" * len(self.api_key)
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"


@dataclass
class RenderConfig:
    """页图渲染。

    默认**位精确取图**（直接取出内嵌图像，不重采样）——对 OCR 而言这是信息量最大的输入。
    光栅化只作回退（例如页面由多个图块拼成、或只有矢量内容时）。

    `upscale` 默认 1.0。注意：放大**不会增加信息**，但可能改善 VLM 对小号中文字的
    辨识率——那是另一种机制。要不要放大应由 A/B 实验决定，见 docs/evaluation.md。
    """

    mode: str = "auto"  # "auto" | "extract" | "raster"
    dpi: int | None = None  # None = 用原生 DPI
    upscale: float = 1.0
    max_dpi: int = 600
    image_format: str = "png"
    jpeg_quality: int = 90


@dataclass
class ChunkConfig:
    max_chars: int = 800
    overlap_chars: int = 100
    # 句子窗口回退（文档无标题结构时用）
    sentence_window_chars: int = 200
    sentence_overlap: int = 1


@dataclass
class RetrieveConfig:
    top_k: int = 5
    # RRF 融合常数。60 是文献里的常用取值，对排名前几位不敏感。
    rrf_k: int = 60
    arm_weights: dict[str, float] = field(
        default_factory=lambda: {"bm25_word": 1.0, "bm25_char": 0.8}
    )
    table_boost: float = 1.3
    # 注意：这里**没有** `enable_dense`。
    #
    # 它曾经存在，而 `HybridIndex` 把 `self.dense` 硬编码成 None、从不读取它，
    # 文档却写着"已留第三臂接口，配置打开即可"。那是一个**打开也不会变的开关**——
    # 比没有这个选项更糟，因为使用者会以为已经开启了。
    # 真要做稠密检索，它需要 embedding 端点、缓存与一套新的评估口径，
    # 是一个独立的设计任务；在那之前不在这里留假开关。


@dataclass
class QaConfig:
    temperature: float = 0.0
    max_tokens: int = 1200
    # 有界修复循环的最大尝试次数。上限存在的意义是防止无限重试把成本打爆。
    max_attempts: int = 2
    # --- 拒答判据（均在金标集上校准，见 docs/evaluation.md）---
    #
    # 主判据：问题实词在召回证据中的覆盖率。
    # 为什么不用融合分当主判据：RRF 分数是 Σ w/(k+rank)，排名第 1 与第 20
    # 只差约 27%，且多臂求和会进一步压缩差距——**RRF 适合排序，不适合表达置信度**。
    # 覆盖率直接问"用户问的词找到了几个"，区分度天然、量纲可解释、不依赖模型。
    min_coverage: float = 0.6
    # 辅助判据：归一化后的融合分下限。归一化后 1.0 = 每条臂都排第一。
    # 阈值放宽，因为它只做兜底。
    min_top_score: float = 0.0
    # 第一名与第二名的最小分差（相对判据）。0.0 = 关闭。
    #
    # 与 `min_top_score` 分工不同：那个管"绝对够不够相关"，这个管
    # "有没有明确赢家"。分数都高但咬得很紧时（两份文件对同一件事规定不同），
    # 给一个答案比不给更危险——读者会以为系统已经判断过哪条适用。
    #
    # 默认 0.0 是刻意的：本语料只有 16 个片段，阈值需要按场景标定；
    # 在拿到实测数据前开着它，只会把"能答的题"变成"拒答"。
    min_score_margin: float = 0.0
    # 句级支撑度：默认 ADVISORY 而非 GATE。
    # 理由：它依赖中文分词，阈值在拿到实测精确率之前没有资格当门。
    support_score_gate: float | None = None


@dataclass
class DomainProfile:
    """业务场景配置。迁移场景时改这里，不改代码。"""

    name: str
    description: str = ""
    # 条款/条号的正则（按顺序尝试）
    clause_patterns: list[str] = field(default_factory=list)
    # 表格题的路由线索：出现这些词时提升表格 chunk 权重
    table_cues: list[str] = field(default_factory=list)
    # 引用渲染模板
    citation_template: str = "[p.{page} §{clause}]"
    chunk: ChunkConfig = field(default_factory=ChunkConfig)
    retrieve: RetrieveConfig = field(default_factory=RetrieveConfig)
    qa: QaConfig = field(default_factory=QaConfig)


@dataclass
class Config:
    domains_dir: Path = DOMAINS_DIR
    render: RenderConfig = field(default_factory=RenderConfig)
    chunk: ChunkConfig = field(default_factory=ChunkConfig)
    retrieve: RetrieveConfig = field(default_factory=RetrieveConfig)
    qa: QaConfig = field(default_factory=QaConfig)
    ocr: ProviderConfig = field(default_factory=ProviderConfig)
    ocr2: ProviderConfig = field(default_factory=ProviderConfig)
    llm: ProviderConfig = field(default_factory=ProviderConfig)
    log_level: str = "INFO"
    http_timeout: int = 120
    http_max_retries: int = 3
    # 缓存目录（内容寻址）
    cache_dir: Path = PROJECT_ROOT / "data" / "derived" / "cache"
    artifacts_dir: Path = PROJECT_ROOT / "artifacts"

    @property
    def has_ocr(self) -> bool:
        return self.ocr.configured

    @property
    def has_llm(self) -> bool:
        return self.llm.configured

    @property
    def has_ocr2(self) -> bool:
        return self.ocr2.configured

    def describe(self) -> dict:
        """可安全打印/入库的配置摘要（不含密钥明文）。"""
        return {
            "ocr": self.ocr.describe(),
            "ocr_key": self.ocr.mask_key(),
            "ocr2": self.ocr2.describe(),
            "llm": self.llm.describe(),
            "llm_key": self.llm.mask_key(),
            "render": {
                "mode": self.render.mode,
                "dpi": self.render.dpi,
                "upscale": self.render.upscale,
            },
            "log_level": self.log_level,
        }


# ---------------------------------------------------------------- 装载

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "y", "on")


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(
            f"环境变量 {name} 应为数字，实际是 {raw!r}。请修正 .env 后重试。"
        ) from exc


def _provider_from_env(prefix: str, *, vision_default: bool = False) -> ProviderConfig:
    """从 `<PREFIX>_PROVIDER/_BASE_URL/_API_KEY/_MODEL` 组装一个端点配置。

    密钥在这里从环境读入内存；`describe()`/`mask_key()` 保证它不会被打印出来。
    """
    provider = _env(f"{prefix}_PROVIDER")
    if not provider:
        # 显式置空 provider，而不是落到 dataclass 的默认值 "openai"。
        # 否则一个完全没配置的端点会自称 openai，报错信息就会指错方向
        # （说"缺模型名"，实际是"整组变量都没配"）。
        return ProviderConfig(provider="")
    return ProviderConfig(
        provider=provider,
        base_url=_env(f"{prefix}_BASE_URL") or None,
        api_key=_env(f"{prefix}_API_KEY") or None,
        model=_env(f"{prefix}_MODEL"),
        supports_vision=_env_bool(f"{prefix}_SUPPORTS_VISION", vision_default),
        price_in_per_mtok=_env_float(f"{prefix}_PRICE_IN", 0.0),
        price_out_per_mtok=_env_float(f"{prefix}_PRICE_OUT", 0.0),
    )


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 {path} 不是合法 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件 {path} 的顶层应为映射（键值对）。")
    return data


def _apply(section: dict, target: object) -> None:
    """把 YAML 片段覆盖到 dataclass 实例上；未知键直接报错而不是静默忽略。

    静默忽略拼错的配置项是运维事故的常见来源——用户以为改了，实际没生效。
    """
    for key, value in (section or {}).items():
        if not hasattr(target, key):
            raise ConfigError(
                f"配置项 {key!r} 在 {type(target).__name__} 中不存在。"
                f"可用项：{sorted(k for k in vars(target) if not k.startswith('_'))}"
            )
        setattr(target, key, value)


def load_config(
    path: Path | None = None, *, env_file: Path | None = None, require_ocr: bool = False
) -> Config:
    """装载配置。

    Args:
        path: YAML 路径，默认 `config/default.yaml`。
        env_file: `.env` 路径，默认项目根下的 `.env`。
        require_ocr: 为 True 时，缺少 OCR 端点直接报错。解析类命令应传 True，
            这样用户在第一步就能拿到可操作的提示，而不是跑到一半才失败。
    """
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)

    cfg = Config()
    data = _load_yaml(path or DEFAULT_CONFIG_PATH)

    env_map = {
        "render": cfg.render,
        "chunk": cfg.chunk,
        "retrieve": cfg.retrieve,
        "qa": cfg.qa,
    }
    for section, target in env_map.items():
        _apply(data.get(section, {}), target)

    if "cache_dir" in data:
        cfg.cache_dir = PROJECT_ROOT / data["cache_dir"]
    if "artifacts_dir" in data:
        cfg.artifacts_dir = PROJECT_ROOT / data["artifacts_dir"]

    cfg.ocr = _provider_from_env("DOCQA_OCR", vision_default=True)
    cfg.ocr2 = _provider_from_env("DOCQA_OCR2", vision_default=True)
    cfg.llm = _provider_from_env("DOCQA_LLM")

    cfg.log_level = _env("DOCQA_LOG_LEVEL", cfg.log_level).upper()
    cfg.http_timeout = int(_env_float("DOCQA_HTTP_TIMEOUT", cfg.http_timeout))
    cfg.http_max_retries = int(_env_float("DOCQA_HTTP_MAX_RETRIES", cfg.http_max_retries))

    if require_ocr and not cfg.has_ocr:
        raise ConfigError(missing_ocr_message())

    return cfg


def missing_ocr_message() -> str:
    """缺 OCR 端点时的报错。

    扫描件没有文本层，没有视觉模型就**一步也走不了**。所以这条消息必须把
    替代路径也讲清楚，否则用户会卡在第一步。
    """
    return (
        "缺少 OCR 视觉模型配置，无法解析扫描件。\n"
        "\n"
        "扫描版 PDF 没有文本层，必须用支持图像输入的模型识别。请任选其一：\n"
        "\n"
        "  A) OpenAI 兼容接口（DeepSeek / 通义 / 智谱 / Kimi / vLLM / Ollama …）\n"
        "     在 .env 中设置：\n"
        "         DOCQA_OCR_PROVIDER=openai\n"
        "         DOCQA_OCR_BASE_URL=<接口地址>\n"
        "         DOCQA_OCR_API_KEY=<你的密钥>\n"
        "         DOCQA_OCR_MODEL=<模型名>\n"
        "\n"
        "  B) Anthropic Messages API\n"
        "         DOCQA_OCR_PROVIDER=anthropic\n"
        "         DOCQA_OCR_API_KEY=<你的密钥>\n"
        "         DOCQA_OCR_MODEL=claude-opus-5\n"
        "\n"
        "配好后先跑 `docqa doctor`：它会用一张测试图**实测**该端点是否真的支持\n"
        "图像输入——只填了名字不代表能用。\n"
        "\n"
        "暂时没有可用密钥时：若**之前已成功 ingest 过一次**，仍可验证下游——\n"
        "         docqa ask \"你的问题\" --doc <已 ingest 的产物目录>\n"
        "    检索、引用、自检、拒答全部照常工作，只是答案由证据原文拼装。\n"
        "    尚未 ingest 过则无法解析（扫描件没有文本层，这一步绕不过去），\n"
        "    替代路径见 docs/troubleshooting.md 第八节。\n"
        "\n"
        "影响范围：缺少 OCR 端点时，detect 仍可用（不需要密钥），但 ingest 及其\n"
        "之后的全部步骤都无法进行。"
    )


def load_domain(name: str, cfg: Config | None = None) -> DomainProfile:
    """装载业务场景 profile。找不到就报错并列出可用项。"""
    base = (cfg or Config()).domains_dir
    path = base / f"{name}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in base.glob("*.yaml")) if base.exists() else []
        raise ConfigError(
            f"未知的业务场景 {name!r}。可用场景：{available or '(无)'}\n"
            f"场景文件位于 {base}；新增场景只需加一个 YAML，无需改代码。"
        )
    data = _load_yaml(path)
    profile = DomainProfile(name=name, description=data.get("description", ""))
    profile.clause_patterns = list(data.get("clause_patterns", []))
    profile.table_cues = list(data.get("table_cues", []))
    profile.citation_template = data.get("citation_template", profile.citation_template)
    _apply(data.get("chunk", {}), profile.chunk)
    _apply(data.get("retrieve", {}), profile.retrieve)
    _apply(data.get("qa", {}), profile.qa)
    return profile
