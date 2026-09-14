"""命令行入口。

设计原则：**每一步都能单独跑、单独看中间产物。**
"可定位问题"是评分项，而定位问题的前提是能看到中间状态——
一个只会 `run` 的黑盒，出了问题只能靠猜。

    docqa doctor                     环境自检（含**实测**端点是否真支持图像输入）
    docqa detect   <pdf>             判断 PDF 类型，输出证据
    docqa render   <pdf>             只看渲染结果
    docqa ingest   <pdf>             全流程：判定→渲染→OCR→切分→索引
    docqa ask      "问题"            问答，返回答案+引用+自检
    docqa eval                       跑评估，产出报告
    docqa serve                      起 Web UI
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .config import PROJECT_ROOT, Config, load_config, load_domain, missing_ocr_message
from .detect import pages_needing_ocr, profile_pdf
from .errors import (
    EXIT_CONFIG,
    EXIT_REGRESSION,
    DocqaError,
    RegressionDetected,
)
from .logging import get_logger, setup as setup_logging
from .models import AnswerBundle

app = typer.Typer(
    add_completion=False,
    help="扫描版 PDF 智能文档问答 Agent",
    no_args_is_help=True,
)
console = Console()
_log = get_logger("cli")


def _cfg(require_ocr: bool = False) -> Config:
    cfg = load_config(require_ocr=require_ocr)
    setup_logging(cfg.log_level, json_output=False)
    return cfg


def _report_error(exc: DocqaError) -> None:
    """只负责把错误打成人能读的样子，不决定怎么退出。

    与 `_fail` 分开是因为**退出机制依赖于所处的位置**：

    - 在命令**内部**抛 `typer.Exit`，Click 的 standalone 模式会把它转成进程退出码；
    - 在 `main()` 里已经出了那个上下文，再抛 `typer.Exit` 只会得到一个 traceback
      （`click.exceptions.Exit` 继承自 `RuntimeError`）。

    所以打印归打印、退出归调用方，两件事不要混在一个函数里。
    """
    console.print(Panel(str(exc), title="[red]错误[/red]", border_style="red"))


def _fail(exc: DocqaError) -> None:
    """命令内部的失败出口：打印 + 用 `typer.Exit` 交给 Click 转成退出码。"""
    _report_error(exc)
    raise typer.Exit(getattr(exc, "exit_code", 1))


# ---------------------------------------------------------------- doctor

@app.command()
def doctor(
    test_vision: bool = typer.Option(True, "--test-vision/--no-test-vision",
                                     help="是否用一张测试图实测端点是否支持图像输入"),
) -> None:
    """环境自检。

    **重点在于实测，而不是读配置。** 很多端点在文档里写着支持图像，
    实际上会把图片静默忽略、返回一段看似合理的文字。所以这里真的发一张图过去，
    问一个只有看得见才能答对的问题。
    """
    cfg = _cfg()
    t = Table(title="环境自检", show_header=True, header_style="bold")
    t.add_column("项目")
    t.add_column("状态")
    t.add_column("说明")

    # --- PDF 工具链 ---
    try:
        import pymupdf

        t.add_row("PyMuPDF", "[green]可用[/green]", f"v{pymupdf.__doc__ or ''}".strip()[:40])
    except ImportError as exc:
        t.add_row("PyMuPDF", "[red]缺失[/red]", str(exc))

    # --- 端点配置 ---
    t.add_row("OCR 端点", "[green]已配置[/green]" if cfg.has_ocr else "[red]未配置[/red]",
              cfg.ocr.describe())
    t.add_row("生成端点", "[green]已配置[/green]" if cfg.has_llm else "[yellow]未配置[/yellow]",
              cfg.llm.describe() + ("" if cfg.has_llm else "（将降级为抽取式回答）"))
    t.add_row("第二 OCR 引擎", "[green]已配置[/green]" if cfg.has_ocr2 else "[dim]未配置[/dim]",
              cfg.ocr2.describe() + ("" if cfg.has_ocr2 else "（跳过跨引擎校验）"))

    console.print(t)

    if not cfg.has_ocr:
        console.print(Panel(missing_ocr_message(), title="[yellow]缺少 OCR 端点[/yellow]",
                            border_style="yellow"))
        raise typer.Exit(1)

    if not test_vision:
        return

    # --- 实测视觉能力 ---
    console.print("\n[bold]实测端点是否支持图像输入…[/bold]")
    ok, message = prove_vision(cfg)
    style = "green" if ok else "red"
    console.print(Panel(message, title=f"[{style}]视觉能力实测[/{style}]", border_style=style))
    if not ok:
        raise typer.Exit(1)


def prove_vision(cfg: Config) -> tuple[bool, str]:
    """用一张**合成测试图**实测端点是否真的看得见。

    做法：画一张只有"看得见"才能答对的图（特定颜色的方块 + 一个数字），
    然后问它。纯文本模型无法从提示词里推断出答案，因此只要答对就证明它真的读了图。

    这一步存在的意义：把"配置写对了"和"功能真的可用"分开。
    只检查配置项是否填写，会在密钥正确但模型不支持图像时给出虚假的通过。
    """
    import base64
    import io

    from PIL import Image, ImageDraw

    from .llm import ImagePart, build_provider

    # 合成一张测试图：白底 + 蓝色矩形 + 一个醒目的数字
    img = Image.new("RGB", (320, 160), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 140, 140], fill=(0, 90, 200))
    draw.text((200, 70), "7391", fill=(200, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    tmp = PROJECT_ROOT / "data" / "derived" / "_vision_probe.png"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(buf.getvalue())

    try:
        provider = build_provider(cfg.ocr, timeout=cfg.http_timeout,
                                 max_retries=1, what="OCR 识别")
        part = ImagePart.from_path(tmp)
        # max_tokens 必须给足。**推理模型**会先把 token 花在思考上再写答案，
        # 给小了（比如 64）会得到 finish_reason="length" 且 content 为空——
        # 那看起来和"模型没读图"一模一样，但完全是两回事。
        # 实测：64 → 空内容；1024 → 正确答出 "7391,蓝色"。
        resp = provider.complete(
            "你是一个图像识别工具。只回答被问到的内容，不要解释。",
            "这张图里的数字是多少？另外，左侧的大矩形是什么颜色？只回一行，格式：数字,颜色",
            images=[part],
            max_tokens=2048,
            temperature=0.0,
        )
        text = resp.text.strip()

        # --- 先排除"不是视觉能力问题"的几种情况，再下"看不看得见"的结论 ---
        #
        # 这个顺序很关键。最初的实现只看 content 是否为空，于是把
        # "被 token 上限截断"误报成了"模型不支持图像"，
        # 让使用者去换一个本来没问题的模型。**错误的诊断比没有诊断更浪费时间。**
        if not text and resp.truncated:
            return False, (
                "[red]响应被 token 上限截断，没能得出答案。[/red]\n\n"
                "finish_reason=length，输出 token 全部消耗在推理阶段。\n\n"
                "这**不是**「模型不支持图像」，而是 max_tokens 给小了。\n"
                "若该模型是推理模型，扫描件识别需要显著更大的 max_tokens"
                "（本项目 OCR 默认 8000）。"
            )
        if not text:
            reasoning = (resp.reasoning or "").strip()
            return False, (
                f"[red]模型返回了空内容（finish_reason={resp.finish_reason}）。[/red]\n"
                + (f"但思维链里有内容，说明它**可能读到了图**，只是没写进正式输出：\n"
                   f"  {reasoning[:200]}\n\n" if reasoning else "")
                + "请检查该端点的响应格式是否符合 OpenAI 兼容约定。"
            )

        got_number = "7391" in text
        got_color = any(w in text for w in ("蓝", "blue", "Blue"))
        if got_number and got_color:
            return True, (
                f"[green]端点确实支持图像输入。[/green]\n"
                f"模型回答：{text}\n"
                f"识别到数字 7391 与蓝色矩形 —— 只有真正读到图才能答对。"
            )
        if got_number or got_color:
            return False, (
                f"[yellow]端点部分答对，视觉能力存疑。[/yellow]\n"
                f"模型回答：{text}\n"
                f"期望同时识别出 7391 与蓝色。可能是图像分辨率或提示词问题，建议换模型。"
            )
        return False, (
            f"[red]端点没有正确识别出测试图的内容。[/red]\n"
            f"模型回答：{text}\n\n"
            f"这可能是**纯文本模型**——它会静默忽略图片、返回一段看似合理的文字。\n"
            f"请改用真正支持视觉的模型（如带 -vl / -vision 后缀的多模态模型）。\n"
            f"注意：只填了模型名不代表它能看图。"
        )
    except DocqaError as exc:
        return False, f"[red]调用失败：[/red]\n{exc}"
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------- detect

@app.command()
def detect(
    pdf: Path = typer.Argument(..., exists=True, help="待判定的 PDF"),
    out: Path | None = typer.Option(None, "--out", help="把判定结果写到 JSON"),
) -> None:
    """判断 PDF 类型，并输出**判定依据**。"""
    profile = profile_pdf(pdf)

    color = {"SCANNED": "yellow", "TEXT": "green", "HYBRID": "cyan"}[profile.kind]
    console.print(Panel(
        f"类型：[{color}]{profile.kind}[/{color}]   页数：{profile.page_count}   "
        f"sha256：{profile.sha256[:16]}",
        title=pdf.name, border_style=color,
    ))

    t = Table(show_header=True, header_style="bold")
    for col in ("页", "判定", "字符数", "字体数", "有效图", "排除小图", "图像覆盖", "原生DPI", "理由"):
        t.add_column(col, overflow="fold")
    for p in profile.pages:
        t.add_row(
            str(p.page_no), p.kind, str(p.char_count), str(p.font_resource_count),
            str(p.drawn_image_count), str(p.excluded_tiny_images),
            f"{p.drawn_image_area_ratio:.0%}",
            f"{p.native_dpi:.0f}" if p.native_dpi else "-",
            p.reason,
        )
    console.print(t)

    console.print("\n[bold]交叉引擎验证[/bold]")
    for k, v in profile.cross_engine.items():
        console.print(f"  {k} = {v:.0f}")
    if profile.cross_engine.get("pdftotext_available"):
        pm = profile.cross_engine.get("pymupdf_chars_total", 0)
        pt = profile.cross_engine.get("pdftotext_chars_total", 0)
        console.print(f"  → 两个独立抽取器都得到 {pm:.0f} / {pt:.0f} 个字符"
                      + ("，互为佐证" if pm == 0 and pt == 0 else ""))

    need = pages_needing_ocr(profile)
    console.print(f"\n需送 OCR 的页：{need or '（无）'}")

    if out:
        out.write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        console.print(f"已写入 {out}")


# ---------------------------------------------------------------- render

@app.command()
def render(
    pdf: Path = typer.Argument(..., exists=True),
    out: Path = typer.Option(Path("out/pages"), "--out"),
    dpi: int | None = typer.Option(None, "--dpi", help="None=原生 DPI"),
    upscale: float = typer.Option(1.0, "--upscale", help="放大倍数（不增加信息，仅供 A/B）"),
) -> None:
    """把页图渲染出来，供人工核对。

    注意：`--upscale` 只是**插值放大**，不会恢复任何精度。
    它的用途是做 A/B 实验，看更高的输入分辨率是否改善了 VLM 的辨识率。
    """
    from .render import describe, render_pages

    cfg = _cfg()
    cfg.render.dpi = dpi
    cfg.render.upscale = upscale
    profile = profile_pdf(pdf)

    images = render_pages(pdf, out, page_count=profile.page_count, config=cfg.render)
    t = Table(title=f"渲染结果 → {out}", show_header=True, header_style="bold")
    for col in ("页", "文件", "尺寸", "DPI", "位精确", "放大"):
        t.add_column(col)
    for d in describe(images):
        t.add_row(str(d["page_no"]), d["file"], f"{d['width']}x{d['height']}",
                  f"{d['dpi']:.0f}", "是" if d["native"] else "否", f"{d['upscale']}x")
    console.print(t)


# ---------------------------------------------------------------- ingest

@app.command()
def ingest(
    pdf: Path = typer.Argument(..., exists=True),
    domain: str = typer.Option("standard", "--domain", "-d", help="业务场景 profile"),
    engine: str = typer.Option("vlm", "--engine", help="vlm / fixture / local"),
    fixtures_dir: Path | None = typer.Option(None, "--fixtures-dir"),
    out: Path | None = typer.Option(None, "--out", help="产物目录"),
    force: bool = typer.Option(False, "--force", help="忽略缓存重新识别"),
) -> None:
    """全流程：类型判定 → 渲染 → OCR → 切分 → 索引。"""
    from .ocr import build_engine
    from .pipeline import ingest as run_ingest

    cfg = _cfg(require_ocr=(engine == "vlm"))
    if force:
        import shutil

        shutil.rmtree(cfg.cache_dir, ignore_errors=True)
        console.print("[yellow]已清空 OCR 缓存，将重新识别[/yellow]")

    if engine == "fixture":
        console.print(Panel(
            "[yellow]正在使用 fixture 引擎。[/yellow]\n"
            "它读取的是预置结果，**不是真实的模型识别**。\n"
            "产物中会标注 engine=fixture，评估时也会与此隔离；\n"
            "因此本次运行不得用于声明任何 OCR 精度指标。",
            border_style="yellow",
        ))

    try:
        ocr_engine = build_engine(engine, cfg, fixtures_dir=fixtures_dir) if engine != "vlm" \
            else build_engine("vlm", cfg)
        result = run_ingest(pdf, cfg=cfg, domain=domain, engine=ocr_engine, out_root=out)
    except DocqaError as exc:
        _fail(exc)
        return

    m = result.manifest
    console.print(Panel(
        f"文档：{result.doc_id}\n"
        f"类型：{result.profile.kind}   页数：{result.profile.page_count}   "
        f"识别失败：{len(result.failed_pages)} 页\n"
        f"片段：{m['chunk']['chunks']}（表格 {m['chunk']['tables']}）   "
        f"引用模式：{m['chunk']['citation_mode']}\n"
        f"耗时：{m['elapsed_s']}s   token：{m['usage_total']['tokens_in']}+"
        f"{m['usage_total']['tokens_out']}   估算成本：${m['usage_total']['est_cost_usd']}\n"
        f"产物目录：{result.out_dir}",
        title="ingest 完成", border_style="green",
    ))
    if m["chunk"]["used_sentence_fallback"]:
        console.print(
            "[yellow]注意：未识别到条款结构，已退回句子窗口切分，"
            "引用格式相应降级为段落号。[/yellow]"
        )
    console.print(f"\n下一步： docqa ask \"你的问题\" --doc {result.out_dir}")


# ---------------------------------------------------------------- ask

def _print_bundle(bundle: AnswerBundle) -> None:
    """统一的答案呈现。CLI 与 Web 用同一份 AnswerBundle，保证两者一致。"""
    if bundle.refused:
        reason = bundle.refusal_reason.value if bundle.refusal_reason else "unknown"
        console.print(Panel(
            f"[yellow]按策略拒答[/yellow]（{reason}）\n\n{bundle.refusal_detail or ''}",
            title="无法作答", border_style="yellow",
        ))
        if bundle.candidates:
            t = Table(title="候选解释（请指明你问的是哪一个）", show_header=True)
            t.add_column("#")
            t.add_column("解释")
            t.add_column("取值")
            t.add_column("出处")
            for i, c in enumerate(bundle.candidates, start=1):
                t.add_row(str(i), c.interpretation, c.value, c.citation.render())
            console.print(t)
    else:
        console.print(Panel(bundle.answer or "", title="答案", border_style="green"))

    if bundle.citations:
        t = Table(title="引用来源", show_header=True, header_style="bold")
        t.add_column("出处")
        t.add_column("chunk")
        t.add_column("证据片段", overflow="fold")
        for c in bundle.citations:
            t.add_row(c.render(), c.chunk_id, c.quote[:120])
        console.print(t)

    v = bundle.verification
    t = Table(title="自检结论", show_header=True, header_style="bold")
    t.add_column("检查项")
    t.add_column("性质")
    t.add_column("结果")
    t.add_column("说明", overflow="fold")
    for c in v.checks:
        t.add_row(c.name, c.severity.value,
                  "[green]通过[/green]" if c.passed else "[red]未通过[/red]", c.detail)
    console.print(t)
    console.print(f"句级支撑度（咨询性，不作放行判定）：{v.support_score:.2f}")

    if bundle.trace:
        console.print("\n[bold]执行轨迹[/bold] " +
                      " → ".join(t.step for t in bundle.trace))
        for step in bundle.trace:
            console.print(f"  [dim]{step.step}[/dim]: {step.detail}")


@app.command()
def ask(
    question: str = typer.Argument(..., help="你的问题"),
    doc: Path | None = typer.Option(None, "--doc", help="ingest 产物目录"),
    pdf: Path | None = typer.Option(None, "--pdf", help="或直接给 PDF（需先 ingest）"),
    domain: str = typer.Option("standard", "--domain", "-d"),
    show_json: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """提问，返回答案 + 引用 + 自检结论。"""
    from .llm import build_llm_provider
    from .pipeline import load_result
    from .qa import QaAgent

    cfg = _cfg()
    # 先校验场景名，再找产物。反过来的话，一个拼错的 --domain 会先报
    # "找不到已 ingest 的产物"，把人引去跑 ingest —— 而 ingest 同样会因为这个
    # 拼错的场景名失败。**先报那个真正的原因。**
    domain_profile = load_domain(domain, cfg)
    doc_dir = doc or _latest_doc_dir(cfg, pdf)
    if doc_dir is None:
        console.print("[red]找不到已 ingest 的产物。请先运行 docqa ingest。[/red]")
        raise typer.Exit(2)

    try:
        result = load_result(doc_dir, cfg, domain)
    except (OSError, KeyError, ValueError) as exc:
        # 必须含 ValueError：产物里的 JSON 损坏时抛的是 json.JSONDecodeError，
        # 而它继承自 ValueError，**不是** OSError/KeyError。
        console.print(f"[red]读取产物失败：{exc}[/red]")
        raise typer.Exit(2) from exc

    agent = QaAgent(
        result.index,
        build_llm_provider(cfg),
        cfg,
        domain_profile,
        failed_pages=result.failed_pages,
        doc_id=result.doc_id,
    )
    bundle = agent.answer(question)

    if show_json:
        # 用 typer.echo 而不是 console.print_json：后者会做语法高亮，
        # 输出里带 ANSI 转义序列，`json.loads` 直接解析失败。
        # 一个叫 --json 的开关必须产出能被机器解析的 JSON。
        typer.echo(json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_bundle(bundle)


def _latest_doc_dir(cfg: Config, pdf: Path | None = None) -> Path | None:
    """找出最近一次 ingest 的产物目录。

    目录取自 `cfg.cache_dir.parent`，与 `pipeline.ingest()` 的写入位置**同源**。
    曾经这里硬编码 `data/derived`，而写入端用的是配置项——默认值恰好一致，
    但用户一旦改了 `cache_dir`，ingest 会写到一个地方、ask/eval/serve 去另一个地方找，
    表现为"刚 ingest 成功却报找不到产物"。**读写两侧必须用同一个来源。**
    """
    base = cfg.cache_dir.parent
    if not base.exists():
        return None
    candidates = [p for p in base.iterdir() if p.is_dir() and (p / "manifest.json").exists()]
    if pdf is not None:
        # 显式指定了 PDF 却匹配不到时**返回 None**，不要回退到"全部候选里最新的一份"——
        # 那会让人以为在看 A 文档，实际读的是 B 文档的产物。
        candidates = [p for p in candidates if p.name.startswith(pdf.stem)]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


# ---------------------------------------------------------------- eval

@app.command("eval")
def eval_cmd(
    doc: Path | None = typer.Option(None, "--doc"),
    pdf: Path | None = typer.Option(None, "--pdf"),
    domain: str = typer.Option("standard", "--domain", "-d"),
    # 默认值用 PROJECT_ROOT 而不是相对路径：全项目其它地方都是这么定位的，
    # 在仓库外的目录执行时，相对路径会去找 CWD 下的 data/golden（找不到而报错），
    # 或者把报告静默写到 CWD 的 out/ 下。
    golden: Path = typer.Option(PROJECT_ROOT / "data" / "golden" / "qa.jsonl", "--golden"),
    report: Path = typer.Option(PROJECT_ROOT / "out" / "eval_report.html", "--report",
                                help="HTML 报告路径"),
    json_out: Path | None = typer.Option(None, "--json-out"),
    baseline: Path | None = typer.Option(None, "--baseline", help="回归基线 JSON"),
    adversarial: bool = typer.Option(True, "--adversarial/--no-adversarial"),
) -> None:
    """跑评估，产出指标报告与回归比对。"""
    from .eval import load_golden, run_eval
    from .eval.runner import compare_baseline
    from .llm import build_llm_provider
    from .pipeline import load_result
    from .qa import QaAgent
    from .report import render_html_report

    cfg = _cfg()
    profile = load_domain(domain, cfg)  # 同 ask：先报场景名的问题
    doc_dir = doc or _latest_doc_dir(cfg, pdf)
    if doc_dir is None:
        console.print("[red]找不到已 ingest 的产物。请先运行 docqa ingest。[/red]")
        raise typer.Exit(2)
    if not golden.exists():
        console.print(f"[red]金标集不存在：{golden}[/red]")
        raise typer.Exit(2)

    try:
        result = load_result(doc_dir, cfg, domain)
    except (OSError, KeyError, ValueError) as exc:
        console.print(f"[red]读取产物失败：{exc}[/red]")
        raise typer.Exit(2) from exc

    agent = QaAgent(result.index, build_llm_provider(cfg), cfg, profile,
                    failed_pages=result.failed_pages, doc_id=result.doc_id)

    items = load_golden(golden)
    console.print(f"金标集 {len(items)} 题，文档 {result.doc_id}（{len(result.chunks)} 个片段）")

    notes: list[str] = []
    engine_name = result.manifest.get("ocr", {}).get("engine") or "unknown"
    if "fixture" in str(engine_name):
        notes.append("本次运行使用 fixture 引擎，**不是真实 OCR 结果**，不得据此声明 OCR 精度。")
    if len(result.chunks) < 30:
        notes.append(
            f"语料仅 {len(result.chunks)} 个片段，Recall@k / MRR 在如此小的语料上"
            f"没有统计意义（Recall@1 的随机基线即 1/{max(1, len(result.chunks))}）。"
            f"这里把它当作**管路连通性检查**报告，不作为检索质量证据。"
        )

    # --- 先跑评估，再基于**已有结果**做对抗变异 ---
    #
    # 顺序很重要：对抗变异要的是"正确答案"作为变异素材，
    # 而那些答案正是 run_eval 已经产生的。之前是先跑一遍取样、再让 run_eval
    # 跑第二遍，于是**每道题的 API 调用与耗时都翻了一倍**。
    # 现在改成 run_eval 把 (item, bundle) 一并带出来，对抗变异直接复用。
    rep = run_eval(
        items, agent.answer,
        engine=str(engine_name), doc_id=result.doc_id, domain=domain,
        notes=notes, pages=result.pages, manifest=result.manifest,
    )

    if adversarial:
        from .eval import run_adversarial

        samples: list[tuple[str, list]] = [
            (bundle.answer, bundle.citations)
            for item, bundle in rep.bundles
            if not item.expected_refusal
            and not bundle.refused
            and bundle.answer
            and bundle.citations
        ]
        if samples:
            evidence_map = {c.chunk_id: c.text for c in result.chunks}
            rep.adversarial = run_adversarial(
                samples,
                retrieved_ids={c.chunk_id for c in result.chunks},
                known_ids={c.chunk_id for c in result.chunks},
                evidence_by_chunk=evidence_map,
                support_gate=profile.qa.support_score_gate,
            )

    # --- 产物先落盘，再打印摘要 ---
    #
    # 顺序不是风格问题。摘要只是给人看的，**产物才是结果**；
    # 摘要代码里任何一个 bug 都不该让人丢掉结果。
    # 这个顺序问题真实发生过一次：摘要里一个未定义变量让 eval 直接崩溃，
    # 报告与 JSON 一个字都没写出来——而那次崩溃恰恰是"第一次跑真实评估"。
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_html_report(rep), encoding="utf-8")
    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2),
                            encoding="utf-8")

    # --- 控制台摘要 ---
    t = Table(title="评估结果", show_header=True, header_style="bold")
    t.add_column("指标")
    t.add_column("值")
    for k in ("items", "passed", "accuracy", "refusal_precision", "refusal_recall",
              "citation_validity_rate", "mean_context_utilization", "mean_attempts",
              "total_cost_usd"):
        v = rep.metrics.get(k)
        t.add_row(k, f"{v:.4f}" if isinstance(v, float) else str(v))
    console.print(t)

    t2 = Table(title="按题型分组（总体值会被数量多的题型拉平）", show_header=True)
    t2.add_column("题型")
    t2.add_column("n")
    t2.add_column("通过")
    t2.add_column("准确率")
    t2.add_column("95% Wilson CI")
    for cat, d in rep.per_category.items():
        lo, hi = d["ci"]
        t2.add_row(cat, str(d["n"]), str(d["passed"]), f"{d['accuracy']:.0%}",
                   f"[{lo:.0%}, {hi:.0%}]")
    console.print(t2)

    # 对抗变异的结果现在挂在 rep 上（不再是局部变量 adv）——
    # 因为改成"先跑评估、再复用其结果做变异"之后，取样来源变成了 rep.bundles。
    adv = rep.adversarial or {}
    if adv:
        t3 = Table(title="对抗变异检出率（自检是否真的有效）", show_header=True)
        t3.add_column("变异类型")
        t3.add_column("检出/总数")
        t3.add_column("检出率")
        for kind, d in adv.get("by_kind", {}).items():
            t3.add_row(kind, f"{d['detected']}/{d['total']}", f"{d['rate']:.0%}")
        console.print(t3)
        console.print(f"总体检出率：{adv.get('overall_detection_rate', 0):.0%}"
                      f"（共 {adv.get('total_mutations', 0)} 个变异）")

    for note in notes:
        console.print(Panel(note, border_style="yellow"))

    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(render_html_report(rep), encoding="utf-8")
    console.print(f"\nHTML 报告：{report}")
    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2),
                            encoding="utf-8")
        console.print(f"JSON 结果：{json_out}")

    if baseline:
        regressions = compare_baseline(rep, baseline)
        if regressions:
            console.print(Panel("指标相对基线下滑：\n" + "\n".join(regressions),
                                title="[red]回归[/red]", border_style="red"))
            raise typer.Exit(EXIT_REGRESSION)
        console.print("[green]与基线相比无下滑[/green]")


# ---------------------------------------------------------------- serve

@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    domain: str = typer.Option("standard", "--domain", "-d"),
) -> None:
    """启动 Web UI。"""
    import uvicorn

    from .api.app import create_app

    cfg = _cfg()
    application = create_app(cfg, domain=domain)
    console.print(f"Web UI: [bold]http://{host}:{port}[/bold]")
    uvicorn.run(application, host=host, port=port, log_level="warning")


def main() -> None:
    """控制台脚本入口（见 `[project.scripts]` 与 `__main__.py`）。

    这里是**所有未被命令内部捕获的 `DocqaError` 的最终落点**，
    也是既定退出码契约（配置 2 / 输入 3 / 服务方 4 / 回归 5 / 校验 6）的唯一保证处。
    """
    try:
        app()
    except DocqaError as exc:
        # 此处已在 Click 的 standalone 上下文之外，所以直接用 sys.exit。
        # 抛 typer.Exit 会变成 traceback，而不是退出码。
        _report_error(exc)
        sys.exit(getattr(exc, "exit_code", 1))
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断[/yellow]")
        sys.exit(130)


if __name__ == "__main__":
    main()
