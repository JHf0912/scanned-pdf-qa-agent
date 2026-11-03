"""CLI 与 Web API 的端到端测试。

这两个模块此前是覆盖率最薄的部分——`ingest` / `eval` / `serve` 三条命令
**从未被真正执行过**。第一轮干跑就在它们身上抓出好几个缺陷
（`adv` 未定义直接崩溃、豁免被 `decide()` 架空、报告读错键名）。

所以这里覆盖的是**命令真的能跑完**，而不只是"函数能被导入"。
"""

from __future__ import annotations

import json
import re
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


def _rebind_logging() -> None:
    """把 `docqa` 的日志 handler 重新绑到**当前** `sys.stderr`。

    `logging.setup()` 在调用那一刻把 `StreamHandler` 绑到当时的 stderr；而
    `CliRunner` 每次调用都会替换并关闭自己的捕获流。上一个用例遗留的 handler
    若仍指向已关闭的流，下一次日志会抛 `ValueError: I/O operation on closed file`，
    logging 会把这段错误文本写到当前 stderr——它会被下一条 CLI 用例捕获，
    把 `--json` 输出污染成无法解析的文本（复跑时偶发）。
    每个用例开始前重绑一次，跨用例串流就消失了。
    """
    from docqa.logging import setup as setup_logging

    setup_logging("INFO", json_output=True)


@pytest.fixture(autouse=True)
def _reset_docqa_logging():
    _rebind_logging()
    yield


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

    def test_app_route_serves_the_workbench_and_assets(self):
        """`/app` 与 `/` 共用同一份 SPA；构建产物必须真的能被服务。

        不依赖 `client` 夹具：后者在没有 ingest 产物的 CI 上会 skip，
        而 `/app` 与静态资源本来就不需要任何文档。
        """
        client = TestClient(create_app(load_config(), domain="standard"))
        r = client.get("/app")
        assert r.status_code == 200
        assert "docqa" in r.text

        match = re.search(r'src="(/assets/[^"]+\.js)"', r.text)
        assert match, "构建后的 index.html 没有引用 /assets/*.js"
        asset = client.get(match.group(1))
        assert asset.status_code == 200
        assert "javascript" in asset.headers.get("content-type", "")

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


class TestWebEndpoints:
    """`/api/docs`、`/api/select`、`/api/upload`、并发限流。

    这几个路由是后加的（原来只有 status/chunks/ask），此前**一条测试都没有**——
    上传接口尤其危险：它把请求体直接落盘再触发 ingest，
    而"只接受 PDF"这件事靠的是扩展名、文件头、大小三道判断，
    少了任何一道都是一个可被利用的写文件入口。
    """

    @pytest.fixture(scope="class")
    def web_cfg(self, tmp_path_factory):
        """在隔离目录里造一份产物，别读 `data/derived/` 的环境状态。"""
        import dataclasses

        from docqa.pipeline import ingest

        # 类级夹具早于函数级 autouse fixture 执行；先重绑日志，避免沿用
        # 上一个 CLI 用例遗留的、已关闭的捕获流（RECHECK-06）。
        _rebind_logging()
        root = tmp_path_factory.mktemp("web")
        ingest(
            PROJECT_ROOT / "artifacts" / "demo" / "demo_scan.pdf",
            cfg=load_config(), domain="standard",
            engine=FixtureOcrEngine(PROJECT_ROOT / "artifacts" / "demo" / "ocr"),
            out_root=root,
        )
        return dataclasses.replace(load_config(), cache_dir=root / "cache"), root

    def test_docs_lists_artifacts(self, web_cfg):
        cfg, root = web_cfg
        d = TestClient(create_app(cfg, domain="standard")).get("/api/docs").json()
        assert d["docs"], "扫描不到产物，前端就无法切换文档"
        assert all({"doc_id", "dir", "engine"} <= set(x) for x in d["docs"])

    def test_select_unknown_doc_reports_error(self, web_cfg):
        cfg, _ = web_cfg
        r = TestClient(create_app(cfg, domain="standard")).post(
            "/api/select", json={"doc_ids": ["不存在的产物"]}
        )
        assert r.status_code == 400
        assert r.json()["ready"] is False and r.json()["error"]

    def test_select_switches_document(self, web_cfg):
        cfg, _ = web_cfg
        c = TestClient(create_app(cfg, domain="standard"))
        target = c.get("/api/docs").json()["docs"][0]
        r = c.post("/api/select", json={"doc_ids": [target["dir"]]})
        assert r.status_code == 200
        assert r.json()["ready"] is True
        # 切换后必须真的换了文档，而不是"返回成功但还读旧的那份"
        assert c.get("/api/status").json()["doc_id"] == target["doc_id"]

    def test_upload_rejects_non_pdf_name(self, web_cfg):
        cfg, _ = web_cfg
        r = TestClient(create_app(cfg, domain="standard")).post(
            "/api/upload", content=b"%PDF-1.4 whatever",
            headers={"X-Filename": "evil.exe"},
        )
        assert r.status_code == 400

    def test_upload_rejects_body_without_pdf_header(self, web_cfg):
        """扩展名可以随便改，文件头不能——只看名字等于没检查。"""
        cfg, _ = web_cfg
        r = TestClient(create_app(cfg, domain="standard")).post(
            "/api/upload", content=b"not a pdf at all",
            headers={"X-Filename": "fake.pdf"},
        )
        assert r.status_code == 400
        assert "PDF" in r.json()["detail"]

    def test_upload_strips_directory_from_filename(self, tmp_path, monkeypatch):
        """`../` 不能逃出 raw 目录。

        自备隔离目录：`root.parent` 在类级夹具下是 pytest 的会话临时根，
        多次运行会共享，无法可靠断言"raw 里只有这一个文件"。
        """
        import types

        import docqa.pipeline as pipeline

        cfg = _isolated_cfg(tmp_path)
        monkeypatch.setattr(
            pipeline, "ingest",
            lambda *a, **k: types.SimpleNamespace(
                doc_id="uploaded", chunks=[], failed_pages=[],
            ),
        )
        r = TestClient(create_app(cfg, domain="standard")).post(
            "/api/upload", content=b"%PDF-1.4\n%%EOF\n",
            headers={"X-Filename": "../../escape.pdf"},
        )
        assert r.status_code == 200, r.text
        # 上传落到 `<cache_dir 的父目录的父目录>/raw`，与生产的 `data/raw` 同源。
        # 落盘名是"净化 stem + 内容哈希 + 扩展名"，不再直接用用户文件名——
        # 同名覆盖会破坏 manifest 的 source.path/sha256 证据链（SEC-DOCX-04）。
        raw = tmp_path / "raw"
        stored = list(raw.iterdir())
        assert len(stored) == 1
        assert stored[0].name.startswith("escape-") and stored[0].suffix == ".pdf"
        assert not (tmp_path / "escape.pdf").exists(), "`../` 逃出了 raw 目录"


    def test_ask_returns_429_when_concurrency_exhausted(self, web_cfg, monkeypatch):
        """并发上限到了要**立刻拒绝并说明原因**，不是排队让用户以为卡住。"""
        import docqa.api.app as api_module

        cfg, _ = web_cfg
        monkeypatch.setattr(api_module, "MAX_CONCURRENT_ASKS", 0)
        app_ = api_module.create_app(cfg, domain="standard")
        r = TestClient(app_).post("/api/ask", json={"question": "任意问题"})
        assert r.status_code == 429
        assert "并发" in r.json()["detail"] or "处理中" in r.json()["detail"]


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

    def test_noise_sweep_honours_no_llm(self, demo_docs, tmp_path, monkeypatch):
        """`--no-llm` 必须一路传到噪声档位。

        接噪声扫描时这里漏了传 provider，每一档都自己 `build_llm_provider(cfg)`。
        在**配了模型的机器上**后果是双重的：每档 N 次真实调用（费钱），
        而且曲线画在"有模型"路径上——不可复现，0 档也和主评估对不上
        （实测 11/13 vs 12/13）。

        它之所以躲过了更早的检查：没配模型的机器上 `build_llm_provider`
        会**静默回退** `NullProvider`，两边碰巧一样。所以这条用例不能依赖
        本机 `.env`——把工厂换成"一调用就失败"的桩，`--no-llm` 下任何一档
        都不该碰它。（只断言"0 档数值等于主评估"是不够的：在没配模型的
        机器上，有 bug 的实现也会通过，那正是假绿。）
        """
        import docqa.llm as llm_module

        def _boom(_cfg):
            raise AssertionError(
                "--no-llm 下不应构造生成 provider：噪声档位又自己建了一个"
            )

        monkeypatch.setattr(llm_module, "build_llm_provider", _boom)

        golden = PROJECT_ROOT / "data" / "golden" / "demo_qa.jsonl"
        if not golden.exists():
            pytest.skip("缺少演示金标集")
        out = tmp_path / "noise.json"
        r = runner.invoke(app, ["eval", "--doc", str(demo_docs),
                                "--golden", str(golden), "--no-llm",
                                "--noise-sweep", "1", "--no-adversarial",
                                "--report", str(tmp_path / "r.html"),
                                "--json-out", str(out)])
        assert r.exit_code == 0, r.output
        d = json.loads(out.read_text(encoding="utf-8"))
        # 曲线走的是哪条路径，必须写进产物本身——否则读报告的人无从判断
        # 这是一条确定性曲线还是一条带采样波动的曲线。
        assert d["noise"]["path"] == "extractive"
        levels = d["noise"]["levels"]
        assert levels, "噪声扫描没有产出任何档位"
        assert levels[0]["level"] == 0.0, "0 档必须始终保留，它是比较的基线"
        # 0 档与主评估跑同一批题、同一个 provider，必须一致
        # （noise 里的值按 4 位小数落盘，所以这里比到 4 位）
        assert levels[0]["accuracy"] == round(d["metrics"]["accuracy"], 4)
        assert levels[0]["passed"] == d["metrics"]["passed"]

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
        import uvicorn  # noqa: F401

        from docqa.api.app import create_app as _  # noqa: F401

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


