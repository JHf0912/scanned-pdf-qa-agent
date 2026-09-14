"""评估指标测试。

重点验证两件容易做错的事：

1. 中文 CER 必须**按字符**算——按空格分词会得到无意义的结果；
2. 小样本必须给出**置信区间**——"n=3 全对"和"n=300 全对"完全不是一回事。
"""

from __future__ import annotations

import pytest

from docqa.eval.metrics import (
    cer,
    character_accuracy,
    ndcg_at_k,
    numeric_field_accuracy,
    recall_at_k,
    reciprocal_rank,
    table_cell_accuracy,
    wilson_interval,
)

REF = "楔键斜度 1:100 的角度公差按 GB/T 11334—2005 中的 AT8 级选取，极限偏差为 ±AT8/2。"


class TestCer:
    def test_identical_is_zero(self):
        assert cer(REF, REF).normalized == 0.0

    def test_single_substitution_is_small(self):
        r = cer(REF, REF.replace("AT8", "AT9"))
        assert 0 < r.normalized < 0.1

    def test_traditional_chinese_normalized_away(self):
        """繁简差异不应被算成替换错误，否则 CER 会虚高。"""
        assert cer("楔键斜度级选取", "楔鍵斜度級選取").normalized == pytest.approx(0.0, abs=1e-9)

    def test_fullwidth_digits_normalized(self):
        assert cer("数值 1568", "数值 １５６８").normalized == pytest.approx(0.0, abs=1e-9)

    def test_reports_two_cer_values(self):
        """归一化 CER 与符号敏感 CER 都要报——去标点会掩盖单位与符号错误。"""
        r = cer(REF, REF.replace("±", "").replace("—", "-"))
        assert r.strict >= r.normalized

    def test_character_accuracy_is_complement(self):
        assert character_accuracy(0.02) == pytest.approx(0.98)

    def test_empty_reference_flagged_unavailable(self):
        assert cer("", "x").available is False


class TestWilson:
    def test_small_perfect_sample_has_low_bound(self):
        """5/5 的 95% 置信下界只有约 57% —— 这正是必须报区间的原因。"""
        lo, hi = wilson_interval(5, 5)
        assert 0.4 < lo < 0.7
        assert hi == pytest.approx(1.0)

    def test_larger_sample_tightens_interval(self):
        lo_small, _ = wilson_interval(5, 5)
        lo_big, _ = wilson_interval(500, 500)
        assert lo_big > lo_small

    def test_zero_total(self):
        assert wilson_interval(0, 0) == (0.0, 0.0)

    def test_bounds_within_unit_interval(self):
        for s, t in [(0, 5), (2, 3), (7, 10)]:
            lo, hi = wilson_interval(s, t)
            assert 0.0 <= lo <= hi <= 1.0


class TestRetrieval:
    def test_recall_at_k(self):
        assert recall_at_k(["a", "b", "c"], {"b"}, 3) == 1.0
        assert recall_at_k(["a", "x", "y"], {"b"}, 3) == 0.0

    def test_recall_respects_k_cutoff(self):
        assert recall_at_k(["a", "b", "c"], {"c"}, 2) == 0.0

    def test_reciprocal_rank(self):
        assert reciprocal_rank(["a", "b"], {"a"}) == 1.0
        assert reciprocal_rank(["a", "b"], {"b"}) == 0.5
        assert reciprocal_rank(["a", "b"], {"z"}) == 0.0

    def test_ndcg_prefers_higher_rank(self):
        assert ndcg_at_k(["a", "b", "c"], {"a"}, 3) > ndcg_at_k(["b", "c", "a"], {"a"}, 3)


class TestTableCell:
    def test_perfect_match(self):
        g = [["a", "b"], ["c", "d"]]
        assert table_cell_accuracy(g, g)["cell_accuracy"] == 1.0

    def test_reports_shape_mismatch(self):
        assert table_cell_accuracy([["a", "b"]], [["a"]])["shape_match"] is False

    def test_partial_credit(self):
        r = table_cell_accuracy([["a", "b"], ["c", "d"]], [["a", "b"], ["c", "X"]])
        assert r["cell_accuracy"] == pytest.approx(0.75)


class TestNumericField:
    def test_missing_number_detected(self):
        r = numeric_field_accuracy("强度应大于等于 590 MPa。", "强度应大于等于 950 MPa。")
        assert r["field_recall"] < 1.0
        assert any("590" in m for m in r["missing"])

    def test_all_present(self):
        s = "强度 590 MPa，斜度 1:100。"
        assert numeric_field_accuracy(s, s)["field_recall"] == 1.0

    def test_spurious_number_reported(self):
        r = numeric_field_accuracy("强度 590 MPa。", "强度 590 MPa，另有 999 项。")
        assert 999 == int(r["spurious"][0].rstrip("项")) or r["spurious"]
