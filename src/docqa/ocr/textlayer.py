"""文本层引擎：从**原生数字文本层**提取结构化内容。

为什么这个模块必须存在（一个真实的功能空洞）
--------------------------------------------
`detect.py` 把 PDF 分成 `TEXT / SCANNED / HYBRID` 三类，并写明了硬不变量：
"有真实文本层的页永远不送 OCR"。这条不变量是对的——已经能读到的文字再识别一次
只会引入新错误。

但在此之前，**管线里根本没有"把文本层取出来"这条路径**：
`chunk_document()` 只接收 OCR 产出的 `OcrPage`，于是：

- 纯 born-digital PDF → `ingest` 产出 **0 个 chunk**，什么问题都只能拒答；
- 混合件（一页有文本层、一页是扫描图）→ **文本层那几页的正文整页丢失**，
  只剩扫描页进了索引。

也就是说，"不送 OCR"这个正确的决定，当时等于"直接丢掉"。
真实客户文档里混合件是常态，所以这条路径不是锦上添花，是可用性的前提。

为什么放在 `ocr/` 下
--------------------
它和 OCR 引擎做的是同一件事：把一页变成 `OcrPage`（块 + 表格）。
接口一致，`chunk_document()` 才能对两类来源一视同仁，
`ingest` 也才能在同一个循环里按页选引擎。
名字叫 TextLayer 而不是 TextOcr，是为了让日志与 manifest 里一眼看出
**这一页没有调用任何模型**。

条文结构怎么来
--------------
从文本层取字，条号一般写在正文里（`3.1  抗弯强度…`），所以这里
**把条号留在 text 里**，由 `chunker` 用 domain 的 `clause_patterns` 去认。
这与 VLM 路径刻意不同（那里条号在单独字段上），两条路径的差别写在这里，
免得日后有人"统一"掉它反而弄丢结构。
"""

from __future__ import annotations

import statistics
from pathlib import Path

import pymupdf

from ..errors import PageOcrError
from ..logging import get_logger
from ..models import OcrBlock, OcrPage, PageImage, PageProfile, Usage
from .base import OcrEngineInfo

_log = get_logger("ocr.textlayer")

# 行高等比于正文字高多少倍，算"空行/段落间隔"。
_PARA_GAP_RATIO = 1.6
# 字号超过正文中位字号多少倍，算标题。
_HEADING_RATIO = 1.25


def _span_text(spans: list[dict]) -> str:
    return "".join(s.get("text", "") for s in spans)


def _line_bbox(line: dict) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = line.get("bbox", (0, 0, 0, 0))
    return float(x0), float(y0), float(x1), float(y1)