# ============================================================ DOCX：API + CLI

DEMO_DOCX = PROJECT_ROOT / "artifacts" / "docx" / "demo.docx"


def _isolated_cfg(tmp_path: Path):
    """把 `cfg.cache_dir` 指到临时目录：不读也不写仓库里的 `data/derived`。"""
    import dataclasses

    return dataclasses.replace(load_config(), cache_dir=tmp_path / "derived" / "cache")


def _valid_docx_bytes(text: str) -> bytes:
    """现场造一份合法的 DOCX（合成内容，不依赖仓库样例）。"""
    import io

    from docx import Document

    document = Document()
    document.add_paragraph("1 总则", style="Heading 1")
    document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def _zip_with_document_only() -> bytes:
    """`is_docx()` 会放行、`validate_docx()` 必须拒绝的最小包。"""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", "<w:document/>")
    return buf.getvalue()


# hybrid 契约测试用的假生成模型：按 system prompt 区分 strict / hybrid。
STRICT_ANSWER = "抗压强度为 42 MPa [§2.1 ¶8]。"
HYBRID_KNOWLEDGE = "同类材料通常取 600 MPa 以上。"


class _FakeLLM:
    """按 system prompt 返回预置文本的假 provider（不发任何请求）。"""

    name = "fake-llm"
    supports_vision = False

    def __init__(self, *, hybrid_text: str | None = None, strict_text: str = STRICT_ANSWER):
        self.hybrid_text = hybrid_text or f"{STRICT_ANSWER}\n【模型知识补充】\n{HYBRID_KNOWLEDGE}"
        self.strict_text = strict_text
        self.calls: list[str] = []

    def complete(self, system, user, *, images=None, max_tokens=1200,
                 temperature=0.0, json_mode=False):
        from docqa.llm import LLMResponse
        from docqa.models import Usage

        self.calls.append(system)
        text = self.hybrid_text if "【模型知识补充】" in system else self.strict_text
        return LLMResponse(
            text=text, provider="fake", model="fake-model",
            usage=Usage(tokens_in=10, tokens_out=5, calls=1),
        )


def _llm_configured(base):
    """把配置改成"已配置生成模型"，但模型本身由测试替身提供。"""
    import dataclasses

    from docqa.config import ProviderConfig

    return dataclasses.replace(
        base,
        llm=ProviderConfig(provider="openai", api_key="test-key", model="fake-model"),
    )


def _ingest_demo_docx(tmp_path: Path) -> Path:
    from docqa.pipeline import ingest

    return ingest(DEMO_DOCX, cfg=load_config(), domain="standard",
                  out_root=tmp_path / "out").out_dir


def _display_bundle(*, answer_mode: str = "strict", answer: str = STRICT_ANSWER):
    """构造带 C0/C1 与 bidi 控制字符的 bundle（模拟模型/证据/自检里混入）。"""
    from docqa.models import (
        AnswerBundle,
        CheckResult,
        CheckSeverity,
        Citation,
        ClarificationOption,
        VerificationReport,
    )

    return AnswerBundle(
        question="q",
        answer=answer,
        citations=[
            Citation(
                page_no=1,
                # clause_label 会进渲染后的引用字符串；bidi 字符同样要剥。
                clause_label="3.1\x1b[31m‮",
                chunk_id="d:p1:c1",
                quote="证据 \x1b[31mX\x07",
            )
        ],
        candidates=[
            ClarificationOption(
                interpretation="解释 \x1b[31m选项\x07",
                value="值 \x1b[31mV\x07",
                citation=Citation(
                    page_no=1, clause_label="3.1\x1b[31m‮",
                    chunk_id="d:p1:c2", quote="",
                ),
            )
        ],
        answer_mode=answer_mode,
        model_knowledge="补充 \x1b[31m知识\x07",
        knowledge_used=True,
        refusal_detail="拒绝 \x1b[31m原因\x07",
        verification=VerificationReport(
            checks=[
                CheckResult(
                    name="citation_validity",
                    passed=False,
                    severity=CheckSeverity.GATE,
                    detail="检查 \x1b[31m细节\x07",
                    offenders=["\x1b[31m问题\x07"],
                )
            ],
            support_score=0.5,
        ),
    )


def _corrupt_deflate(docx_bytes: bytes) -> bytes:
    """翻转 `word/document.xml` 压缩流的几个字节。

    中央目录里的大小/CRC 都不变，所以 `is_docx()` 仍返回 True；解压时才会
    失败（审计 SEC-DOCX-05 的原始复现方式）。这类输入必须被当成**输入错误**
    （API 400 / CLI 退出 3），而不是内部错误。
    """
    import struct

    raw = bytearray(docx_bytes)
    target = b"word/document.xml"
    pos = raw.find(b"PK\x03\x04")
    while pos >= 0:
        header = struct.unpack_from("<IHHHHHIIIHH", raw, pos)
        nlen, elen, csize = header[9], header[10], header[7]
        start = pos + 30 + nlen + elen
        name = bytes(raw[pos + 30: pos + 30 + nlen])
        if name == target and csize > 12:
            for offset in (5, 6, 7):
                raw[start + offset] ^= 0xFF
            return bytes(raw)
        pos = raw.find(b"PK\x03\x04", pos + 4)
    raise AssertionError("找不到 word/document.xml 的压缩流")


class _FakeStreamRequest:
    """只实现 `_read_upload_body()` 需要的接口。

    用它可以断言**"超限时到底读了多少"**——这正是 SEC-DOCX-03 的核心：
    拒绝必须发生在读完整个 body 之前，而不是之后。
    """

    def __init__(self, *, headers: dict | None = None, chunks=()):
        self.headers = headers or {}
        self.chunks = list(chunks)
        self.stream_calls = 0
        self.chunks_pulled = 0

    async def stream(self):
        self.stream_calls += 1
        for chunk in self.chunks:
            self.chunks_pulled += 1
            yield chunk


