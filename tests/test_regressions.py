"""回归测试：为三个"不报错、只是变贵/变丑"的 P0 缺陷各钉一条断言。

为什么单独一个文件
------------------
这三条缺陷有共同特征：**代码不报错、测试不失败、日志显示成功**，
只是行为悄悄变差（退出码退化、白花钱、多花一倍钱）。
这类问题靠读代码 review 抓不住，必须让断言来守。

所以每条修复都配一条**计数或退出码断言**，而不是"看一眼觉得对了"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest
from PIL import Image
from typer.testing import CliRunner

from docqa.cli import app
from docqa.config import load_config
from docqa.llm.base import LLMResponse
from docqa.models import Usage
from docqa.pipeline import ingest as run_ingest
from docqa.ocr.fixture import FixtureOcrEngine

runner = CliRunner()


# ============================================================ 缺陷 1：入口点
class TestEntryPointReachesMain:
    """入口必须经过 `main()`，否则 `_fail()` 与退出码契约全是死代码。

    这条不能靠"跑一下看看"——要断言的是**具体退出码**，
    因为退化成 1 与设计好的 2/3/4/5/6 在肉眼上都是"失败了"。
    """

    def test_console_script_binds_to_main_not_app(self):
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        assert 'docqa = "docqa.cli:main"' in text, (
            "入口必须绑到 main，绑到 app 会让 cli.py 里的 DocqaError 处理成为死代码"
        )

    def test_module_entry_point_exists(self):
        """`python -m docqa` 也要能走通（控制台脚本不会执行 `if __name__`）。"""
        from docqa import __main__ as m

        assert callable(m.main)

    def test_main_uses_sys_exit_not_typer_exit(self):
        """在 main() 里抛 typer.Exit 会变成 traceback（已在 Click standalone 上下文之外）。

        断言用 `raise typer.Exit` 而不是裸的 `typer.Exit`——
        后者会被注释里的一句说明误伤（这条断言自己就踩过）。
        """
        import inspect

        from docqa import cli

        src = inspect.getsource(cli.main)
        assert "sys.exit" in src
        assert "raise typer.Exit" not in src


class TestExitCodeContract:
    """退出码契约只在**真正的入口**（`main()`）上成立。

    这一点必须说清楚，否则很容易写出**看起来在测、实际没测到**的测试：

    - `CliRunner.invoke(app, ...)` 调的是 `app` 本身，**绕过了 `main()`**，
      所以未捕获的 `DocqaError` 会以 exit_code=1 + 异常对象的形式返回；
    - 生产路径 `docqa ...` 走的是 `[project.scripts]` → `main()`，
      那里才有 `_report_error` + `sys.exit(exit_code)`。

    所以这些用例**显式调用 `main()`**，而不是图省事用 CliRunner。
    """

    @staticmethod
    def _strip_ocr_env(monkeypatch, tmp_path):
        """把 OCR 配置彻底清掉，让「缺密钥」这条路径**不依赖环境状态**。

        这两个用例原先依赖「这台机器恰好没配 OCR」。等 .env 真的配上密钥之后
        它们就集体失效了——**测试不该依赖环境状态**，那不是测试失败，
        是测试写错了。
        """
        import os

        for key in list(os.environ):
            if key.startswith(("DOCQA_OCR", "DOCQA_LLM")):
                monkeypatch.delenv(key, raising=False)
        # 指向一个没有 .env 的目录，避免 load_config 又从项目根把它读回来
        monkeypatch.setattr("docqa.config.PROJECT_ROOT", tmp_path)

    @staticmethod
    def _run_main(monkeypatch, argv: list[str]):
        """以给定 argv 调用生产入口，返回 SystemExit 的退出码。"""
        import sys

        from docqa.cli import main

        monkeypatch.setattr(sys, "argv", ["docqa", *argv])
        try:
            main()
        except SystemExit as exc:
            return exc.code
        pytest.fail("main() 未通过 SystemExit 退出——退出码契约未生效")

    def test_missing_ocr_config_exits_2(self, tmp_path, monkeypatch, capsys):
        """无密钥跑 ingest：必须是退出码 2，而不是退化成 1。"""
        self._strip_ocr_env(monkeypatch, tmp_path)
        pdf = _make_scan(tmp_path)
        code = self._run_main(monkeypatch, ["ingest", str(pdf), "--out", str(tmp_path / "docs")])
        assert code == 2, f"应退出码 2（配置错误），实际 {code}"
        out = capsys.readouterr().out
        assert "缺少 OCR" in out, "应打出可操作的缺密钥提示"

    def test_unknown_domain_exits_2(self, tmp_path, monkeypatch, capsys):
        self._strip_ocr_env(monkeypatch, tmp_path)
        code = self._run_main(monkeypatch, ["ask", "任意问题", "--domain", "并不存在的场景"])
        assert code == 2
        assert "未知的业务场景" in capsys.readouterr().out

    def test_missing_file_exits_2_from_typer_validation(self, tmp_path, monkeypatch):
        """typer 的参数校验失败由 Click 自己处理（退出码 2），不经过 main() 的 except。"""
        code = self._run_main(monkeypatch, ["detect", str(tmp_path / "不存在.pdf")])
        assert code == 2

    def test_clirunner_bypasses_main_so_exit_code_is_1(self, tmp_path, monkeypatch):
        """记录这个差异本身——防止后来者误以为 CliRunner 能验证退出码契约。

        `--out` 必须显式指向临时目录：这个用例调的是**真 CLI**，
        不指定就会把产物写进项目的 data/derived。
        曾经漏了这一步，于是测试在自己项目里留下两个空壳产物目录，
        Web API 又按 mtime 取"最新一份"，正好取到空壳——表现为 /api/status
        返回 chunks=0。**测试写脏项目数据，是会被下游真实消费到的。**
        """
        self._strip_ocr_env(monkeypatch, tmp_path)
        pdf = _make_scan(tmp_path)
        result = runner.invoke(app, ["ingest", str(pdf), "--out", str(tmp_path / "docs")])
        assert result.exit_code == 1, "CliRunner 调的是 app，绕过了 main()，故为 1"


# ============================================================ 缺陷 2：空目标页
def _make_scan(tmp_path: Path, pages: int = 2) -> Path:
    """合成一份纯扫描件（整页图像、无文本层）。"""
    doc = pymupdf.open()
    for i in range(pages):
        img = tmp_path / f"p{i}.png"
        Image.new("RGB", (600, 800), "white").save(img)
        page = doc.new_page(width=300, height=400)
        page.insert_image(pymupdf.Rect(0, 0, 300, 400), filename=str(img))
    out = tmp_path / "scan.pdf"
    doc.save(out, garbage=4, deflate=True)
    doc.close()
    return out


def _make_text_pdf(tmp_path: Path, pages: int = 3) -> Path:
    """合成一份**有真实文本层**的 PDF（born-digital）。"""
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=300, height=400)
        page.insert_text((40, 60), f"Page {i + 1}: this page has a real text layer.",
                         fontsize=12)
    out = tmp_path / "text.pdf"
    doc.save(out)
    doc.close()
    return out


class _CountingEngine:
    """把调用次数记下来的假引擎。"""

    name = "counting"
    model = "counting"
    supports_vision = False

    def __init__(self):
        self.calls: list[int] = []

    def ocr_page(self, image, **kw):
        self.calls.append(image.page_no)
        raise AssertionError("纯文本 PDF 不应该有任何页被送去 OCR")

    def close(self):
        pass


class TestEmptyTargetPagesStayEmpty:
    """`[] or None` 会把"一页都不需要识别"变成"识别全部页"。

    后果有两层：违反 detect.py 写明的硬不变量，以及**白烧识别费用**。
    这里用计数引擎来守——断言"零调用"，而不是断言"看起来没跑"。
    """

    def test_text_pdf_never_reaches_ocr(self, tmp_path):
        pdf = _make_text_pdf(tmp_path, pages=3)
        cfg = load_config()
        cfg.cache_dir = tmp_path / "cache"
        engine = _CountingEngine()

        result = run_ingest(pdf, cfg=cfg, domain="standard", engine=engine,
                            out_root=tmp_path / "docs")

        assert engine.calls == [], f"有真实文本层的页被送去 OCR 了：{engine.calls}"
        assert result.profile.kind == "TEXT"
        # 渲染产物也应为空 —— 不是"没记录"，是真的没渲染
        pages_json = json.loads((result.out_dir / "pages.json").read_text(encoding="utf-8"))
        assert pages_json == []

    def test_render_pages_none_means_all_but_empty_means_none(self, tmp_path):
        """显式区分 `None`（未指定）与 `[]`（明确为空）。"""
        from docqa.render import render_pages

        pdf = _make_scan(tmp_path, pages=2)
        assert len(render_pages(pdf, tmp_path / "a", page_count=2, pages=None)) == 2
        assert len(render_pages(pdf, tmp_path / "b", page_count=2, pages=[])) == 0


class TestEngineNoneIsExplicit:
    """`engine=None` 时日志说"跳过识别"，旧代码却照样调用 → AttributeError。"""

    def test_engine_none_with_pages_to_ocr_raises_clearly(self, tmp_path):
        from docqa.errors import ConfigError

        pdf = _make_scan(tmp_path, pages=1)  # 扫描件 → 有待识别页
        cfg = load_config()
        cfg.cache_dir = tmp_path / "cache"
        with pytest.raises(ConfigError, match="没有提供 OCR 引擎"):
            run_ingest(pdf, cfg=cfg, domain="standard", engine=None,
                       out_root=tmp_path / "docs")

    def test_engine_none_is_fine_when_nothing_needs_ocr(self, tmp_path):
        """纯文本 PDF：引擎无关紧要，不该因为这个报错。"""
        pdf = _make_text_pdf(tmp_path, pages=1)
        cfg = load_config()
        cfg.cache_dir = tmp_path / "cache"
        result = run_ingest(pdf, cfg=cfg, domain="standard", engine=None,
                            out_root=tmp_path / "docs")
        assert result.profile.kind == "TEXT"


# ============================================================ 缺陷 3：重复调用
class _CountingProvider:
    """每次 complete 都计数的假生成模型。"""

    name = "counting-llm"
    supports_vision = False
    model = "counting-llm"

    def __init__(self):
        self.calls = 0

    def complete(self, system, user, **kw) -> LLMResponse:
        self.calls += 1
        # 给一个带引用的答案，好让它能进入对抗变异的样本池
        return LLMResponse(
            text="抗拉强度应大于等于 590 MPa。[p.1 §3.1]",
            provider="counting", model="counting-llm", usage=Usage(calls=1),
        )

    def close(self):
        pass


class TestEvalAnswersEachQuestionOnce:
    """`eval` 曾经把每道题回答两遍（取样一遍 + run_eval 一遍），花费与耗时翻倍。

    这条只能靠**计数断言**守住——两遍调用在输出上完全看不出来，
    只有账单和耗时能反映。
    """

    @pytest.fixture
    def setup(self, tmp_path):
        from docqa.chunker import chunk_document
        from docqa.config import load_domain
        from docqa.eval.runner import GoldenItem, run_eval
        from docqa.index import HybridIndex
        from docqa.models import OcrBlock, OcrPage
        from docqa.qa import QaAgent

        cfg = load_config()
        profile = load_domain("standard", cfg)
        blocks = [
            OcrBlock(block_id="p1-b0", page_no=1, type="clause", order=0,
                     text="3.1 抗拉强度", clause_label="3.1"),
            OcrBlock(block_id="p1-b1", page_no=1, type="paragraph", order=1,
                     text="键的抗拉强度应大于等于 590 MPa。"),
        ]
        page = OcrPage(page_no=1, blocks=blocks, tables=[], image_sha256="x",
                       provider="t", model="t", prompt_version="v1", usage=Usage())
        chunks, _ = chunk_document([page], "d", profile)
        provider = _CountingProvider()
        agent = QaAgent(HybridIndex(chunks, profile), provider, cfg, profile, doc_id="d")

        items = [GoldenItem(qid=f"Q{i}", question="抗拉强度要求是多少", category="clause",
                            must_contain=["590"], citation_pages=[1])
                 for i in range(5)]
        return cfg, profile, agent, provider, items, chunks

    def test_run_eval_returns_reusable_bundles(self, setup):
        """返回 bundle 是对抗变异能复用它们的前提。"""
        from docqa.eval.runner import run_eval

        cfg, profile, agent, provider, items, chunks = setup
        rep = run_eval(items, agent.answer, engine="t", doc_id="d", domain="standard")
        assert len(rep.bundles) == len(items)
        assert all(bundle.answer for _, bundle in rep.bundles)

    def test_one_model_call_per_question(self, setup):
        """核心断言：N 道题就只应有 N 次调用（含对抗变异在内）。"""
        from docqa.eval import run_adversarial
        from docqa.eval.runner import run_eval

        cfg, profile, agent, provider, items, chunks = setup

        rep = run_eval(items, agent.answer, engine="t", doc_id="d", domain="standard")
        calls_after_eval = provider.calls
        assert calls_after_eval == len(items), (
            f"{len(items)} 道题产生了 {calls_after_eval} 次调用；"
            f"每道题只应被回答一次"
        )

        # 对抗变异复用已有 bundle，**不应再产生任何新调用**
        samples = [(b.answer, b.citations) for _, b in rep.bundles if b.answer]
        evidence_map = {c.chunk_id: c.text for c in chunks}
        run_adversarial(samples, retrieved_ids={c.chunk_id for c in chunks},
                        known_ids={c.chunk_id for c in chunks},
                        evidence_by_chunk=evidence_map)
        assert provider.calls == calls_after_eval, "对抗变异不应再调用模型"

    def test_to_dict_excludes_non_serialisable_fields(self, setup):
        from docqa.eval.runner import run_eval

        cfg, profile, agent, provider, items, chunks = setup
        rep = run_eval(items, agent.answer, engine="t", doc_id="d", domain="standard")
        data = rep.to_dict()
        assert "bundles" not in data and "pages" not in data
        json.dumps(data, ensure_ascii=False)  # 必须可序列化
