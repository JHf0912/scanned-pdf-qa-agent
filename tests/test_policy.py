"""拒答策略与歧义检测测试。

歧义检测最容易做成"对什么都报歧义"——那等于把系统变成一台拒答机器。
所以这里既测"该报的要报出来"，也测"不该报的别乱报"。
（这个测试类里的反例不是凭空写的：第一版实现只有"不同条款 + 数字不同"
两条判据，结果对几乎每个问题都报歧义，下面几条反例就是那时加上的。）
"""

from __future__ import annotations

from docqa.config import load_config
from docqa.models import Chunk
from docqa.qa.policy import (
    detect_ambiguity,
    evidence_coverage,
    is_existence_question,
    is_in_document_negative,
    question_terms,
)


def mk(cid: str, text: str, label: str, page: int = 1) -> Chunk:
    return Chunk(
        chunk_id=cid, doc_id="d", page_no=page, ordinal=1, kind="clause",
        text=text, clause_path=(label,), clause_label=label,
    )


class TestQuestionTerms:
    def test_function_words_removed(self):
        assert "什么" not in question_terms("什么是抗拉强度")

    def test_generic_nouns_removed(self):
        """「要求」「规定」这类泛化词信息量极低，留着会把覆盖率无谓拉低。"""
        terms = question_terms("抗拉强度要求是多少")
        assert "要求" not in terms
        assert "多少" not in terms
        assert "抗拉强度" in terms


class TestCoverage:
    def test_relevant_question_has_high_coverage(self):
        chunks = [mk("c1", "键的抗拉强度应大于等于 590 MPa。", "3.1")]
        assert evidence_coverage("抗拉强度要求是多少", chunks) >= 0.9

    def test_irrelevant_question_has_zero_coverage(self):
        chunks = [mk("c1", "键的抗拉强度应大于等于 590 MPa。", "3.1")]
        assert evidence_coverage("今天天气怎么样", chunks) == 0.0

    def test_no_content_terms_returns_one(self):
        """问题里没有实词时无从判断，不应因此拒答。"""
        assert evidence_coverage("？", [mk("c1", "x", "1")]) == 1.0


class TestExistenceQuestion:
    def test_detects_yes_no_phrasing(self):
        assert is_existence_question("本标准是否规定了键的材料牌号？")
        assert is_existence_question("有没有涉及包装要求")

    def test_ordinary_question_not_flagged(self):
        assert not is_existence_question("抗拉强度要求是多少")


class TestAmbiguity:
    def test_no_ambiguity_with_single_hit(self):
        assert detect_ambiguity("q", [(mk("c1", "值 1.0", "3.1"), 1.0)]) == []

    def test_conflicting_values_are_surfaced_regardless_of_ranking(self):
        """两段都在回答同一个问题、却给出**不同数值**时，必须报歧义。

        早先这条断言写的是反面（"第一名明显领先就不叫歧义"），依据是一条
        "分数要接近"的判据。那条判据后来被证明**毫无区分度**——RRF 下任意
        两个候选的分数比恒在 0.95~1.00——所以删掉了。

        删掉之后这条的行为反过来了，而且**反过来才是对的**：
        排序反映的是检索次序，不是真值。两段给出互相冲突的数值，
        正该把两个候选连同出处一起摆出来让用户判断，
        而不是"谁排前面就信谁"——那正是歧义检测要防的事。
        """
        hits = [
            (mk("c1", "抗拉强度 590 MPa", "3.1"), 1.0),
            (mk("c2", "抗拉强度 950 MPa", "3.2"), 0.3),
        ]
        got = detect_ambiguity("抗拉强度是多少", hits)
        assert len(got) == 2, "冲突的取值必须被摆出来，不能按排名悄悄取舍"
        assert {c.value for c in got} == {"590", "950"}

    def test_no_ambiguity_when_values_agree(self):
        hits = [
            (mk("c1", "强度 590 MPa", "3.1"), 1.0),
            (mk("c2", "强度 590 MPa", "3.2"), 0.98),
        ]
        assert detect_ambiguity("强度是多少", hits) == []

    def test_no_ambiguity_when_candidates_are_off_topic(self):
        """候选与问题不相关时不能报歧义——这是误报的主要来源。

        实测反例：问"本文件引用了哪些规范性文件"，检索会带出"1 范围"，
        它含"本文件""规定"等词，看着相关，实则完全不回答该问题。
        字符集合重叠给这两类打出的分**分不开**（都在 0.60~0.64），
        换成实词覆盖率后才拉开："1 范围"里既没有"引用"也没有"规范性"。
        """
        hits = [
            (mk("c1", "包装要求 1.0", "5.1"), 1.0),
            (mk("c2", "标志要求 2.0", "5.2"), 0.98),
        ]
        assert detect_ambiguity("抗拉强度是多少", hits) == []

    def test_partially_related_candidate_does_not_trigger_ambiguity(self):
        """只**部分**命中问题实词的候选，不该被算作冲突方。

        实测反例：问"示例件的工作温度范围是多少"，检索带出"1 范围"，
        它含"范围""示例件"，覆盖率 0.67，却完全不回答温度问题。
        若把阈值放宽到 0.6，这类片段就会被当成冲突方，
        把证据唯一且明确的问题判成歧义。
        """
        hits = [
            (mk("c1", "示例件的额定载荷 590 MPa", "3.1"), 1.0),
            (mk("c2", "1 范围 本文件规定了示例件的术语与通用规则，另有 950 项。", "1"), 0.98),
        ]
        assert detect_ambiguity("示例件的额定载荷是多少", hits) == []

    def test_ambiguity_detected_on_conflicting_values(self):
        """两段都相关、分数接近、但给出的数值不同 —— 这才是真歧义。"""
        hits = [
            (mk("c1", "报送截止时间为 9 月 10 日", "一"), 1.0),
            (mk("c2", "报送截止时间为 9 月 20 日", "二"), 0.98),
        ]
        got = detect_ambiguity("报送截止时间是什么时候", hits)
        assert len(got) >= 2
        assert len({c.value for c in got}) >= 2