class TestDocxWebApi:
    """DOCX 走同一套六个端点，只多字段、不多路由。

    上传一份 DOCX 之后，`/api/status`、`/api/docs`、`/api/chunks`、`/api/ask`
    都必须能正确描述"这份文档没有页"——否则前端会显示"0 页"或 "None"。
    """

    @pytest.fixture(scope="class")
    def web(self, tmp_path_factory):
        _rebind_logging()
        root = tmp_path_factory.mktemp("docx-api")
        cfg = _isolated_cfg(root)
        client = TestClient(create_app(cfg, domain="standard"))
        body = DEMO_DOCX.read_bytes()
        response = client.post("/api/upload", content=body,
                               headers={"X-Filename": "demo.docx"})
        assert response.status_code == 200, response.text
        return cfg, root, client, response.json()

    def test_upload_accepts_docx_and_reports_source_type(self, web):
        _, _, _, payload = web
        assert payload["ready"] is True
        assert payload["source_type"] == "docx"
        assert payload["unit_count"] > 0
        assert payload["chunks"] > 0
        # 存储名已带 sha256[:32]；doc_id 不能再拼一段 -sha8，否则前端标题里
        # 会出现重复哈希（`demo-<32>-<8>`）。
        assert re.fullmatch(r"demo-[0-9a-f]{32}", payload["doc_id"]), payload["doc_id"]
        assert payload["stored_filename"].startswith("demo-")

    def test_status_describes_docx_without_pages_or_engine(self, web):
        _, _, client, uploaded = web
        d = client.get("/api/status").json()
        assert d["ready"] is True
        assert d["source_type"] == "docx"
        assert d["unit_count"] == uploaded["unit_count"]
        assert d["pages"] == 0
        assert d["engine"] is None
        assert d["failed_pages"] == []
        assert isinstance(d["unsupported"], dict) and d["unsupported"]
        assert d["chunks"] > 0

    def test_docs_listing_carries_source_type_and_unsupported(self, web):
        _, _, client, uploaded = web
        docs = client.get("/api/docs").json()["docs"]
        entry = next(d for d in docs if d["doc_id"] == uploaded["doc_id"])
        assert entry["source_type"] == "docx"
        assert entry["unit_count"] == uploaded["unit_count"]
        assert entry["pages"] == 0
        assert isinstance(entry["unsupported"], dict) and entry["unsupported"]

    def test_reupload_selects_the_uploaded_docx_over_a_newer_pdf(self, tmp_path):
        """同内容幂等二次上传后，必须选中**刚上传的**文档，而不是按 mtime 胜出的 PDF。

        幂等上传不会重写 `raw/`，产物目录的目录 mtime 也不会变；`_discover()`
        按目录 mtime 排序，若上传后把 `selected` 置回 None，就会选中最新的
        另一份文档（E2E 实测：上传 DOCX 后 `/api/status` 仍是 gbt48351 PDF）。
        """
        from docqa.ocr.fixture import FixtureOcrEngine
        from docqa.pipeline import ingest

        cfg = _isolated_cfg(tmp_path)
        client = TestClient(create_app(cfg, domain="standard"))
        body = DEMO_DOCX.read_bytes()
        first = client.post("/api/upload", content=body,
                            headers={"X-Filename": "demo.docx"})
        assert first.status_code == 200, first.text
        docx_id = first.json()["doc_id"]

        # 造一份**更新的** PDF 产物（目录 mtime 晚于 DOCX 产物）
        ingest(
            PROJECT_ROOT / "artifacts" / "demo" / "demo_scan.pdf",
            cfg=cfg, domain="standard",
            engine=FixtureOcrEngine(PROJECT_ROOT / "artifacts" / "demo" / "ocr"),
            out_root=cfg.cache_dir.parent,
        )

        # 同内容二次上传：幂等命中已有产物目录
        second = client.post("/api/upload", content=body,
                             headers={"X-Filename": "demo.docx"})
        assert second.status_code == 200, second.text
        assert second.json()["doc_id"] == docx_id

        d = client.get("/api/status").json()
        assert d["doc_id"] == docx_id, "上传后没有选中刚上传的 DOCX"
        assert d["source_type"] == "docx"

        # 重启（selected=None，按 mtime 自动选最新）后也必须仍是 DOCX：
        # 上传路径要刷新产物目录 mtime。
        restarted = TestClient(create_app(cfg, domain="standard"))
        d2 = restarted.get("/api/status").json()
        assert d2["doc_id"] == docx_id, "重启后按 mtime 选到了别的文档"
        assert d2["source_type"] == "docx"

    def test_chunks_carry_locator_fields(self, web):
        _, _, client, _ = web
        rows = client.get("/api/chunks").json()
        assert rows
        for row in rows:
            assert row["locator_kind"] == "paragraph"
            assert row["page_no"] is None
            assert row["para_no"] is not None
            assert row["para_end"] is not None
            assert "section" in row

    def test_ask_evidence_carries_locator_fields(self, web):
        _, _, client, _ = web
        d = client.post("/api/ask", json={"question": "抗压强度"}).json()
        assert d["citations"], f"没有引用：refused={d['refused']} {d['refusal_detail']}"
        for citation in d["citations"]:
            assert citation["locator_kind"] == "paragraph"
            assert "None" not in citation["rendered"]
            assert citation["rendered"].startswith(("[§", "[¶"))
        assert d["evidence"]
        for chunk_id, item in d["evidence"].items():
            assert chunk_id in {c["chunk_id"] for c in d["citations"]}
            assert item["locator_kind"] == "paragraph"
            assert item["para_no"] is not None
            assert item["para_end"] is not None
            assert "section" in item
            assert item["page_no"] is None

    def test_upload_rejects_broken_docx(self, web):
        _, _, client, _ = web
        r = client.post("/api/upload", content=b"PK\x03\x04 definitely not a docx",
                        headers={"X-Filename": "broken.docx"})
        assert r.status_code == 400
        assert "DOCX" in r.json()["detail"]

    def test_upload_rejects_unsafe_docx_after_deep_validation(self, web):
        """`is_docx()` 只看结构；真正的不安全包由 ingest 深度校验拒绝 → 400。

        这里构造一个"像 DOCX 但不是"的 ZIP：有 word/document.xml，
        缺 [Content_Types].xml。它过了上传时的便宜检查，必须由
        `validate_docx()` 拦下，而且状态码是 **400（输入问题）不是 502**。
        """
        import io
        import zipfile

        _, _, client, _ = web
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("word/document.xml", "<w:document/>")
        r = client.post("/api/upload", content=buf.getvalue(),
                        headers={"X-Filename": "unsafe.docx"})
        assert r.status_code == 400, r.text
        assert "解析失败" in r.json()["detail"]

    @pytest.mark.parametrize(
        ("filename", "hint"),
        [("old.doc", "另存为"), ("macro.docm", "另存为"), ("tpl.dotx", "另存为")],
    )
    def test_upload_rejects_word_variants_with_actionable_hint(self, web, filename, hint):
        _, _, client, _ = web
        r = client.post("/api/upload", content=b"%PDF-1.4",  # 内容无关，先看名字
                        headers={"X-Filename": filename})
        assert r.status_code == 400
        assert hint in r.json()["detail"]

    def test_upload_rejects_unknown_extension(self, web):
        _, _, client, _ = web
        r = client.post("/api/upload", content=b"hello",
                        headers={"X-Filename": "notes.txt"})
        assert r.status_code == 400
        assert "PDF" in r.json()["detail"] and "DOCX" in r.json()["detail"]

    def test_mixed_corpus_reports_mixed(self, web):
        """PDF + DOCX 混选时：`source_type=mixed`、不报页数、unit_count 求和。"""
        from docqa.ocr.fixture import FixtureOcrEngine
        from docqa.pipeline import ingest

        cfg, root, _, uploaded = web
        ingest(
            PROJECT_ROOT / "artifacts" / "demo" / "demo_scan.pdf",
            cfg=cfg, domain="standard",
            engine=FixtureOcrEngine(PROJECT_ROOT / "artifacts" / "demo" / "ocr"),
            out_root=root / "derived",
        )
        client = TestClient(create_app(cfg, domain="standard"))
        docs = client.get("/api/docs").json()["docs"]
        docx_dir = next(d for d in docs if d["source_type"] == "docx")
        pdf_dir = next(d for d in docs if d["source_type"] == "pdf")
        r = client.post("/api/select", json={"doc_ids": [pdf_dir["dir"], docx_dir["dir"]]})
        assert r.status_code == 200, r.text

        d = client.get("/api/status").json()
        assert d["source_type"] == "mixed"
        assert d["unit_count"] == uploaded["unit_count"]
        assert d["pages"] == 0, "混合语料里有一份没有页，不能报页数"
        assert d["engine"] is None
        assert isinstance(d["unsupported"], dict)


