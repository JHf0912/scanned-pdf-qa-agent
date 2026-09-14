"""OCR 引擎注册表。

新增引擎只需实现 `OcrEngine` 协议并在此注册。
`ingest` 通过名字取引擎，不直接依赖任何具体实现。
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..errors import ConfigError
from .base import OcrEngine, OcrEngineInfo, load_prompt
from .cache import OcrCache, Provenance, cache_key, prompt_sha256
from .fixture import FixtureOcrEngine
from .spotcheck import ClaimDiff, diff_claims, evaluate_spotcheck, spotcheck_page
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
    "ClaimDiff",
    "diff_claims",
    "spotcheck_page",
    "evaluate_spotcheck",
    "available_engines",
    "build_engine",
]

# 已注册的引擎名。local 是预留位：日后接入本地 ONNX 引擎
# （如 RapidOCR / RapidTable）时在此注册即可，ingest 无需改动。
_REGISTRY = ("vlm", "fixture", "local")


def available_engines() -> tuple[str, ...]:
    return _REGISTRY


def build_engine(
    name: str,
    cfg: Config,
    *,
    prompt_path: Path | None = None,
    fixtures_dir: Path | None = None,
) -> OcrEngine:
    """按名字构造 OCR 引擎。

    Args:
        name: "vlm"（主路径）/ "fixture"（仅测试）/ "local"（预留，未实现）
    """
    if name not in _REGISTRY:
        raise ConfigError(f"未知的 OCR 引擎 {name!r}。可用：{list(_REGISTRY)}")

    if name == "fixture":
        if fixtures_dir is None:
            raise ConfigError("fixture 引擎需要 --fixtures-dir 指定预置结果目录。")
        return FixtureOcrEngine(fixtures_dir)

    if name == "local":
        raise ConfigError(
            "本地 OCR 引擎为预留位，尚未接入。\n"
            "接入方式：在 src/docqa/ocr/ 下实现 OcrEngine 协议（一个 ocr_page 方法），\n"
            "然后在 _REGISTRY 中替换掉 'local' 占位。\n"
            "候选方案（均无需 GPU、可离线）：RapidOCR（onnxruntime，纯 wheel，"
            "支持到 Python 3.13）+ RapidTable（表格结构，wheel 仅 43 KB）。"
        )

    from ..llm import build_ocr_provider

    provider = build_ocr_provider(cfg)
    return VlmOcrEngine(
        provider,
        cfg,
        prompt_path=prompt_path or _default_prompt_path(),
        cache=OcrCache(cfg.cache_dir),
    )


def _default_prompt_path() -> Path:
    root = Path(__file__).resolve().parents[3]
    return root / "prompts" / "ocr_page.v1.md"
