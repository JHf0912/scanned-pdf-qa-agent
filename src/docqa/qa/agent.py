"""问答主流程：检索 → 起草 → 自检 → 修复 → 复检 → 放行或拒答。

这个模块存在的理由
------------------
一个"检索完直接生成"的流水线，它的自检只能写进输出——报告里多一行
"已检查：通过"。那种自检**不可能失败**，因此也不可能有用。

这里把自检接成**控制流**：不过门就带着具体的失败原因去修复，
修完再检；修复轮次用尽才拒答。于是：

- 自检在结构上不可能走过场——它真的会改变系统做什么；
- 轨迹里留下 `verify FAIL(numeric_grounding) → repair → verify PASS`
  这样的证据，评审可以直接看到自检起了作用；
- "Agent 在哪里"这个问题有了具体答案：就在这个有界循环里。

修复手段（按代价从低到高）
--------------------------
1. **改写查询**：把失败信息回灌给生成侧，要求只用证据中的数值；
2. **扩大召回**：证据不足时提高 top_k 再检一次；
3. **走表格路由**：数值类失败时强制提升表格 chunk 权重。

轮次上限是硬要求。没有上限的自我修复会把成本打爆，
而且会在"模型反复改不好"的情况下无限循环。
"""

from __future__ import annotations

import time

from ..config import Config, DomainProfile
from ..index import HybridIndex
from ..llm import LLMProvider
from ..logging import get_logger
from ..models import (
    AnswerBundle,
    Citation,
    RefusalReason,
    TraceStep,
    Usage,
    VerificationReport,
)
from .answer import Draft, draft_answer, build_evidence_block
from .policy import (
    decide,
    detect_ambiguity,
    evidence_coverage,
    is_existence_question,
    is_in_document_negative,
)
from .verify import verify

_log = get_logger("qa.agent")


