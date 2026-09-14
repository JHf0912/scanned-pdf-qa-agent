"""HTML 报告测试。

报告是交给评审的演示材料，所以它必须：自包含（无外部依赖）、
把不确定性如实呈现（每个比例带 Wilson 区间）、逐题可追溯。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docqa.eval.runner import GoldenItem, load_golden, run_eval
from docqa.report import render_html_report


@pytest.fixture
def report(agent, tmp_path: Path):
    p = tmp_path / "qa.jsonl"
    p.write_text("\n".join(json.dumps(i, ensure_ascii=False) for i in [
        {"qid": "1", "question": "抗拉强度要求是多少", "category": "clause",
         "must_contain": ["590"], "citation_pages": [1]},
        {"qid": "2", "question": "今天天气怎么样", "category": "no_answer",
         "expected_refusal": True, "expected_reason": "not_in_document"},
    ]), encoding="utf-8")
    return run_eval(load_golden(p), agent.answer, engine="null", doc_id="d",
                    domain="standard")


class TestHtmlReport:
    def test_renders_valid_html(self, report):
        html = render_html_report(report)
        assert html.startswith("<!DOCTYPE html>")
        assert html.rstrip().endswith("</html>")

    def test_is_self_contained(self, report):
        """不能有外部资源引用——报告要能直接发出去、离线打开。"""
        html = render_html_report(report)
        assert "<script src=" not in html
        assert "<link " not in html
        assert "@import" not in html

    def test_reports_confidence_intervals_not_just_point_estimates(self, report):
        """小样本下的「100%」极具误导性，必须同时给出区间。"""
        html = render_html_report(report)
        assert "CI" in html or "置信区间" in html
        assert "Wilson" in html

    def test_includes_every_question(self, report):
        """题干必须出现在**逐题卡片区域**，而不是只在文末的原始 JSON dump 里。

        这是一条被修正过的断言。原先只写 `"…" in html`，而 `to_dict()` 的
        原始 JSON 里也含题干——于是"报告读错键名、卡片只显示 Q1"这个缺陷
        被断言放了过去。**断言必须落在它该在的位置上。**
        """
        html = render_html_report(report)
        cards_area = html.split("<h2>逐题明细</h2>", 1)[1].split("<h2>原始结果</h2>", 1)[0]
        assert "抗拉强度要求是多少" in cards_area
        assert "今天天气怎么样" in cards_area

    def test_includes_metric_cards(self, report):
        html = render_html_report(report)
        assert "准确率" in html and "拒答精确率" in html

    def test_includes_raw_json(self, report):
        html = render_html_report(report)
        assert "原始结果" in html

    def test_notes_are_surfaced(self, report):
        report.notes.append("这是一条测试用的口径说明")
        assert "这是一条测试用的口径说明" in render_html_report(report)

    def test_escapes_html_in_content(self, report):
        """答案里若含 HTML，必须转义——否则报告本身成了注入点。"""
        report.per_item[0]["answer"] = "<script>alert(1)</script>"
        html = render_html_report(report)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


class TestAdversarialSection:
    def test_renders_detection_table_when_present(self, report):
        report.adversarial = {
            "total_mutations": 10,
            "overall_detection_rate": 0.8,
            "by_kind": {"number": {"detected": 8, "total": 10, "rate": 0.8, "misses": []}},
            "responsibility": {"number": "numeric_grounding"},
        }
        html = render_html_report(report)
        assert "对抗变异" in html
        assert "numeric_grounding" in html

    def test_absent_adversarial_section_does_not_crash(self, report):
        report.adversarial = {}
        assert "对抗变异" not in render_html_report(report)


class TestParseSection:
    """「PDF 解析结果」小节——作业演示要求第 2 条的落点。"""

    @pytest.fixture
    def report_with_pages(self, report):
        from docqa.models import OcrBlock, OcrPage, TableGrid, Usage

        grid = TableGrid(
            table_id="p1-t1", page_no=1, caption="表 1", n_rows=2, n_cols=3,
            cells=[["检查项目", "半圆键", "楔键"], ["键宽 b", "1.0", "1.5"]],
            n_header_rows=1,
        )
        page = OcrPage(
            page_no=1,
            blocks=[
                OcrBlock(block_id="p1-b0", page_no=1, type="clause", order=0,
                         text="3.1 抗拉强度", clause_label="3.1"),
                OcrBlock(block_id="p1-b1", page_no=1, type="paragraph", order=1,
                         text="键的抗拉强度应大于等于 590 MPa。"),
                OcrBlock(block_id="p1-b2", page_no=1, type="paragraph", order=2,
                         text="此处字迹模糊", uncertain=True),
            ],
            tables=[grid], image_sha256="x", provider="t", model="t",
            prompt_version="v1", usage=Usage(),
        )
        report.pages = [page]
        report.manifest = {
            "ocr": {"engine": "vlm", "model": "test-model", "pages_failed": []},
            "render": {"images": [{"page_no": 1, "width": 2315, "height": 3274,
                                   "dpi": 280.0, "native": True}]},
        }
        return report

    def test_section_present(self, report_with_pages):
        html = render_html_report(report_with_pages)
        assert "PDF 解析结果" in html

    def test_body_text_is_shown(self, report_with_pages):
        html = render_html_report(report_with_pages)
        assert "键的抗拉强度应大于等于 590 MPa。" in html
        assert "3.1" in html  # 条款号

    def test_table_rendered_as_real_table(self, report_with_pages):
        html = render_html_report(report_with_pages)
        assert "<table>" in html
        assert "<th>半圆键</th>" in html      # 表头用 th
        assert "<td>1.0</td>" in html          # 数据用 td

    def test_uncertain_block_is_flagged(self, report_with_pages):
        html = render_html_report(report_with_pages)
        assert "模型标不确定" in html

    def test_render_provenance_shown(self, report_with_pages):
        """要能看到页图是怎么来的：原生 DPI、是否位精确取图。"""
        html = render_html_report(report_with_pages)
        assert "280 DPI" in html
        assert "位精确取图" in html

    def test_missing_pages_degrades_gracefully(self, report):
        report.pages = []
        html = render_html_report(report)
        assert "PDF 解析结果" in html
        assert "无法展示正文与表格" in html

    def test_merged_cells_preserved(self, report):
        """合并块必须还原成 colspan/rowspan，而不是把值重复三遍。"""
        from docqa.models import CellSpan, TableGrid

        grid = TableGrid(
            table_id="t", page_no=1, caption="表 1", n_rows=2, n_cols=3,
            cells=[["平键", "平键", "平键"], ["a", "b", "c"]],
            spans=[CellSpan(row=0, col=0, rowspan=1, colspan=3)], n_header_rows=1,
        )
        report.pages = []
        from docqa.report import _render_grid

        html = _render_grid(grid)
        assert 'colspan="3"' in html
        assert html.count("<th") == 1  # 合并后只有一个表头格，不是三个