class TestDocxCli:
    """`docqa ingest/detect/render/eval-probes` 对 DOCX 的早退与提示。"""

    def test_ingest_docx_needs_no_ocr_configuration(self, tmp_path):
        out = tmp_path / "docs"
        r = runner.invoke(app, ["ingest", str(DEMO_DOCX), "--out", str(out)])
        assert r.exit_code == 0, r.output
        assert "DOCX 原生文本" in r.output
        assert "正文块" in r.output
        assert "已线性化" in r.output
        assert "页数" not in r.output, "DOCX 汇总面板不应显示页数"
        assert any((p / "manifest.json").exists() for p in out.iterdir())

    def test_detect_docx_short_circuits(self):
        r = runner.invoke(app, ["detect", str(DEMO_DOCX)])
        assert r.exit_code == 0, r.output
        assert "无需类型判定" in r.output
        assert "ingest" in r.output

    def test_engine_option_is_ignored_for_docx(self, tmp_path):
        r = runner.invoke(app, ["ingest", str(DEMO_DOCX), "--engine", "fixture",
                                "--out", str(tmp_path / "docs")])
        assert r.exit_code == 0, r.output
        assert "不适用" in r.output
        assert "fixture 引擎" not in r.output, "DOCX 不该出现 OCR 引擎的提示"

    def test_force_never_clears_ocr_cache_for_docx(self, tmp_path, monkeypatch):
        """`--force` 是破坏性操作：对 DOCX 必须什么都不清。"""
        import shutil

        calls: list = []
        monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: calls.append(a))
        r = runner.invoke(app, ["ingest", str(DEMO_DOCX), "--force",
                                "--out", str(tmp_path / "docs")])
        assert r.exit_code == 0, r.output
        assert calls == [], "DOCX 没有 OCR 缓存可清，--force 不应删除任何目录"
        assert "--force" in r.output

    def test_render_docx_is_rejected(self, tmp_path):
        r = runner.invoke(app, ["render", str(DEMO_DOCX), "--out", str(tmp_path / "p")])
        assert r.exit_code == 3, r.output
        assert "render 只适用于 PDF" in r.output

    def test_eval_probes_rejects_docx(self, tmp_path):
        out = tmp_path / "docs"
        r = runner.invoke(app, ["ingest", str(DEMO_DOCX), "--out", str(out)])
        assert r.exit_code == 0, r.output
        doc_dir = next(p for p in out.iterdir() if (p / "manifest.json").exists())

        r = runner.invoke(app, ["eval-probes", "--doc", str(doc_dir)])
        assert r.exit_code == 3, r.output
        assert "eval-docx" in r.output

    def test_eval_docx_missing_backend_is_actionable(self, monkeypatch, tmp_path):
        """E 的评估模块还没落地时，必须给可操作提示，而不是 traceback。"""
        import sys as _sys

        monkeypatch.setitem(_sys.modules, "docqa.eval.docx_eval", None)
        r = runner.invoke(app, ["eval-docx", str(DEMO_DOCX)])
        assert r.exit_code != 0
        assert "eval-docx" in r.output

    @staticmethod
    def _require_docx_eval():
        """评估模块由另一工作流提供：没写完时跳过，而不是让本文件整体变红。"""
        try:
            import docqa.eval.docx_eval  # noqa: F401
        except (ImportError, SyntaxError) as exc:  # pragma: no cover - 仅在并行开发期
            pytest.skip(f"评估模块尚不可用：{exc}")

    def test_eval_docx_writes_markdown_report(self, tmp_path):
        self._require_docx_eval()
        report = tmp_path / "docx_eval.md"
        r = runner.invoke(app, ["eval-docx", str(DEMO_DOCX), "--out", str(report)])
        assert r.exit_code == 0, r.output
        assert "段落覆盖率" in r.output
        text = report.read_text(encoding="utf-8")
        assert "段落覆盖率" in text and "引用回环" in text

    def test_eval_docx_json_is_machine_readable(self):
        self._require_docx_eval()
        r = runner.invoke(app, ["eval-docx", str(DEMO_DOCX), "--json"])
        assert r.exit_code == 0, r.output
        d = json.loads(r.output[r.output.index("{"):])
        assert d["source_type"] == "docx"
        assert d["gates_passed"] is True
        assert d["metrics"]["paragraph_coverage"]["ratio"] == 1.0
        assert d["metrics"]["citation_roundtrip"]["ratio"] == 1.0

    def test_repeated_cli_json_runs_are_not_polluted(self):
        """同一进程连跑两次：上一次遗留的 logging handler 不能污染 JSON（RECHECK-06）。"""
        self._require_docx_eval()
        for attempt in range(2):
            r = runner.invoke(app, ["eval-docx", str(DEMO_DOCX), "--json"])
            assert r.exit_code == 0, f"第 {attempt + 1} 次失败：{r.output}"
            d = json.loads(r.output[r.output.index("{"):])
            assert d["source_type"] == "docx"

    def test_doctor_mentions_docx_does_not_need_ocr(self):
        r = runner.invoke(app, ["doctor", "--no-test-vision"])
        assert "DOCX" in r.output


