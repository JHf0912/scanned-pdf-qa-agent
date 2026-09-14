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
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..errors import RegressionDetected
from ..logging import get_logger
from ..models import AnswerBundle, RefusalReason
from .metrics import (
    cer,
    ndcg_at_k,
    numeric_field_accuracy,
    recall_at_k,
    reciprocal_rank,
    table_cell_accuracy,
    wilson_interval,
)

_log = get_logger("eval.runner")

# 被视为"拒答正确"的理由集合——用于口径统一
REFUSAL_REASONS = {r.value for r in RefusalReason}


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
        """只保留可比对的标量指标。"""
        return {
            "engine": self.engine,
            "doc_id": self.doc_id,
            "metrics": {k: v for k, v in self.metrics.items() if isinstance(v, (int, float))},
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
    refused = [d for d in per_item if d.get("refused")]
    expected_refusals = [d for d in per_item if d["category"] in ("no_answer",)]
    actual_refusals = [d for d in per_item if d.get("refused")]

    refusal_tp = sum(1 for d in actual_refusals if d["category"] == "no_answer")
    refusal_fp = sum(1 for d in actual_refusals if d["category"] != "no_answer")
    refusal_fn = sum(1 for d in per_item if d["category"] == "no_answer" and not d.get("refused"))

    metrics = {
        "items": total,
        "passed": passed,
        "accuracy": passed / total if total else 0.0,
        "refusal_precision": refusal_tp / (refusal_tp + refusal_fp) if (refusal_tp + refusal_fp) else 0.0,
        "refusal_recall": refusal_tp / (refusal_tp + refusal_fn) if (refusal_tp + refusal_fn) else 0.0,
        "citation_validity_rate": (
            sum(1 for d in per_item if not d.get("refused") and d.get("gates_passed"))
            / max(1, sum(1 for d in per_item if not d.get("refused")))
        ),
        "mean_context_utilization": (
            sum(d.get("context_utilization", 0.0) for d in per_item) / total if total else 0.0
        ),
        "mean_attempts": sum(d.get("attempts", 1) for d in per_item) / total if total else 0.0,
        "total_cost_usd": round(sum(d.get("est_cost_usd", 0.0) for d in per_item), 6),
    }

    intervals = {
        "accuracy": wilson_interval(passed, total),
        "refusal_precision": wilson_interval(refusal_tp, refusal_tp + refusal_fp),
        "refusal_recall": wilson_interval(refusal_tp, refusal_tp + refusal_fn),
    }

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
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            if new < old - tolerance:
                regressions.append(f"{key}: {old:.4f} -> {new:.4f}（下滑 {old - new:.4f}）")
    return regressions
