"""自检测试。这是全项目最重要的测试文件。

每条 gate 都必须有**正例与反例**：只测"正确答案能通过"是不够的，
必须证明"已知的错误答案会被拦下"——否则这个自检就是摆设。
"""

from __future__ import annotations

from docqa.models import CheckSeverity, Citation
from docqa.qa.verify import cn_to_arabic, extract_claims, normalize, verify

KNOWN = {"c1", "c2"}
RETRIEVED = {"c1"}


# 文档里真实存在的条号。**必须传**——否则 `clause_consistency` 无法区分
# "条号 3.6" 与"表格数值 2.5"，两者形态完全相同。
KNOWN_LABELS = {"3.1", "3.6", "3.9", "1", "3", "5"}


def run(answer: str, citations: list[Citation], evidence: dict[str, str],
        labels: set[str] | None = None):
    return verify(answer, citations, retrieved_ids=RETRIEVED, known_ids=KNOWN,
                  evidence_by_chunk=evidence,
                  known_clause_labels=KNOWN_LABELS if labels is None else labels)


class TestNormalization:
    def test_fullwidth_to_halfwidth(self):
        assert normalize("ＧＢ／Ｔ　１５６８—２００８") == "GB/T 1568-2008"

    def test_various_dashes_unified(self):
        assert normalize("2005－2006–2007—2008") == "2005-2006-2007-2008"

    def test_cn_to_arabic(self):
        assert cn_to_arabic("十五") == "15"
        assert cn_to_arabic("三十六") == "36"
        assert cn_to_arabic("十") == "10"


class TestClaimExtraction:
    def test_citation_markers_are_excluded(self):
        """引用标记里的页码与条号绝不能被当成待校验的数值。"""
        c = extract_claims("数值为 590 MPa [p.3 §3.1]，另有 1.0 的 AQL [p.4 §表 1]")
        # 数值与单位会一起构成"量" token（只比数字防不住单位错误）
        assert "590 MPa" in c.numbers
        assert "3" not in c.numbers and "4" not in c.numbers

    def test_doc_ids_extracted(self):
        c = extract_claims("按 GB/T 11334—2005 选取")
        assert any("11334" in d for d in c.doc_ids)


class TestGatesPositive:
    def test_correct_answer_passes(self, evidence_map, good_citation):
        r = run("按 AT8 级选取，极限偏差为 ±AT8/2。[p.3 §3.6]", [good_citation], evidence_map)
        assert r.gates_passed, [c.detail for c in r.failed_gates]

    def test_fullwidth_digits_still_grounded(self, evidence_map, good_citation):
        r = run("斜度 １：１００，偏差 ±AT8/2。[p.3 §3.6]", [good_citation], evidence_map)
        assert r.gates_passed


class TestGatesNegative:
    def test_numeric_hallucination_is_caught(self, evidence_map, good_citation):
        """把 AT8 改成 AT9 —— 读起来毫无破绽，但证据里没有 AT9。"""
        r = run("极限偏差为 ±AT9/2。[p.3 §3.6]", [good_citation], evidence_map)
        assert not r.gates_passed

    def test_fabricated_doc_id_is_caught(self, evidence_map, good_citation):
        r = run("按 GB/T 9999—2020 选取。[p.3 §3.6]", [good_citation], evidence_map)
        assert not r.gates_passed

    def test_fabricated_citation_is_caught(self, evidence_map):
        bad = Citation(page_no=9, clause_label="7.1", chunk_id="nonexistent", quote="")
        r = run("按 AT8 级选取。[p.9 §7.1]", [bad], evidence_map)
        assert not r.gates_passed
        assert any(c.name == "citation_validity" for c in r.failed_gates)

    def test_zero_citations_is_a_failure_not_a_pass(self, evidence_map):
        """零引用必须判失败——否则"只要不引用，数值就永远校验通过"。"""
        r = run("按 AT8 级选取。", [], evidence_map)
        assert not r.gates_passed
        assert any(c.name == "citation_validity" for c in r.failed_gates)

    def test_numbers_without_evidence_fail(self):
        """有数值却拿不出证据可比对，必须失败。"""
        cite = Citation(page_no=1, clause_label="1", chunk_id="c1", quote="")
        r = run("数值是 123 MPa。[p.1 §1]", [cite], {})
        assert not r.gates_passed

    def test_clause_mismatch_is_caught(self, evidence_map, good_citation):
        """串条：3.9 确实是本文档的条号，但所引证据的条号是 3.6。"""
        r = run("按 AT8 级选取。[p.3 §3.6] 另见 3.9 条。", [good_citation], evidence_map)
        assert not r.gates_passed
        assert any(c.name == "clause_consistency" for c in r.failed_gates)

    def test_data_values_are_not_treated_as_clause_refs(self, evidence_map, good_citation):
        """**回归**：表格数值（2.5 / 1.0）与条号（3.6）形态相同，不能混为一谈。

        实测踩到的坑：一条完全正确的表格问答被 `clause_consistency` 判为
        "答案提到条款 2.5 但证据里没有"，随即触发修复、复检、最终拒答。
        **把正确回答判成错误，比没有这条检查更糟**——它会训练使用者忽略所有告警。
        """
        r = run("乙型的壁厚 AQL 为 2.5，外径为 1.0。[p.3 §3.6]", [good_citation], evidence_map)
        check = next(c for c in r.checks if c.name == "clause_consistency")
        assert check.passed, check.detail
        assert "非文档条号" in check.detail  # 明确说明这两处被跳过了

    def test_forged_clause_number_is_caught_by_numeric_grounding(self, evidence_map, good_citation):
        """编造一个文档里不存在的条号——由数值落地兜住，不留缺口。"""
        r = run("按 AT8 级选取。[p.3 §3.6] 参见 9.9 条。", [good_citation], evidence_map)
        assert not r.gates_passed

    def test_empty_labels_degrades_visibly_not_silently(self, evidence_map, good_citation):
        """不传条号集合时检查退化为空操作，但**降级本身要写在结论里**。

        断言落在 CheckResult.detail 而不是日志上：评审看的是报告，
        不是 stderr。而且 `logging.setup()` 特意设了 `propagate=False`，
        日志本来就不该被测试的 caplog 捕获。
        """
        # 答案里的小数必须**本身就在证据里**，否则 numeric_grounding 会因别的原因
        # 失败，就测不到本条要测的东西了。条号候选必须是带小数点的形态（如 2.5），
        # `1:100` 用的是冒号，压根不会被当成条号候选。
        ev = {"c1": "3.6 斜度公差。合格质量水平 AQL 为 2.5。"}
        r = run("斜度公差的 AQL 为 2.5。[p.3 §3.6]", [good_citation], ev, labels=set())
        check = next(c for c in r.checks if c.name == "clause_consistency")
        assert check.passed, "没有条号名单时应跳过而非误判"
        assert "已跳过" in check.detail, "降级必须写在结论里，不能静默失效"


