"""从"国家标准全文公开系统"(openstd.samr.gov.cn) 的公开预览中还原标准页。

背景
----
该系统以**瓦片打乱**方式提供在线预览，作为反抓取措施：
一张大图（WebP 雪碧图）里，所有页面被切成固定尺寸的小瓦片并故意打乱排列。
但查看器 HTML 里带着完整的还原映射——每个 `<span class="pdfImg-<列>-<行>">`
的 `background-position` 指出了该瓦片在雪碧图中的**来源**坐标，而类名里的
(列, 行) 指出了它在页面上的**目标**位置。按这张映射表把瓦片贴回去即可还原页面。

采集到的雪碧图为 2320x5904，即 10 列 x 18 行、每片 232x328 的瓦片池；
每个页面是 10x10 的目标网格（页容器 2315x3274），空白瓦片被省略。

用途与限制
----------
仅供本次作业在本地做技术验证。还原出的文本**不再分发**，请以正式标准出版物为准。

用法
----
    uv run python tools/fetch_openstd.py --viewer data/derived/openstd_viewer.html \
                                        --sprite data/derived/openstd_sprite.webp \
                                        --out data/raw
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

# 瓦片尺寸与目标网格，由雪碧图尺寸(2320x5904)与页容器尺寸(2315x3274)推得。
TILE_W = 232
TILE_H = 328
PAGE_W = 2315
PAGE_H = 3274

# <div id="0" class="page" bg="..." style="width:2315px;height:3274px;...">
_PAGE_RE = re.compile(
    r'<div id="(?P<idx>\d+)" class="page"(?P<attrs>[^>]*)>(?P<body>.*?)</div>',
    re.DOTALL,
)
# <span class="pdfImg-3-4" style="background-position: -232px -0px;"></span>
_TILE_RE = re.compile(
    r'<span class="pdfImg-(?P<col>\d+)-(?P<row>\d+)"'
    r'\s+style="background-position:\s*-?(?P<x>\d+)px\s+-?(?P<y>\d+)px;"'
)


@dataclass(frozen=True)
class Tile:
    """一片瓦片的搬运指令：从雪碧图 (sx, sy) 取，贴到页面的 (col, row)。"""

    col: int
    row: int
    sx: int
    sy: int

    @property
    def dest(self) -> tuple[int, int]:
        return self.col * TILE_W, self.row * TILE_H

    @property
    def source(self) -> tuple[int, int, int, int]:
        return self.sx, self.sy, self.sx + TILE_W, self.sy + TILE_H


def parse_viewer(html: str) -> list[list[Tile]]:
    """解析查看器 HTML，返回每页的瓦片搬运表。"""
    pages: list[list[Tile]] = []
    for page_match in _PAGE_RE.finditer(html):
        tiles = [
            Tile(int(t["col"]), int(t["row"]), int(t["x"]), int(t["y"]))
            for t in _TILE_RE.finditer(page_match["body"])
        ]
        if tiles:
            pages.append(tiles)
    return pages


def reassemble(sprite: Image.Image, tiles: list[Tile], page_no: int) -> Image.Image:
    """把一页的瓦片按目标网格贴回，返回该页图像。"""
    page = Image.new("RGB", (PAGE_W, PAGE_H), "white")
    for tile in tiles:
        piece = sprite.crop(tile.source)
        page.paste(piece, tile.dest)
    return page


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="还原 openstd 公开预览页（仅供本地技术验证）")
    ap.add_argument("--viewer", type=Path, required=True, help="查看器 HTML 快照")
    ap.add_argument("--sprite", type=Path, required=True, help="瓦片雪碧图 (WebP)")
    ap.add_argument("--out", type=Path, required=True, help="页图输出目录")
    ap.add_argument("--stem", default="gbt1568", help="输出文件名前缀")
    args = ap.parse_args(argv)

    for path in (args.viewer, args.sprite):
        if not path.exists():
            print(f"错误：找不到 {path}", file=sys.stderr)
            return 2

    html = args.viewer.read_text(encoding="utf-8", errors="replace")
    pages = parse_viewer(html)
    if not pages:
        print("错误：未在查看器 HTML 中解析到任何页", file=sys.stderr)
        return 3

    args.out.mkdir(parents=True, exist_ok=True)
    with Image.open(args.sprite) as sprite:
        sprite = sprite.convert("RGB")
        print(f"雪碧图 {sprite.width}x{sprite.height}；解析到 {len(pages)} 页")
        for i, tiles in enumerate(pages, start=1):
            page = reassemble(sprite, tiles, i)
            dest = args.out / f"{args.stem}_p{i:04d}.png"
            page.save(dest)
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()[:16]
            print(f"  第 {i} 页：{len(tiles):>3} 片 -> {dest.name}  sha256:{digest}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
