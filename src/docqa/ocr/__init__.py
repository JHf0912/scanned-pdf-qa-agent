"""OCR 引擎注册表。

新增引擎只需实现 `OcrEngine` 协议并在此注册。
`ingest` 通过名字取引擎，不直接依赖任何具体实现。
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config, ProviderConfig
from ..errors import ConfigError
from .base import OcrEngine, OcrEngineInfo, load_prompt
from .cache import OcrCache, Provenance, cache_key, prompt_sha256
from .fixture import FixtureOcrEngine
from .spotcheck import ClaimDiff, diff_claims, evaluate_spotcheck, spotcheck_page
from .textlayer import TextLayerEngine
from .vlm import VlmOcrEngine

__all__ = [
    "OcrEngine",
    "OcrEngineInfo",
    "OcrCache",
    "Provenance",
    "cache_key",
    "prompt_sha256",
    "load_prompt",
    "VlmOcrEngine",
    "FixtureOcrEngine",
    "TextLayerEngine",
    "ClaimDiff",
    "diff_claims",
    "spotcheck_page",
    "evaluate_spotcheck",
    "available_engines",
    "build_engine",
]

# 已注册的引擎名。
#   vlm       —— 主路径（视觉模型）
#   fixture   —— 仅测试（预置结果，机械隔离）
#   textlayer —— 原生文本层（不调用模型）
#   local     —— 预留位：日后接入本地 ONNX 引擎（RapidOCR 等）时在此注册即可。
#
# 注意 `textlayer` **不是**用户可选的主引擎：它由 `ingest` 按页类型自动使用，
# 因为"这一页有没有文本层"是 PDF 的客观属性，不该让人去选。
_REGISTRY = ("vlm", "fixture", "textlayer", "local")


def available_engines() -> tuple[str, ...]:
    return _REGISTRY


def build_engine(
    name: str,
    cfg: Config,
    *,
    prompt_path: Path | None = None,
    fixtures_dir: Path | None = None,
    provider_cfg: ProviderConfig | None = None,
    pdf_path: Path | None = None,
) -> OcrEngine:
    """按名字构造 OCR 引擎。

    Args:
        name: "vlm"（主路径）/ "fixture"（仅测试）/ "textlayer"（原生文本层）/
            "local"（预留，未实现）
        provider_cfg: 覆盖默认的 provider 配置。用于构造**第二引擎**
            （跨引擎一致性校验）——它走 `cfg.ocr2` 而不是 `cfg.ocr`。
        pdf_path: `textlayer` 引擎需要它来读文本层（页图之外还要访问 PDF 本身）。
    """
    if name not in _REGISTRY:
        raise ConfigError(f"未知的 OCR 引擎 {name!r}。可用：{list(_REGISTRY)}")

    if name == "fixture":
        if fixtures_dir is None:
            raise ConfigError("fixture 引擎需要 --fixtures-dir 指定预置结果目录。")
        return FixtureOcrEngine(fixtures_dir)

    if name == "textlayer":
        if pdf_path is None:
            raise ConfigError(
                "textlayer 引擎需要知道 PDF 路径（它直接读文件，不读页图）。\n"
                "调用方应传 pdf_path；`ingest` 会自动按页类型启用它，无需手动指定。"
            )
        return TextLayerEngine(pdf_path)

    if name == "local":
        raise ConfigError(
            "本地 OCR 引擎为预留位，尚未接入。\n"
            "接入方式：在 src/docqa/ocr/ 下实现 OcrEngine 协议（一个 ocr_page 方法），\n"
            "然后在 _REGISTRY 中替换掉 'local' 占位。\n"
            "候选方案（均无需 GPU、可离线）：RapidOCR（onnxruntime，纯 wheel，"
            "支持到 Python 3.13）+ RapidTable（表格结构，wheel 仅 43 KB）。"
        )

    from ..llm import build_ocr_provider

    provider = (build_provider_for(provider_cfg, cfg) if provider_cfg is not None
                else build_ocr_provider(cfg))
    return VlmOcrEngine(
        provider,
        cfg,
        prompt_path=prompt_path or _default_prompt_path(),
        # 第二引擎**不共用缓存**：共用的话它会把主引擎的结果当自己的返回，
        # 比对必然一致——又是一个永远不会失败的检查。
        cache=None if provider_cfg is not None else OcrCache(cfg.cache_dir),
    )


def build_provider_for(pcfg: ProviderConfig, cfg: Config):
    """按**指定的** provider 配置构造 LLM provider（跨引擎校验的第二路）。"""
    from ..llm import build_provider

    return build_provider(
        pcfg, timeout=cfg.http_timeout, max_retries=cfg.http_max_retries,
        what="第二 OCR 引擎",
    )


def _default_prompt_path() -> Path:
    root = Path(__file__).resolve().parents[3]
    return root / "prompts" / "ocr_page.v1.md"
