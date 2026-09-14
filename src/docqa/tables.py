"""表格结构解析：HTML → 矩形单元格网格。

为什么要展开合并单元格
----------------------
表格问答要能回答"表 1 里半圆键的键宽 b 的 AQL 是多少"——这需要按
(行, 列) 精确定位到一个格子。如果保留原始的 rowspan/colspan 结构，
每次取值都要先算"这个坐标落在哪个合并块里"，极易出错。

所以这里把合并**展开**：被合并覆盖的每个坐标都填上同一个值。
取值从此不需要任何额外判断。原始合并信息保留在 `spans` 里，
渲染回 HTML 或做单元格级评估时仍然可用。

降级策略
--------
HTML 结构不合法或解析不出行列时，**不猜结构**——把整段 HTML 塞进一个
1x1 网格并置 `parse_degraded=True`。宁可让下游看到"这里结构没解析出来"，
也不要产出一个行列错位的表格，因为错位的表格会让数值落到错误的列上，
而那种错误在问答里几乎不可能被发现。
"""

from __future__ import annotations

import html
import re

from .logging import get_logger
from .models import CellSpan, TableGrid

_log = get_logger("tables")

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_CELL_RE = re.compile(
    r"<t[dh]([^>]*)>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE
)
_ROWSPAN_RE = re.compile(r'rowspan\s*=\s*["\']?(\d+)', re.IGNORECASE)
_COLSPAN_RE = re.compile(r'colspan\s*=\s*["\']?(\d+)', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_THEAD_RE = re.compile(r"<thead[^>]*>(.*?)</thead>", re.DOTALL | re.IGNORECASE)


def _cell_text(raw: str) -> str:
    """去掉标签、还原实体、压平空白。

    注意**不删除破折号**：国标表格里 `—` 表示"无此项"，是有语义的值，
    与空单元格含义不同。把它归一成空会让"这一项不适用"和"这一格漏识别了"混为一谈。
    """
    text = _TAG_RE.sub("", raw)
    text = html.unescape(text)
    text = text.replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


def parse_html_table(
    html_str: str,
    *,
    table_id: str,
    page_no: int,
    caption: str | None = None,
) -> TableGrid:
    """把 HTML 表格解析成矩形网格。"""
    if not html_str or "<tr" not in html_str.lower():
        # 走同一条降级路径：**保留原始内容**。返回空网格会让"这里有个表格但
        # 结构没解析出来"和"这里根本没有表格"变得无法区分，
        # 下游会因此悄悄丢掉整张表的信息。
        return _degraded(html_str, table_id, page_no, caption)

    rows = _ROW_RE.findall(html_str)
    if not rows:
        return _degraded(html_str, table_id, page_no, caption)

    # 表头行数：<thead> 里的行数；没有 thead 时，若首行单元格全是 <th> 也算表头
    n_header_rows = 1
    thead = _THEAD_RE.search(html_str)
    if thead:
        n_header_rows = max(1, len(_ROW_RE.findall(thead.group(1))))
    else:
        first = _CELL_RE.findall(rows[0]) if rows else []
        if first and all(attrs.strip().lower().startswith(("", " ")) is False for attrs, _ in first):
            n_header_rows = 1

    # 展开合并单元格，得到矩形网格
    grid: list[list[str | None]] = []
    spans: list[CellSpan] = []

    for r, row_html in enumerate(rows):
        while len(grid) <= r:
            grid.append([])
        col = 0
        for attrs, cell_html in _CELL_RE.findall(row_html):
            while col < len(grid[r]) and grid[r][col] is not None:
                col += 1  # 跳过被上方 rowspan 占用的位置

            rs = int(_ROWSPAN_RE.search(attrs).group(1)) if _ROWSPAN_RE.search(attrs) else 1
            cs = int(_COLSPAN_RE.search(attrs).group(1)) if _COLSPAN_RE.search(attrs) else 1
            rs, cs = max(1, rs), max(1, cs)

            value = _cell_text(cell_html)
            if rs > 1 or cs > 1:
                spans.append(CellSpan(row=r, col=col, rowspan=rs, colspan=cs))

            for dr in range(rs):
                while len(grid) <= r + dr:
                    grid.append([])
                target = grid[r + dr]
                while len(target) < col + cs:
                    target.append(None)
                for dc in range(cs):
                    target[col + dc] = value
            col += cs

    n_cols = max((len(row) for row in grid), default=0)
    if n_cols == 0:
        return _degraded(html_str, table_id, page_no, caption)

    cells = [
        [(c if c is not None else "") for c in row] + [""] * (n_cols - len(row))
        for row in grid
    ]

    # 行列数明显异常（例如把所有内容挤成一列）时标记降级，供下游复核
    degraded = n_cols == 1 and len(cells) > 2

    return TableGrid(
        table_id=table_id,
        page_no=page_no,
        caption=caption,
        n_rows=len(cells),
        n_cols=n_cols,
        cells=cells,
        spans=spans,
        n_header_rows=min(n_header_rows, len(cells)),
        parse_degraded=degraded,
    )


def _degraded(html_str: str, table_id: str, page_no: int, caption: str | None) -> TableGrid:
    """结构解析失败时的降级产物。

    把原始 HTML 作为单个单元格保留——**信息不丢，只是结构没了**，
    并且 `parse_degraded=True` 会让报告与 UI 显式标注这一点。
    """
    _log.warning("表格结构解析失败，降级为单格", extra={"page": page_no, "detail": table_id})
    return TableGrid(
        table_id=table_id,
        page_no=page_no,
        caption=caption,
        n_rows=1,
        n_cols=1,
        cells=[[html_str.strip()]],
        parse_degraded=True,
    )


def find_cell(grid: TableGrid, *, row_label: str, col_label: str) -> tuple[str, int, int] | None:
    """按行标题与列标题定位单元格。

    先在第一列里找行标题所在行，再在表头区域里找列标题所在列，两者的交点即答案。
    这是表格问答最常用的一类定位，单独提出来便于测试。

    Returns:
        (单元格值, 行号, 列号)；任一标题找不到时返回 None。
    """
    if not grid.cells or grid.n_rows == 0:
        return None

    row_idx: int | None = None
    for r in range(grid.n_rows):
        if grid.cells[r] and row_label in grid.cells[r][0]:
            row_idx = r
            break
    if row_idx is None:
        return None

    col_idx: int | None = None
    for r in range(min(grid.n_header_rows, grid.n_rows)):
        for c in range(grid.n_cols):
            if col_label in grid.cells[r][c]:
                col_idx = c
                break
        if col_idx is not None:
            break
    if col_idx is None:
        return None

    return grid.cells[row_idx][col_idx], row_idx, col_idx