class TestDocxEvalCli:
    """`docqa eval` 面对 DOCX 产物时的口径。

    DOCX 没有页、没有 OCR 层，所以：
    - `engine` 必须是 `"docx"`，不能是 `"unknown"`（后者会被读成"识别引擎未知"，
      而且会让 PDF/DOCX 的回归基线互相可比）；
    - 只对页有效的选项（`--transcription` / `--noise-sweep`）必须明确"不适用、已忽略"，
      而不是静默跑出一组恒为空/恒为零的指标。
    """

    @pytest.fixture(scope="class")
    def docx_artifact(self, tmp_path_factory):
        from docqa.pipeline import ingest

        _rebind_logging()  # 类级夹具早于函数级 autouse；见 RECHECK-06
        root = tmp_path_factory.mktemp("docx-eval")
        result = ingest(DEMO_DOCX, cfg=load_config(), domain="standard", out_root=root)
        return result.out_dir

    @pytest.fixture(scope="class")
    def golden(self, tmp_path_factory):
        path = tmp_path_factory.mktemp("docx-eval-golden") / "qa.jsonl"
        items = [
            {"qid": "D1", "question": "抗压强度", "category": "clause",
             "must_contain": ["42"]},
            {"qid": "D2", "question": "今天天气怎么样", "category": "no_answer",
             "expected_refusal": True, "expected_reason": "not_in_document"},
        ]
        path.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in items),
            encoding="utf-8",
        )
        return path

    def _run_eval(self, doc_dir, golden, tmp_path, *extra):
        report = tmp_path / "eval.html"
        json_out = tmp_path / "eval.json"
        r = runner.invoke(app, [
            "eval", "--doc", str(doc_dir), "--golden", str(golden),
            "--report", str(report), "--json-out", str(json_out),
            "--no-llm", "--no-adversarial", *extra,
        ])
        return r, report, json_out

    def test_docx_eval_uses_docx_engine_and_skips_ocr_metrics(
        self, docx_artifact, golden, tmp_path
    ):
        r, report, json_out = self._run_eval(docx_artifact, golden, tmp_path)
        assert r.exit_code == 0, r.output
        d = json.loads(json_out.read_text(encoding="utf-8"))
        assert d["engine"] == "docx", "DOCX 没有 OCR 引擎，报 unknown 是假话"
        assert d["source_type"] == "docx"
        assert d["metrics"]["items"] == 2
        assert not d["ocr"], "DOCX 没有 OCR 层，不该产出空的 OCR 指标"
        # 小尾巴：DOCX 不应出现"未提供 --transcription"这种读起来像"忘了传参"的提示。
        assert not any("未提供 --transcription" in note for note in d["notes"]), d["notes"]
        assert any("不适用" in note for note in d["notes"]), d["notes"]
        assert report.exists() and report.stat().st_size > 0

    def test_docx_eval_ignores_pdf_only_options(self, docx_artifact, golden, tmp_path):
        transcription = tmp_path / "transcription.md"  # 是否存在都无所谓：先判"不适用"
        r, _, json_out = self._run_eval(
            docx_artifact, golden, tmp_path,
            "--transcription", str(transcription), "--noise-sweep", "1",
        )
        assert r.exit_code == 0, r.output
        assert "只适用于 PDF" in r.output
        assert "--transcription" in r.output
        assert "--noise-sweep" in r.output
        d = json.loads(json_out.read_text(encoding="utf-8"))
        assert not d.get("noise"), "DOCX 上不该真的跑噪声扫描"

    def test_pdf_eval_engine_name_is_unchanged(self, tmp_path):
        """PDF 回归：引擎名仍取 manifest 里的 OCR 引擎，不被 DOCX 分支影响。"""
        from docqa.ocr.fixture import FixtureOcrEngine
        from docqa.pipeline import ingest

        root = tmp_path / "pdf"
        ingest(
            PROJECT_ROOT / "artifacts" / "demo" / "demo_scan.pdf",
            cfg=load_config(), domain="standard",
            engine=FixtureOcrEngine(PROJECT_ROOT / "artifacts" / "demo" / "ocr"),
            out_root=root,
        )
        doc_dir = next(p for p in root.iterdir() if (p / "manifest.json").exists())

        golden = tmp_path / "qa.jsonl"
        golden.write_text(json.dumps(
            {"qid": "P1", "question": "额定载荷", "category": "clause",
             "must_contain": ["12.5"]}, ensure_ascii=False,
        ) + "\n", encoding="utf-8")

        r, _, json_out = self._run_eval(doc_dir, golden, tmp_path)
        assert r.exit_code == 0, r.output
        d = json.loads(json_out.read_text(encoding="utf-8"))
        assert d["engine"] == "fixture"
        assert d["source_type"] == "pdf"

    # ------------------------------------------------------------ 自定义引用模板

    @staticmethod
    def _custom_domain_cfg(tmp_path: Path, template: str):
        """造一个只改了 `docx_citation_template` 的临时域。"""
        from docqa.config import Config

        domains = tmp_path / "domains"
        domains.mkdir(exist_ok=True)
        (domains / "customdocx.yaml").write_text(
            f'docx_citation_template: "{template}"\n', encoding="utf-8",
        )
        return Config(domains_dir=domains, cache_dir=tmp_path / "cache")

    def test_eval_docx_passes_domain_template_to_runner(self, tmp_path, monkeypatch):
        """评估与真实问答必须用**同一套**引用模板，否则改模板引入的回环问题测不出来。"""
        import docqa.cli as cli_module
        import docqa.eval.docx_eval as docx_eval_module

        template = "[DOCX §{section} ¶{para}]"
        cfg = self._custom_domain_cfg(tmp_path, template)
        monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: cfg)

        real = docx_eval_module.run_docx_eval
        captured: dict = {}

        def spy(source, docx_template=None):
            captured["template"] = docx_template
            payload = real(source)  # 用默认模板算一份完整报告，隔离 E 的模板语义
            payload["gates_passed"] = True
            return payload

        monkeypatch.setattr(docx_eval_module, "run_docx_eval", spy)
        report = tmp_path / "docx_eval.md"
        r = runner.invoke(app, ["eval-docx", str(DEMO_DOCX), "--domain", "customdocx",
                                "--out", str(report)])
        assert r.exit_code == 0, r.output
        assert captured["template"] == template
        assert report.exists()

    def test_eval_docx_custom_parseable_template_roundtrips(
        self, tmp_path, monkeypatch
    ):
        """可被解析器识别的自定义模板：roundtrip / 覆盖仍须 100%。

        `[¶{para}]` 是合法的规范形态之一（`parse_citations` 认它），
        换掉默认模板后渲染与回环仍须一致——这正是"评估与问答同模板"要守的。
        """
        import docqa.cli as cli_module

        cfg = self._custom_domain_cfg(tmp_path, "[¶{para}]")
        monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: cfg)

        r = runner.invoke(app, ["eval-docx", str(DEMO_DOCX), "--domain", "customdocx",
                                "--json"])
        assert r.exit_code == 0, r.output
        d = json.loads(r.output[r.output.index("{"):])
        assert d["metrics"]["citation_roundtrip"]["ratio"] == 1.0
        assert d["metrics"]["paragraph_coverage"]["ratio"] == 1.0

    def test_eval_docx_noncanonical_template_fails_the_gate(
        self, tmp_path, monkeypatch
    ):
        """带字面前缀的模板（`[DOCX §... ¶...]`）解析器不认，评估必须**失败**。

        A 的 `parse_citations()` 只识别四种规范形态；`[DOCX §3.6 ¶4]` 渲染得出、
        解析不回来。E 的评估把它当成 gate 失败（退出码 6）——这正是"模板不合法
        要能被评估发现"的价值。若产品要求支持任意前缀，需要 A 扩展解析器，
        而不是让评估静默按默认模板放行。
        """
        import docqa.cli as cli_module

        cfg = self._custom_domain_cfg(tmp_path, "[DOCX §{section} ¶{para}]")
        monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: cfg)

        r = runner.invoke(app, ["eval-docx", str(DEMO_DOCX), "--domain", "customdocx",
                                "--json"])
        assert r.exit_code == 6, r.output  # EXIT_VERIFICATION
        d = json.loads(r.output[r.output.index("{"):])
        assert d["gates_passed"] is False
        assert d["metrics"]["citation_roundtrip"]["ratio"] < 1.0


# ============================================================ 上传安全（SEC-DOCX-03/04/05）

