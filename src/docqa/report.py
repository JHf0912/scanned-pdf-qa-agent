"""静态 HTML 评估报告。

这份报告是**交给评审的演示材料**，所以它要能独立回答三个问题：

1. 系统答对了什么、答错了什么 —— 逐题列出，带引用与自检明细；
2. 自检到底有没有用 —— 对抗变异的检出率按类型列出；
3. 这些数字有多可信 —— 每个比例都带 Wilson 区间与样本量 n。

第 3 点是刻意做的。小样本下的"100%"极具误导性（n=3 的 100%，
其 95% 置信下界只有 44%），所以宁可让报告看起来"不够漂亮"，
也要把不确定性摆在明面上。

全部样式内联，产出的 HTML 不依赖任何外部资源，可以直接发出去。
"""

from __future__ import annotations

import html
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .eval.runner import EvalReport

_CSS = """
:root{
  --bg:#fbfaf7; --panel:#fff; --ink:#1b1a17; --muted:#6b6862; --line:#e3ded4;
  --ok:#1f7a4d; --warn:#a86500; --bad:#a32b1e; --accent:#2c5f8a;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif;}
.wrap{max-width:1080px;margin:0 auto;padding:48px 24px 96px}
h1{font-size:30px;letter-spacing:-.02em;margin:0 0 6px}
h2{font-size:20px;margin:44px 0 14px;padding-bottom:8px;border-bottom:2px solid var(--line)}
h3{font-size:16px;margin:26px 0 8px}
.sub{color:var(--muted);margin:0 0 8px}
.meta{color:var(--muted);font-size:13px;font-family:var(--mono)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px;margin:20px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .k{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.card .v{font-size:26px;font-weight:650;margin-top:4px;font-variant-numeric:tabular-nums}
.card .n{font-size:12px;color:var(--muted);margin-top:2px}
table{width:100%;border-collapse:collapse;margin:12px 0;font-size:14px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-weight:600;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
td.num{font-variant-numeric:tabular-nums;font-family:var(--mono)}
.pass{color:var(--ok);font-weight:600}
.fail{color:var(--bad);font-weight:600}
.warnbox{background:#fff8e6;border:1px solid #f0dfae;border-left:4px solid var(--warn);
  border-radius:8px;padding:12px 16px;margin:14px 0;font-size:14px}
.q{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:16px 18px;margin:14px 0}
.q .head{display:flex;justify-content:space-between;gap:12px;align-items:baseline;flex-wrap:wrap}
.q .qq{font-weight:640;font-size:15.5px}
.tag{font-size:11px;padding:2px 8px;border-radius:99px;background:#eef1f4;color:var(--muted);
  font-family:var(--mono);white-space:nowrap}
.ans{margin:10px 0 6px;padding:10px 12px;background:#f7f9f7;border-left:3px solid var(--ok);border-radius:0 6px 6px 0}
.ans.refused{background:#fdf6f4;border-left-color:var(--warn)}
.cites{margin-top:10px;font-size:13.5px}
.cites code{font-family:var(--mono);background:#eef1f4;padding:1px 6px;border-radius:4px;font-size:12.5px}
.checks{margin-top:10px;font-size:13px}
.checks span{font-family:var(--mono);margin-right:12px}
.trace{margin-top:8px;font-size:12.5px;color:var(--muted);font-family:var(--mono)}
.bar{height:7px;background:#eceae4;border-radius:99px;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;background:var(--accent)}
.tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;margin:8px 0}
.tablewrap table{margin:0}
.tablewrap th,.tablewrap td{border:1px solid var(--line);text-align:center}
.tablewrap th{background:#f5f3ee}
.blocks{margin:8px 0;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.blk{padding:8px 12px;border-bottom:1px solid var(--line);font-size:14px;white-space:pre-wrap}
.blk:last-child{border-bottom:0}
.blk .bt{font-family:var(--mono);font-size:11px;color:var(--muted);background:#eef1f4;
  padding:1px 7px;border-radius:99px;margin-right:8px}
.blk .chip{font-size:11px;padding:1px 7px;border-radius:99px;margin-left:8px}
.blk .chip.warn{background:#fdeecd;color:var(--warn)}
h4{font-size:14px;font-weight:600}
details{margin-top:8px}
summary{cursor:pointer;color:var(--muted);font-size:13px}
pre{background:#f5f3ee;border:1px solid var(--line);border-radius:8px;padding:12px;
  overflow:auto;font-size:12.5px;font-family:var(--mono)}
footer{margin-top:64px;padding-top:18px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12.5px}
"""