class TestNegativeAnswer:
    def test_detects_negation_phrasing(self):
        """「未规定」是有效答案，不是拒答——必须能识别出来。"""
        assert is_in_document_negative("本标准未规定键的材料牌号。")

    def test_positive_answer_not_flagged(self):
        assert not is_in_document_negative("本标准规定了包装标志。")


class TestScoreMargin:
    """`min_score_margin` 与 `second_score` 曾经是一对"看起来能用"的旋钮。

    `second_score` 由索引层算出、四处调用点一路传参、`decide()` 收下就丢掉；
    `min_score_margin` 更彻底——在 `config.py` 与 `config/default.yaml` 里
    声明着，**全仓零处读取**。两个加起来等于没有。

    这里测的不只是"设了会拒答"，还有**设成 0 时必须完全不介入**：
    死开关的典型危害是"调了没反应"，而过修的典型危害是"没调也乱拒"，
    两头都得钉住。
    """

    def _decide(self, *, margin: float, top: float, second: float):
        import dataclasses

        from docqa.qa.policy import decide

        cfg = dataclasses.replace(load_config().qa, min_score_margin=margin)
        return decide(
            cfg=cfg, top_score=top, second_score=second, gates_passed=True,
            failed_gate_names=[], evidence_chunks=[mk("c1", "额定载荷 12.5 kN。", "3.1")],
            failed_pages=set(), candidates=[],
        )

    def test_margin_closes_refusal_when_scores_are_tight(self):
        d = self._decide(margin=0.3, top=0.90, second=0.85)   # 差距 0.05 < 0.30
        assert d.refused
        assert d.reason.value == "low_confidence"
        # 提示必须说清"是差距不够"，而不是笼统的"相关性不足"——
        # 否则用户不知道该怎么办（补限定条件 vs 换个问法）
        assert "差距" in d.detail

    def test_margin_allows_when_there_is_a_clear_winner(self):
        d = self._decide(margin=0.3, top=0.90, second=0.10)   # 差距 0.80 > 0.30
        assert not d.refused, f"有明确赢家却被拒：{d.reason}"

    def test_zero_margin_never_intervenes(self):
        """默认值 0.0 = 关闭。此时分差再小也不能因此拒答。"""
        d = self._decide(margin=0.0, top=0.900, second=0.899)
        assert not d.refused, "关闭状态下仍然介入，等于偷偷改了默认行为"

    def test_no_second_hit_does_not_trigger(self):
        """只有一条证据时没有"第二名"可言，不该因此拒答。"""
        d = self._decide(margin=0.3, top=0.90, second=0.0)
        assert not d.refused

    def test_config_default_is_off(self):
        """默认必须是关闭的：本语料只有 16 个片段，阈值未标定。"""
        assert load_config().qa.min_score_margin == 0.0
