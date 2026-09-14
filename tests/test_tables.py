"""表格结构解析测试：合并单元格、两级表头、降级路径。"""

from __future__ import annotations

import pytest

from docqa.tables import find_cell, parse_html_table

# 与 GB/T 1568-2008 表 1 同构的两级表头 + 横向合并结构
TWO_LEVEL = """<table>
<thead>
<tr><td rowspan="3">检查项目</td><td colspan="7">合格质量水平 AQL</td></tr>
<tr><td colspan="3">平　键</td><td rowspan="2">半圆键</td><td colspan="3">楔　键</td></tr>
<tr><td>普通</td><td>导向</td><td>薄型</td><td>普通</td><td>薄型</td><td>钩头</td></tr>
</thead>
<tbody>
<tr><td>键宽 b</td><td colspan="3">1.0</td><td>1.0</td><td colspan="3">1.5</td></tr>
<tr><td>键长 L</td><td colspan="3">4</td><td>—</td><td colspan="3">4</td></tr>
</tbody></table>"""


class TestMergedCells:
    def test_grid_is_rectangular(self):
        g = parse_html_table(TWO_LEVEL, table_id="t1", page_no=4)
        assert g.n_rows == 5
        assert g.n_cols == 8
        assert all(len(r) == 8 for r in g.cells)

    def test_colspan_expanded_to_all_covered_columns(self):
        g = parse_html_table(TWO_LEVEL, table_id="t1", page_no=4)
        # "平　键" 跨 3 列，展开后三列都应拿到同一个值
        assert g.cells[1][1] == g.cells[1][2] == g.cells[1][3] == "平 键"

    def test_rowspan_repeated_down_rows(self):
        g = parse_html_table(TWO_LEVEL, table_id="t1", page_no=4)
        assert g.cells[0][0] == g.cells[1][0] == g.cells[2][0] == "检查项目"

    def test_spans_recorded(self):
        g = parse_html_table(TWO_LEVEL, table_id="t1", page_no=4)
        assert len(g.spans) >= 5  # AQL 跨 7 列 + 平键/楔键各跨 3 列 + rowspan

    def test_header_rows_counted(self):
        g = parse_html_table(TWO_LEVEL, table_id="t1", page_no=4)
        assert g.n_header_rows == 3


class TestFindCell:
    def test_locates_value_by_row_and_column_label(self):
        g = parse_html_table(TWO_LEVEL, table_id="t1", page_no=4)
        value, r, c = find_cell(g, row_label="键宽 b", col_label="半圆键")
        assert value == "1.0"

    def test_distinguishes_columns_that_share_a_group_header(self):
        """半圆键 1.0 与 楔键 1.5 都属"键宽 b"这一行，绝不能串列。"""
        g = parse_html_table(TWO_LEVEL, table_id="t1", page_no=4)
        assert find_cell(g, row_label="键宽 b", col_label="半圆键")[0] == "1.0"
        assert find_cell(g, row_label="键宽 b", col_label="楔")[0] == "1.5"

    def test_dash_is_preserved_not_treated_as_empty(self):
        """国标表格里「—」表示"不适用"，与空白格含义不同，不能被归一成空。"""
        g = parse_html_table(TWO_LEVEL, table_id="t1", page_no=4)
        value, _, _ = find_cell(g, row_label="键长 L", col_label="半圆键")
        assert value == "—"

    def test_missing_row_returns_none(self):
        g = parse_html_table(TWO_LEVEL, table_id="t1", page_no=4)
        assert find_cell(g, row_label="不存在的行", col_label="平") is None


class TestDegradation:
    def test_non_table_html_degrades_but_preserves_content(self):
        g = parse_html_table("<p>不是表格</p>", table_id="x", page_no=1)
        assert g.parse_degraded is True
        assert g.n_rows == 1 and g.n_cols == 1
        # 关键：原始内容必须保留，不能返回空网格
        assert "不是表格" in g.cells[0][0]

    def test_empty_string_degrades(self):
        g = parse_html_table("", table_id="x", page_no=1)
        assert g.parse_degraded is True

    def test_markdown_render_includes_caption(self):
        g = parse_html_table(TWO_LEVEL, table_id="t1", page_no=4, caption="表 1")
        md = g.to_markdown()
        assert "表 1" in md and "|" in md


class TestRealHeaderStructure:
    """针对 GB/T 1568-2008 表 1 的**真实**表头结构。

    这个结构是双人独立阅读核对出来的：表头有三行，
    `合格质量水平 AQL` 横跨全部 7 个数据列，`检查项目` 纵跨全部 3 行，
    `半圆键` 纵跨第 2~3 行，而数据区里平键组与楔键组各自合并为一格。

    最初记录成"两行表头"是错的——这个测试类就是那个修正的固化。
    """

    def test_header_spans_all_data_columns(self):
        g = parse_html_table(TWO_LEVEL, table_id="t1", page_no=4)
        # 第 1 行的第 1~7 列都应是「合格质量水平 AQL」
        assert all(g.cells[0][c] == "合格质量水平 AQL" for c in range(1, 8))

    def test_row_label_spans_three_header_rows(self):
        g = parse_html_table(TWO_LEVEL, table_id="t1", page_no=4)
        assert g.cells[0][0] == g.cells[1][0] == g.cells[2][0] == "检查项目"

    def test_half_round_key_spans_two_header_rows(self):
        """半圆键纵跨第 2~3 行：它既是二级表头也是三级表头。"""
        g = parse_html_table(TWO_LEVEL, table_id="t1", page_no=4)
        assert g.cells[1][4] == g.cells[2][4] == "半圆键"

    def test_data_row_group_merge_gives_one_value_per_group(self):
        g = parse_html_table(TWO_LEVEL, table_id="t1", page_no=4)
        row = g.cells[3]  # 键宽 b
        assert row[1] == row[2] == row[3] == "1.0"   # 平键组
        assert row[5] == row[6] == row[7] == "1.5"   # 楔键组
        assert row[4] == "1.0"                        # 半圆键独立一列

    def test_lookup_matches_double_read_ground_truth(self):
        """与双人独立阅读一致的表 1 取值。"""
        g = parse_html_table(TWO_LEVEL, table_id="t1", page_no=4)
        assert find_cell(g, row_label="键宽 b", col_label="半圆键")[0] == "1.0"
        assert find_cell(g, row_label="键宽 b", col_label="楔")[0] == "1.5"
        assert find_cell(g, row_label="键长 L", col_label="半圆键")[0] == "—"
        assert find_cell(g, row_label="键长 L", col_label="楔")[0] == "4"