def _esc(s: object) -> str:
    return html.escape(str(s if s is not None else ""))


def _pct(v: float) -> str:
    return f"{v * 100:.0f}%"


def _metric_card(label: str, value: str, note: str = "") -> str:
    n = f'<div class="n">{_esc(note)}</div>' if note else ""
    return f'<div class="card"><div class="k">{_esc(label)}</div><div class="v">{_esc(value)}</div>{n}</div>'


def _render_grid(grid) -> str:
    """把一个 TableGrid 渲染成**真的 HTML 表格**（保留合并单元格）。

    直接照搬展开后的 `cells` 会丢掉合并信息——评审看到的就是一张
    "平键"重复三遍的怪表，而不是原件里那个横向合并的表头。
    所以这里用 `spans` 还原 colspan/rowspan，被覆盖的坐标跳过不输出。
    """
    if not grid.cells:
        return '<div class="empty">（空表）</div>'

    # 被合并覆盖的坐标 -> 跳过
    covered: set[tuple[int, int]] = set()
    span_at: dict[tuple[int, int], tuple[int, int]] = {}
    for s in getattr(grid, "spans", []) or []:
        span_at[(s.row, s.col)] = (s.rowspan, s.colspan)
        for dr in range(s.rowspan):
            for dc in range(s.colspan):
                if dr or dc:
                    covered.add((s.row + dr, s.col + dc))

    rows_html: list[str] = []
    for r, row in enumerate(grid.cells):
        cells_html: list[str] = []
        for c, value in enumerate(row):
            if (r, c) in covered:
                continue
            rs, cs = span_at.get((r, c), (1, 1))
            attrs = ""
            if rs > 1:
                attrs += f' rowspan="{rs}"'
            if cs > 1:
                attrs += f' colspan="{cs}"'
            tag = "th" if r < grid.n_header_rows else "td"
            cells_html.append(f"<{tag}{attrs}>{_esc(value)}</{tag}>")
        rows_html.append("<tr>" + "".join(cells_html) + "</tr>")

    flag = (
        '<div class="warnbox" style="margin:8px 0">'
        "该表结构解析**降级**：行列可能不对齐，原始 HTML 已保留在产物中。</div>"
        if grid.parse_degraded
        else ""
    )
    return (
        f'{flag}<div class="tablewrap"><table>{"".join(rows_html)}</table></div>'
        f'<div class="meta" style="margin-top:6px">{grid.n_rows} 行 × {grid.n_cols} 列'
        f'　表头 {grid.n_header_rows} 行　合并块 {len(getattr(grid, "spans", []) or [])} 处</div>'
    )