class TextLayerEngine:
    """按页从 PDF 文本层提取块。**不调用任何模型，零 API 成本。**

    与 VLM 引擎共享同一个 `ocr_page` 接口，所以两者在 `ingest` 里可以按页混用。
    """

    supports_vision = False

    def __init__(self, pdf_path: Path | None = None) -> None:
        self.name = "textlayer"
        self.model = "pymupdf-textlayer"
        self.info = OcrEngineInfo(self.name, self.model, prompt_version="n/a", vision=False)
        # `ingest` 在循环里只拿到 `PageImage`（页图），拿不到 PDF 路径，
        # 所以这里持有 PDF 路径；由调用方在构造时注入。
        self._pdf_path = Path(pdf_path) if pdf_path is not None else None
        self._doc: pymupdf.Document | None = None

    # ------------------------------------------------------------ 接口

    def ocr_page(self, image: PageImage, *, profile: PageProfile | None = None) -> OcrPage:
        """取一页的文本层。

        注意：`image` 只用来拿页码与页图哈希（用于溯源），**不读这张图**。
        这就是"文本层页不送 OCR"的字面含义。
        """
        doc = self._open()
        page_no = image.page_no
        if page_no < 1 or page_no > doc.page_count:
            raise PageOcrError(page_no, f"页码超出范围（共 {doc.page_count} 页）")

        try:
            page = doc[page_no - 1]
            blocks = self._extract_blocks(page, page_no)
        except PageOcrError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单页失败要能被调用方隔离
            raise PageOcrError(page_no, f"文本层提取失败：{exc}") from exc

        if not blocks:
            # 空页面是合法结果（真空白页），但**一个字符都没有**时更可能是
            # 这一页其实没有可用文本层——如实记 warning，让下游可诊断。
            return OcrPage(
                page_no=page_no, blocks=[], tables=[],
                image_sha256=image.sha256, provider=self.name, model=self.model,
                prompt_version="n/a", usage=Usage(),
                warnings=["该页未提取到任何文本层内容（可能是空白页，或文本层不可用）"],
            )

        return OcrPage(
            page_no=page_no,
            blocks=blocks,
            tables=[],  # 表格在文本层里没有结构信息，`_extract_blocks` 已把它降级为正文行
            image_sha256=image.sha256,
            provider=self.name,
            model=self.model,
            prompt_version="n/a",
            usage=Usage(),
            warnings=["本页内容取自 PDF 原生文本层，未调用识别模型"],
        )

    def close(self) -> None:
        if self._doc is not None:
            try:
                self._doc.close()
            finally:
                self._doc = None

    # ------------------------------------------------------------ 内部

    def _open(self) -> pymupdf.Document:
        if self._doc is not None:
            return self._doc
        if self._pdf_path is None:
            raise PageOcrError(0, "TextLayerEngine 未绑定 PDF 路径，无法读取文本层")
        try:
            self._doc = pymupdf.open(self._pdf_path)
        except Exception as exc:  # noqa: BLE001
            raise PageOcrError(0, f"无法打开 PDF：{self._pdf_path}（{exc}）") from exc
        return self._doc

    def _extract_blocks(self, page, page_no: int) -> list[OcrBlock]:
        """把文本层按行聚成块。

        聚块规则刻意简单（不引版面分析模型）：
        - 行间距超过正文行高一定倍数 → 新段落；
        - 字号明显大于正文中位字号 → 单独成 `heading` 块。

        **为什么不做表格结构还原**：文本层里表格只剩"按行排列的文字"，
        列信息在 PDF 里以坐标存在，靠坐标猜列边界很容易造出错误的网格，
        而错误的表格比没有表格更危险（数值会落到错误的行列上）。
        所以表格在文本层路径下**降级为正文行**，如实记录，不假装识别出了结构。
        """
        raw = page.get_text("dict") or {}
        lines: list[tuple[str, float, float, tuple[float, float, float, float]]] = []
        for block in raw.get("blocks", []):
            if block.get("type") != 0:  # 0 = 文本块，1 = 图像块
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = _span_text(spans).strip()
                if not text:
                    continue
                size = max((float(s.get("size", 0.0)) for s in spans), default=0.0)
                x0, y0, _x1, y1 = _line_bbox(line)
                lines.append((text, size, y0, (x0, y0, _x1, y1)))

        if not lines:
            return []

        # 按阅读顺序（从上到下、从左到右）排；PDF 里块的顺序偶尔会乱。
        lines.sort(key=lambda t: (round(t[2], 1), t[3][0]))
        body_size = statistics.median([t[1] for t in lines]) or 10.0

        blocks: list[OcrBlock] = []
        buf: list[str] = []
        buf_bbox: list[tuple[float, float, float, float]] = []
        prev_y1 = lines[0][2]
        order = 0

        def flush(kind: str = "paragraph") -> None:
            nonlocal order, buf, buf_bbox
            text = "\n".join(buf).strip()
            if not text:
                buf, buf_bbox = [], []
                return
            bbox = (
                min(b[0] for b in buf_bbox), min(b[1] for b in buf_bbox),
                max(b[2] for b in buf_bbox), max(b[3] for b in buf_bbox),
            )
            blocks.append(OcrBlock(
                block_id=f"p{page_no}-b{order}",
                page_no=page_no,
                type=kind,  # type: ignore[arg-type]
                order=order,
                text=text,
                bbox=bbox,
            ))
            order += 1
            buf, buf_bbox = [], []

        for text, size, y0, bbox in lines:
            is_heading = size >= body_size * _HEADING_RATIO and len(text) <= 60
            gap = y0 - prev_y1
            new_para = gap > body_size * _PARA_GAP_RATIO

            if is_heading:
                flush()
                buf, buf_bbox = [text], [bbox]
                flush(kind="heading")
            else:
                if new_para and buf:
                    flush()
                buf.append(text)
                buf_bbox.append(bbox)
            prev_y1 = bbox[3]

        flush()
        return blocks
