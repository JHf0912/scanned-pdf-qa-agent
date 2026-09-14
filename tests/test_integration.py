"""集成测试：把流水线串起来跑，验证端到端行为。

这些测试**不碰网络、不碰模型**——用 fixture 引擎与合成文档。
真实文档与真实模型的行为由 `docqa eval` + `data/golden/` 覆盖。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docqa.chunker import chunk_document
from docqa.config import load_domain
from docqa.errors import InputError
from docqa.index import HybridIndex
from docqa.llm import NullProvider
from docqa.models import Citation, RefusalReason
from docqa.pipeline import assert_not_golden
from docqa.qa import QaAgent
from docqa.qa.verify import verify


class TestGoldenIsolation:
    """金标目录绝不能被当作管线输入。

    这条约束必须在代码里，而不是靠文档提醒——靠人记得的事情早晚会忘，
    而且一旦失效是**静默**的：指标会变得很好看，没人会发现。
    """

    def test_golden_dir_rejected(self):
        with pytest.raises(InputError, match="拒绝把金标集内容"):
            assert_not_golden(Path("data/golden/qa.jsonl"))

    def test_nested_golden_path_rejected(self):
        with pytest.raises(InputError):
            assert_not_golden(Path("data/golden/sub/dir/file.pdf"))

    def test_raw_dir_allowed(self):
        assert_not_golden(Path("data/raw/gbt1568_scan.pdf"))  # 不应抛出


class TestEndToEnd:
    @pytest.fixture
    def agent(self, structured_doc, standard_profile, cfg):
        chunks, _ = chunk_document([structured_doc], "testdoc", standard_profile)
        index = HybridIndex(chunks, standard_profile)
        return QaAgent(index, NullProvider(), cfg, standard_profile, doc_id="testdoc")

    def test_relevant_question_answers_with_citations(self, agent):
        """无模型时走抽取式：答案就是证据，因此必然带引用、必然通过数值落地。"""
        bundle = agent.answer("抗拉强度要求是多少")
        assert not bundle.refused
        assert bundle.citations
        assert bundle.verification.gates_passed
        assert "590" in (bundle.answer or "")

    def test_irrelevant_question_is_refused(self, agent):
        bundle = agent.answer("今天天气怎么样")
        assert bundle.refused
        assert bundle.refusal_reason in (
            RefusalReason.NOT_IN_DOCUMENT, RefusalReason.LOW_CONFIDENCE
        )

    def test_refusal_carries_an_actionable_reason(self, agent):
        """拒答必须解释为什么——六个原因对应六种不同的用户动作。"""
        bundle = agent.answer("今天天气怎么样")
        assert bundle.refusal_detail
        assert len(bundle.refusal_detail) > 10

    def test_trace_records_every_stage(self, agent):
        bundle = agent.answer("抗拉强度要求是多少")
        steps = [t.step for t in bundle.trace]
        assert "retrieve" in steps and "draft" in steps and "verify" in steps

    def test_low_coverage_refuses_before_calling_model(self, agent):
        """覆盖率过低时应在生成**之前**拒答，不浪费一次调用。"""
        bundle = agent.answer("完全不相关的问题内容")
        steps = [t.step for t in bundle.trace]
        assert "precheck" in steps or bundle.refused
        assert "draft" not in steps

    def test_answer_bundle_serialises(self, agent):
        bundle = agent.answer("抗拉强度要求是多少")
        data = bundle.to_dict()
        assert json.dumps(data, ensure_ascii=False)  # 可序列化
        assert data["verification"]["checks"]


class TestRepairLoop:
    """修复循环必须真的会触发，且必须有上限。"""

    def test_repair_loop_is_bounded(self, cfg, structured_doc, standard_profile):
        """用一个总是产出"数值不存在"的假 provider，验证循环会终止且带上限。"""

        class BadProvider:
            name = "bad"
            supports_vision = False
            model = "bad"
            calls = 0

            def complete(self, system, user, **kw):
                from docqa.llm.base import LLMResponse
                from docqa.models import Usage

                self.calls += 1
                # 每次都给一个证据里不存在的数值 —— 必然过不了 numeric_grounding
                return LLMResponse(
                    text="根据证据，该值为 99999 MPa。[p.1 §3.1]",
                    provider="bad", model="bad", usage=Usage(calls=1),
                )

            def close(self):
                pass

        profile = load_domain("standard", cfg)
        profile.qa.max_attempts = 2
        chunks, _ = chunk_document([structured_doc], "d", profile)
        index = HybridIndex(chunks, profile)
        provider = BadProvider()
        agent = QaAgent(index, provider, cfg, profile, doc_id="d")

        bundle = agent.answer("抗拉强度要求是多少")

        # 轮次有上限
        assert bundle.attempts <= profile.qa.max_attempts
        assert provider.calls <= profile.qa.max_attempts
        # 修不好就必须拒答，而不是把不过门的答案交出去
        assert bundle.refused
        assert bundle.refusal_reason is RefusalReason.VERIFICATION_FAILED
        # 轨迹里能看到"修了但还是没过"的完整过程
        steps = [t.step for t in bundle.trace]
        assert "repair" in steps and "give_up" in steps


class TestCrossEngineVerification:
    """跨引擎一致性：这是防 OCR 污染的那一侧（数值落地防不住它）。"""

    def test_spotcheck_marks_disagreement(self):
        from docqa.ocr.spotcheck import diff_claims

        a = "键宽 b 为 1.0，键长 L 为 4。"
        b = "键宽 b 为 1.5，键长 L 为 4。"
        result = diff_claims(a, b)
        assert not result.agreed
        assert "1.0" in result.only_in_a or "1.5" in result.only_in_b

    def test_agreeing_engines_report_no_mismatch(self):
        from docqa.ocr.spotcheck import diff_claims

        same = "键宽 b 为 1.0，键长 L 为 4。"
        assert diff_claims(same, same).agreed
