"""CLI 与 Web API 的端到端测试。

这两个模块此前是覆盖率最薄的部分——`ingest` / `eval` / `serve` 三条命令
**从未被真正执行过**。第一轮干跑就在它们身上抓出好几个缺陷
（`adv` 未定义直接崩溃、豁免被 `decide()` 架空、报告读错键名）。

所以这里覆盖的是**命令真的能跑完**，而不只是"函数能被导入"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from docqa.api.app import create_app
from docqa.cli import app
from docqa.config import load_config
from docqa.ocr.fixture import FixtureOcrEngine

runner = CliRunner()
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ============================================================ Web API

class TestWebApi:
    """`docqa serve` 背后的三个路由。"""

    @pytest.fixture(scope="class")
    def client(self):
        cfg = load_config()
        # 用真实产物目录；没有就跳过（CI 上可能没跑过 ingest）
        base = cfg.cache_dir.parent
        docs = [
            p for p in base.iterdir()
            if p.is_dir() and (p / "manifest.json").exists()
        ] if base.exists() else []
        if not docs:
            pytest.skip("尚未 ingest 任何文档")
        return TestClient(create_app(cfg, domain="standard"))

    def test_index_serves_the_ui(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "docqa" in r.text or "问答" in r.text

    def test_status_reports_ready(self, client):
        r = client.get("/api/status")
        assert r.status_code == 200
        d = r.json()
        assert d["ready"] is True
        assert d["chunks"] > 0
        assert "citation_mode" in d

    def test_chunks_returns_all_chunks(self, client):
        r = client.get("/api/chunks")
        assert r.status_code == 200
        rows = r.json()
        assert rows and all("chunk_id" in c and "text" in c for c in rows)

    def test_ask_returns_bundle_with_evidence(self, client):
        r = client.post("/api/ask", json={"question": "示例件的额定载荷要求是多少？"})
        assert r.status_code == 200
        d = r.json()
        assert "verification" in d and "trace" in d
        # 前端靠 evidence 高亮被引片段，缺了它界面就退化成纯文本
        if d["citations"]:
            assert d["evidence"]

    def test_empty_question_rejected(self, client):
        assert client.post("/api/ask", json={"question": "   "}).status_code == 400

    def test_answer_matches_cli_path(self, tmp_path: Path):
        """API 与 CLI 必须给出同一个答案——它们共用同一个 AnswerBundle。

        **必须自备产物，不能读 `data/derived/` 的环境状态。**
        原实现取"目录 mtime 最新的那一份"，而 API 与测试各自算一次；
        只要有人重跑过任意一份 ingest，两边的选择就可能不同——
        于是这条"两条路径答案一致"的断言会**因为选错了文档而失败**，
        与它想验证的事情毫无关系。改成两边都指向同一个显式产物。
        """
        import dataclasses

        from docqa.pipeline import ingest

        # 往独立的根目录 ingest 一份，让 API 的"最新产物"只能是它
        isolated = tmp_path / "derived"
        ingest(
            PROJECT_ROOT / "artifacts" / "demo" / "demo_scan.pdf",
            cfg=load_config(), domain="standard",
            engine=FixtureOcrEngine(PROJECT_ROOT / "artifacts" / "demo" / "ocr"),
            out_root=isolated,
        )
        doc = next(p for p in isolated.iterdir() if (p / "manifest.json").exists())

        # API 侧的"最新产物"由 cfg.cache_dir.parent 决定，指到隔离目录即可
        cfg = dataclasses.replace(load_config(), cache_dir=isolated / "cache")

        q = "示例件的额定载荷要求是多少？"
        api = TestClient(create_app(cfg, domain="standard")).post(
            "/api/ask", json={"question": q}
        ).json()
        cli = runner.invoke(app, ["ask", q, "--doc", str(doc), "--json"])
        assert cli.exit_code == 0, cli.output
        cli_bundle = json.loads(cli.output[cli.output.index("{"):])
        assert api["refused"] == cli_bundle["refused"], (
            f"同一条链路给出不同结论：api.refused={api['refused']} "
            f"cli.refused={cli_bundle['refused']}"
        )
        assert api["answer"] == cli_bundle["answer"]
        assert api["answer"] == cli_bundle["answer"]


# ============================================================ CLI 全链路

class TestCliDryRun:
    """把干跑走通的路径固化成测试，防止再次退化。"""

    @pytest.fixture(scope="class")
    def demo_docs(self):
        cfg = load_config()
        base = cfg.cache_dir.parent
        docs = [
            p for p in base.iterdir()
            if p.is_dir() and (p / "manifest.json").exists()
            and p.name.startswith("demo_scan")
        ] if base.exists() else []
        if not docs:
            pytest.skip("尚未 ingest 合成演示件（跑 tools/make_demo_artifact.py）")
        return max(docs, key=lambda p: p.stat().st_mtime)

    def test_ask_json_is_machine_readable(self, demo_docs):
        r = runner.invoke(app, ["ask", "示例件的额定载荷要求是多少？",
                                "--doc", str(demo_docs), "--json"])
        assert r.exit_code == 0, r.output
        d = json.loads(r.output[r.output.index("{"):])
        assert "verification" in d and "trace" in d

    def test_eval_runs_end_to_end(self, demo_docs, tmp_path):
        """`eval` 曾经因为摘要里一个未定义变量直接崩溃，报告一个字都没写出来。"""
        golden = PROJECT_ROOT / "data" / "golden" / "demo_qa.jsonl"
        if not golden.exists():
            pytest.skip("缺少演示金标集")
        report = tmp_path / "r.html"
        out = tmp_path / "r.json"
        r = runner.invoke(app, ["eval", "--doc", str(demo_docs),
                                "--golden", str(golden),
                                "--report", str(report), "--json-out", str(out)])
        assert r.exit_code == 0, r.output
        assert report.exists() and report.stat().st_size > 1000
        assert out.exists()
        d = json.loads(out.read_text(encoding="utf-8"))
        assert d["metrics"]["items"] > 0

    def test_eval_report_contains_parse_section(self, demo_docs, tmp_path):
        """报告必须能看到**从 PDF 里读出了什么**——作业演示要求第 2 条。"""
        golden = PROJECT_ROOT / "data" / "golden" / "demo_qa.jsonl"
        if not golden.exists():
            pytest.skip("缺少演示金标集")
        report = tmp_path / "r.html"
        runner.invoke(app, ["eval", "--doc", str(demo_docs), "--golden", str(golden),
                            "--report", str(report), "--no-adversarial"])
        html = report.read_text(encoding="utf-8")
        assert "PDF 解析结果" in html
        assert "<table>" in html          # 表格渲染成真表格
        assert "额定载荷" in html          # 正文块

    def test_eval_report_shows_question_not_just_id(self, demo_docs, tmp_path):
        """逐题卡片要显示题干。曾经读错键名，卡片只剩 `D1` 这种编号。"""
        golden = PROJECT_ROOT / "data" / "golden" / "demo_qa.jsonl"
        if not golden.exists():
            pytest.skip("缺少演示金标集")
        report = tmp_path / "r.html"
        runner.invoke(app, ["eval", "--doc", str(demo_docs), "--golden", str(golden),
                            "--report", str(report), "--no-adversarial"])
        html = report.read_text(encoding="utf-8")
        cards = html.split("<h2>逐题明细</h2>", 1)[1].split("<h2>原始结果</h2>", 1)[0]
        assert "额定载荷要求是多少" in cards

    def test_serve_module_imports(self):
        """`serve` 命令本身依赖 uvicorn + create_app，导入链不能断。"""
        from docqa.api.app import create_app as _  # noqa: F401
        import uvicorn  # noqa: F401

    def test_eval_writes_artifacts_before_printing_summary(self, demo_docs, tmp_path):
        """产物写入必须在控制台摘要**之前**。

        顺序反了的话，摘要代码里任何一个 bug 都会让人丢掉结果——
        这个顺序问题真实发生过一次（摘要里一个未定义变量，
        导致报告与 JSON 全都没被创建）。
        """
        import inspect

        from docqa import cli

        src = inspect.getsource(cli.eval_cmd)
        assert src.index("report.write_text") < src.index("# --- 控制台摘要 ---"), \
            "产物写入必须排在控制台摘要之前"
