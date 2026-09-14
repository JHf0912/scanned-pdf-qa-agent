"""PDF 类型判定：这份 PDF 是扫描件、有真文本层，还是混合件？

为什么不能只用一个阈值
----------------------
业界常见的做法是"每页字符数 < N 就送去 OCR"。这在实际项目里会出错，原因有三：

1. **伪文本层**：扫描件可能带着上一次劣质 OCR 留下的文本层——字符数是够的，
   内容却是垃圾。只看字符数会放它过去，于是全文检索建立在一堆错字上。
2. **整本文档的聚合阈值会漏掉局部**：一份 90 页里夹着 55 页扫描件的标书，
   文档级均值可能仍然达标。
3. **干扰图会毒化图像计数**：扫描件里常带一张 1x1 的占位 JPEG
   （导出工具留下的残骸），它存在于资源里但通常不被绘制。按"图像对象数 > 0 就是扫描件"判断，会被这种图像带偏。

所以本模块的做法是：**逐页判定，多信号取证，把证据全部留下**，
最后按页聚合。判定结论永远附带 `signals`，误判时能复盘是哪条信号的问题。

一条硬不变量
------------
**有真实文本层的页永远不送去 OCR。** 已经能直接读到的文字再经一次识别，
只会引入新错误。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pymupdf

from .errors import PdfUnreadableError
from .models import DetectSignal, PageProfile, PdfProfile, PageKind

# --- 阈值（集中在此，便于按语料调整；调整依据写进 docs/evaluation.md）---

# 每页可抽取字符数低于此值，且存在大面积图像 -> 判为扫描页。
# 用 10 而不是 0：OCR 残留的零星字符（页码、页眉）不应改变判定。
MIN_TEXT_CHARS_PER_PAGE = 10

# 单张图像若像素面积小于此值，视为装饰/占位/软掩膜，不计入"整页扫描"证据。
# 扫描件里的 1x1 占位图（往往只有一百多字节）就落在这里被排除。
MIN_IMAGE_PIXEL_AREA = 1024  # 约 32x32

# 绘制图像面积占页面面积超过此比例 -> 支持"整页扫描"。
IMAGE_COVERAGE_SCANNED = 0.5

# 乱码文本层检测
GARBLED_TOKEN_MIN_LEN = 18  # 超长"粘连"token 的长度门槛
GARBLED_TOKEN_RATIO = 0.04  # 粘连 token 占比超过此值 -> 疑似乱码
REPLACEMENT_CHAR_RATIO = 0.01  # U+FFFD 占比

# 视为"正文"的字符（用于区分真正的文本层与零星装饰字符）
_MEANINGFUL_RE = re.compile(r"[一-鿿㐀-䶿A-Za-z0-9]")
_GLUED_TOKEN_RE = re.compile(r"[A-Za-z0-9]{18,}")


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _pdftotext_chars(path: Path, page_no: int) -> int | None:
    """用第二个独立引擎抽取字符数，作为交叉验证。

    `pdftotext` 对**不存在的页也返回退出码 0**，所以不能用退出码判断，
    只能看实际输出。返回 None 表示该工具不可用（此时不参与交叉验证）。
    """
    exe = shutil.which("pdftotext")
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, "-f", str(page_no), "-l", str(page_no), str(path), "-"],
            capture_output=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    text = proc.stdout.decode("utf-8", errors="replace")
    return len(_MEANINGFUL_RE.findall(text))


def _garbled_ratio(text: str) -> tuple[float, float]:
    """返回 (粘连 token 占比, 替换字符占比)。

    用于识别"有文本层但内容不可用"的情形——通常是上一次劣质 OCR 的残留。
    这类页面的字符数完全达标，只有看内容质量才能发现。
    """
    if not text:
        return 0.0, 0.0
    tokens = text.split()
    if not tokens:
        return 0.0, 0.0
    glued = sum(1 for t in tokens if _GLUED_TOKEN_RE.fullmatch(t))
    return glued / len(tokens), text.count("�") / len(text)


def _profile_page(doc: pymupdf.Document, page_no: int, pdf_path: Path) -> PageProfile:
    """对单页做多信号取证。"""
    page = doc[page_no - 1]
    page_area = abs(page.rect.width * page.rect.height) or 1.0

    signals: list[DetectSignal] = []

    # --- 信号 1：可抽取字符数（主引擎） ---
    raw_text = page.get_text() or ""
    char_count = len(_MEANINGFUL_RE.findall(raw_text))
    signals.append(
        DetectSignal(
            name="pymupdf_chars",
            value=char_count,
            threshold=MIN_TEXT_CHARS_PER_PAGE,
            passed=char_count >= MIN_TEXT_CHARS_PER_PAGE,
            detail=f"PyMuPDF 抽出 {char_count} 个有效字符（阈值 {MIN_TEXT_CHARS_PER_PAGE}）",
            source="pymupdf",
        )
    )

    # --- 信号 2：独立引擎交叉验证 ---
    cross_chars = _pdftotext_chars(pdf_path, page_no)
    if cross_chars is not None:
        signals.append(
            DetectSignal(
                name="pdftotext_chars",
                value=cross_chars,
                threshold=MIN_TEXT_CHARS_PER_PAGE,
                passed=cross_chars >= MIN_TEXT_CHARS_PER_PAGE,
                detail=f"pdftotext 独立抽出 {cross_chars} 个有效字符",
                source="pdftotext",
            )
        )

    # --- 信号 3：字体资源数 ---
    # 整份文档不含任何字体对象 = 文字不可能是"文本"，只能是图像像素。
    # 这比字符数阈值更硬：它不依赖抽取器行为，是结构事实。
    fonts = page.get_fonts() or []
    font_count = len(fonts)
    signals.append(
        DetectSignal(
            name="font_resources",
            value=font_count,
            threshold=1,
            passed=font_count > 0,
            detail=f"页面引用 {font_count} 个字体资源；0 表示文字只能是图像像素",
            source="pymupdf",
        )
    )

    # --- 信号 4：绘制图像覆盖比 ---
    # 关键：用 get_image_info() 的**显示列表**数据（真实绘制位置与尺寸），
    # 而不是数图像对象、也不是看图像声明的像素尺寸。
    # 1x1 的占位图在显示列表里面积近似为 0，因此天然不会被计入；
    # 仍额外做一次显式过滤并单独计数，让"排除了什么"可审计。
    drawn_area = 0.0
    drawn_count = 0
    excluded_tiny = 0
    largest_dpi: float | None = None

    for info in page.get_image_info():
        bbox = info.get("bbox")
        if not bbox:
            continue
        w = abs(bbox[2] - bbox[0])
        h = abs(bbox[3] - bbox[1])
        px_w = int(info.get("width") or 0)
        px_h = int(info.get("height") or 0)

        if px_w * px_h < MIN_IMAGE_PIXEL_AREA:
            excluded_tiny += 1
            continue

        drawn_count += 1
        drawn_area += w * h

        # 原生 DPI：该图像每英寸实际铺了多少像素
        if w > 0 and px_w > 0:
            dpi = px_w / (w / 72.0)
            if largest_dpi is None or px_w * px_h > 0 and dpi > largest_dpi:
                largest_dpi = dpi

    coverage = min(drawn_area / page_area, 1.0)
    signals.append(
        DetectSignal(
            name="drawn_image_area_ratio",
            value=coverage,
            threshold=IMAGE_COVERAGE_SCANNED,
            passed=coverage >= IMAGE_COVERAGE_SCANNED,
            detail=(
                f"绘制图像占页面面积 {coverage:.1%}"
                f"（{drawn_count} 张有效图，已排除 {excluded_tiny} 张小于 "
                f"{MIN_IMAGE_PIXEL_AREA}px² 的干扰图）"
            ),
            source="pymupdf.display_list",
        )
    )

    # --- 信号 5：乱码文本层 ---
    glued_ratio, replacement_ratio = _garbled_ratio(raw_text)
    garbled = glued_ratio > GARBLED_TOKEN_RATIO or replacement_ratio > REPLACEMENT_CHAR_RATIO
    signals.append(
        DetectSignal(
            name="garbled_text_layer",
            value=max(glued_ratio, replacement_ratio),
            threshold=max(GARBLED_TOKEN_RATIO, REPLACEMENT_CHAR_RATIO),
            passed=not garbled,
            detail=(
                f"粘连 token 占比 {glued_ratio:.2%}，替换字符占比 {replacement_ratio:.2%}"
                f"{'；疑似劣质 OCR 残留文本层' if garbled else ''}"
            ),
            source="pymupdf",
        )
    )

    # --- 综合判定 ---
    has_text = char_count >= MIN_TEXT_CHARS_PER_PAGE
    looks_scanned = coverage >= IMAGE_COVERAGE_SCANNED and drawn_count > 0

    if has_text and garbled:
        # 有文本层但内容不可信 —— 这正是单阈值方案会漏掉的情形
        kind: PageKind = "SCANNED"
        reason = "存在文本层但内容呈乱码特征（疑似劣质 OCR 残留），按扫描页处理"
    elif has_text and looks_scanned:
        kind = "HYBRID"
        reason = "既有可读文本层又有整页图像，按混合页处理"
    elif has_text:
        kind = "TEXT"
        reason = f"文本层可读（{char_count} 字符），无需 OCR"
    elif looks_scanned:
        kind = "SCANNED"
        reason = (
            f"无有效文本（{char_count} 字符）且图像覆盖 {coverage:.0%}"
            + ("，且无任何字体资源" if font_count == 0 else "")
        )
    else:
        kind = "TEXT"
        reason = f"无有效文本也无整页图像（{char_count} 字符，覆盖 {coverage:.0%}），按空白页处理"

    return PageProfile(
        page_no=page_no,
        kind=kind,
        char_count=char_count,
        font_resource_count=font_count,
        drawn_image_count=drawn_count,
        excluded_tiny_images=excluded_tiny,
        drawn_image_area_ratio=coverage,
        native_dpi=largest_dpi,
        signals=signals,
        reason=reason,
    )


def profile_pdf(path: Path, *, cross_check: bool = True) -> PdfProfile:
    """对整份 PDF 取证并逐页判定类型。

    Args:
        path: PDF 路径。
        cross_check: 是否启用第二个抽取引擎做交叉验证。测试里可关掉以脱离外部依赖。

    Raises:
        PdfUnreadableError: 文件不存在、加密或结构损坏。
    """
    if not path.exists():
        raise PdfUnreadableError(f"文件不存在：{path}")

    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # pymupdf 的异常类型不稳定，统一包装
        raise PdfUnreadableError(f"无法打开 PDF：{path}（{exc}）") from exc

    try:
        if doc.needs_pass:
            raise PdfUnreadableError(f"PDF 已加密，需要口令：{path}")
        if doc.page_count == 0:
            raise PdfUnreadableError(f"PDF 不含任何页面：{path}")

        pages = [
            _profile_page(doc, i, path) for i in range(1, doc.page_count + 1)
        ]
    finally:
        doc.close()

    cross_engine: dict[str, float] = {}
    if cross_check:
        total_cross = 0
        available = True
        for i in range(1, len(pages) + 1):
            n = _pdftotext_chars(path, i)
            if n is None:
                available = False
                break
            total_cross += n
        cross_engine["pdftotext_available"] = 1.0 if available else 0.0
        if available:
            cross_engine["pdftotext_chars_total"] = float(total_cross)
    cross_engine["pymupdf_chars_total"] = float(sum(p.char_count for p in pages))

    kinds = {p.kind for p in pages}
    if kinds == {"SCANNED"}:
        overall: PageKind = "SCANNED"
    elif kinds == {"TEXT"}:
        overall = "TEXT"
    else:
        overall = "HYBRID"

    return PdfProfile(
        path=path,
        sha256=_sha256_of(path),
        page_count=len(pages),
        pages=pages,
        kind=overall,
        cross_engine=cross_engine,
    )


def pages_needing_ocr(profile: PdfProfile) -> list[int]:
    """返回需要送 OCR 的页码。

    硬不变量在此落地：**判为 TEXT 的页永不送去 OCR**。
    已经能直接读到的文字再识别一次只会引入新错误。
    """
    return [p.page_no for p in profile.pages if p.kind in ("SCANNED", "HYBRID")]