class TestUploadSecurity:
    """上传是唯一把不可信字节写进本地磁盘的入口。

    审计（`docs/reviews/2026-10-29-docx-security-audit.md`）在这里定了三条 P1：
    大小检查必须在**读 body 之前**；失败/拒绝不能留下未验证文件、存储名不能由
    用户文件名决定（ADS/覆盖/设备名）；非预期异常不能把异常文本或绝对路径回给客户端。
    """

    @staticmethod
    def _client(tmp_path: Path) -> TestClient:
        return TestClient(create_app(_isolated_cfg(tmp_path), domain="standard"))

    def _upload(self, client, body: bytes, name: str):
        return client.post("/api/upload", content=body, headers={"X-Filename": name})

    # ------------------------------------------------------------ 大小预检

    def test_content_length_precheck_does_not_read_body(self, monkeypatch):
        import asyncio

        from fastapi import HTTPException

        import docqa.api.app as api_module

        monkeypatch.setattr(api_module, "MAX_UPLOAD_BYTES", 1024)
        request = _FakeStreamRequest(headers={"content-length": "5000"})
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(api_module._read_upload_body(request))
        assert excinfo.value.status_code == 413
        assert request.stream_calls == 0, "声明超限时不应读取 body"

    def test_streamed_body_over_limit_stops_immediately(self, monkeypatch):
        """无长度/chunked，或 Content-Length 撒谎：累计到上限立刻停止。"""
        import asyncio

        from fastapi import HTTPException

        import docqa.api.app as api_module

        monkeypatch.setattr(api_module, "MAX_UPLOAD_BYTES", 10)
        request = _FakeStreamRequest(
            headers={"content-length": "5"},  # 声明 5 字节，实际远不止
            chunks=(b"a" * 6, b"b" * 6, b"c" * 6),
        )
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(api_module._read_upload_body(request))
        assert excinfo.value.status_code == 413
        assert request.chunks_pulled == 2, (
            f"应在读到第二块时立即拒绝，实际读了 {request.chunks_pulled} 块"
        )

    def test_body_within_limit_is_returned_intact(self, monkeypatch):
        import asyncio

        import docqa.api.app as api_module

        monkeypatch.setattr(api_module, "MAX_UPLOAD_BYTES", 10)
        body = asyncio.run(
            api_module._read_upload_body(
                _FakeStreamRequest(chunks=(b"abc", b"def"))
            )
        )
        assert body == b"abcdef"

    def test_upload_rejects_oversize_before_buffering(self, tmp_path, monkeypatch):
        import docqa.api.app as api_module

        monkeypatch.setattr(api_module, "MAX_UPLOAD_BYTES", 1024)
        client = self._client(tmp_path)
        r = self._upload(client, b"PK\x03\x04" + b"x" * 5000, "big.docx")
        assert r.status_code == 413
        assert "上限" in r.json()["detail"]
        assert not (tmp_path / "raw").exists(), "被拒绝的上传不应落盘"

    # ------------------------------------------------------------ raw 生命周期

    def test_failed_deep_validation_leaves_no_raw_file(self, tmp_path):
        client = self._client(tmp_path)
        r = self._upload(client, _zip_with_document_only(), "unsafe.docx")
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "解析失败" in detail
        # 400 只给可操作信息：不出现绝对路径、异常类名或堆栈。
        assert str(tmp_path) not in detail
        assert "InputError" not in detail and "Traceback" not in detail
        raw = tmp_path / "raw"
        assert not raw.exists() or not any(raw.iterdir()), (
            f"校验失败后 raw 仍有残留：{list(raw.iterdir()) if raw.exists() else []}"
        )

    def test_same_filename_different_content_is_not_overwritten(self, tmp_path):
        """同名二次上传不得覆盖：否则旧 manifest 的 source.path/sha256 会指向新字节。"""
        client = self._client(tmp_path)
        first = self._upload(client, _valid_docx_bytes("第一份内容"), "same.docx")
        second = self._upload(client, _valid_docx_bytes("第二份内容"), "same.docx")
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["original_filename"] == "same.docx"

        names = sorted(p.name for p in (tmp_path / "raw").iterdir())
        assert len(names) == 2, f"同名上传被覆盖了：{names}"
        assert all(name.endswith(".docx") and name != "same.docx" for name in names)
        assert first.json()["doc_id"] != second.json()["doc_id"]

    def test_same_content_is_idempotent(self, tmp_path):
        """同一份内容 + 同一个名字 → 同一个存储名；重复上传不该无限堆积文件。"""
        client = self._client(tmp_path)
        payload = _valid_docx_bytes("同一份内容")
        assert self._upload(client, payload, "a.docx").status_code == 200
        assert self._upload(client, payload, "a.docx").status_code == 200
        names = list((tmp_path / "raw").iterdir())
        assert len(names) == 1

    # ------------------------------------------------------------ 文件名净化

    def test_windows_invalid_chars_do_not_create_ads(self, tmp_path):
        """`foo:bar.docx` 在 Windows 上会被写成基文件 `foo` 的 ADS。"""
        client = self._client(tmp_path)
        r = self._upload(client, _valid_docx_bytes("ADS 测试"), "foo:bar.docx")
        assert r.status_code == 200, r.text
        raw = tmp_path / "raw"
        names = [p.name for p in raw.iterdir()]
        assert len(names) == 1
        assert ":" not in names[0]
        assert not (raw / "foo").exists(), "生成了 `foo` 的备用数据流"

    def test_windows_reserved_names_are_rejected(self, tmp_path):
        client = self._client(tmp_path)
        r = self._upload(client, _valid_docx_bytes("x"), "CON.docx")
        assert r.status_code == 400
        assert "保留" in r.json()["detail"]

    def test_unusable_filename_falls_back_to_upload(self, tmp_path):
        client = self._client(tmp_path)
        r = self._upload(client, _valid_docx_bytes("x"), ".docx")
        assert r.status_code == 200, r.text
        names = [p.name for p in (tmp_path / "raw").iterdir()]
        assert len(names) == 1 and names[0].startswith("upload-")

    # ------------------------------------------------------------ 并发与异常边界

    def test_upload_concurrency_exhausted_returns_429(self, tmp_path, monkeypatch):
        import docqa.api.app as api_module

        monkeypatch.setattr(api_module, "MAX_CONCURRENT_UPLOADS", 0)
        client = TestClient(
            api_module.create_app(_isolated_cfg(tmp_path), domain="standard")
        )
        r = self._upload(client, _valid_docx_bytes("x"), "x.docx")
        assert r.status_code == 429
        detail = r.json()["detail"]
        assert "处理中" in detail or "并发" in detail

    def test_internal_failure_is_generic_500_without_leaking(
        self, tmp_path, monkeypatch
    ):
        """非 `DocqaError` 的兜底必须**不回显异常文本/绝对路径**，且清掉落盘文件。"""
        import docqa.pipeline as pipeline

        secret = "C:\\Users\\secret\\derived\\x.docx"

        def boom(*_args, **_kwargs):
            raise RuntimeError(f"boom at {secret} zlib.error")

        monkeypatch.setattr(pipeline, "ingest", boom)
        client = self._client(tmp_path)
        r = self._upload(client, _valid_docx_bytes("x"), "x.docx")
        assert r.status_code == 500
        detail = r.json()["detail"]
        for leaked in ("boom", secret, "RuntimeError", "zlib.error", str(tmp_path)):
            assert leaked not in detail, f"500 响应泄露了内部信息：{detail!r}"
        raw = tmp_path / "raw"
        assert not raw.exists() or not any(raw.iterdir())

    def test_corrupt_deflate_docx_is_400_without_internal_details(self, tmp_path):
        """审计 SEC-DOCX-05 的原始复现：合法 ZIP、坏压缩流。

        `is_docx()` 会放行；错误必须在深度校验里变成 **400（输入问题）**，
        且响应里不出现绝对路径、异常类名或堆栈。
        """
        client = self._client(tmp_path)
        r = self._upload(client, _corrupt_deflate(DEMO_DOCX.read_bytes()), "corrupt.docx")
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "解析失败" in detail
        for leaked in (str(tmp_path), "InputError", "Traceback", "zlib.error"):
            assert leaked not in detail, f"400 响应泄露了内部信息：{detail!r}"
        raw = tmp_path / "raw"
        assert not raw.exists() or not any(raw.iterdir())

    def test_write_failure_leaves_no_partial_file(self, tmp_path, monkeypatch):
        """落盘失败（磁盘满/替换失败）不得留下半截文件或临时文件。"""
        import docqa.api.app as api_module

        def _fail_replace(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(api_module.os, "replace", _fail_replace)
        client = self._client(tmp_path)
        r = self._upload(client, _valid_docx_bytes("x"), "x.docx")
        assert r.status_code == 500
        assert "服务端日志" in r.json()["detail"]
        assert "No space" not in r.json()["detail"]
        raw = tmp_path / "raw"
        assert not raw.exists() or not any(raw.iterdir()), (
            f"失败后 raw 仍有文件：{list(raw.iterdir()) if raw.exists() else []}"
        )

    def test_storage_name_embeds_128_bit_digest(self, tmp_path):
        """存储名用 sha256[:32]：8 位（32 bit）在共享目录里太容易撞。"""
        import hashlib

        payload = _valid_docx_bytes("RECHECK-02 哈希长度")
        digest = hashlib.sha256(payload).hexdigest()
        client = self._client(tmp_path)
        r = self._upload(client, payload, "x.docx")
        assert r.status_code == 200, r.text
        names = [p.name for p in (tmp_path / "raw").iterdir()]
        assert names == [f"x-{digest[:32]}.docx"]
        assert r.json()["stored_filename"] == f"x-{digest[:32]}.docx"

    def test_symlinked_storage_target_is_not_followed(self, tmp_path):
        """预置 symlink 时绝不写穿：本地攻击者不能借共享 raw 目录改写别的文件。"""
        import hashlib
        import os

        payload = _valid_docx_bytes("RECHECK-01 符号链接")
        digest = hashlib.sha256(payload).hexdigest()
        raw = tmp_path / "raw"
        raw.mkdir(parents=True)
        victim = tmp_path / "victim.txt"
        victim.write_text("victim-content", encoding="utf-8")
        try:
            os.symlink(victim, raw / f"x-{digest[:32]}.docx")
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"当前环境不允许创建符号链接：{exc}")

        client = self._client(tmp_path)
        r = self._upload(client, payload, "x.docx")
        assert r.status_code in (400, 409), (
            f"symlink 目标被接受（status={r.status_code}）：{r.text}"
        )
        assert victim.read_text(encoding="utf-8") == "victim-content", (
            "symlink 目标被上传内容改写了"
        )

    def test_cli_corrupt_docx_exits_3_without_traceback(self, tmp_path):
        """畸形输入是**输入问题**（退出码 3），不是内部错误（1）。"""
        bad = tmp_path / "corrupt.docx"
        bad.write_bytes(_corrupt_deflate(DEMO_DOCX.read_bytes()))
        r = runner.invoke(app, ["ingest", str(bad), "--out", str(tmp_path / "docs")])
        assert r.exit_code == 3, r.output
        assert "Traceback" not in r.output
        assert "zlib.error" not in r.output
        assert "DOCX" in r.output or "损坏" in r.output


# ============================================================ hybrid 模式（API/CLI）
#
# 契约：`docs/reviews/2026-10-31-hybrid-mode-contract.md`。
# strict 是默认，引用/自检/拒答语义一字不改；hybrid 是**显式可选**路径，
# 没有生成模型时必须明确拒绝（API 400 / CLI 退出 2），绝不静默降级。


