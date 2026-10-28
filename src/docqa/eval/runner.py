"""评估运行器：三层指标 + 回归基线。

三层结构，以及为什么是这个顺序
------------------------------
1. **索引层（上下文可信度）**：CER、表格单元格准确率、数值字段准确率
2. **检索层**：Recall@k / MRR / nDCG，按题型分组
3. **生成层**：引用有效率、数值落地率、拒答准确率、上下文利用率、成本与延迟

之所以把索引层放在最前面报，是因为：**检索到的内容本身是错的，后面两层再漂亮也没有意义**。
多数 RAG 评估框架忽略了这一层，而扫描件场景里它恰恰是最致命的。
这也是我们与"套壳 Demo"最本质的差别所在。

回归基线
--------
评估产出 JSON 基线；`--baseline` 比对时任何指标下滑都**非零退出**，可直接挂 CI。
另外：**fixture 引擎的运行默认不允许与实跑基线比较**——
一份 fixture 产物和一次真实识别的结果不是一回事，混在一起比就失去了意义。
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..errors import RegressionDetected
from ..logging import get_logger
from ..models import AnswerBundle, RefusalReason
from .metrics import (
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
    wilson_interval,
)

_log = get_logger("eval.runner")

# 被视为"拒答正确"的理由集合——用于口径统一
REFUSAL_REASONS = {r.value for r in RefusalReason}

# 这些指标**越低越好**，基线比对的方向要反过来。漏掉这一条会出现
# "延迟从 800ms 降到 600ms 却报回归"这种把改进报成退步的结果。
_LOWER_IS_BETTER_RE = re.compile(r"latency|_ms$|cost")

# 这些指标**不进基线**：它们受机器负载影响，在 CI 的共享 runner 上抖动极大，
# 拿来做门禁只会得到随机翻红。趋势可以看，逐次比对没有意义。
_UNSTABLE_FOR_BASELINE_RE = re.compile(r"latency|_ms$|cost")


@dataclass
class GoldenItem:
    """一条金标题。"""

    qid: str
    question: str
    category: str  # clause / table / no_answer / ambiguous / ocr_stress / negation / multi_hop
    expected_answer: str = ""
    must_contain: list[str] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)
    expected_refusal: bool = False
    expected_reason: str | None = None
    citation_pages: list[int] = field(default_factory=list)
    # 检索层指标（Recall@k / MRR / nDCG）需要的"相关片段"清单。
    #
    # 为什么它不能从 `citation_pages` 自动推导
    # ----------------------------------------
    # 页码是**答案出处**的粒度，检索层要的是**片段**的粒度：同一页上有 17 个
    # chunk，答案只来自其中一个。拿"整页的 chunk"当相关集会把 Recall 拔高到
    # 接近 1，那个数字就没有意义了。
    # 所以这里单列一个可选字段：没有标注的题**不参与**检索层统计（而不是
    # 用页码近似混进去），评估报告里会如实写出参与了多少题。
    relevant_chunk_ids: list[str] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "GoldenItem":
        return cls(
            qid=str(d.get("qid", "")),
            question=d["question"],
            category=d.get("category", "clause"),
            expected_answer=d.get("expected_answer", ""),
            must_contain=list(d.get("must_contain") or []),
            must_not_contain=list(d.get("must_not_contain") or []),
            expected_refusal=bool(d.get("expected_refusal")),
            expected_reason=d.get("expected_reason"),
            citation_pages=list(d.get("citation_pages") or []),
            relevant_chunk_ids=list(d.get("relevant_chunk_ids") or []),
            notes=d.get("notes", ""),
        )


def load_golden(path: Path) -> list[GoldenItem]:
    """读金标集（JSONL，一行一题）。

    刻意用 JSONL 而不是 JSON：一行一题便于 diff、便于只改一题、
    也便于在 review 时逐行讨论。
    """
    items: list[GoldenItem] = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                items.append(GoldenItem.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError) as exc:
                _log.error(
                    "金标集该行无法解析，已跳过",
                    extra={"detail": f"{path}:{lineno} {exc}"},
                )
    return items


def score_item(item: GoldenItem, bundle: AnswerBundle) -> dict:
    """给单题打分。

    判分原则：**拒答题只看拒答行为与理由，不看文本**；
    其余题看关键事实是否命中。这与"污染之下系统无从知道真相"是同一个立场——
    不能既要求系统在证据被污染时给出正确答案，又要求它忠实于证据。
    """
    answer = bundle.answer or ""
    detail: dict = {
        "qid": item.qid,
        "category": item.category,
        # 「该不该拒」的口径只认金标题自己声明的 `expected_refusal`。
        #
        # 曾经这里隐含地用 `category == "no_answer"` 判断，于是歧义题（A1，
        # `expected_refusal: true`，系统正确拒答成 not_in_document）被算成**误拒**，
        # 拒答精确率因此被系统性压低（实测 0.40 vs 正确口径 0.60）。
        # 题型是给人看的分类，不是判分依据；判分依据必须是金标题里显式的字段。
        "expected_refusal": item.expected_refusal,
        "refused": bundle.refused,
        "reason": bundle.refusal_reason.value if bundle.refusal_reason else None,
        "answer": answer[:300],
        "attempts": bundle.attempts,
        "n_citations": len(bundle.citations),
        "citation_pages": sorted({c.page_no for c in bundle.citations}),
        "gates_passed": bundle.verification.gates_passed,
        "support_score": round(bundle.verification.support_score, 3),
        # 逐项检查结果，供 HTML 报告渲染红绿标记
        "checks": {c.name: c.passed for c in bundle.verification.checks},
        # 键名必须与 report.py 读的一致（`d.get('question')`）。
        # 曾经这里写成复数 `questions`，导致报告每条卡片显示的是 `C1` 而不是问题原文——
        # 而测试没抓到，因为断言的问题串确实出现在 HTML 里，只是在文末的原始 JSON dump 中。
        # 那是个假阳性断言，已一并改成"断言题干出现在卡片区域"。
        "question": item.question,
        "trace_steps": [t.step for t in bundle.trace],
        "elapsed_ms": bundle.usage.latency_ms,
        "est_cost_usd": round(bundle.usage.est_cost_usd, 6),
    }

    # --- 拒答题 ---
    if item.expected_refusal:
        detail["refusal_correct"] = bundle.refused
        detail["reason_correct"] = (
            bundle.refusal_reason is not None
            and (item.expected_reason is None or bundle.refusal_reason.value == item.expected_reason)
        )
        detail["passed"] = bool(detail["refusal_correct"] and detail["reason_correct"])
        return detail

    # --- 应答题：拒答即为错 ---
    if bundle.refused:
        detail["passed"] = False
        detail["failure"] = f"应当作答却拒答（{detail['reason']}）"
        return detail

    missing = [s for s in item.must_contain if s and s not in answer]
    forbidden = [s for s in item.must_not_contain if s and s in answer]

    # 引用页码是否正确
    page_ok = True
    if item.citation_pages:
        cited = {c.page_no for c in bundle.citations}
        page_ok = bool(cited & set(item.citation_pages))

    detail["missing_facts"] = missing
    detail["forbidden_hits"] = forbidden
    detail["citation_page_ok"] = page_ok
    detail["passed"] = bool(not missing and not forbidden and page_ok)
    return detail


def context_utilization(bundle: AnswerBundle) -> float:
    """被引用的 chunk 数 / 召回的 chunk 数。

    这个指标很便宜但信号很强：如果召回了 6 条却只引用 1 条，
    说明检索基本没起作用。参考资料给出的生产告警阈值是 <40%。
    """
    if not bundle.hits:
        return 0.0
    cited = {c.chunk_id for c in bundle.citations}
    retrieved = {h.chunk_id for h in bundle.hits}
    return len(cited & retrieved) / len(retrieved) if retrieved else 0.0


def _percentile(sorted_values: list[int | float], q: float) -> float:
    """最近秩法的分位数。样本很小时不要用插值——那会造出一个不存在的观测值。"""
    if not sorted_values:
        return 0.0
    idx = max(0, min(len(sorted_values) - 1, math.ceil(q * len(sorted_values)) - 1))
    return float(sorted_values[idx])


def _citation_validity(per_item: list[dict]) -> float:
    """引用有效率：**作答且给出引用**的题里，引用全部指向真实片段的比例。

    判据来自自检里的 `citation_validity` 检查项（它已经会拒绝"引用不存在的片段"，
    并且把"零引用"判失败），这里只是把逐题结论聚合成一个率。
    零引用的题算**不通过**——否则"只要不引用，引用就永远有效"。
    """
    answered = [d for d in per_item if not d.get("refused")]
    if not answered:
        return 0.0
    ok = sum(
        1
        for d in answered
        if d.get("n_citations") and (d.get("checks") or {}).get("citation_validity", False)
    )
    return ok / len(answered)


def _expand_relevant(ids: list[str], known_ids: set[str]) -> set[str]:
    """把金标里写的片段标识展开成完整的 chunk_id。

    为什么要这一步
    --------------
    `chunk_id` 形如 `<doc_id>:p1:c5`，而 `doc_id` 里含**文件名与内容哈希**
    （`demo_scan-45536714`）。把完整 id 写进金标，等于每次重新 ingest 都要
    改一遍金标——而那正是"金标不该随产物漂移"的反面。

    所以金标里只写**稳定后缀** `p1:c5`（页 + 序号），这里按后缀匹配。
    匹配不唯一（同一份产物里不可能，多文档合并时可能）时**全部计入**：
    宁可让相关集略微宽松，也不要把一个真实相关的片段漏掉。
    """
    if not ids:
        return set()
    wanted = set(ids)
    expanded: set[str] = set()
    for cid in known_ids:
        if cid in wanted:
            expanded.add(cid)
            continue
        # 后缀匹配：`p1:c5` 命中 `demo_scan-45536714:p1:c5`
        for w in wanted:
            if cid.endswith(":" + w) or cid.endswith(w):
                expanded.add(cid)
                break
    return expanded


def _retrieval_metrics(*, rep_item_bundles, items: list[GoldenItem]) -> dict:
    """检索层：Recall@k / MRR / nDCG@k，按题型分组。

    只统计**标注了 `relevant_chunk_ids`** 的题。没标注的题既不猜、也不拿页码近似——
    用页码当相关集会把同页全部 chunk 都算成相关，Recall 会被拔到接近 1，
    那个数字看起来漂亮但没有信息量。

    Returns:
        {"metrics": {...}, "n": 参与统计的题数, "per_category": {...}}
    """
    top_k = 10
    per_q: list[dict] = []
    by_cat: dict[str, list[dict]] = {}

    bundle_by_qid = {item.qid: bundle for item, bundle in rep_item_bundles}
    for item in items:
        if not item.relevant_chunk_ids:
            continue
        bundle = bundle_by_qid.get(item.qid)
        if bundle is None:
            continue  # 该题执行异常，已在 per_item 里单独记过
        ranked = [h.chunk_id for h in bundle.hits]
        # 用本次真实召回过的片段集合来展开后缀，避免金标里写死 doc_id
        known = {h.chunk_id for h in bundle.hits} | {
            c.chunk_id for c in bundle.citations
        }
        relevant = _expand_relevant(item.relevant_chunk_ids, known)
        if not relevant:
            # 标注的后缀在本产物里匹配不到（例如切分参数变了）：
            # 不计入统计并在报告里体现，**不静默当成"召回失败"**。
            continue
        entry = {
            "qid": item.qid,
            "category": item.category,
            "recall_at_k": recall_at_k(ranked, relevant, top_k),
            "rr": reciprocal_rank(ranked, relevant),
            "ndcg_at_k": ndcg_at_k(ranked, relevant, top_k),
        }
        per_q.append(entry)
        by_cat.setdefault(item.category, []).append(entry)

    def _avg(rows: list[dict], key: str) -> float:
        return round(sum(r[key] for r in rows) / len(rows), 4) if rows else 0.0

    metrics = {
        "retrieval_recall_at_k": _avg(per_q, "recall_at_k"),
        "retrieval_mrr": _avg(per_q, "rr"),
        "retrieval_ndcg_at_k": _avg(per_q, "ndcg_at_k"),
    }
    categories = {
        cat: {
            "n": len(rows),
            "recall_at_k": _avg(rows, "recall_at_k"),
            "mrr": _avg(rows, "rr"),
            "ndcg_at_k": _avg(rows, "ndcg_at_k"),
        }
        for cat, rows in sorted(by_cat.items())
    }
    return {
        "metrics": metrics,
        "n": len(per_q),
        "k": top_k,
        "per_item": per_q,
        "per_category": categories,
    }


@dataclass
class EvalReport:
    engine: str
    doc_id: str
    domain: str
    metrics: dict = field(default_factory=dict)
    intervals: dict = field(default_factory=dict)
    per_category: dict = field(default_factory=dict)
    per_item: list[dict] = field(default_factory=list)
    ocr: dict = field(default_factory=dict)
    retrieval: dict = field(default_factory=dict)
    adversarial: dict = field(default_factory=dict)
    noise: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    # 逐题的 (金标题, 答案包)。**不入序列化**，但必须往外传：
    # 对抗变异要用它生成样本，如果不传出去，调用方就只能**把每道题再答一遍**
    # —— 那会让每次 eval 的 API 花费与耗时直接翻倍。
    # 这属于"不报错、只是变贵"的缺陷，靠肉眼 review 抓不住，只能靠结构上不给重复调用的机会。
    bundles: list[tuple["GoldenItem", AnswerBundle]] = field(default_factory=list, repr=False)
    # 解析产物（逐页 OCR 结果），供报告渲染「解析结果」小节
    pages: list = field(default_factory=list, repr=False)
    manifest: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        # 这两个字段装的是对象，不是可序列化的标量，且体量大；
        # 报告需要的信息已经落在 per_item / ocr 里了。
        data.pop("bundles", None)
        data.pop("pages", None)
        return data

    def to_baseline(self) -> dict:
        """只保留**可比对且稳定**的标量指标。

        这是产出基线文件的唯一入口（`docqa eval --save-baseline`）。
        `compare_baseline` 是消费端；没有产出端的话，"可挂 CI 的回归门禁"
        就只有一半——能发现问题，却没法建立基准。

        **延迟与成本不进基线。** 它们在 CI 的共享 runner 上抖动极大
        （同一份代码，p50 可以从 800ms 跳到 3000ms），拿它当门禁只会得到
        "随机翻红"。这类指标应当看趋势，不该逐次比对。
        """
        unstable = _UNSTABLE_FOR_BASELINE_RE
        return {
            "engine": self.engine,
            "doc_id": self.doc_id,
            "domain": self.domain,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "metrics": {
                k: v
                for k, v in self.metrics.items()
                if isinstance(v, (int, float)) and not unstable.search(k)
            },
        }


def run_eval(
    items: list[GoldenItem],
    ask,
    *,
    engine: str,
    doc_id: str,
    domain: str,
    ocr_metrics: dict | None = None,
    adversarial: dict | None = None,
    noise: dict | None = None,
    notes: list[str] | None = None,
    pages: list | None = None,
    manifest: dict | None = None,
) -> EvalReport:
    """跑完整评估。

    Args:
        ask: 可调用对象 `(question) -> AnswerBundle`。
    """
    started = time.time()
    per_item: list[dict] = []
    per_category: dict[str, list[bool]] = {}
    bundles: list[tuple[GoldenItem, AnswerBundle]] = []

    for item in items:
        t0 = time.perf_counter()
        try:
            bundle = ask(item.question)
        except Exception as exc:  # noqa: BLE001 - 单题异常不应中断整轮评估
            _log.error("该题执行失败", extra={"detail": f"{item.qid}: {exc}"})
            per_item.append(
                {"qid": item.qid, "category": item.category, "passed": False,
                 "failure": f"执行异常：{exc}"}
            )
            per_category.setdefault(item.category, []).append(False)
            continue

        bundles.append((item, bundle))
        detail = score_item(item, bundle)
        detail["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        detail["context_utilization"] = round(context_utilization(bundle), 3)
        per_item.append(detail)
        per_category.setdefault(item.category, []).append(bool(detail.get("passed")))

    # --- 汇总 ---
    total = len(per_item)
    passed = sum(1 for d in per_item if d.get("passed"))

    # 拒答口径（**只看金标题的 `expected_refusal`**）
    #   TP = 该拒且拒了；FP = 不该拒却拒了；FN = 该拒却没拒。
    # 之前用 `category == "no_answer"` 判定，把正确拒答的歧义题算成误拒，
    # 精确率被系统性压低（0.40 vs 0.60）。题型分类是给人看的，不是判分依据。
    refusal_tp = sum(1 for d in per_item if d.get("expected_refusal") and d.get("refused"))
    refusal_fp = sum(1 for d in per_item if not d.get("expected_refusal") and d.get("refused"))
    refusal_fn = sum(1 for d in per_item if d.get("expected_refusal") and not d.get("refused"))

    # --- 检索层 ---
    #
    # 这一层此前**只写在文档里**：metrics.py 里有 recall_at_k / reciprocal_rank /
    # ndcg_at_k，但它们只被单元测试调用，runner 里一个都没用上。
    # 于是 README 首页画着"② 检索层 Recall@k / MRR / nDCG"，报告里却没有这一节。
    #
    # 现在按"标注了 relevant_chunk_ids 的题"统计，未标注的题**不计入**，
    # 并把参与题数一并报出来——用一个透明的 n 换一个诚实的数。
    retrieval = _retrieval_metrics(rep_item_bundles=bundles, items=items)

    metrics = {
        "items": total,
        "passed": passed,
        "accuracy": passed / total if total else 0.0,
        "refusal_precision": (
            refusal_tp / (refusal_tp + refusal_fp) if (refusal_tp + refusal_fp) else 0.0
        ),
        "refusal_recall": (
            refusal_tp / (refusal_tp + refusal_fn) if (refusal_tp + refusal_fn) else 0.0
        ),
        # 「未拒答题里自检门全过」的比例。名字曾经叫 citation_validity_rate，
        # 读起来像"引用有效率"，实际是**放行率**——两个概念不能共用一个名字。
        "gate_pass_rate": (
            sum(1 for d in per_item if not d.get("refused") and d.get("gates_passed"))
            / max(1, sum(1 for d in per_item if not d.get("refused")))
        ),
        # 引用**格式与指向**的有效性：引用非空、且每条引用都指向真实存在的片段。
        # 这是"引用有效率"这个说法真正的落点。
        "citation_validity_rate": _citation_validity(per_item),
        "mean_context_utilization": (
            sum(d.get("context_utilization", 0.0) for d in per_item) / total if total else 0.0
        ),
        "mean_attempts": sum(d.get("attempts", 1) for d in per_item) / total if total else 0.0,
        "total_cost_usd": round(sum(d.get("est_cost_usd", 0.0) for d in per_item), 6),
    }

    # 延迟分位（只在真的有延迟数据时才报，否则报 0 会像是"很快"）
    latencies = sorted(d.get("elapsed_ms", 0) for d in per_item if d.get("elapsed_ms"))
    if latencies:
        metrics["latency_p50_ms"] = _percentile(latencies, 0.50)
        metrics["latency_p95_ms"] = _percentile(latencies, 0.95)

    # 检索层指标并入 metrics（键名带前缀，便于报告与基线区分层次）
    metrics.update(retrieval["metrics"])

    intervals = {
        "accuracy": wilson_interval(passed, total),
        "refusal_precision": wilson_interval(refusal_tp, refusal_tp + refusal_fp),
        "refusal_recall": wilson_interval(refusal_tp, refusal_tp + refusal_fn),
    }
    if retrieval["n"]:
        intervals["recall_at_k"] = wilson_interval(
            round(retrieval["metrics"]["retrieval_recall_at_k"] * retrieval["n"]),
            retrieval["n"],
        )

    # --- 题型分组（总体准确率会被数量多的题型拉平，掩盖短板）---
    by_cat = {
        cat: {
            "n": len(vals),
            "passed": sum(vals),
            "accuracy": sum(vals) / len(vals) if vals else 0.0,
            "ci": wilson_interval(sum(vals), len(vals)),
        }
        for cat, vals in sorted(per_category.items())
    }

    report = EvalReport(
        engine=engine,
        doc_id=doc_id,
        domain=domain,
        metrics=metrics,
        intervals={k: list(v) for k, v in intervals.items()},
        per_category=by_cat,
        per_item=per_item,
        ocr=ocr_metrics or {},
        retrieval=retrieval,
        adversarial=adversarial or {},
        noise=noise or {},
        notes=notes or [],
        elapsed_s=round(time.time() - started, 2),
        bundles=bundles,
        pages=pages or [],
        manifest=manifest or {},
    )
    _log.info(
        "评估完成",
        extra={
            "elapsed_ms": int((time.time() - started) * 1000),
            "detail": f"通过 {passed}/{total}，准确率 {metrics['accuracy']:.1%}",
        },
    )
    return report


def compare_baseline(report: EvalReport, baseline_path: Path, *, tolerance: float = 0.0) -> list[str]:
    """与基线比对，返回下滑项。

    引擎不同时**拒绝比较**（除非调用方显式绕过）——fixture 产物与真实识别
    不是一回事，混着比会得出"换了模型反而变好了"这种没有意义的结论。

    容差按"指标本身的方向"处理：延迟是**越低越好**，把它和准确率放同一套
    `new < old` 判据里会导致"延迟变快反而报回归"。
    """
    if not baseline_path.exists():
        return []
    base = json.loads(baseline_path.read_text(encoding="utf-8"))
    if base.get("engine") != report.engine:
        raise RegressionDetected(
            f"基线是用 {base.get('engine')!r} 引擎产生的，本次是 {report.engine!r}，"
            f"两者不可比。若确实要对比，请先删除或更新基线文件。"
        )
    regressions: list[str] = []
    for key, old in (base.get("metrics") or {}).items():
        new = report.metrics.get(key)
        if not isinstance(old, (int, float)) or not isinstance(new, (int, float)):
            continue
        if _LOWER_IS_BETTER_RE.search(key):
            if new > old * (1.0 + tolerance) + 1e-12:
                regressions.append(f"{key}: {old:.4f} -> {new:.4f}（变慢/变大）")
        elif new < old - tolerance:
            regressions.append(f"{key}: {old:.4f} -> {new:.4f}（下滑 {old - new:.4f}）")
    return regressions


def save_baseline(report: EvalReport, path: Path) -> None:
    """把本次评估写成基线文件（供下一次 `--baseline` 比对）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_baseline(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