def _render_index_layer(rep: "EvalReport") -> str:
    """渲染**索引层**（上下文可信度）指标。

    为什么排在最前面
    ----------------
    检索层与生成层的每一个指标，都建立在"检索到的内容是对的"这个前提上。
    扫描件场景里这个前提恰恰最不牢靠——识别错一个数字，
    后面检索得再准、生成得再规范，答案也是错的，而且**错得毫无痕迹**。
    所以这一层先报：它决定了后面那些数字有多少可信度。
    """
    ocr = getattr(rep, "ocr", None) or {}
    if not ocr or not ocr.get("pages_evaluated"):
        return (
            "<h2>① 索引层（上下文可信度）</h2>"
            '<div class="warnbox">本次未提供人工转写基准，'
            "<b>这一层没有度量</b>——报告里不会出现 CER / 表格单元格准确率。"
            "这不代表识别没有问题，只代表没有量。"
            "加上 <code>--transcription data/golden/transcription.md</code> 即可计算。</div>"
        )

    out: list[str] = ["<h2>① 索引层（上下文可信度）——先行报告</h2>"]
    out.append(
        "<p class='sub'>这一层决定了后面两层的数字有多少可信度："
        "检索与生成都建立在「检索到的内容是对的」这个前提上，"
        "而扫描件场景里它最不牢靠。</p>"
    )

    cards: list[str] = []
    if "character_accuracy" in ocr:
        cards.append(_metric_card("字符准确率 (1−CER)", _pct(ocr["character_accuracy"]),
                                  "与人工双读转写基准比对"))
    if "cer_normalized" in ocr:
        cards.append(_metric_card("归一化 CER", _pct(ocr["cer_normalized"]),
                                  "去标点/繁简归一后"))
    if "cer_strict" in ocr:
        cards.append(_metric_card("符号敏感 CER", _pct(ocr["cer_strict"]),
                                  "保留标点与单位，暴露符号级错误"))
    if "numeric_field_recall" in ocr:
        cards.append(_metric_card("数值字段召回", _pct(ocr["numeric_field_recall"]),
                                  "数字漏读会直接导致答错"))
    if "table_cell_accuracy" in ocr:
        cards.append(_metric_card("表格单元格准确率", _pct(ocr["table_cell_accuracy"]),
                                  "按语义位置取格比对"))
    out.append(f'<div class="cards">{"".join(cards)}</div>')

    out.append(
        f"<p class='sub'>评估了 <b>{ocr['pages_evaluated']}</b> 页；"
        f"对照方法：<code>{_esc(ocr.get('method', ''))}</code>。"
        "参考基准是人工按页抽样的节选（非全文），所以采用逐行定位对齐，"
        "而非整段直接比对——后者会把整页多出的内容全算成插入错误。</p>"
    )

    rows: list[str] = []
    for e in ocr.get("per_page", []):
        if not e.get("evaluated"):
            rows.append(
                f"<tr><td>{e.get('page_no')}</td><td colspan='4' class='muted'>"
                f"未评估：{_esc(e.get('reason', ''))}</td></tr>"
            )
            continue
        if e.get("kind") == "text":
            rows.append(
                f"<tr><td>{e['page_no']}</td><td>正文</td>"
                f"<td>{_pct(e.get('cer_normalized', 0))}</td>"
                f"<td>{_pct(e.get('cer_strict', 0))}</td>"
                f"<td>{_pct(e.get('numeric_field_recall', 0))}</td></tr>"
            )
        else:
            rows.append(
                f"<tr><td>{e['page_no']}</td><td>表格</td>"
                f"<td colspan='2'>{e.get('cells_correct')}/{e.get('cells_compared')} 格正确</td>"
                f"<td>{_pct(e.get('cell_accuracy', 0))}</td></tr>"
            )
    if rows:
        out.append(
            "<table><tr><th>页</th><th>类型</th><th>归一化 CER</th>"
            "<th>符号敏感 CER</th><th>数值召回 / 单元格准确率</th></tr>"
            + "".join(rows) + "</table>"
        )
    return "".join(out)


def _render_parse_section(rep: "EvalReport") -> str:
    """「PDF 解析结果」小节：正文 + 表格。

    作业的演示要求第 2 条明确要"PDF 解析结果，**包括正文和表格**"。
    这一节就是那个要求的落点——没有它，报告只证明了"问答跑通了"，
    却看不到**究竟从 PDF 里读出了什么**，而后者才是这份交付的核心。
    """
    pages = getattr(rep, "pages", None) or []
    if not pages:
        return (
            "<h2>PDF 解析结果</h2>"
            '<div class="warnbox">本次运行未附带解析产物（例如从缓存重建的报告），'
            "因此无法展示正文与表格。请重跑 <code>docqa eval</code> 以生成完整报告。</div>"
        )

    manifest = getattr(rep, "manifest", None) or {}
    ocr_meta = manifest.get("ocr", {})
    render_meta = manifest.get("render", {})
    failed = set(ocr_meta.get("pages_failed") or [])

    out: list[str] = ["<h2>PDF 解析结果</h2>"]
    out.append(
        f'<p class="meta">引擎 <code>{_esc(ocr_meta.get("engine"))}</code>'
        f'　模型 <code>{_esc(ocr_meta.get("model"))}</code>'
        f'　共 {len(pages)} 页　识别失败 {len(failed)} 页</p>'
    )

    images = {im.get("page_no"): im for im in (render_meta.get("images") or [])}
    total_blocks = sum(len(p.blocks) for p in pages)
    total_tables = sum(len(p.tables) for p in pages)
    out.append('<div class="cards">')
    out.append(_metric_card("解析页数", str(len(pages))))
    out.append(_metric_card("文本块", str(total_blocks)))
    out.append(_metric_card("表格", str(total_tables)))
    out.append(_metric_card("识别失败页", str(len(failed))))
    out.append("</div>")

    for page in pages:
        im = images.get(page.page_no, {})
        header_bits = [f"第 {page.page_no} 页"]
        if im:
            header_bits.append(f"{im.get('width')}×{im.get('height')}px")
            header_bits.append(f"{im.get('dpi', 0):.0f} DPI")
            header_bits.append("位精确取图" if im.get("native") else "光栅化")
        status = (
            '<span class="fail">识别失败</span>'
            if page.page_no in failed or page.failed
            else f'{len(page.blocks)} 块 / {len(page.tables)} 表'
        )
        out.append(
            f'<h3>{" · ".join(_esc(b) for b in header_bits)}'
            f'<span class="tag" style="margin-left:10px">{status}</span></h3>'
        )

        if page.page_no in failed or page.failed:
            out.append(
                f'<div class="warnbox">该页识别失败：{_esc(page.error or "未提供原因")}<br>'
                f"凡是证据涉及本页的问题，都会以 "
                f"<code>extraction_failed</code> 拒答——"
                f"<b>绝不用不完整的证据作答</b>。</div>"
            )
            continue

        # --- 表格：渲染成真表格 ---
        for grid in page.tables:
            out.append(f'<h4 style="margin:14px 0 4px">{_esc(grid.caption or grid.table_id)}</h4>')
            out.append(_render_grid(grid))

        # --- 正文：按块列出 ---
        if page.blocks:
            items: list[str] = []
            for b in sorted(page.blocks, key=lambda x: x.order):
                if b.type == "table":
                    continue  # 表格已单独渲染
                flags = ""
                if b.uncertain:
                    flags += '<span class="chip warn">模型标不确定</span>'
                if b.needs_review:
                    flags += '<span class="chip warn">跨引擎不一致</span>'
                label = (
                    f'<code>{_esc(b.clause_label)}</code> ' if b.clause_label else ""
                )
                items.append(
                    f'<div class="blk"><span class="bt">{_esc(b.type)}</span>'
                    f"{label}{_esc(b.text)}{flags}</div>"
                )
            out.append('<div class="blocks">' + "".join(items) + "</div>")

    return "".join(out)


