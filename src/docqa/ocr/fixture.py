"""fixture 引擎：从本地 JSON 读取识别结果。

用途**仅限测试与开发**，例如在没有人可用的模型端点时验证下游的
切分/检索/问答/自检/评估链路。

它为什么必须是显式的
--------------------
一份别人递进来的 JSON 与"手工录入全文"在性质上无法区分——两者都绕过了
对真实页图的识别。所以：

- 引擎名固定为 `fixture`，会写进 manifest 与溯源链，无法伪装成真实运行；
- `ingest` 要求显式传 `--engine fixture` 才会启用；
- `eval` 会拒绝把 fixture 运行与实跑基线相比（引擎名不同即拒绝），**没有绕过开关**；
- 评估报告里 fixture 运行会被**机械标注**（而不是靠人记得），
  且不得据此声明任何 OCR 精度指标。

"机械隔离"是关键词：靠文档里写一句"请注意这是假的"是拦不住误用的。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..errors import PageOcrError
from ..logging import get_logger
from ..models import OcrBlock, OcrPage, PageImage, PageProfile, TableGrid, Usage
from ..tables import parse_html_table
from .base import OcrEngineInfo

_log = get_logger("ocr.fixture")


class FixtureOcrEngine:
    """按页图哈希查找预置的识别结果。"""

    supports_vision = False

    def __init__(self, fixtures_dir: Path, *, name: str = "fixture"):
        self.dir = fixtures_dir
        self.name = name
        self.model = "fixture"
        self.info = OcrEngineInfo(self.name, self.model, prompt_version="n/a", vision=False)
        if not self.dir.exists():
            _log.warning("fixture 目录不存在", extra={"detail": str(self.dir)})

    def ocr_page(self, image: PageImage, *, profile: PageProfile | None = None) -> OcrPage:
        # 先按内容哈希找（首选），再退回按页码找（便于人工维护小样本）
        for candidate in (
            self.dir / f"{image.sha256}.json",
            self.dir / f"p{image.page_no:04d}.json",
            self.dir / f"p{image.page_no}.json",
        ):
            if candidate.exists():
                return self._load(candidate, image)
        raise PageOcrError(
            image.page_no,
            f"fixture 目录 {self.dir} 中没有该页的结果"
            f"（查找键：{image.sha256[:12]}… 或 p{image.page_no:04d}.json）",
        )

    def _load(self, path: Path, image: PageImage) -> OcrPage:
        payload = json.loads(path.read_text(encoding="utf-8"))
        blocks: list[OcrBlock] = []
        tables: list[TableGrid] = []
        table_idx = 0

        for i, rb in enumerate(payload.get("blocks", [])):
            btype = rb.get("type", "paragraph")
            text = rb.get("text", "")
            block = OcrBlock(
                block_id=rb.get("block_id") or f"p{image.page_no}-b{i}",
                page_no=image.page_no,
                type=btype,
                order=int(rb.get("order", i)),
                text=text,
                bbox=tuple(rb["bbox"]) if rb.get("bbox") else None,
                clause_label=rb.get("clause_label"),
                uncertain=bool(rb.get("uncertain")),
                alternatives=list(rb.get("alternatives") or []),
            )
            blocks.append(block)

            if btype == "table":
                table_idx += 1
                tinfo = rb.get("table") or {}
                grid = parse_html_table(
                    tinfo.get("html") or text,
                    table_id=f"p{image.page_no}-t{table_idx}",
                    page_no=image.page_no,
                    caption=tinfo.get("caption"),
                )
                tables.append(grid)
                block.text = grid.to_markdown() or text

        return OcrPage(
            page_no=image.page_no,
            blocks=sorted(blocks, key=lambda b: b.order),
            tables=tables,
            image_sha256=image.sha256,
            provider=self.name,
            model=self.model,
            prompt_version="n/a",
            usage=Usage(),
            warnings=["本页由 fixture 引擎提供，非真实模型识别结果"],
        )

    def close(self) -> None:
        return None
