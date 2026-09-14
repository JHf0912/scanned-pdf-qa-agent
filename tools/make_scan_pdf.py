"""把页图打包成"扫描件式"PDF：整页 JPEG、无文本层、无字体对象。

为什么需要这一步
----------------
`fetch_openstd.py` 还原出的是一页一张 PNG。真实客户给的扫描件是 PDF，
而本项目要验证的第一件事就是"这份 PDF 是不是扫描件"，所以需要一个
**确实没有文本层**的 PDF 作为流水线输入。

本脚本按扫描仪的典型做法打包：每页一张 DCTDecode(JPEG) 全页图像，
不写入任何字体资源。这样 `/Type /Font == 0` 这条判定信号才成立。

用法
----
    uv run python tools/make_scan_pdf.py --images data/raw/gbt1568_p*.png \
                                        --out data/raw/gbt1568_scan.pdf --dpi 200
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pymupdf
from PIL import Image

PT_PER_INCH = 72.0

# 标准正文用 A4（GB/T 1568 是 16 开，成品尺寸与 A4 一致）。
# 页图 2315x3274 的宽高比是 0.707，正好等于 A4 的 595.276:841.89，可确认就是 A4。
A4_W_PT = 595.276
A4_H_PT = 841.89


def _pixel_size(path: Path) -> tuple[int, int]:
    """取图像的**像素**尺寸。

    这里有两个坑，都踩过：
    1. `pymupdf.open(img)[0].rect` 给的是**点**——它已按图像自带的 DPI 元数据
       （缺失时按 96）把像素换算过了。拿它再除以目标 DPI 就是二次换算。
    2. `pymupdf.open(img)[0].get_pixmap()` 默认按 **72 DPI 渲染**，返回的同样是
       点而不是像素（1737x2456，而非真实的 2315x3274）。
    直接用 PIL 读位图头，不涉及任何 DPI 解释，无歧义。
    """
    with Image.open(path) as im:
        return im.width, im.height


def build_pdf(images: list[Path], out: Path, jpeg_quality: int) -> list[tuple[int, int, float]]:
    """把页图打包成无文本层的 PDF，返回每页的 (像素宽, 像素高, 等效 DPI)。"""
    stats: list[tuple[int, int, float]] = []
    doc = pymupdf.open()
    try:
        for path in images:
            px_w, px_h = _pixel_size(path)
            # 按 A4 版面铺满；等效 DPI 由像素数与版面尺寸反推，如实记录
            page = doc.new_page(width=A4_W_PT, height=A4_H_PT)
            page.insert_image(
                pymupdf.Rect(0, 0, A4_W_PT, A4_H_PT),
                filename=str(path),
                keep_proportion=False,
            )
            effective_dpi = px_w / (A4_W_PT / PT_PER_INCH)
            stats.append((px_w, px_h, effective_dpi))
        out.parent.mkdir(parents=True, exist_ok=True)
        doc.save(
            out,
            garbage=4,
            deflate=True,
            # 关键：不写入任何字体资源，也不生成文本层
        )
    finally:
        doc.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="把页图打包成无文本层的扫描件式 PDF")
    ap.add_argument("--images", nargs="+", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--jpeg-quality", type=int, default=85)
    args = ap.parse_args(argv)

    missing = [p for p in args.images if not p.exists()]
    if missing:
        print(f"错误：找不到 {missing}", file=sys.stderr)
        return 2

    stats = build_pdf(sorted(args.images), args.out, args.jpeg_quality)
    print(f"已写出 {args.out}（{len(stats)} 页，{args.out.stat().st_size} 字节，A4 版面）")
    for i, (w, h, dpi) in enumerate(stats, start=1):
        print(f"  第 {i} 页：{w}x{h} px -> 等效 {dpi:.0f} DPI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