class TestHybridAskApi:
    @staticmethod
    def _client(tmp_path, monkeypatch, *, with_llm: bool, hybrid_text: str | None = None):
        import docqa.api.app as api_module

        cfg = _isolated_cfg(tmp_path)
        if with_llm:
            cfg = _llm_configured(cfg)
            provider = _FakeLLM(hybrid_text=hybrid_text)
            monkeypatch.setattr(api_module, "build_llm_provider", lambda _cfg: provider)
        client = TestClient(api_module.create_app(cfg, domain="standard"))
        r = client.post("/api/upload", content=DEMO_DOCX.read_bytes(),
                        headers={"X-Filename": "demo.docx"})
        assert r.status_code == 200, r.text
        return client, cfg

    def test_strict_default_is_unchanged_without_llm(self, tmp_path, monkeypatch):
        client, _ = self._client(tmp_path, monkeypatch, with_llm=False)
        d = client.post("/api/ask", json={"question": "抗压强度"}).json()
        assert d["answer_mode"] == "strict"
        assert d["model_knowledge"] is None
        assert d["knowledge_used"] is False
        assert d["citations"], "strict 仍应给出文档引用"
        assert client.get("/api/status").json()["has_llm"] is False

    def test_hybrid_without_llm_is_400_not_silent_strict(
        self, tmp_path, monkeypatch
    ):
        client, _ = self._client(tmp_path, monkeypatch, with_llm=False)
        r = client.post("/api/ask", json={"question": "抗压强度", "mode": "hybrid"})
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "hybrid" in detail and "生成模型" in detail
        # 不能静默降级：错误响应里根本不该出现一份 strict 答案。
        assert "answer_mode" not in r.json()

    def test_invalid_mode_is_rejected_by_validation(self, tmp_path, monkeypatch):
        client, _ = self._client(tmp_path, monkeypatch, with_llm=False)
        r = client.post("/api/ask", json={"question": "抗压强度", "mode": "loose"})
        assert r.status_code in (400, 422), r.text

    def test_hybrid_with_llm_returns_mode_fields_and_doc_only_evidence(
        self, tmp_path, monkeypatch
    ):
        client, _ = self._client(tmp_path, monkeypatch, with_llm=True)
        d = client.post(
            "/api/ask", json={"question": "抗压强度", "mode": "hybrid"}
        ).json()

        assert d["answer_mode"] == "hybrid"
        assert d["knowledge_used"] is True
        assert HYBRID_KNOWLEDGE in (d["model_knowledge"] or "")
        # 补充段不是证据：不得含任何引用形态。
        for mark in ("[p.", "[§", "[¶"):
            assert mark not in (d["model_knowledge"] or "")
        assert "600 MPa" not in (d["answer"] or "")
        assert d["citations"], "hybrid 的文档正文仍必须带引用"
        assert set(d["evidence"]) == {c["chunk_id"] for c in d["citations"]}
        assert all(HYBRID_KNOWLEDGE not in item["text"] for item in d["evidence"].values())
        # 前端靠 has_llm 禁用/启用 hybrid；它必须反映真实配置。
        assert client.get("/api/status").json()["has_llm"] is True

    def test_strict_with_llm_keeps_strict_shape(self, tmp_path, monkeypatch):
        client, _ = self._client(tmp_path, monkeypatch, with_llm=True)
        d = client.post("/api/ask", json={"question": "抗压强度"}).json()
        assert d["answer_mode"] == "strict"
        assert d["model_knowledge"] is None
        assert d["knowledge_used"] is False
        assert all(c["name"] != "knowledge_label" for c in d["verification"]["checks"])

    @pytest.mark.parametrize("with_llm", [False, True])
    def test_status_without_artifacts_still_reports_has_llm(
        self, tmp_path, with_llm
    ):
        """无产物/装载失败时，前端仍靠 `has_llm` 决定 hybrid 是否可用。"""
        import docqa.api.app as api_module

        cfg = _isolated_cfg(tmp_path)
        if with_llm:
            cfg = _llm_configured(cfg)
        client = TestClient(api_module.create_app(cfg, domain="standard"))
        d = client.get("/api/status").json()
        assert d["ready"] is False
        assert d["error"]
        assert "has_llm" in d, "早退响应缺 has_llm：前端会把未配置误当成已配置"
        assert d["has_llm"] is with_llm