def render_html_report(rep: "EvalReport") -> str:
    """把 EvalReport 渲染成自包含的 HTML。"""
    m = rep.metrics
    ci = rep.intervals

    def ci_str(key: str) -> str:
        v = ci.get(key)
        if not v:
            return ""
        return f"95% CI [{_pct(v[0])}, {_pct(v[1])}]"

    parts: list[str] = []
    parts.append(f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>docqa 评估报告 · {_esc(rep.doc_id)}</title>
<style>{_CSS}</style></head><body><div class="wrap">""")

    parts.append(f"""<h1>扫描版 PDF 问答系统 · 评估报告</h1>
<p class="sub">文档 <b>{_esc(rep.doc_id)}</b> ｜ 场景 <code>{_esc(rep.domain)}</code>
 ｜ OCR 引擎 <code>{_esc(rep.engine)}</code></p>
<p class="meta">耗时 {rep.elapsed_s}s ｜ 共 {m.get('items', 0)} 题</p>""")

    # --- 核心指标 ---
    parts.append('<div class="cards">')
    parts.append(_metric_card("题目数", str(m.get("items", 0))))
    parts.append(_metric_card("通过", f"{m.get('passed', 0)}", ci_str("accuracy")))
    parts.append(_metric_card("准确率", _pct(m.get("accuracy", 0)), ci_str("accuracy")))
    parts.append(_metric_card("拒答精确率", _pct(m.get("refusal_precision", 0)),
                              ci_str("refusal_precision")))
    parts.append(_metric_card("拒答召回率", _pct(m.get("refusal_recall", 0)),
                              ci_str("refusal_recall")))
    parts.append(_metric_card("上下文利用率", _pct(m.get("mean_context_utilization", 0)),
                              "被引 chunk / 召回 chunk"))
    parts.append(_metric_card("平均尝试轮次", f"{m.get('mean_attempts', 0):.2f}",
                              "自检触发修复的频次"))
    if m.get("total_cost_usd"):
        parts.append(_metric_card("总成本", f"${m['total_cost_usd']:.4f}"))
    parts.append("</div>")

    # --- 口径说明 ---
    parts.append(
        '<div class="warnbox"><b>关于这些数字的可信度。</b>'
        '每个比例都附了 Wilson 95% 置信区间。本报告样本量小，'
        '「100%」往往只反映样本不足——例如 n=3 全对时，置信下界仅 44%。'
        '请结合区间与样本量解读，不要只看点估计。</div>'
    )

    for note in rep.notes:
        parts.append(f'<div class="warnbox">{_esc(note)}</div>')

    # 索引层排在 PDF 解析结果之前：它决定了后面所有数字的可信度
    parts.append(_render_index_layer(rep))
    parts.append(_render_parse_section(rep))

    # --- 分题型 ---
    if rep.per_category:
        parts.append("<h2>按题型分组</h2>")
        parts.append("<p class='sub'>总体准确率会被数量多的题型拉平，掩盖短板，所以必须分组看。</p>")
        parts.append("<table><tr><th>题型</th><th>n</th><th>通过</th><th>准确率</th>"
                     "<th>95% Wilson CI</th></tr>")
        for cat, d in rep.per_category.items():
            lo, hi = d["ci"]
            parts.append(
                f"<tr><td><code>{_esc(cat)}</code></td><td class='num'>{d['n']}</td>"
                f"<td class='num'>{d['passed']}</td><td class='num'>{_pct(d['accuracy'])}</td>"
                f"<td class='num'>[{_pct(lo)}, {_pct(hi)}]</td></tr>"
            )
        parts.append("</table>")
        parts.append('<div class="bar"><i style="width:'
                     f'{_pct(m.get("accuracy", 0))}"></i></div>')

    # --- 对抗变异 ---
    adv = rep.adversarial
    if adv:
        parts.append("<h2>自检有效性：对抗变异检出率</h2>")
        parts.append(
            "<p class='sub'>对正确答案注入**已知的**错误，看自检能否抓住。"
            "这是「我做了自检」这句话唯一能被证伪的方式。</p>"
        )
        parts.append("<table><tr><th>变异类型</th><th>检出 / 总数</th><th>检出率</th>"
                     "<th>由哪条检查负责</th></tr>")
        resp = adv.get("responsibility", {})
        for kind, d in adv.get("by_kind", {}).items():
            cls = "pass" if d["rate"] >= 0.9 else ("warn" if d["rate"] >= 0.5 else "fail")
            parts.append(
                f"<tr><td><code>{_esc(kind)}</code></td>"
                f"<td class='num'>{d['detected']} / {d['total']}</td>"
                f"<td class='num {cls}'>{_pct(d['rate'])}</td>"
                f"<td>{_esc(resp.get(kind, ''))}</td></tr>"
            )
        parts.append("</table>")
        parts.append(
            f"<p class='sub'>总体检出率 <b>{_pct(adv.get('overall_detection_rate', 0))}</b>"
            f"（共 {adv.get('total_mutations', 0)} 个变异）。"
            f"检出率低的类型不代表自检没用，而说明**该类错误超出了现有检查的设计范围**——"
            f"例如纯文字编造需要句级支撑度或人工复核，报告里已如实标出。</p>"
        )

    # --- 逐题 ---
    parts.append("<h2>逐题明细</h2>")
    for d in rep.per_item:
        ok = d.get("passed")
        badge = "pass" if ok else "fail"
        extra = ""
        if d.get("missing_facts"):
            extra += f"<div class='fail'>缺失关键事实：{_esc('、'.join(d['missing_facts']))}</div>"
        if d.get("forbidden_hits"):
            extra += f"<div class='fail'>出现禁止内容：{_esc('、'.join(d['forbidden_hits']))}</div>"
        if d.get("failure"):
            extra += f"<div class='fail'>{_esc(d['failure'])}</div>"
        if d.get("refused") and d.get("reason"):
            extra += f"<div class='meta'>拒答理由：{_esc(d['reason'])}"
            if d.get("refusal_correct") is not None:
                extra += ("　（拒答正确）" if d.get("refusal_correct") else "　（拒答错误）")
            extra += "</div>"

        cites = ""
        for c in d.get("citation_pages", []) or []:
            cites += f"<code>p.{_esc(c)}</code> "

        checks = ""
        for name, okc in (d.get("checks") or {}).items():
            mark = "✓" if okc else "✗"
            cls = "pass" if okc else "fail"
            checks += f"<span class='{cls}'>{mark} {_esc(name)}</span>"

        parts.append(f"""<div class="q">
<div class="head"><span class="qq">{_esc(d.get('question', d.get('qid', '')))}</span>
<span class="tag">{_esc(d.get('category',''))} · {badge.upper()} · 尝试 {d.get('attempts',1)} 轮</span></div>
<div class="ans {'refused' if d.get('refused') else ''}">{_esc(d.get('answer','')) or '<i>（无答案）</i>'}</div>
{extra}
<div class="cites">引用页码：{cites or '<i>无</i>'}　支撑度 {d.get('support_score', 0):.2f}
　上下文利用率 {_pct(d.get('context_utilization', 0))}</div>
<div class="checks">{checks}</div>
<div class="trace">轨迹：{' → '.join(d.get('trace_steps', []))}</div>
</div>""")

    # --- 原始数据 ---
    parts.append("<h2>原始结果</h2><details><summary>展开 JSON</summary><pre>"
                 + _esc(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2)[:20000])
                 + "</pre></details>")

    parts.append("""<footer>
由 <code>docqa eval</code> 生成。所有指标均为确定性实现，不依赖评委模型；
每个比例附 Wilson 95% 置信区间与样本量。
</footer></div></body></html>""")

    return "".join(parts)
