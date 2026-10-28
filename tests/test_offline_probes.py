"""离线探针的回归测试。

这些测试守住的不只是"函数能跑"，而是几个**容易被悄悄改坏的口径**：

- 句级支撑度的正例/负例标签方向不能反（flagged 表示"支撑不足"）；
- 跨引擎探针注入的错误必须真的能被集合差看见，否则召回率是个假 1.0；
- 对照样本必须**只改排版**，一旦它改到字段，精确率就没有意义了；
- 报告里必须留下复现命令，否则数字无法被独立复核。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import docqa.config as config_module
from conftest import PROJECT_ROOT, make_block, make_page
from docqa.cli import app
from docqa.eval.corruption import (
    alternate_values,
    contains_token,
    pick_target,
    probe_targeted_corruption,
)
from docqa.eval.probes import (
    probe_cross_engine,
    probe_sentence_support,
    render_markdown,
    run_probes,
)
from docqa.eval.runner import GoldenItem
from docqa.models import Chunk
from docqa.ocr.spotcheck import diff_claims

runner = CliRunner()


def _chunk(cid: str, text: str, *, page: int = 1, kind: str = "clause") -> Chunk:
    return Chunk(
        chunk_id=cid,
        doc_id="d",
        page_no=page,
        ordinal=int(cid.lstrip("c")) if cid.lstrip("c").isdigit() else 0,
        kind=kind,  # type: ignore[arg-type]
        text=text,
        clause_label=None,
    )


@pytest.fixture
def probe_chunks() -> list[Chunk]:
    # 每个片段都 >= 30 字符：探针刻意跳过过短的片段（它们没有完整的句子结构）。
    return [
        _chunk("c1", "3.1 抗拉强度应大于等于 590 MPa。该要求适用于所有规格。"),
        _chunk("c2", "3.3 工作温度范围为 -20 ℃ ~ 60 ℃。超出范围时应停止使用。"),
        _chunk("c3", "3.4 密封等级应不低于 IP54。试验方法按第 4 章执行。"),
        _chunk("c4", "4.2 样本量 n 为 8，合格判定数为 Ac。抽样方案见表 1。"),
    ]


@pytest.fixture
def probe_page():
    return make_page([
        make_block(0, "clause", "3.1 示例件的额定载荷应大于等于 12.5 kN，执行 GB/T 11334—2005。", "3.1"),
        make_block(1, "paragraph", "3.3 工作温度范围为 -20 ℃ ~ 60 ℃。"),
    ])


class TestSentenceSupportProbe:
    def test_extractive_is_supported_and_fabricated_is_flagged(self, probe_chunks):
        report = probe_sentence_support(probe_chunks, seed=0)
        kinds = report["by_kind"]

        # 正例：证据原句必须落在"有支撑"一侧
        assert kinds["extractive"]["n"] > 0
        assert kinds["extractive"]["false_alarm"] == 0

        # 负例：与文档无关的整句必须被抓到
        assert kinds["fabricated"]["flagged"] == kinds["fabricated"]["n"]
        assert kinds["fabricated"]["missed"] == 0

    def test_mixed_sample_sits_exactly_on_the_threshold(self, probe_chunks):
        """一句真 + 一句假 → 2 句里 1 句有支撑 → 0.5，当前实现判"通过"。

        这不是 bug，而是阈值的真实边界：**假内容可以靠一句真话被"摊薄"**。
        探针必须把这个漏报报出来，否则召回率会显得比实际好。
        """
        report = probe_sentence_support(probe_chunks, seed=0)
        mixed = report["by_kind"]["mixed"]
        assert mixed["n"] > 0
        assert mixed["mean_score"] == pytest.approx(0.5)
        assert mixed["missed"] == mixed["n"]
        assert mixed["flagged"] == 0

        # 整体召回率因此必须小于 1——如果它变成 1，说明边界行为被改掉了
        assert report["confusion"]["recall"] < 1.0
        assert "不建议升格为 gate" in report["gate_recommendation"]

    def test_probe_is_deterministic(self, probe_chunks):
        assert (
            probe_sentence_support(probe_chunks, seed=0)
            == probe_sentence_support(probe_chunks, seed=0)
        )

    def test_threshold_sweep_exposes_the_tradeoff(self, probe_chunks):
        report = probe_sentence_support(probe_chunks, seed=0)
        sweep = {row["threshold"]: row for row in report["threshold_sweep"]}
        assert set(sweep) == {0.3, 0.4, 0.5, 0.6, 0.7, 0.8}
        # 把阈值抬到 0.6 能抓住 mixed，但本样本集的精确率不应因此崩塌
        assert sweep[0.6]["recall"] >= sweep[0.5]["recall"]


class TestCrossEngineProbe:
    def test_injected_field_errors_are_all_detected(self, probe_page):
        report = probe_cross_engine([probe_page])
        conf = report["confusion"]

        assert conf["tp"] > 0
        assert conf["fn"] == 0
        assert conf["recall"] == 1.0
        # 精确率必须是真测出来的：对照样本一条都不该报警
        assert conf["fp"] == 0
        assert conf["precision"] == 1.0

    def test_docid_field_is_actually_covered(self, probe_page):
        """干跑只覆盖 number 字段是个常见假象——这里必须看到 docid。"""
        report = probe_cross_engine([probe_page])
        assert "docid" in report["kinds_exercised"]
        assert "number" in report["kinds_exercised"]
        assert "name" in report["kinds_not_exercised"]

    def test_clean_variants_never_raise_false_alarms(self, probe_page):
        report = probe_cross_engine([probe_page])
        clean = {k: v for k, v in report["by_kind"].items() if k.startswith("clean_")}
        assert clean, "对照样本没有生成，精确率就是无源之水"
        for entry in clean.values():
            assert entry["false_alarm"] == 0

    def test_repeated_value_is_mutated_as_a_group(self):
        """同一个值出现两次时，单点差异集合差看不见，所以探针按取值组整体替换。"""
        page = make_page([
            make_block(0, "paragraph", "载荷为 12.5 kN。"),
            make_block(1, "paragraph", "温度也为 12.5 ℃。"),
        ])
        report = probe_cross_engine([page])
        assert report["confusion"]["tp"] == 1  # 只有一个 12.5 取值组
        assert report["confusion"]["fn"] == 0

    def test_single_occurrence_difference_can_be_invisible_to_set_diff(self):
        """当前实现的真实边界：改一处、而新值恰好已在页内别处出现时，集合差完全不报警。

        探针因此按"取值组"整体替换——否则注入的真值会有一部分根本不可见，
        召回率就不再是检测器的性质，而是注入方式的运气。
        """
        a = "载荷 12.5 kN，备用 12.5 kN，温度 12.6 ℃。"
        b = "载荷 12.6 kN，备用 12.5 kN，温度 12.6 ℃。"
        assert diff_claims(a, b).agreed is True


def _labeled_chunk(cid: str, text: str, label: str, ordinal: int) -> Chunk:
    return Chunk(
        chunk_id=cid,
        doc_id="d",
        page_no=1,
        ordinal=ordinal,
        kind="clause",
        text=text,
        clause_label=label,
    )


@pytest.fixture
def corruption_corpus() -> tuple[list[Chunk], list[GoldenItem]]:
    """三片段小语料：每个数值字段在整份语料里都只出现一次。"""
    chunks = [
        _labeled_chunk("d:p1:c1", "3.1 示例件的额定载荷应大于等于 12.5 kN。", "3.1", 1),
        _labeled_chunk("d:p1:c2", "3.2 示例件表面不允许有裂纹。", "3.2", 2),
        _labeled_chunk("d:p1:c3", "3.3 示例件的工作温度范围为 -20 ℃ ~ 60 ℃。", "3.3", 3),
    ]
    items = [
        GoldenItem(
            qid="Q1", question="示例件的额定载荷要求是多少？", category="clause",
            must_contain=["12.5", "kN"], relevant_chunk_ids=["p1:c1"],
        ),
        GoldenItem(
            qid="Q2", question="示例件的工作温度范围是多少？", category="clause",
            must_contain=["-20", "60"], relevant_chunk_ids=["p1:c3"],
        ),
    ]
    return chunks, items


class TestTargetedCorruptionProbe:
    """噪声命中答案字段时，系统会不会"忠实于错误证据"地答错。"""

    def test_hitting_the_answer_field_produces_silent_wrong_answers(
        self, corruption_corpus
    ):
        chunks, items = corruption_corpus
        report = probe_targeted_corruption(chunks, items)

        assert report["n_treatment"] == 2
        assert report["silent_wrong"]["count"] == 2
        assert report["silent_wrong"]["rate"] == 1.0
        # 关键点：错答案不是被哪条 gate 抓住，而是**全过了**
        for sample in report["samples"]:
            if sample["arm"] != "treatment":
                continue
            assert sample["gates_passed"] is True
            assert sample["gate_status"]["numeric_grounding"] is True
            assert sample["contains_corrupted"] is True
            assert sample["contains_original"] is False

    def test_control_arm_reaches_the_answer_without_breaking_it(
        self, corruption_corpus
    ):
        chunks, items = corruption_corpus
        report = probe_targeted_corruption(chunks, items)

        # 对照改动必须真的进入了答案，否则"没影响"只是因为它没被引用
        assert report["control_reached_answer"]["count"] == report["n_control"]
        assert report["control_preserved"]["count"] == report["n_control"]
        assert report["control_preserved"]["rate"] == 1.0

    def test_probe_is_deterministic(self, corruption_corpus):
        chunks, items = corruption_corpus
        assert (
            probe_targeted_corruption(chunks, items)
            == probe_targeted_corruption(chunks, items)
        )

    def test_non_unique_answer_field_is_excluded_with_reason(self):
        """同一个值出现在两个片段时，污染后原值仍在语料里，判据不再无歧义。"""
        chunks = [
            _labeled_chunk("d:p1:c1", "3.1 额定载荷应大于等于 12.5 kN。", "3.1", 1),
            _labeled_chunk("d:p1:c2", "3.2 备用载荷也为 12.5 kN。", "3.2", 2),
        ]
        item = GoldenItem(
            qid="Q1", question="示例件的额定载荷要求是多少？", category="clause",
            must_contain=["12.5"], relevant_chunk_ids=["p1:c1"],
        )
        target, reason = pick_target(item, chunks)
        assert target is None
        assert "不唯一" in reason

    def test_non_numeric_answer_fact_is_excluded_with_reason(self):
        chunks = [_labeled_chunk("d:p1:c1", "3.1 本文件适用于示例件。", "3.1", 1)]
        item = GoldenItem(
            qid="Q1", question="本文件的适用范围是什么？", category="clause",
            must_contain=["适用"], relevant_chunk_ids=["p1:c1"],
        )
        target, reason = pick_target(item, chunks)
        assert target is None
        assert "不含数字" in reason

    def test_excluded_questions_are_reported_not_dropped(self, corruption_corpus):
        chunks, items = corruption_corpus
        items = items + [
            GoldenItem(
                qid="Q3", question="示例件的质保期是多久？", category="no_answer",
                expected_refusal=True,
            )
        ]
        report = probe_targeted_corruption(chunks, items)
        assert [e["qid"] for e in report["excluded"]] == ["Q3"]


class TestCorruptionHelpers:
    def test_alternate_values_keep_length_and_avoid_zero(self):
        values = alternate_values("1.0")
        assert values[0] == "2.0"  # 先试离原值最近的改动
        assert all(len(v) == len("1.0") for v in values)
        assert "0.0" not in values
        assert "1.1" in values  # 换个数字位，避免与语料中已有的 2.0/3.0 冲突

    def test_token_boundary_does_not_match_inside_other_numbers(self):
        assert contains_token("条款 4.1 的取值", "4") is False
        assert contains_token("4  验收规则", "4") is True
        assert contains_token("1.5 与 1.0", "1.0") is True
        # `54` 是 `IP54` 的一部分：这解释了对照组为什么必须按字符区间排除
        assert contains_token("密封等级 IP54", "54") is True


class TestProbeReport:
    def test_markdown_contains_numbers_command_and_limitations(
        self, probe_page, probe_chunks
    ):
        payload = run_probes([probe_page], probe_chunks, seed=0)
        payload["metadata"] = {
            "doc_id": "d", "engine": "fixture", "domain": "standard", "source": "test"
        }
        payload["notes"] = ["合成件"]
        md = render_markdown(payload)

        assert "# 离线探针报告" in md
        assert "句级支撑度" in md
        assert "跨引擎一致性校验" in md
        assert "精确率" in md and "召回率" in md
        assert "uv run docqa eval-probes" in md
        assert "已知局限" in md

    def test_markdown_renders_the_targeted_corruption_section(
        self, probe_page, corruption_corpus
    ):
        chunks, items = corruption_corpus
        payload = run_probes([probe_page], chunks, seed=0, items=items)
        payload["metadata"] = {
            "doc_id": "d", "engine": "fixture", "domain": "standard", "source": "test"
        }
        md = render_markdown(payload)

        assert "定向污染" in md
        assert "静默错误率" in md
        assert "numeric_grounding" in md or "数值落地" in md
        assert payload["probes"]["targeted_corruption"]["n_treatment"] == 2

    def test_payload_is_json_serialisable(self, probe_page, probe_chunks):
        payload = run_probes([probe_page], probe_chunks, seed=0)
        json.dumps(payload, ensure_ascii=False)  # 不抛异常即可


class TestEvalProbesCli:
    def test_cli_does_not_read_project_dotenv(self, tmp_path, monkeypatch):
        """用户 shell 里有完整 `.env`、且没有 `DOCQA_DISABLE_DOTENV` 时，命令也不读它。

        这条守的是**命令自身的离线承诺**：`eval-probes` 的报告要能和干净环境逐字节
        对比，就不能依赖"本机恰好配了什么"。所以判据不是报告里的数字，而是边界：
        `load_dotenv` 一次都不该被调用。

        反证在同一用例里：同一环境下默认 `load_config()` 确实会调用它——否则
        "没被调用"可能只是因为这份 `.env` 路径根本没被看过，测试就是空的。
        """
        pdf = PROJECT_ROOT / "artifacts" / "demo" / "demo_scan.pdf"
        fixtures = PROJECT_ROOT / "artifacts" / "demo" / "ocr"
        if not pdf.exists() or not fixtures.exists():
            pytest.skip("缺少合成件或 fixture 识别结果")

        # 模拟开发机：项目根有一份配好了端点的 .env，进程环境里没有隔离开关。
        project = tmp_path / "project"
        project.mkdir()
        (project / ".env").write_text(
            "DOCQA_LLM_PROVIDER=openai\n"
            "DOCQA_LLM_API_KEY=sk-should-not-be-read\n"
            "DOCQA_LLM_MODEL=fake-model\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(config_module, "PROJECT_ROOT", project)
        for key in [k for k in os.environ if k.startswith("DOCQA_")]:
            monkeypatch.delenv(key, raising=False)

        calls: list[tuple] = []
        monkeypatch.setattr(
            config_module, "load_dotenv", lambda *a, **k: calls.append((a, k))
        )

        result = runner.invoke(
            app,
            [
                "eval-probes",
                "--pdf", str(pdf),
                "--fixtures-dir", str(fixtures),
                "--out-root", str(tmp_path / "derived"),
                "--report", str(tmp_path / "probes.md"),
                "--json-out", str(tmp_path / "probes.json"),
            ],
        )

        assert result.exit_code == 0, result.output
        assert calls == [], "eval-probes 在离线路由上调用了 load_dotenv"

        # 反证：同一份 .env、同一环境下，默认配置路径**会**读它。
        config_module.load_config()
        assert calls, "默认路径没有读 .env，这条隔离测试证明不了任何事"

    def test_cli_runs_offline_on_the_synthetic_fixture(self, tmp_path):
        """从干净环境跑一次真实命令：现场 fixture ingest → 两个探针 → 两份报告。"""
        pdf = PROJECT_ROOT / "artifacts" / "demo" / "demo_scan.pdf"
        fixtures = PROJECT_ROOT / "artifacts" / "demo" / "ocr"
        if not pdf.exists() or not fixtures.exists():
            pytest.skip("缺少合成件或 fixture 识别结果")

        report = tmp_path / "probes.md"
        json_out = tmp_path / "probes.json"
        result = runner.invoke(
            app,
            [
                "eval-probes",
                "--pdf", str(pdf),
                "--fixtures-dir", str(fixtures),
                "--out-root", str(tmp_path / "derived"),
                "--report", str(report),
                "--json-out", str(json_out),
            ],
        )
        assert result.exit_code == 0, result.output
        assert report.exists() and report.stat().st_size > 500

        payload = json.loads(json_out.read_text(encoding="utf-8"))
        assert payload["metadata"]["engine"] == "fixture"
        assert payload["probes"]["sentence_support"]["n"] > 0
        assert payload["probes"]["cross_engine_spotcheck"]["n"] > 0
        assert payload["probes"]["cross_engine_spotcheck"]["confusion"]["fp"] == 0
        # 默认 --golden 存在时必须跑定向污染，否则"静默错答"那项又退回未测量
        corruption = payload["probes"]["targeted_corruption"]
        assert corruption["n_treatment"] > 0
        assert corruption["silent_wrong"]["count"] > 0
        assert "定向污染" in report.read_text(encoding="utf-8")