class TestSeverity:
    def test_numeric_grounding_is_a_gate(self, evidence_map, good_citation):
        r = run("按 AT8 级选取。[p.3 §3.6]", [good_citation], evidence_map)
        chk = next(c for c in r.checks if c.name == "numeric_grounding")
        assert chk.severity is CheckSeverity.GATE

    def test_sentence_support_is_advisory_by_default(self, evidence_map, good_citation):
        """句级支撑度默认只上报、不拦截——未校准的阈值没有资格当门。"""
        r = run("按 AT8 级选取。[p.3 §3.6]", [good_citation], evidence_map)
        chk = next(c for c in r.checks if c.name == "sentence_support")
        assert chk.severity is CheckSeverity.ADVISORY

    def test_support_can_be_promoted_to_gate(self, evidence_map, good_citation):
        r = verify("完全无关的内容。[p.3 §3.6]", [good_citation],
                   retrieved_ids=RETRIEVED, known_ids=KNOWN,
                   evidence_by_chunk=evidence_map, support_gate=0.9)
        chk = next(c for c in r.checks if c.name == "sentence_support")
        assert chk.severity is CheckSeverity.GATE


class TestUnitGrounding:
    """**回归**：只校验数字是防不住单位错误的。

    对抗变异套件实测出 `unit` 类检出率为 **0%**：把 `590 MPa` 改成 `590 kPa`，
    数字 `590` 照样能在证据里找到，检查一路放行，而答案已经完全错了。

    根因是 `_UNIT_RE` 定义了却从未被使用——**死代码掩盖了一个真实的检测缺口**。
    现在数值与单位会一起构成"量" token 再比对。
    """

    EV = {"c1": "3.1 示例件的额定载荷应大于等于 12.5 kN。3.5 d ≤ 20 mm 取 2.0 mm。"}

    def _ng(self, answer):
        cite = Citation(page_no=1, clause_label="3.1", chunk_id="c1", quote="")
        r = verify(answer, [cite], retrieved_ids={"c1"}, known_ids={"c1"},
                   evidence_by_chunk=self.EV, known_clause_labels={"3.1", "3.5"})
        return next(c for c in r.checks if c.name == "numeric_grounding")

    def test_correct_unit_passes(self):
        assert self._ng("载荷为 12.5 kN。[p.1 §3.1]").passed

    def test_changed_unit_is_caught(self):
        check = self._ng("载荷为 12.5 kg。[p.1 §3.1]")
        assert not check.passed
        assert any("12.5 kg" in o for o in check.offenders)

    def test_changed_number_is_caught(self):
        assert not self._ng("载荷为 13.5 kN。[p.1 §3.1]").passed

    def test_missing_space_between_number_and_unit_still_matches(self):
        """`12.5kN` 与 `12.5 kN` 是同一条信息，不该判为幻觉。"""
        assert self._ng("载荷为 12.5kN。[p.1 §3.1]").passed

    def test_quantity_tokens_include_units(self):
        assert "12.5 kN" in extract_claims("载荷 12.5 kN。").numbers
        assert "2.0 mm" in extract_claims("壁厚 2.0 mm。").numbers
