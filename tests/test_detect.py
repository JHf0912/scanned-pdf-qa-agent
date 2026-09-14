"""PDF 类型判定的测试。

重点是**已知会出错的场景**：干扰图、乱码文本层、无字体资源。
这些正是单阈值方案会误判的地方。

测试用的 PDF 全部**运行时合成**，不依赖仓库里的任何文件——
自足，且不会因为删掉某个输入文件而静默变成 skip。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from docqa.detect import (
    MIN_IMAGE_PIXEL_AREA,
    _garbled_ratio,
    pages_needing_ocr,
    profile_pdf,
)
from docqa.errors import PdfUnreadableError


# ---------------------------------------------------------------- 合成件

def _jpeg(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG", quality=80)
    return buf.getvalue()


def make_scan_pdf(path: Path, *, orphan_tiny_image: bool = False) -> Path:
    """合成一份"扫描件式" PDF：整页 JPEG、无文本层、无字体资源。

    Args:
        orphan_tiny_image: 为 True 时，额外在页面的 `/Resources/XObject` 里放一个
            **1×1 的 JPEG 对象，但内容流里不绘制它**。

            这个"孤立小图"是真实扫描件里常见的产物（导出工具留下的占位/软掩膜残骸），
            也是判定逻辑最容易踩的坑：如果实现写成"数图像对象个数 > 0 就是扫描件"，
            它会被这个 1×1 带偏。**必须从显示列表算绘制面积，而不是数对象。**

    手写 PDF 而不是用 PyMuPDF 拼装：`insert_image` 一定会把图画出来，
    而这里需要的恰恰是"**存在于资源里但不被绘制**"——手写内容流才控制得住。
    """
    big = _jpeg(400, 560, (240, 240, 240))
    objs: list[bytes] = []

    objs.append(b"<</Type/Catalog/Pages 2 0 R>>")
    objs.append(b"<</Type/Pages/Kids[3 0 R]/Count 1>>")

    xobjects = b"/ImBig 4 0 R"
    if orphan_tiny_image:
        xobjects += b" /ImTiny 5 0 R"
    # 括号要对齐：/Resources<< 与 /XObject<< 一共开 4 层，就必须正好闭 4 个。
    # 多写一个会把 Page 字典提前关掉，剩下 /Contents 悬空，
    # MuPDF 只会含糊地报一句 "invalid key in dict"，很难定位。
    objs.append(
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 420]"
        b"/Resources<</XObject<<" + xobjects + b">>>>"
        b"/Contents " + (b"6 0 R" if orphan_tiny_image else b"5 0 R") + b">>"
    )

    # 4：整页图
    objs.append(
        b"<</Type/XObject/Subtype/Image/Width 400/Height 560"
        b"/ColorSpace/DeviceRGB/BitsPerComponent 8/Filter/DCTDecode/Length "
        + str(len(big)).encode() + b">>\nstream\n" + big + b"\nendstream"
    )

    if orphan_tiny_image:
        tiny = _jpeg(1, 1, (255, 255, 255))
        objs.append(
            b"<</Type/XObject/Subtype/Image/Width 1/Height 1"
            b"/ColorSpace/DeviceRGB/BitsPerComponent 8/Filter/DCTDecode/Length "
            + str(len(tiny)).encode() + b">>\nstream\n" + tiny + b"\nendstream"
        )

    # 内容流：**只**绘制整页图。1×1 那个对象出现在 Resources 里，但从不被 Do 调用。
    content = b"q 300 0 0 420 0 0 cm /ImBig Do Q"
    objs.append(b"<</Length " + str(len(content)).encode() + b">>\nstream\n" + content + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    n = len(objs) + 1
    out += f"xref\n0 {n}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<</Size {n}/Root 1 0 R>>\nstartxref\n{xref_at}\n%%EOF\n".encode()

    path.write_bytes(bytes(out))
    return path


@pytest.fixture
def scan_pdf(tmp_path: Path) -> Path:
    """普通扫描件：整页图，无干扰对象。"""
    return make_scan_pdf(tmp_path / "scan.pdf")


@pytest.fixture
def scan_pdf_with_orphan(tmp_path: Path) -> Path:
    """含"存在但未绘制"的 1×1 对象的扫描件。"""
    return make_scan_pdf(tmp_path / "scan_orphan.pdf", orphan_tiny_image=True)


# ---------------------------------------------------------------- 测试

class TestGarbledDetection:
    def test_clean_text_is_not_garbled(self):
        glued, repl = _garbled_ratio("键的抗拉强度应大于等于 590 MPa。楔键斜度 1:100。")
        assert glued == 0.0 and repl == 0.0

    def test_glued_long_tokens_detected(self):
        # 劣质 OCR 残留的典型特征：长串无空格的字母数字
        text = " ".join(["abcdefghijklmnopqrstuvwxyz012345"] * 5 + ["正常文字"])
        glued, _ = _garbled_ratio(text)
        assert glued > 0.04

    def test_replacement_chars_detected(self):
        glued, repl = _garbled_ratio("正常文字�����")
        assert repl > 0.01

    def test_empty_text(self):
        assert _garbled_ratio("") == (0.0, 0.0)


class TestProfilePdf:
    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(PdfUnreadableError, match="不存在"):
            profile_pdf(tmp_path / "nope.pdf")

    def test_scan_is_detected_as_scanned(self, scan_pdf: Path):
        """整页图、0 字体、0 字符 → 判为扫描件，且确实需要 OCR。"""
        p = profile_pdf(scan_pdf)
        assert p.kind == "SCANNED"
        assert p.pages[0].font_resource_count == 0
        assert p.pages[0].char_count == 0
        assert p.pages[0].drawn_image_area_ratio > 0.9
        assert pages_needing_ocr(p) == [1]

    def test_cross_engine_agreement_recorded(self, scan_pdf: Path):
        """两个独立抽取器都给出 0 字符——比单个引擎的阈值判定硬得多。"""
        p = profile_pdf(scan_pdf)
        assert p.cross_engine["pymupdf_chars_total"] == 0
        if p.cross_engine["pdftotext_available"]:
            assert p.cross_engine["pdftotext_chars_total"] == 0

    def test_evidence_is_recorded_for_every_page(self, scan_pdf: Path):
        """判定依据必须留下，误判时才能复盘是哪条信号的问题。"""
        p = profile_pdf(scan_pdf)
        names = {s.name for s in p.pages[0].signals}
        assert {"pymupdf_chars", "font_resources", "drawn_image_area_ratio"} <= names


class TestTinyImageFiltering:
    def test_threshold_is_documented_value(self):
        # 1x1 占位图面积 = 1，远低于门槛
        assert MIN_IMAGE_PIXEL_AREA > 1

    def test_orphan_tiny_image_does_not_poison_the_count(self, scan_pdf_with_orphan: Path):
        """**核心回归**：资源里存在但未被绘制的 1×1 对象，不得影响图像计数。

        朴素写法"数图像对象个数 > 0 就是扫描件"会在这里出错；
        正确写法从**显示列表**算绘制面积，所以 1×1 根本进不了候选。

        注意这里断言的是 `excluded_tiny_images == 0`——它没被绘制，
        所以连"被排除"都轮不上；`drawn_image_count` 仍是 1（只有整页图）。
        """
        p = profile_pdf(scan_pdf_with_orphan)
        page = p.pages[0]
        assert page.drawn_image_count == 1, "只有整页图进入绘制列表"
        assert page.excluded_tiny_images == 0, "1×1 未被绘制，不应进入候选"
        assert page.drawn_image_area_ratio > 0.9, "覆盖比按绘制面积算，不受孤立对象影响"
        assert p.kind == "SCANNED"

    def test_orphan_object_really_is_in_the_file(self, scan_pdf_with_orphan: Path):
        """证明上面那条不是"文件里根本没有那个对象"——它确实存在。

        用 PyMuPDF 直接数**页面引用的**图像对象（不经显示列表过滤）。
        如果不做这一步，`excluded_tiny_images == 0` 可能只是因为文件里压根没有 1×1，
        测试就成了空转。
        """
        import pymupdf

        doc = pymupdf.open(scan_pdf_with_orphan)
        try:
            refs = doc[0].get_images(full=True)
            sizes = sorted((im[2], im[3]) for im in refs)
            assert (1, 1) in sizes, f"合成件里应含 1×1 对象，实际：{sizes}"
            assert len(refs) == 2, "应有两个图像对象：整页图 + 1×1"
        finally:
            doc.close()