class QaAgent:
    """有界修复循环的问答 Agent。"""

    def __init__(
        self,
        index: HybridIndex,
        provider: LLMProvider,
        cfg: Config,
        profile: DomainProfile,
        *,
        failed_pages: set[int] | None = None,
        doc_id: str = "",
    ):
        self.index = index
        self.provider = provider
        self.cfg = cfg
        self.profile = profile
        self.failed_pages = failed_pages or set()
        self.doc_id = doc_id
        # 文档里真实存在的条号，供条号一致性检查区分"条号"与"数据值"
        self._known_clause_labels = {
            c.clause_label.strip() for c in index.chunks if c.clause_label
        }

    # ---------------------------------------------------------------- 主流程

    def answer(self, question: str) -> AnswerBundle:
        started = time.perf_counter()
        trace: list[TraceStep] = []
        usage = Usage()
        qa_cfg = self.profile.qa
        attempts = 0
        draft: Draft | None = None
        report = None
        hits = []
        result = None
        current_k = self.profile.retrieve.top_k

        for attempt in range(1, qa_cfg.max_attempts + 1):
            attempts = attempt

            # --- 检索 ---
            t0 = time.perf_counter()
            result = self.index.search(question, k=current_k)
            evidence = [self.index.get(h.chunk_id) for h in result.hits]
            evidence = [c for c in evidence if c is not None]
            hits = result.hits
            trace.append(
                TraceStep(
                    step="retrieve",
                    detail=(
                        f"第 {attempt} 轮：召回 {len(evidence)} 个片段"
                        f"（最高分 {result.top_score:.4f}"
                        f"{'，已启用表格路由' if result.table_routed else ''}）"
                    ),
                    ms=int((time.perf_counter() - t0) * 1000),
                )
            )

            coverage = evidence_coverage(question, evidence)

            # --- 生成之前的两次预判，避免白花一次模型调用 ---
            #
            # 这两项都只依赖检索结果，不需要调用模型。放在生成前，是因为
            # "证据明显不相关"和"问题有歧义"这两种情况，生成再多轮也救不回来——
            # 让模型对不相关的证据硬答，只会得到一段引不出处的文字，
            # 然后被自检拦下，最终仍然拒答，但已经浪费了一次调用。
            candidates = detect_ambiguity(
                question, [(c, h.score) for c, h in zip(evidence, result.hits)]
            )
            if candidates:
                trace.append(
                    TraceStep(
                        step="ambiguity",
                        detail=f"检出 {len(candidates)} 个取值冲突的候选，转消歧",
                    )
                )
                decision = decide(
                    cfg=qa_cfg,
                    top_score=result.top_score,
                    second_score=result.second_score,
                    gates_passed=True,
                    failed_gate_names=[],
                    evidence_chunks=evidence,
                    failed_pages=self.failed_pages,
                    candidates=candidates,
                    coverage=coverage,
                    existence_question=is_existence_question(question),
                )
                return self._finalize(
                    question, None, [], decision, report, hits, trace,
                    usage, attempts, started,
                )

            # 覆盖率过低 = 文档根本没覆盖这个问题，直接拒答。
            # 例外：「是否规定 X」型问句不走这条路——它们的正确答案常常就是
            # 「未规定」，属于有效答案而非答不了，必须交给生成侧处理。
            if (
                attempt == 1
                and coverage < qa_cfg.min_coverage
                and not is_existence_question(question)
            ):
                decision = decide(
                    cfg=qa_cfg,
                    top_score=result.top_score,
                    second_score=result.second_score,
                    gates_passed=True,
                    failed_gate_names=[],
                    evidence_chunks=evidence,
                    failed_pages=self.failed_pages,
                    candidates=[],
                    coverage=coverage,
                )
                if decision.refused:
                    trace.append(
                        TraceStep(
                            step="precheck",
                            detail=f"覆盖率 {coverage:.0%} 低于阈值，未调用模型即拒答",
                        )
                    )
                    return self._finalize(
                        question, None, [], decision, report, hits, trace,
                        usage, attempts, started,
                    )

            # --- 起草 ---
            t0 = time.perf_counter()
            repair_hint = getattr(self, "_repair_hint", None)
            draft = draft_answer(
                question,
                evidence,
                self.provider,
                self.cfg.llm,
                temperature=qa_cfg.temperature,
                max_tokens=qa_cfg.max_tokens,
                repair_hint=repair_hint,
            )
            usage = usage + draft.usage
            trace.append(
                TraceStep(
                    step="draft",
                    detail=(
                        f"第 {attempt} 轮：{'抽取式' if draft.extractive else '模型生成'}"
                        f"，{len(draft.text)} 字，{len(draft.citations)} 条引用"
                    ),
                    ms=int((time.perf_counter() - t0) * 1000),
                    usage=draft.usage,
                )
            )

            # --- 自检 ---
            evidence_map = {c.chunk_id: c.text for c in evidence}
            report = verify(
                draft.text,
                draft.citations,
                retrieved_ids={h.chunk_id for h in result.hits},
                known_ids={c.chunk_id for c in self.index.chunks},
                evidence_by_chunk=evidence_map,
                support_gate=qa_cfg.support_score_gate,
                # 传入全文档的条号集合：条号与表格数值形态相同（都是 3.6 / 2.5），
                # 不给这份名单，检查会把每个小数都当成条号声明。
                known_clause_labels=self._known_clause_labels,
            )
            trace.append(
                TraceStep(
                    step="verify",
                    detail=(
                        f"第 {attempt} 轮：{'全部通过' if report.gates_passed else '未通过 ' + '、'.join(c.name for c in report.failed_gates)}"
                        f"（支撑度 {report.support_score:.2f}）"
                    ),
                )
            )

            if report.gates_passed:
                decision = decide(
                    cfg=qa_cfg,
                    top_score=result.top_score,
                    second_score=result.second_score,
                    gates_passed=True,
                    failed_gate_names=[],
                    evidence_chunks=evidence,
                    failed_pages=self.failed_pages,
                    candidates=[],
                    coverage=coverage,
                    existence_question=is_existence_question(question),
                )
                return self._finalize(
                    question, draft, draft.citations, decision, report, hits,
                    trace, usage, attempts, started,
                )

            # --- 修复 ---
            self._repair_hint = self._make_repair_hint(report, draft, evidence)
            if report and any(c.name == "numeric_grounding" for c in report.failed_gates):
                # 数值落地失败通常意味着"证据给少了"，扩大召回往往比让模型重写更有效
                current_k = min(current_k + 3, current_k * 2)
            trace.append(
                TraceStep(
                    step="repair",
                    detail=f"第 {attempt} 轮：{self._repair_hint[:120]}",
                )
            )
            _log.info(
                "自检未通过，进入修复",
                extra={"detail": f"attempt={attempt} hint={self._repair_hint[:160]}"},
            )

        # --- 修复轮次用尽 ---
        self._repair_hint = None
        evidence = [self.index.get(h.chunk_id) for h in hits]
        evidence = [c for c in evidence if c is not None]
        decision = decide(
            cfg=qa_cfg,
            top_score=result.top_score if result else 0.0,
            second_score=result.second_score if result else 0.0,
            gates_passed=False,
            failed_gate_names=[c.name for c in report.failed_gates] if report else [],
            evidence_chunks=evidence,
            failed_pages=self.failed_pages,
            candidates=[],
            coverage=evidence_coverage(question, evidence),
            existence_question=is_existence_question(question),
        )
        trace.append(
            TraceStep(
                step="give_up",
                detail=f"修复 {attempts} 轮后仍未通过自检，按策略拒答",
            )
        )
        return self._finalize(
            question, draft, draft.citations if draft else [], decision, report,
            hits, trace, usage, attempts, started,
        )

    # ---------------------------------------------------------------- 内部

    def _make_repair_hint(self, report, draft: Draft, evidence: list) -> str:
        """把自检失败翻译成给生成侧的修正指令。

        关键：要**具体**。"答案不准确"这种提示没有用；
        "数值 1.5 在证据中不存在，请只使用证据中出现的数值"才可执行。
        """
        parts: list[str] = []
        for check in report.failed_gates:
            if check.name == "numeric_grounding":
                detail = "；".join(check.offenders[:3])
                parts.append(
                    f"你使用了证据中不存在的数值或文号：{detail}。"
                    f"请只使用证据中**逐字出现**的数值；如果证据里没有这个信息，"
                    f"就直接说明文档中未提及。"
                )
            elif check.name == "citation_validity":
                parts.append(
                    f"引用无效：{'；'.join(check.offenders[:3])}。"
                    f"请只使用证据块头部方括号里的编号作为引用来源。"
                )
            elif check.name == "clause_consistency":
                parts.append(
                    f"条号不一致：{'；'.join(check.offenders[:3])}。"
                    f"请核对证据块标注的条号，不要改动。"
                )
            elif check.name == "sentence_support":
                parts.append("部分结论在证据中找不到对应文字，请只依据证据作答。")
        if not draft.citations:
            parts.append("你的回答没有任何引用，请为每一句结论补充 [p.页码 §条款号] 形式的引用。")
        return " ".join(parts) or "请重新作答，确保每句结论都有证据支撑。"

    def _finalize(
        self,
        question: str,
        draft: Draft | None,
        citations: list[Citation],
        decision,
        report,
        hits,
        trace: list[TraceStep],
        usage: Usage,
        attempts: int,
        started: float,
    ) -> AnswerBundle:
        text = draft.text if draft else None

        # 否定型答案不是拒答：`"本标准未规定……"` 是有效答案，不该被拦成"答不了"。
        if decision.refused and text and is_in_document_negative(text):
            negative_ok = (
                decision.reason in (RefusalReason.NOT_IN_DOCUMENT, RefusalReason.LOW_CONFIDENCE)
                and bool(citations)
            )
            if negative_ok:
                trace.append(
                    TraceStep(
                        step="negative_answer",
                        detail="识别为「文档明确未规定」型答案，按有效答案返回而非拒答",
                    )
                )
                decision = type(decision)(refused=False)

        bundle = AnswerBundle(
            question=question,
            answer=None if decision.refused else text,
            citations=[] if decision.refused and not decision.candidates else citations,
            refused=decision.refused,
            refusal_reason=decision.reason,
            refusal_detail=decision.detail,
            candidates=decision.candidates or [],
            verification=report if report is not None else VerificationReport(),
            hits=hits,
            trace=trace,
            usage=usage,
            attempts=attempts,
        )
        _log.info(
            "问答完成",
            extra={
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "detail": (
                    f"refused={bundle.refused} reason={bundle.refusal_reason.value if bundle.refusal_reason else None} "
                    f"attempts={attempts} citations={len(bundle.citations)}"
                ),
                "usage": usage.to_dict(),
            },
        )
        return bundle
