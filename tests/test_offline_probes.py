"""离线探针的回归测试。

这些测试守住的不只是"函数能跑"，而是几个**容易被悄悄改坏的口径**：

- 句级支撑度的正例/负例标签方向不能反（flagged 表示"支撑不足"）；
- 跨引擎探针注入的错误必须真的能被集合差看见，否则召回率是个假 1.0；
- 对照样本必须**只改排版**，一旦它改到字段，精确率就没有意义了；
- 报告里必须留下复现命令，否则数字无法被独立复核。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import PROJECT_ROOT, make_block, make_page
from docqa.cli import app
from docqa.eval.probes import (
    probe_cross_engine,
    probe_sentence_support,
    render_markdown,
    run_probes,
)
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

    def test_payload_is_json_serialisable(self, probe_page, probe_chunks):
        payload = run_probes([probe_page], probe_chunks, seed=0)
        json.dumps(payload, ensure_ascii=False)  # 不抛异常即可


class TestEvalProbesCli:
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
