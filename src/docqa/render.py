"""PDF 页 → 图像。

两个决策
--------
**1. 默认位精确取图，而不是重新光栅化。**
扫描件的每一页本质上就是一张嵌在 PDF 里的位图。把它原样取出来喂给模型，
比"渲染一次再编码"少一道重采样——对 OCR 而言，任何重采样都只可能损失信息。
光栅化只在取不到合适位图时回退（例如页面由多个图块拼成、或含矢量内容）。

**2. 放大不等于变清晰。**
本项目的两份输入原生分别是 280 DPI 与 200 DPI。渲染到更高 DPI 只是插值，
**不会产生新信息**——这一点必须说清楚，否则很容易把"我把它放大到 400 DPI"
当成"我提升了精度"。

但放大确实可能改善 VLM 对小号中文字的**辨识率**（那是模型输入有效分辨率的
问题，不是信息量的问题）。所以 `upscale` 是一个可配置的实验变量，
由 A/B 消融来决定要不要开，而不是拍一个默认值。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
from PIL import Image

from .config import RenderConfig
from .errors import InputError
from .logging import get_logger
from .models import PageImage, PageProfile

_log = get_logger("render")

# 低于此像素面积的图像不视为"页面内容"（页眉里的装饰、软掩膜、占位图）。
# 与 detect.py 用同一套判据，避免两处标准不一致。
MIN_IMAGE_PIXEL_AREA = 1024


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _extract_native(doc: pymupdf.Document, page_no: int, out_dir: Path) -> tuple[Path, float, int, int] | None:
    """尝试位精确取出页面主图像。

    Returns:
        (路径, 原生 DPI, 宽, 高)；取不到合适图像时返回 None。
    """
    page = doc[page_no - 1]
    best = None
    # 必须传 xrefs=True：默认的 get_image_info() **不返回 xref 字段**，
    # 拿不到 xref 就取不出原始位图，会静默退化成光栅化——
    # 表现为"位精确取图"从未真正生效，页图分辨率反而低于源文件。
    for info in page.get_image_info(xrefs=True):
        bbox = info.get("bbox")
        if not bbox:
            continue
        px_w, px_h = int(info.get("width") or 0), int(info.get("height") or 0)
        if px_w * px_h < MIN_IMAGE_PIXEL_AREA:
            continue
        area = abs(bbox[2] - bbox[0]) * abs(bbox[3] - bbox[1])
        if best is None or area > best[0]:
            best = (area, info)

    if best is None:
        return None

    info = best[1]
    xref = info.get("xref")
    if not xref:
        return None

    try:
        img = doc.extract_image(xref)
    except Exception as exc:  # noqa: BLE001 - 取图失败应回退而非中断
        _log.warning("位精确取图失败，将回退光栅化", extra={"page": page_no, "detail": str(exc)})
        return None

    ext = img.get("ext", "png")
    data = img["image"]
    dest = out_dir / f"p{page_no:04d}_native.{ext}"
    dest.write_bytes(data)

    bbox = info["bbox"]
    drawn_w = abs(bbox[2] - bbox[0])
    native_dpi = img["width"] / (drawn_w / 72.0) if drawn_w > 0 else 0.0
    return dest, native_dpi, img["width"], img["height"]


def _rasterize(
    doc: pymupdf.Document, page_no: int, out_dir: Path, dpi: int, fmt: str,
    jpeg_quality: int = 90,
) -> tuple[Path, int, int]:
    """回退路径：按指定 DPI 光栅化整页。

    `jpeg_quality` 只在 `fmt == "jpg"` 时有意义。它曾经是配置里的一个死开关：
    声明在 `RenderConfig` 与 `config/default.yaml` 里，实际却硬编码 `quality=90`，
    调它不产生任何变化。对扫描件而言这不是小事——JPEG 质量直接决定
    **喂给 OCR 的输入有多少压缩伪影**，而伪影正是识别错字的来源之一。
    """
    page = doc[page_no - 1]
    pix = page.get_pixmap(dpi=dpi, alpha=False)
    dest = out_dir / f"p{page_no:04d}_raster.{fmt}"
    if fmt == "jpg":
        pix.pil_save(str(dest), format="JPEG", quality=jpeg_quality)
    else:
        pix.save(str(dest))
    return dest, pix.width, pix.height


def _apply_upscale(path: Path, factor: float, out_dir: Path) -> tuple[Path, int, int]:
    """按倍数放大。用 LANCZOS 而非最近邻——目标是让模型看清笔画，不是保持像素。"""
    with Image.open(path) as im:
        new_size = (max(1, int(im.width * factor)), max(1, int(im.height * factor)))
        up = im.convert("RGB").resize(new_size, Image.LANCZOS)
        dest = out_dir / f"{path.stem}_x{factor:g}.png"
        up.save(dest)
        return dest, up.width, up.height


def render_pages(
    pdf_path: Path,
    out_dir: Path,
    *,
    page_count: int,
    config: RenderConfig | None = None,
    profiles: dict[int, PageProfile] | None = None,
    pages: list[int] | None = None,
) -> list[PageImage]:
    """渲染（或取出）指定页的图像。

    Args:
        pages: 要处理的页码；None 表示全部。
        profiles: 逐页画像。用于记录原生 DPI，以及未来按页类型选择策略。
        config: 渲染配置；None 用默认值。
    """
    cfg = config or RenderConfig()
    out_dir.mkdir(parents=True, exist_ok=True)
    # `None` = 未指定（渲染全部页）；`[]` = 明确指定"一页都不渲染"。
    # 两者语义不同，**不能用 `or` 合并**——`[] or [...]` 会把"不需要渲染"
    # 悄悄变成"渲染全部"，而这个错误的代价是整本 PDF 被送去 OCR。
    targets = list(range(1, page_count + 1)) if pages is None else list(pages)

    try:
        doc = pymupdf.open(pdf_path)
    except Exception as exc:
        raise InputError(f"无法打开 PDF：{pdf_path}（{exc}）") from exc

    results: list[PageImage] = []
    try:
        for page_no in targets:
            if page_no < 1 or page_no > doc.page_count:
                raise InputError(f"页码 {page_no} 超出范围（共 {doc.page_count} 页）")

            native = None
            if cfg.mode in ("auto", "extract"):
                native = _extract_native(doc, page_no, out_dir)

            if native is not None and cfg.mode != "raster":
                path, native_dpi, px_w, px_h = native
                dpi = native_dpi
                is_native = True
            else:
                if cfg.mode == "extract":
                    raise InputError(
                        f"第 {page_no} 页没有可位精确取出的整页图像，"
                        f"但渲染模式被强制为 extract。请改用 --mode raster 或 auto。"
                    )
                # 未指定 dpi 时用一个保守回退值；该页通常不是纯扫描页
                dpi = float(cfg.dpi or 200)
                dpi = min(dpi, float(cfg.max_dpi))
                path, px_w, px_h = _rasterize(
                    doc, page_no, out_dir, int(dpi), cfg.image_format,
                    jpeg_quality=cfg.jpeg_quality,
                )
                is_native = False

            if cfg.upscale and cfg.upscale != 1.0:
                path, px_w, px_h = _apply_upscale(path, cfg.upscale, out_dir)
                dpi = dpi * cfg.upscale

            results.append(
                PageImage(
                    page_no=page_no,
                    path=path,
                    sha256=_sha256_file(path),
                    width=px_w,
                    height=px_h,
                    dpi=dpi,
                    native=is_native,
                    upscale=cfg.upscale,
                )
            )
    finally:
        doc.close()

    return results


def describe(images: list[PageImage]) -> list[dict]:
    """渲染结果摘要，写进 manifest。"""
    return [
        {
            "page_no": im.page_no,
            "file": im.path.name,
            "sha256": im.sha256[:16],
            "width": im.width,
            "height": im.height,
            "dpi": round(im.dpi, 1),
            "native": im.native,
            "upscale": im.upscale,
        }
        for im in images
    ]
