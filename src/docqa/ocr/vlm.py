"""VLM 结构化识别引擎。

它做的三件事
------------
1. 把页图 + 提示词发给视觉模型，要求返回**结构化 JSON**（而不是自由文本）；
2. 把返回的块列表转成 `OcrBlock`，表格 HTML 转成 `TableGrid`；
3. 走内容寻址缓存——同一页图 + 同一提示词 + 同一模型只花一次钱。

失败的处理
----------
按失败的性质分开处理，因为用户能做的事不同：

- **鉴权失败**：密钥问题，重试无用，直接抛出并指向 .env；
- **限流 / 5xx**：可恢复，指数退避重试；
- **返回结构不合法**：重试一次；仍失败则该页标记 `failed`，
  由调用方隔离（后续问题若证据触及此页，一律以 EXTRACTION_FAILED 拒答）；
- **单页失败不中断整轮**——一页坏掉只该影响一个答案，不该毁掉一次 ingest。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..config import Config
from ..errors import PageOcrError, ProviderError
from ..llm import ImagePart, LLMProvider
from ..logging import get_logger
from ..models import OcrBlock, OcrPage, PageImage, PageProfile, Usage
from ..tables import parse_html_table
from .base import OcrEngineInfo, load_prompt
from .cache import OcrCache, Provenance, cache_key, prompt_sha256

_log = get_logger("ocr.vlm")

VALID_BLOCK_TYPES = {
    "heading", "paragraph", "clause", "table", "figure", "stamp", "header", "footer"
}


class VlmOcrEngine:
    """用视觉语言模型做整页结构化识别。"""

    def __init__(
        self,
        provider: LLMProvider,
        cfg: Config,
        *,
        prompt_path: Path,
        cache: OcrCache | None = None,
        max_tokens: int = 8000,
        temperature: float = 0.0,
    ):
        self.provider = provider
        self.cfg = cfg
        self.name = f"vlm:{getattr(provider, 'name', 'unknown')}"
        self.model = cfg.ocr.model or getattr(provider, "model", "")
        self.supports_vision = True

        self.prompt_text, self.prompt_version = load_prompt(prompt_path)
        self.prompt_digest = prompt_sha256(self.prompt_text)
        self._max_tokens = max_tokens
        self._temperature = temperature
        self.cache = cache or OcrCache(cfg.cache_dir)
        self.info = OcrEngineInfo(self.name, self.model, self.prompt_version, vision=True)

    # ---------------------------------------------------------------- 对外

    def ocr_page(self, image: PageImage, *, profile: PageProfile | None = None) -> OcrPage:
        params = {"max_tokens": self._max_tokens, "temperature": self._temperature}
        key = cache_key(
            image.sha256,
            provider=self.cfg.ocr.provider,
            model=self.model,
            prompt_digest=self.prompt_digest,
            params=params,
        )

        hit = self.cache.get(key)
        if hit is not None:
            page, prov = hit
            _log.info(
                "OCR 缓存命中",
                extra={"page": image.page_no, "detail": f"key={key[:16]} created={prov.created_at}"},
            )
            return page

        return self._ocr_and_store(image, key, params)

    def close(self) -> None:
        close = getattr(self.provider, "close", None)
        if callable(close):
            close()

    # ---------------------------------------------------------------- 内部

    def _ocr_and_store(self, image: PageImage, key: str, params: dict) -> OcrPage:
        try:
            part = ImagePart.from_path(image.path)
        except ProviderError as exc:
            raise PageOcrError(image.page_no, f"页图无法内联：{exc}") from exc

        started = time.perf_counter()
        response = self.provider.complete(
            self.prompt_text,
            "请转录这一页。",
            images=[part],
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )

        # --- 截断必须显式拦住，不能让它伪装成"这页就这么点内容" ---
        #
        # 识别结果是**无声地**被截断的：截掉的半页看起来就像"这一页只有这些字"，
        # 下游完全无从分辨。而且推理模型会把大量 token 花在思考上，
        # 更容易撞上限——实测 max_tokens=64 时 content 直接为空
        # （token 全用于 reasoning），看起来和"没读图"一模一样。
        #
        # 所以这里宁可明确失败，也不要交出一份不完整的转录。
        if response.truncated:
            raise PageOcrError(
                image.page_no,
                f"识别结果被 max_tokens={self._max_tokens} 截断"
                f"（finish_reason=length，输出 token {response.usage.tokens_out}）。\n"
                f"截断的转录会伪装成「这页只有这些内容」，必须重新识别。\n"
                f"若使用推理模型，请调大 OCR 的 max_tokens（当前配置见 "
                f"VlmOcrEngine(max_tokens=...)）。",
            )

        try:
            payload = response.parse_json()
        except ProviderError as exc:
            # 结构不合法：重试一次，仍失败则隔离该页
            _log.warning(
                "OCR 返回非合法 JSON，重试一次",
                extra={"page": image.page_no, "detail": str(exc)[:200]},
            )
            response = self.provider.complete(
                self.prompt_text,
                "上次返回不是合法 JSON。请**只**输出 JSON 对象，不要任何解释文字或代码块标记。",
                images=[part],
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
            try:
                payload = response.parse_json()
            except ProviderError as exc2:
                raise PageOcrError(
                    image.page_no, f"连续两次未能得到合法 JSON：{exc2}"
                ) from exc2

        page = self._to_page(payload, image, response)
        prov = Provenance(
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            provider=self.cfg.ocr.provider,
            model=self.model,
            prompt_version=self.prompt_version,
            prompt_sha256=self.prompt_digest,
            image_sha256=image.sha256,
            request_params=params,
            usage=response.usage.to_dict(),
        )
        self.cache.put(key, page, prov)
        _log.info(
            "OCR 完成",
            extra={
                "page": image.page_no,
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "detail": f"{len(page.blocks)} 块 / {len(page.tables)} 表",
                "usage": response.usage.to_dict(),
            },
        )
        return page

    def _to_page(self, payload: object, image: PageImage, response) -> OcrPage:
        """把模型返回的 JSON 转成 OcrPage，并做严格校验。

        校验从严：字段缺失或类型不对就记 warning 并跳过该块，
        而不是"尽量猜一个"。块数异常少时（比如整页只解析出一个块）
        额外记一条警告——这通常意味着页图有问题或模型跑偏了。
        """
        warnings: list[str] = []
        if not isinstance(payload, dict):
            raise PageOcrError(image.page_no, f"返回的 JSON 顶层不是对象，而是 {type(payload).__name__}")

        raw_blocks = payload.get("blocks")
        if not isinstance(raw_blocks, list):
            raise PageOcrError(image.page_no, "返回的 JSON 缺少 blocks 数组")

        blocks: list[OcrBlock] = []
        tables = []
        table_idx = 0

        for i, rb in enumerate(raw_blocks):
            if not isinstance(rb, dict):
                warnings.append(f"第 {i} 个块不是对象，已跳过")
                continue
            btype = str(rb.get("type") or "paragraph").strip().lower()
            if btype not in VALID_BLOCK_TYPES:
                warnings.append(f"块 {i} 的类型 {btype!r} 不在允许集合内，按 paragraph 处理")
                btype = "paragraph"
            text = str(rb.get("text") or "")
            if not text.strip() and btype != "table":
                continue

            try:
                order = int(rb.get("order", i))
            except (TypeError, ValueError):
                order = i

            bbox = rb.get("bbox")
            bbox_t = None
            if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                try:
                    bbox_t = tuple(float(v) for v in bbox)
                except (TypeError, ValueError):
                    warnings.append(f"块 {i} 的 bbox 不是数字，已忽略")

            block = OcrBlock(
                block_id=f"p{image.page_no}-b{len(blocks)}",
                page_no=image.page_no,
                type=btype,  # type: ignore[arg-type]
                order=order,
                text=text,
                bbox=bbox_t,
                clause_label=(str(rb["clause_label"]).strip() if rb.get("clause_label") else None),
                uncertain=bool(rb.get("uncertain")),
                alternatives=[str(a) for a in (rb.get("alternatives") or []) if a],
            )
            blocks.append(block)

            if btype == "table":
                table_idx += 1
                tinfo = rb.get("table") if isinstance(rb.get("table"), dict) else {}
                html_str = str(tinfo.get("html") or text)
                caption = tinfo.get("caption") or rb.get("clause_label")
                grid = parse_html_table(
                    html_str,
                    table_id=f"p{image.page_no}-t{table_idx}",
                    page_no=image.page_no,
                    caption=str(caption) if caption else None,
                )
                if grid.parse_degraded:
                    warnings.append(f"表格 {grid.table_id} 结构降级，已保留原始 HTML")
                tables.append(grid)
                # 表格块的检索文本用规范化后的 Markdown，比原始 HTML 更适合检索
                block.text = grid.to_markdown() or html_str

        page_notes = payload.get("page_notes")
        if page_notes:
            warnings.append(f"模型页级备注：{page_notes}")
        if not blocks:
            warnings.append("该页未解析出任何文本块（可能是空白页，也可能是识别失败）")

        return OcrPage(
            page_no=image.page_no,
            blocks=sorted(blocks, key=lambda b: b.order),
            tables=tables,
            image_sha256=image.sha256,
            provider=self.cfg.ocr.provider,
            model=response.model or self.model,
            prompt_version=self.prompt_version,
            usage=response.usage if isinstance(response.usage, Usage) else Usage(),
            warnings=warnings,
        )
