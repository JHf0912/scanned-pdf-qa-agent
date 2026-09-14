"""模型 provider 工厂。

按配置构造 provider，并在**配置不完整时给出可操作的错误**，
而不是等到第一次调用才抛一个晦涩的异常。
"""

from __future__ import annotations

from ..config import Config, ProviderConfig
from ..errors import ConfigError
from .base import (
    ImagePart,
    LLMProvider,
    LLMResponse,
    classify_status,
    estimate_cost,
    with_retry,
)
from .null import Evidence, NullProvider, assemble_extractive

__all__ = [
    "ImagePart",
    "LLMProvider",
    "LLMResponse",
    "NullProvider",
    "Evidence",
    "assemble_extractive",
    "build_provider",
    "build_ocr_provider",
    "build_llm_provider",
    "build_ocr2_provider",
    "classify_status",
    "estimate_cost",
    "with_retry",
]

# 支持的 provider 名。加新端点只需在这里注册 + 写一个适配器。
_KNOWN = ("openai", "anthropic")


def build_provider(
    cfg: ProviderConfig, *, timeout: int = 120, max_retries: int = 3, what: str = "模型"
) -> LLMProvider:
    """按配置构造一个 provider。

    Args:
        cfg: 端点配置。
        what: 出错时用于说明"是哪个用途的端点"（如 "OCR 识别" / "答案生成"），
            让报错能直接指向 .env 里该改哪一组变量。
    """
    if not cfg.provider:
        raise ConfigError(
            f"未配置{what}端点。请在 .env 中设置对应的 *_PROVIDER / *_API_KEY / *_MODEL。"
        )
    if cfg.provider not in _KNOWN:
        raise ConfigError(
            f"不支持的{what} provider：{cfg.provider!r}。"
            f"当前支持：{list(_KNOWN)}。"
            f"（如需接入新端点，在 src/docqa/llm/ 下加一个适配器并注册到 _KNOWN）"
        )
    if not cfg.model:
        raise ConfigError(f"{what}端点的模型名为空。请在 .env 中设置 *_MODEL。")
    if not cfg.api_key:
        raise ConfigError(
            f"{what}端点缺少密钥。请在 .env 中设置 *_API_KEY（该文件已在 .gitignore 中，不会入库）。"
        )
    if not cfg.api_key.isascii():
        # 密钥必须能放进 HTTP 头（ASCII）。含中文的密钥会在 httpx 构造请求头时
        # 抛出 `UnicodeEncodeError: 'ascii' codec can't encode characters`，
        # 那是一个从第三方库深处冒出来的、**完全无法定位**的错误——
        # 使用者看到的是一个 httpx 内部栈帧，而不是"你的密钥没填"。
        #
        # 最常见的成因：复制 .env.example 后**占位符没替换**就被当成了真值。
        # 所以这里直接点名，并给出该改哪个变量。
        raise ConfigError(
            f"{what}端点的密钥含有非 ASCII 字符，无法用于 HTTP 请求头。\n"
            f"\n"
            f"最常见的原因是 **.env 里的占位符还没有替换成真实密钥**。\n"
            f"请打开 .env，把对应的 *_API_KEY 改成真实密钥"
            f"（通常是 sk- 开头的纯 ASCII 字符串）。\n"
            f"\n"
            f"如果你只是想先跑通检索/问答/自检，**把该变量留空**即可：\n"
            f"生成侧会自动降级为抽取式回答，其余功能不受影响；\n"
            f"OCR 侧则必须有真实密钥（扫描件没有文本层，这一步绕不过去）。\n"
            f"\n"
            f"当前值打码后：{cfg.mask_key()}"
        )

    if cfg.provider == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(cfg, timeout=timeout, max_retries=max_retries)

    from .openai_compat import OpenAICompatProvider

    return OpenAICompatProvider(cfg, timeout=timeout, max_retries=max_retries)


def build_ocr_provider(cfg: Config) -> LLMProvider:
    """OCR 主力引擎。缺失时抛出的错误会讲清替代路径。"""
    if not cfg.has_ocr:
        raise ConfigError(_missing_ocr_hint())
    return build_provider(
        cfg.ocr, timeout=cfg.http_timeout, max_retries=cfg.http_max_retries, what="OCR 识别"
    )


def build_ocr2_provider(cfg: Config) -> LLMProvider | None:
    """跨引擎校验用的第二引擎。未配置时返回 None（这是允许的，会在报告里标注）。"""
    if not cfg.has_ocr2:
        return None
    return build_provider(
        cfg.ocr2, timeout=cfg.http_timeout, max_retries=cfg.http_max_retries, what="第二 OCR 引擎"
    )


def build_llm_provider(cfg: Config) -> LLMProvider:
    """生成模型。未配置时返回 NullProvider（抽取式降级），不抛错。"""
    if not cfg.has_llm:
        return NullProvider(cfg.llm)
    return build_provider(
        cfg.llm, timeout=cfg.http_timeout, max_retries=cfg.http_max_retries, what="答案生成"
    )


def _missing_ocr_hint() -> str:
    return (
        "缺少 OCR 视觉模型配置。\n"
        "扫描件没有文本层，必须有支持图像输入的模型才能解析。请在 .env 中配置：\n"
        "    DOCQA_OCR_PROVIDER=openai      # 或 anthropic\n"
        "    DOCQA_OCR_BASE_URL=<接口地址>   # anthropic 可留空\n"
        "    DOCQA_OCR_API_KEY=<密钥>\n"
        "    DOCQA_OCR_MODEL=<模型名>\n"
        "配好后先跑 `docqa doctor` —— 它会用一张测试图**实测**该端点是否真的支持图像输入。"
    )