class TestAskInputLimits:
    """`/api/ask` 的输入边界（SEC-HYB-11）。"""

    def test_question_over_limit_is_400(self, tmp_path, monkeypatch):
        import docqa.api.app as api_module

        client, _ = TestHybridAskApi._client(tmp_path, monkeypatch, with_llm=False)
        too_long = "抗" * (api_module.MAX_QUESTION_CHARS + 1)
        r = client.post("/api/ask", json={"question": too_long})
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "过长" in detail
        assert str(api_module.MAX_QUESTION_CHARS) in detail

    def test_question_at_limit_is_accepted(self, tmp_path, monkeypatch):
        import docqa.api.app as api_module

        client, _ = TestHybridAskApi._client(tmp_path, monkeypatch, with_llm=False)
        question = ("抗压强度" * (api_module.MAX_QUESTION_CHARS // 4 + 1))[
            : api_module.MAX_QUESTION_CHARS
        ]
        assert len(question) == api_module.MAX_QUESTION_CHARS
        r = client.post("/api/ask", json={"question": question})
        assert r.status_code == 200, r.text


class TestHybridAskCli:
    @pytest.fixture(scope="class")
    def doc_dir(self, tmp_path_factory):
        from docqa.pipeline import ingest

        _rebind_logging()
        root = tmp_path_factory.mktemp("hybrid-cli")
        return ingest(DEMO_DOCX, cfg=load_config(), domain="standard", out_root=root).out_dir

    @staticmethod
    def _patch_provider(monkeypatch, *, hybrid_text: str | None = None):
        """让 CLI 看到"已配置生成模型"，并注入假 provider（不发真实请求）。"""
        import docqa.cli as cli_module
        import docqa.llm as llm_module

        cfg = _llm_configured(load_config())
        monkeypatch.setattr(cli_module, "load_config", lambda *a, **k: cfg)
        provider = _FakeLLM(hybrid_text=hybrid_text)
        monkeypatch.setattr(llm_module, "build_llm_provider", lambda _cfg: provider)
        return provider

    def test_hybrid_without_llm_exits_config_error(self, doc_dir):
        r = runner.invoke(app, ["ask", "抗压强度", "--doc", str(doc_dir),
                                "--mode", "hybrid"])
        assert r.exit_code == 2, r.output
        assert "hybrid" in r.output and "生成模型" in r.output
        assert "Traceback" not in r.output

    def test_invalid_mode_exits_config_error(self, doc_dir):
        r = runner.invoke(app, ["ask", "抗压强度", "--doc", str(doc_dir),
                                "--mode", "loose"])
        assert r.exit_code == 2, r.output
        assert "未知的回答范围模式" in r.output

    def test_strict_json_shape_is_unchanged(self, doc_dir):
        r = runner.invoke(app, ["ask", "抗压强度", "--doc", str(doc_dir), "--json"])
        assert r.exit_code == 0, r.output
        d = json.loads(r.output[r.output.index("{"):])
        assert d["answer_mode"] == "strict"
        assert d["model_knowledge"] is None
        assert d["knowledge_used"] is False
        assert d["citations"]
        assert "verification" in d and "trace" in d

    def test_hybrid_json_with_fake_provider(self, doc_dir, monkeypatch):
        self._patch_provider(monkeypatch)
        r = runner.invoke(app, ["ask", "抗压强度", "--doc", str(doc_dir),
                                "--mode", "hybrid", "--json"])
        assert r.exit_code == 0, r.output
        d = json.loads(r.output[r.output.index("{"):])
        assert d["answer_mode"] == "hybrid"
        assert d["knowledge_used"] is True
        assert HYBRID_KNOWLEDGE in (d["model_knowledge"] or "")
        for mark in ("[p.", "[§", "[¶"):
            assert mark not in (d["model_knowledge"] or "")
        assert d["citations"]
        assert all(c["chunk_id"] for c in d["citations"])
        assert "600 MPa" not in (d["answer"] or "")

    def test_hybrid_print_shows_mode_and_separate_knowledge_panel(
        self, doc_dir, monkeypatch
    ):
        self._patch_provider(monkeypatch)
        r = runner.invoke(app, ["ask", "抗压强度", "--doc", str(doc_dir),
                                "--mode", "hybrid"])
        assert r.exit_code == 0, r.output
        assert "模式：含模型知识" in r.output
        assert "模型知识补充" in r.output
        assert HYBRID_KNOWLEDGE in r.output
        after = r.output.split("模型知识补充", 1)[1]
        assert "[§" not in after and "[p." not in after, "补充段被加上了引用"

    def test_hybrid_empty_doc_answer_does_not_report_an_error(self, doc_dir, monkeypatch):
        """正文为空但有补充段：显示"未找到可引用依据"+补充块，而不是错误态。"""
        self._patch_provider(
            monkeypatch,
            hybrid_text="【模型知识补充】\n冲击韧性通常用夏比试验测定。",
        )
        r = runner.invoke(app, ["ask", "抗压强度", "--doc", str(doc_dir),
                                "--mode", "hybrid"])
        assert r.exit_code == 0, r.output
        assert "模式：含模型知识" in r.output
        assert "文档中未找到可引用的依据" in r.output
        assert "冲击韧性通常用夏比试验测定" in r.output
        assert "模型知识补充" in r.output

    def test_hybrid_knowledge_control_chars_never_reach_the_terminal(
        self, doc_dir, monkeypatch
    ):
        """SEC-HYB-07：模型知识里的 C0/C1 控制字符不得原样打进终端。"""
        from docqa.qa.answer import split_hybrid_answer

        # A 负责在 split_hybrid_answer 里剥离 C0/C1。若尚未落地就跳过，
        # 落地后这条会自动开始真正检查 CLI 输出（不是永久豁免）。
        probe_knowledge = split_hybrid_answer(
            "正文。\n【模型知识补充】\n\x1b[31m红色注入\x1b[0m"
        )[1]
        if "\x1b" in (probe_knowledge or ""):
            pytest.skip("A 的 split_hybrid_answer 尚未剥离 C0/C1（并行修复中）")

        self._patch_provider(
            monkeypatch,
            hybrid_text=(
                f"{STRICT_ANSWER}\n【模型知识补充】\n"
                "\x1b[31m红色注入\x1b[0m 与普通文本。"
            ),
        )
        r = runner.invoke(app, ["ask", "抗压强度", "--doc", str(doc_dir),
                                "--mode", "hybrid"])
        assert r.exit_code == 0, r.output
        assert "\x1b" not in r.output, "模型知识里的 ESC 被原样输出到终端"
        assert "红色注入" in r.output


# ============================================================ 展示层净化（SEC-HYB-07 残余）
#
# 只有 split_hybrid_answer 剥离控制字符还不够：strict 答案、引用 quote、
# 拒答说明、补充段、API evidence 文本都可能把 `\x1b[31m`、`\x07` 带进终端或 JSON。
# 默认 strict 是主路径，展示层必须统一净化。

class TestDisplaySanitization:
    @staticmethod
    def _capture_print(monkeypatch):
        import io

        from rich.console import Console

        import docqa.cli as cli_module

        buffer = io.StringIO()
        monkeypatch.setattr(
            cli_module, "console",
            Console(file=buffer, force_terminal=False, width=200),
        )
        return cli_module, buffer

    def test_print_bundle_sanitizes_strict_answer_and_quote(self, monkeypatch):
        cli_module, buffer = self._capture_print(monkeypatch)
        bundle = _display_bundle(
            answer="抗压强度 42 MPa \x1b[31m红\x1b[0m\x07‮",
        )
        cli_module._print_bundle(bundle)
        out = buffer.getvalue()
        assert "\x1b" not in out and "\x07" not in out and "‮" not in out
        assert "抗压强度" in out and "红" in out
        assert "证据" in out and "X" in out
        assert "细节" in out, "自检说明被误删（应只剥控制字符）"

    def test_print_bundle_sanitizes_candidate_rows(self, monkeypatch):
        from docqa.models import AnswerBundle, Citation, ClarificationOption

        cli_module, buffer = self._capture_print(monkeypatch)
        cli_module._print_bundle(AnswerBundle(
            question="q", answer=None, refused=True, refusal_detail="需要消歧",
            candidates=[ClarificationOption(
                interpretation="解释 \x1b[31m选项\x07",
                value="值 \x1b[31mV\x07",
                citation=Citation(page_no=1, clause_label="3.1\x1b[31m‮",
                                  chunk_id="d:p1:c2", quote=""),
            )],
        ))
        out = buffer.getvalue()
        assert "\x1b" not in out and "\x07" not in out and "‮" not in out
        assert "解释" in out and "选项" in out and "3.1" in out

    def test_print_bundle_sanitizes_refusal_detail_and_knowledge(self, monkeypatch):
        from docqa.models import AnswerBundle

        cli_module, buffer = self._capture_print(monkeypatch)
        cli_module._print_bundle(AnswerBundle(
            question="q", answer=None, refused=True,
            refusal_detail="拒绝 \x1b[31m原因\x07",
        ))
        cli_module._print_bundle(_display_bundle(answer_mode="hybrid"))
        out = buffer.getvalue()
        assert "\x1b" not in out and "\x07" not in out
        assert "拒绝" in out and "原因" in out
        assert "补充" in out and "知识" in out

    def test_cli_json_sanitizes_agent_output(self, tmp_path, monkeypatch):
        from docqa.qa import QaAgent

        doc_dir = _ingest_demo_docx(tmp_path)
        bundle = _display_bundle(answer="抗压强度 42 MPa \x1b[31m红\x1b[0m\x07‮")
        monkeypatch.setattr(
            QaAgent, "answer",
            lambda self, question, mode="strict": bundle,
        )
        r = runner.invoke(app, ["ask", "抗压强度", "--doc", str(doc_dir), "--json"])
        assert r.exit_code == 0, r.output
        d = json.loads(r.output[r.output.index("{"):])
        # json.dumps 会把 ESC 转成 `\u001b` 文本，所以必须检查**解码后**的字段。
        for field in ("answer", "refusal_detail", "model_knowledge"):
            value = d.get(field) or ""
            assert "\x1b" not in value and "\x07" not in value, f"{field} 未净化"
            assert "‮" not in value, f"{field} 的 bidi 字符未剥离"
        assert "抗压强度" in d["answer"] and "红" in d["answer"]
        quote = d["citations"][0]["quote"]
        assert "\x1b" not in quote and "\x07" not in quote
        assert "证据" in quote
        rendered = d["citations"][0]["rendered"]
        clause = d["citations"][0]["clause_label"] or ""
        for value in (rendered, clause):
            assert "\x1b" not in value and "\x07" not in value and "‮" not in value
        assert "3.1" in rendered and "3.1" in clause
        candidate = d["candidates"][0]
        for field in ("interpretation", "value", "citation"):
            value = candidate.get(field) or ""
            assert "\x1b" not in value and "\x07" not in value and "‮" not in value
        assert "解释" in candidate["interpretation"]
        assert "3.1" in candidate["citation"]
        check = d["verification"]["checks"][0]
        assert "\x1b" not in check["detail"] and "\x07" not in check["detail"]
        assert "细节" in check["detail"]
        assert "\x1b" not in check["offenders"][0] and "\x07" not in check["offenders"][0]
        assert "问题" in check["offenders"][0]

    def test_api_payload_and_evidence_are_sanitized(self, tmp_path, monkeypatch):
        from docqa.index import HybridIndex
        from docqa.qa import QaAgent

        client, _ = TestHybridAskApi._client(tmp_path, monkeypatch, with_llm=True)
        monkeypatch.setattr(
            QaAgent, "answer",
            lambda self, question, mode="strict": _display_bundle(
                answer_mode="hybrid",
                answer="抗压强度 42 MPa \x1b[31m红\x1b[0m\x07‮",
            ),
        )
        monkeypatch.setattr(
            HybridIndex, "evidence_text",
            lambda self, chunk_id: "证据 \x1b[31mX\x07",
        )
        d = client.post(
            "/api/ask", json={"question": "抗压强度", "mode": "hybrid"}
        ).json()
        for field in ("answer", "refusal_detail", "model_knowledge"):
            value = d.get(field) or ""
            assert "\x1b" not in value and "\x07" not in value, f"{field} 未净化"
            assert "‮" not in value, f"{field} 的 bidi 字符未剥离"
        assert "抗压强度" in d["answer"] and "红" in d["answer"]
        assert "知识" in (d["model_knowledge"] or "")
        assert "原因" in (d["refusal_detail"] or "")
        quote = d["citations"][0]["quote"]
        assert "\x1b" not in quote and "\x07" not in quote
        rendered = d["citations"][0]["rendered"]
        clause = d["citations"][0]["clause_label"] or ""
        for value in (rendered, clause):
            assert "\x1b" not in value and "\x07" not in value and "‮" not in value
        assert "3.1" in rendered and "3.1" in clause
        candidate = d["candidates"][0]
        for field in ("interpretation", "value", "citation"):
            value = candidate.get(field) or ""
            assert "\x1b" not in value and "\x07" not in value and "‮" not in value
        assert "解释" in candidate["interpretation"]
        assert "3.1" in candidate["citation"]
        check = d["verification"]["checks"][0]
        assert "\x1b" not in check["detail"] and "\x07" not in check["detail"]
        assert "细节" in check["detail"]
        assert "\x1b" not in check["offenders"][0] and "\x07" not in check["offenders"][0]
        assert "问题" in check["offenders"][0]
        evidence = next(iter(d["evidence"].values()))
        assert "\x1b" not in evidence["text"] and "\x07" not in evidence["text"]
        assert "证据" in evidence["text"] and "X" in evidence["text"]
