"""评估运行器、报告、pipeline 编排与 CLI 的测试。

这些模块大多是 I/O 编排代码，测试的重点不是分支覆盖本身，而是
**几条必须在代码里守住的约束**：

- fixture 运行不得与实跑基线相比（否则会得出"换模型反而更好了"这种无意义结论）；
- 指标下滑必须导致非零退出（否则挂不上 CI）；
- 单页 OCR 失败必须被隔离，且证据触及该页的问题一律拒答。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from docqa.chunker import chunk_document
from docqa.cli import app
from docqa.errors import RegressionDetected
from docqa.eval.adversarial import generate_mutations, run_adversarial
from docqa.eval.noise import DEFAULT_LEVELS, NoiseConfig, inject_noise, noise_sweep_levels
from docqa.eval.runner import (
    GoldenItem,
    compare_baseline,
    context_utilization,
    load_golden,
    run_eval,
    score_item,
)
from docqa.index import HybridIndex
from docqa.llm import NullProvider
from docqa.models import AnswerBundle, Citation, RefusalReason, RetrievalHit, Usage
from docqa.qa import QaAgent

runner = CliRunner()


# ---------------------------------------------------------------- fixtures

@pytest.fixture
def golden_file(tmp_path: Path) -> Path:
    p = tmp_path / "qa.jsonl"
    items = [
        {"qid": "1", "question": "抗拉强度要求是多少", "category": "clause",
         "must_contain": ["590"], "citation_pages": [1]},
        {"qid": "2", "question": "今天天气怎么样", "category": "no_answer",
         "expected_refusal": True, "expected_reason": "not_in_document"},
    ]
    p.write_text("\n".join(json.dumps(i, ensure_ascii=False) for i in items), encoding="utf-8")
    return p


# ---------------------------------------------------------------- golden io

class TestGoldenIo:
    def test_loads_items(self, golden_file):
        items = load_golden(golden_file)
        assert len(items) == 2
        assert items[1].expected_refusal is True

    def test_comment_lines_skipped(self, tmp_path):
        p = tmp_path / "g.jsonl"
        p.write_text('// 注释\n{"qid":"1","question":"q","category":"clause"}\n', encoding="utf-8")
        assert len(load_golden(p)) == 1

    def test_malformed_line_skipped_not_fatal(self, tmp_path):
        p = tmp_path / "g.jsonl"
        p.write_text('{"qid":"1","question":"q","category":"clause"}\n{ 坏行\n', encoding="utf-8")
        assert len(load_golden(p)) == 1


# ---------------------------------------------------------------- scoring

class TestScoring:
    def test_refusal_question_scored_on_refusal_not_text(self):
        item = GoldenItem(qid="n", question="q", category="no_answer", expected_refusal=True)
        bundle = AnswerBundle(question="q", answer=None, refused=True,
                              refusal_reason=RefusalReason.NOT_IN_DOCUMENT)
        assert score_item(item, bundle)["passed"] is True

    def test_wrong_refusal_reason_fails(self):
        item = GoldenItem(qid="n", question="q", category="no_answer",
                          expected_refusal=True, expected_reason="not_in_document")
        bundle = AnswerBundle(question="q", answer=None, refused=True,
                              refusal_reason=RefusalReason.LOW_CONFIDENCE)
        assert score_item(item, bundle)["passed"] is False

    def test_refusing_an_answerable_question_fails(self):
        item = GoldenItem(qid="c", question="q", category="clause", must_contain=["590"])
        bundle = AnswerBundle(question="q", answer=None, refused=True,
                              refusal_reason=RefusalReason.NOT_IN_DOCUMENT)
        assert score_item(item, bundle)["passed"] is False

    def test_missing_fact_fails(self):
        item = GoldenItem(qid="c", question="q", category="clause", must_contain=["590"])
        bundle = AnswerBundle(question="q", answer="强度很高。[p.1 §1]",
                              citations=[Citation(1, "1", "c1", "x")])
        d = score_item(item, bundle)
        assert d["passed"] is False and "590" in d["missing_facts"]

    def test_forbidden_fact_fails(self):
        item = GoldenItem(qid="t", question="q", category="table",
                          must_contain=["1.0"], must_not_contain=["1.5"])
        bundle = AnswerBundle(question="q", answer="是 1.5。[p.4 §表 1]",
                              citations=[Citation(4, "表 1", "c1", "x")])
        assert score_item(item, bundle)["passed"] is False

    def test_citation_page_checked(self):
        item = GoldenItem(qid="c", question="q", category="clause", citation_pages=[4])
        bundle = AnswerBundle(question="q", answer="内容。[p.1 §1]",
                              citations=[Citation(1, "1", "c1", "x")])
        assert score_item(item, bundle)["citation_page_ok"] is False

    def test_context_utilization(self):
        bundle = AnswerBundle(
            question="q", answer="a",
            citations=[Citation(1, "1", "c1", "x")],
            hits=[RetrievalHit("c1", 1.0, "rrf", 1), RetrievalHit("c2", 0.5, "rrf", 2)],
        )
        assert context_utilization(bundle) == pytest.approx(0.5)


# ---------------------------------------------------------------- runner

class TestRunEval:
    def test_report_shape(self, golden_file, agent):
        rep = run_eval(load_golden(golden_file), agent.answer,
                       engine="null", doc_id="d", domain="standard")
        assert rep.metrics["items"] == 2
        assert "accuracy" in rep.metrics
        assert rep.intervals["accuracy"][1] <= 1.0
        assert "clause" in rep.per_category
        assert json.dumps(rep.to_dict(), ensure_ascii=False)

    def test_single_item_exception_does_not_abort_run(self, golden_file):
        def exploding(question: str):
            raise RuntimeError("boom")

        rep = run_eval(load_golden(golden_file), exploding,
                       engine="null", doc_id="d", domain="standard")
        assert rep.metrics["items"] == 2
        assert rep.metrics["passed"] == 0

    def test_per_category_has_confidence_intervals(self, golden_file, agent):
        rep = run_eval(load_golden(golden_file), agent.answer,
                       engine="null", doc_id="d", domain="standard")
        for d in rep.per_category.values():
            lo, hi = d["ci"]
            assert 0.0 <= lo <= hi <= 1.0


class TestBaseline:
    def test_no_baseline_file_is_not_a_regression(self, tmp_path, golden_file, agent):
        rep = run_eval(load_golden(golden_file), agent.answer,
                       engine="null", doc_id="d", domain="standard")
        assert compare_baseline(rep, tmp_path / "nope.json") == []

    def test_regression_detected(self, tmp_path, golden_file, agent):
        """基线设为不可达的高分，验证下滑会被检出并非零退出。"""
        rep = run_eval(load_golden(golden_file), agent.answer,
                       engine="null", doc_id="d", domain="standard")
        base = tmp_path / "base.json"
        # 1.01 不可达 —— 任何实际结果都低于它，等价于"基线明显更好"
        base.write_text(json.dumps({"engine": "null", "metrics": {"accuracy": 1.01}}),
                        encoding="utf-8")
        regressions = compare_baseline(rep, base)
        assert regressions and "accuracy" in regressions[0]

    def test_no_regression_when_metrics_hold(self, tmp_path, golden_file, agent):
        rep = run_eval(load_golden(golden_file), agent.answer,
                       engine="null", doc_id="d", domain="standard")
        base = tmp_path / "base.json"
        base.write_text(json.dumps({"engine": "null",
                                    "metrics": {"accuracy": rep.metrics["accuracy"]}}),
                        encoding="utf-8")
        assert compare_baseline(rep, base) == []

    def test_engine_mismatch_refuses_comparison(self, tmp_path, golden_file, agent):
        """fixture 产物与真实识别不是一回事，混着比毫无意义——必须拒绝。"""
        rep = run_eval(load_golden(golden_file), agent.answer,
                       engine="null", doc_id="d", domain="standard")
        base = tmp_path / "base.json"
        base.write_text(json.dumps({"engine": "fixture", "metrics": {"accuracy": 0.1}}),
                        encoding="utf-8")
        with pytest.raises(RegressionDetected, match="不可比"):
            compare_baseline(rep, base)


# ---------------------------------------------------------------- adversarial

class TestAdversarial:
    # 样本刻意包含 数值 / 标准号 / 单位 / 引用 四类可变异目标
    REF = "斜度 1:100 的角度公差按 GB/T 11334—2005 中的 AT8 级选取，极限偏差为 ±590 MPa。[p.1 §3.1]"

    def test_generates_multiple_mutation_kinds(self):
        muts = generate_mutations(self.REF, [Citation(1, "3.1", "c1", "x")])
        kinds = {m.kind for m in muts}
        assert {"number", "docid", "citation", "fabricate"} <= kinds
        assert len(muts) >= 5

    def test_mutations_actually_differ_from_original(self):
        muts = generate_mutations(self.REF, [Citation(1, "3.1", "c1", "x")])
        assert all(m.answer != self.REF or m.citations != [Citation(1, "3.1", "c1", "x")]
                   for m in muts)

    def test_detection_report_structure(self, agent):
        report = run_adversarial(
            [(self.REF, [Citation(1, "3.1", "c1", "x")])],
            retrieved_ids={"c1"}, known_ids={"c1"},
            evidence_by_chunk={
                "c1": "3.1 斜度 1:100 的角度公差按 GB/T 11334—2005 中的 AT8 级选取，"
                      "极限偏差为 ±590 MPa。"
            },
        )
        assert report["total_mutations"] > 0
        assert 0.0 <= report["overall_detection_rate"] <= 1.0
        # 必须说明每类变异由哪条检查负责，否则看不出缺口在哪
        assert "responsibility" in report


# ---------------------------------------------------------------- noise

class TestNoise:
    def test_zero_level_is_identity(self):
        text = "键的抗拉强度应大于等于 590 MPa。"
        assert inject_noise(text, NoiseConfig(level=0.0)) == text

    def test_noise_actually_changes_text(self):
        text = "键的抗拉强度应大于等于 590 MPa。" * 5
        assert inject_noise(text, NoiseConfig(level=0.5, seed=1)) != text

    def test_same_seed_is_reproducible(self):
        """噪声实验必须可复现，否则那条退化曲线没有意义。"""
        text = "键的抗拉强度应大于等于 590 MPa。" * 5
        a = inject_noise(text, NoiseConfig(level=0.3, seed=7))
        b = inject_noise(text, NoiseConfig(level=0.3, seed=7))
        assert a == b

    def test_different_seeds_differ(self):
        text = "键的抗拉强度应大于等于 590 MPa。" * 5
        assert inject_noise(text, NoiseConfig(level=0.3, seed=1)) != \
               inject_noise(text, NoiseConfig(level=0.3, seed=2))

    def test_sweep_covers_levels_and_seeds(self):
        pts = noise_sweep_levels(seeds=2)
        assert len(pts) == len(DEFAULT_LEVELS) * 2
        assert {p.level for p in pts} == set(DEFAULT_LEVELS)


# ---------------------------------------------------------------- CLI

class TestCli:
    def test_help(self):
        assert runner.invoke(app, ["--help"]).exit_code == 0

    def test_detect_on_synthetic_scan(self, tmp_path):
        """端到端跑一次类型判定，验证 CLI 真的能起来。"""
        import pymupdf
        from PIL import Image

        img = tmp_path / "p.png"
        Image.new("RGB", (300, 400), "white").save(img)
        doc = pymupdf.open()
        page = doc.new_page(width=150, height=200)
        page.insert_image(pymupdf.Rect(0, 0, 150, 200), filename=str(img))
        pdf = tmp_path / "s.pdf"
        doc.save(pdf)
        doc.close()

        result = runner.invoke(app, ["detect", str(pdf)])
        assert result.exit_code == 0, result.output
        assert "SCANNED" in result.output

    def test_detect_writes_json(self, tmp_path):
        import pymupdf
        from PIL import Image

        img = tmp_path / "p.png"
        Image.new("RGB", (300, 400), "white").save(img)
        doc = pymupdf.open()
        page = doc.new_page(width=150, height=200)
        page.insert_image(pymupdf.Rect(0, 0, 150, 200), filename=str(img))
        pdf = tmp_path / "s.pdf"
        doc.save(pdf)
        doc.close()

        out = tmp_path / "prof.json"
        assert runner.invoke(app, ["detect", str(pdf), "--out", str(out)]).exit_code == 0
        assert json.loads(out.read_text(encoding="utf-8"))["kind"] == "SCANNED"

    def test_missing_pdf_rejected_by_typer(self, tmp_path):
        result = runner.invoke(app, ["detect", str(tmp_path / "nope.pdf")])
        assert result.exit_code != 0

    def test_ask_without_ingest_errors_clearly(self, tmp_path):
        """产物目录不存在时给出可读提示。

        显式传 `--doc` 指向不存在的目录，**不要依赖"环境里恰好没有 ingest 产物"**——
        这条断言原先就是那么写的，结果仓库里一旦有了 demo 产物它就失效了。
        **测试不该依赖环境状态。**
        """
        result = runner.invoke(app, ["ask", "任意问题", "--doc", str(tmp_path / "并不存在")])
        assert result.exit_code != 0
        assert "找不到" in result.output or "读取产物失败" in result.output
