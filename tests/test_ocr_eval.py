"""索引层评估测试。

这个模块最容易出的错**不是代码错，是度量口径错**——算得出数字，但数字没意义。
下面每个用例都对应一次真实踩坑，注释里记的是"当时错成什么样"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docqa.eval.ocr_eval import (
    TranscriptionRef,
    best_aligned_cer,
    compare_tables_semantically,
    evaluate_ocr,
    parse_transcription,
)
from docqa.models import OcrBlock, OcrPage, TableGrid

# ---------------------------------------------------------------- 解析基准


class TestParseTranscription:
    def test_parses_fenced_text_and_md_table(self, tmp_path: Path):
        p = tmp_path / "t.md"
        p.write_text(
            "# 基准\n\n"
            "### 第 1 页（正文页）\n\n```\n3.1  抗拉强度应大于等于 590 MPa。\n```\n\n"
            "### 第 2 页（表格页）\n\n"
            "| 项目 | 平键 | 楔键 |\n|---|---|---|\n| 键宽 b | 1.0 | 1.5 |\n",
            encoding="utf-8",
        )
        ref = parse_transcription(p)
        assert ref.pages == [1, 2]
        assert "590 MPa" in ref.text_by_page[1]
        assert ref.table_by_page[2][0] == ["项目", "平键", "楔键"]
        # 分隔行 |---|---|---| 必须被丢掉，否则会当成数据行参与比对
        assert len(ref.table_by_page[2]) == 2


# ---------------------------------------------------------------- 对齐


class TestAlignment:
    def test_identical_text_scores_zero(self):
        s = "3.1  键的抗拉强度应大于等于 590 MPa。"
        norm, strict, n = best_aligned_cer(s, s)
        assert norm == pytest.approx(0.0, abs=1e-9)
        assert strict == pytest.approx(0.0, abs=1e-9)
        assert n > 0

    def test_strict_cer_is_never_below_normalized(self):
        """**不变量**：保留标点只可能暴露更多错误，不可能更少。

        曾经两个口径各自搜一次窗口，位置不同，跑出过"符号敏感 1.9% < 归一化 5.1%"
        这种反常值。这个不等式就是那次错误的回归防线。
        """
        ref = "3.6  楔键斜度 1:100 的角度公差按 GB/T 11334—2005 中的 AT8 级选取。"
        ocr = "3.6  楔键斜度 1:100 的角度公差按 GB/T 11334-2005 中的 AT9 级选取。"
        norm, strict, _ = best_aligned_cer(ref, ocr)
        assert strict >= norm - 1e-9

    def test_noncontiguous_excerpt_still_scores_low(self):
        """**回归**：参考是**跳过中间条款**的抽样节选，不是连续段落。

        第 3 页参考的条款序列是 3.1 → 3.2 → **3.4**（跳过了 3.3）。
        整段滑窗实现下这一段永远匹配不上，实测 CER 44.5%——
        而人工核对过 OCR 是对的。改成逐行对齐后应当接近 0。
        """
        page = (
            "3.1  键的抗拉强度应大于等于 590 MPa。\n"
            "3.2  键表面不允许有裂纹、浮锈、氧化皮和毛刺。\n"
            "3.3  这一条**没有**被抽样进参考基准。\n"
            "3.4  半圆键的键长 L 两端允许倒圆角，圆角半径 r=0.5 mm～1.5 mm。\n"
        )
        excerpt = (
            "3.1  键的抗拉强度应大于等于 590 MPa。\n"
            "3.2  键表面不允许有裂纹、浮锈、氧化皮和毛刺。\n"
            "3.4  半圆键的键长 L 两端允许倒圆角，圆角半径 r=0.5 mm～1.5 mm。\n"
        )
        norm, _, _ = best_aligned_cer(excerpt, page)
        assert norm < 0.05, f"非连续节选被误算成 {norm:.1%}"

    def test_short_lines_are_skipped(self):
        """页码、单字标题这类短行不参与统计——它们没有统计意义，只会拉偏均值。"""
        _, _, n = best_aligned_cer("1\n2\n3\n", "任意内容")
        assert n == 0

    def test_empty_reference_is_safe(self):
        assert best_aligned_cer("", "abc") == (0.0, 0.0, 0)

    def test_ocr_longer_than_reference(self):
        """OCR 整页远长于参考行——这正是需要定位的原因。"""
        ref = "3.1  抗拉强度应大于等于 590 MPa。"
        pad = "前言" * 200
        norm, _, _ = best_aligned_cer(ref, pad + ref + pad)
        assert norm < 0.01


# ---------------------------------------------------------------- 表格


def _grid(rows: list[list[str]], n_header_rows: int = 2) -> TableGrid:
    return TableGrid(
        table_id="p1-t1", page_no=1, caption=None,
        n_rows=len(rows), n_cols=max(len(r) for r in rows),
        cells=rows, n_header_rows=n_header_rows,
    )


class TestSemanticTable:
    def test_folded_reference_vs_expanded_ocr(self):
        """**核心用例**：参考是 4 列折叠表，OCR 是展开后的分层网格。

        逐格比会得到 14% 这种毫无意义的数字（形状 4×4 vs 9×8）。
        按语义位置取格才对得上。
        """
        expected = [
            ["检查项目", "平键", "半圆键", "楔键"],
            ["键宽 b", "1.0", "1.0", "1.5"],
        ]
        # 展开后的网格：合并的表头在每个子列上重复（`cells` 的既定语义）
        ocr = _grid([
            ["检查项目", "平键", "平键", "半圆键", "半圆键", "楔键", "楔键"],
            ["键宽 b", "1.0", "", "1.0", "", "1.5", ""],
        ], n_header_rows=1)
        r = compare_tables_semantically(expected, ocr)
        assert r["comparable"] is True
        assert r["cells_correct"] == 3
        assert r["cell_accuracy"] == pytest.approx(1.0)
        assert r["ocr_shape"] == [2, 7]

    def test_header_with_fullwidth_space_still_matches(self):
        """国标表头常写成「平　键」（全角空格），调用方写「平键」。

        空白敏感的匹配会静默失败，表现出来是"这一格找不到"——
        很容易被误读成"OCR 读错了"，而其实是比对代码的锅。
        """
        expected = [["检查项目", "平　键"], ["键宽 b", "1.0"]]
        ocr = _grid([["检查项目", "平键"], ["键宽 b", "1.0"]], n_header_rows=1)
        assert compare_tables_semantically(expected, ocr)["cell_accuracy"] == 1.0

    def test_subscript_folding_locates_but_value_still_compared(self):
        """OCR 丢下标（`d₁` 读成 `d1`）时**要能定位到行**。

        不折叠的话整行定位失败（返回 None），与"取值不对"混为一谈——
        前者说明表结构没对上，后者才是识别错误，两者的严重程度不同。
        """
        expected = [["检查项目", "半圆键"], ["直径 d₁", "2.5"]]
        ocr = _grid([["检查项目", "半圆键"], ["直径 d1", "2.5"]], n_header_rows=1)
        r = compare_tables_semantically(expected, ocr)
        assert r["cells_compared"] == 1
        assert r["cell_accuracy"] == 1.0

    def test_wrong_value_is_counted_as_mismatch(self):
        expected = [["检查项目", "平键"], ["键宽 b", "1.0"]]
        ocr = _grid([["检查项目", "平键"], ["键宽 b", "1.5"]], n_header_rows=1)
        r = compare_tables_semantically(expected, ocr)
        assert r["cell_accuracy"] == 0.0
        assert r["mismatches"][0]["expected"] == "1.0"
        assert r["mismatches"][0]["got"] == "1.5"

    def test_missing_cell_reported_not_crashed(self):
        expected = [["检查项目", "平键"], ["键宽 b", "1.0"]]
        ocr = _grid([["检查项目", "楔键"], ["键宽 b", "1.0"]], n_header_rows=1)
        r = compare_tables_semantically(expected, ocr)
        assert r["cell_accuracy"] == 0.0
        assert r["mismatches"][0]["got"] is None


# ---------------------------------------------------------------- 汇总


class TestEvaluateOcr:
    def _page(self, no: int, text: str, *, failed: bool = False,
              tables: list[TableGrid] | None = None) -> OcrPage:
        block = OcrBlock(
            block_id=f"p{no}-b0", page_no=no, type="paragraph", order=0, text=text,
        )
        return OcrPage(
            page_no=no, blocks=[block], tables=tables or [],
            image_sha256="0" * 64, provider="test", model="test",
            prompt_version="v1", failed=failed,
        )

    def test_unevaluated_pages_do_not_drag_the_average_down(self):
        """**关键口径**：没有基准的页如实标"未评估"，**不记 0 分**。

        记 0 分会让"没量"伪装成"很差"——两件事必须分开。
        """
        ref = TranscriptionRef(text_by_page={1: "3.1  抗拉强度应大于等于 590 MPa。"})
        pages = [
            self._page(1, "3.1  抗拉强度应大于等于 590 MPa。"),
            self._page(2, "这一页没有基准"),
        ]
        m = evaluate_ocr(pages, ref)
        assert m["pages_evaluated"] == 1
        assert m["cer_normalized"] == pytest.approx(0.0, abs=1e-9)
        assert 2 not in m["pages_not_evaluated"]

    def test_failed_page_reported_unavailable(self):
        ref = TranscriptionRef(text_by_page={1: "任意基准文本内容"})
        m = evaluate_ocr([self._page(1, "", failed=True)], ref)
        assert m["pages_evaluated"] == 0
        assert m["pages_not_evaluated"] == [1]
        assert "失败" in m["per_page"][0]["reason"]

    def test_missing_page_reported(self):
        ref = TranscriptionRef(text_by_page={9: "任意基准文本内容"})
        m = evaluate_ocr([self._page(1, "x")], ref)
        assert m["pages_not_evaluated"] == [9]
        assert "不在" in m["per_page"][0]["reason"]

    def test_table_page_without_ocr_table_is_flagged(self):
        ref = TranscriptionRef(table_by_page={1: [["项目", "平键"], ["键宽 b", "1.0"]]})
        m = evaluate_ocr([self._page(1, "无表格")], ref)
        assert m["pages_evaluated"] == 0
        assert "未产出表格" in m["per_page"][0]["reason"]
