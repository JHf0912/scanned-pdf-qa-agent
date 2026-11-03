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
from ..errors import ConfigError
from ..index import HybridIndex
from ..llm import LLMProvider, NullProvider
from ..logging import get_logger
from ..models import (
    AnswerBundle,
    AnswerMode,
    Citation,
    RefusalReason,
    TraceStep,
    Usage,
    VerificationReport,
)
from .answer import (
    HYBRID_KNOWLEDGE_MARKER,
    HYBRID_REQUIRES_LLM_MESSAGE,
    Draft,
    draft_answer,
    validate_answer_mode,
)
from .policy import (
    Decision,
    decide,
    detect_ambiguity,
    evidence_coverage,
    is_existence_question,
    is_generic_negative,
    is_in_document_negative,
)
from .verify import check_extraction_health, verify

_log = get_logger("qa.agent")


def _format_failed_pages(failed_pages: set[tuple[str, int]]) -> str:
    """把失败页集合格式化成可读文案；多文档时带 doc_id（P2-6）。

    「A 第 1 页」能让用户知道该重扫哪份文件；只写「[1]」在多文档场景下
    会把 A 文档的失败页误报成当前问题所属文档。
    """
    parts = [
        f"{doc} 第 {page} 页" if doc else f"第 {page} 页"
        for doc, page in sorted(failed_pages)
    ]
    return "、".join(parts)


class QaAgent:
    """有界修复循环的问答 Agent。"""

    def __init__(
        self,
        index: HybridIndex,
        provider: LLMProvider,
        cfg: Config,
        profile: DomainProfile,
        *,
        failed_pages: set[int] | set[tuple[str, int]] | None = None,
        doc_id: str = "",
    ):
        self.index = index
        self.provider = provider
        self.cfg = cfg
        self.profile = profile
        # 归一成 **(doc_id, page_no) 元组**这一种形态。
        #
        # 调用方给裸页码（单文档场景，也是更自然的写法），内部补上 doc_id；
        # 已经是元组的（多文档场景）原样保留。
        # 统一成元组是因为多文档下**不同文档的同一页码是两回事**——
        # 只比页码会让 A 文档的失败页误伤 B 文档的证据。
        self.failed_pages = {
            f if isinstance(f, tuple) else (doc_id, f) for f in (failed_pages or set())
        }
        self.doc_id = doc_id
        # 文档里真实存在的条号，供条号一致性检查区分"条号"与"数据值"
        self._known_clause_labels = {
            c.clause_label.strip() for c in index.chunks if c.clause_label
        }

    # ---------------------------------------------------------------- 主流程

    def answer(self, question: str, mode: AnswerMode = "strict") -> AnswerBundle:
        validate_answer_mode(mode)
        # hybrid 必须真的由生成模型作答。没有模型时明确报错，
        # **绝不静默降级成 strict**——那会让前端显示的模式与实际行为错位。
        if mode == "hybrid" and (
            self.provider.name == "null" or isinstance(self.provider, NullProvider)
        ):
            raise ConfigError(HYBRID_REQUIRES_LLM_MESSAGE)

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

        # 修复提示是**这一道题**的状态，绝不能是 Agent 实例的状态。
        #
        # 它曾经挂在 `self._repair_hint` 上，而只有"修复轮次用尽"那条出口会清空它：
        # 一道题修复成功并放行之后，这个提示会留在实例上，**下一道题的第一轮起草
        # 就带着上一题的修复提示**。实测可复现（Q1 数值错→修复通过；Q2 的 prompt
        # 末尾仍带"你使用了证据中不存在的数值 950 MPa"）。
        #
        # 对 CLI 一次性进程影响小，但 `serve` 是长驻进程：一个用户的问题会污染
        # 之后所有人的问题，而且使用者完全看不见。所以改成局部变量。
        repair_hint: str | None = None

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

            # --- SEC-HYB-01 / NEW-6：失败页前置拒答 ---
            #
            # 失败页通常**没有可召回的文本**（OCR 失败/未入库），所以
            # `decide()` 的“证据触及失败页”检查看不到它；而 hybrid 又跳过
            # 无证据/覆盖率步骤。若不在这里拦，系统会把“检索不到”解释成
            # “文档没规定”，再用模型知识补位——那正是可追溯性的底线。
            #
            # 第二种情况：证据充足、但证据本身落在失败页上。它在 `decide()`
            # 里注定会拒答，提前拦下可以省掉一次无意义的模型调用（NEW-6）。
            # 证据充足且不涉及失败页时正常回答。
            if mode == "hybrid" and self.failed_pages:
                if not evidence or coverage < qa_cfg.min_coverage:
                    failed_refs = _format_failed_pages(self.failed_pages)
                    trace.append(
                        TraceStep(
                            step="precheck",
                            detail=(
                                f"文档存在未成功识别的页面（{failed_refs}），"
                                f"且证据不足（{len(evidence)} 个片段，"
                                f"覆盖率 {coverage:.0%}），未调用模型即拒答"
                            ),
                        )
                    )
                    decision = Decision(
                        refused=True,
                        reason=RefusalReason.EXTRACTION_FAILED,
                        detail=(
                            f"文档存在未成功识别的页面（{failed_refs}），"
                            "本次检索没有取得足够的可引用证据。"
                            "这不是「文档里没有」，而是「这些页没读出来」——"
                            "不能用模型知识替代。请重新识别失败页"
                            "（或提供更清晰的扫描件）后再问。"
                        ),
                    )
                    return self._finalize(
                        question, None, [], decision, report, hits, trace,
                        usage, attempts, started, mode=mode,
                    )

                health = check_extraction_health(evidence, self.failed_pages)
                if not health.passed:
                    offenders = "、".join(health.offenders)
                    trace.append(
                        TraceStep(
                            step="precheck",
                            detail=(
                                f"证据涉及未成功识别的页面：{offenders}，"
                                "未调用模型即拒答"
                            ),
                        )
                    )
                    decision = Decision(
                        refused=True,
                        reason=RefusalReason.EXTRACTION_FAILED,
                        detail=(
                            f"证据涉及未成功识别的页面：{offenders}。"
                            "不能用模型知识替代；请重新识别这些页面"
                            "（或提供更清晰的扫描件）后再问。"
                        ),
                    )
                    return self._finalize(
                        question, None, [], decision, report, hits, trace,
                        usage, attempts, started, mode=mode,
                    )

            # --- 生成之前的两次预判，避免白花一次模型调用 ---
            #
            # 这两项都只依赖检索结果，不需要调用模型。放在生成前，是因为
            # "证据明显不相关"和"问题有歧义"这两种情况，生成再多轮也救不回来——
            # 让模型对不相关的证据硬答，只会得到一段引不出处的文字，
            # 然后被自检拦下，最终仍然拒答，但已经浪费了一次调用。
            candidates = detect_ambiguity(
                question, [(c, h.score) for c, h in zip(evidence, result.hits, strict=True)]
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
                    answer_mode=mode,
                )
                return self._finalize(
                    question, None, [], decision, report, hits, trace,
                    usage, attempts, started, mode=mode,
                )

            # 覆盖率过低 = 文档根本没覆盖这个问题，直接拒答。
            # 例外一：「是否规定 X」型问句不走这条路——它们的正确答案常常就是
            # 「未规定」，属于有效答案而非答不了，必须交给生成侧处理。
            # 例外二：hybrid 模式——文档没覆盖正是模型知识要补的那部分，
            # 覆盖率预判在此不成立（拿统计判据拦一个"允许超出文档"的模式）。
            if (
                mode == "strict"
                and attempt == 1
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
                    answer_mode=mode,
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
                        usage, attempts, started, mode=mode,
                    )

            # --- 起草 ---
            t0 = time.perf_counter()
            draft = draft_answer(
                question,
                evidence,
                self.provider,
                self.cfg.llm,
                temperature=qa_cfg.temperature,
                max_tokens=qa_cfg.max_tokens,
                repair_hint=repair_hint,
                # 引用模板来自场景配置。此前这个配置项从未被读取（格式写死在
                # Citation.render），于是"五个场景各配一份引用模板"一直是空转的。
                # PDF 与 DOCX 各一套：chunk 的 locator_kind 决定用哪一套。
                citation_template=self.profile.citation_template,
                docx_citation_template=self.profile.docx_citation_template,
                mode=mode,
            )
            usage = usage + draft.usage
            draft_detail = (
                f"第 {attempt} 轮：{'抽取式' if draft.extractive else '模型生成'}"
            )
            if mode == "hybrid":
                draft_detail += "（hybrid）"
            draft_detail += f"，{len(draft.text)} 字，{len(draft.citations)} 条引用"
            if mode == "hybrid":
                # strict 轨迹保持原样；hybrid 额外记录模式与补充段长度，
                # 便于复盘"模型知识有没有真的出现"。
                draft_detail += (
                    f"，模型知识补充 {len(draft.model_knowledge)} 字"
                    if draft.knowledge_used else "，未使用模型知识"
                )
            trace.append(
                TraceStep(
                    step="draft",
                    detail=draft_detail,
                    ms=int((time.perf_counter() - t0) * 1000),
                    usage=draft.usage,
                )
            )

            # --- 自检 ---
            evidence_map = {c.chunk_id: c.text for c in evidence}
            # 零引用放行只有两种情况（本轮再次收紧）：
            # 1) 正文为空，且**确实有补充段**（两者都空不是成功，走拒答）；
            # 2) 正文只由**通用否定短句**组成（精确白名单模板，
            #    不是“含否定词”的启发式；具体否定必须引用证据或改写）。
            # 有肯定内容的正文零引用仍按“无出处”拦截。
            allow_zero_citations = mode == "hybrid" and (
                (not draft.text.strip() and draft.knowledge_used)
                or is_generic_negative(draft.text)
            )
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
                answer_mode=mode,
                model_knowledge=draft.model_knowledge,
                knowledge_used=draft.knowledge_used,
                knowledge_citations_stripped=draft.knowledge_citations_stripped,
                allow_zero_citations=allow_zero_citations,
            )
            verify_detail = (
                f"第 {attempt} 轮：{'全部通过' if report.gates_passed else '未通过 ' + '、'.join(c.name for c in report.failed_gates)}"
                f"（支撑度 {report.support_score:.2f}）"
            )
            if mode == "hybrid":
                verify_detail += "，模式 hybrid"
            trace.append(
                TraceStep(
                    step="verify",
                    detail=verify_detail,
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
                    answer_mode=mode,
                )
                return self._finalize(
                    question, draft, draft.citations, decision, report, hits,
                    trace, usage, attempts, started, mode=mode,
                )

            # --- 修复 ---
            repair_hint = self._make_repair_hint(report, draft, evidence, mode=mode)
            if report and any(c.name == "numeric_grounding" for c in report.failed_gates):
                # 数值落地失败通常意味着"证据给少了"，扩大召回往往比让模型重写更有效
                current_k = min(current_k + 3, current_k * 2)
            trace.append(
                TraceStep(
                    step="repair",
                    detail=f"第 {attempt} 轮：{repair_hint[:120]}",
                )
            )
            _log.info(
                "自检未通过，进入修复",
                extra={"detail": f"attempt={attempt} hint={repair_hint[:160]}"},
            )

        # --- 修复轮次用尽 ---
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
            answer_mode=mode,
        )
        trace.append(
            TraceStep(
                step="give_up",
                detail=f"修复 {attempts} 轮后仍未通过自检，按策略拒答",
            )
        )
        return self._finalize(
            question, draft, draft.citations if draft else [], decision, report,
            hits, trace, usage, attempts, started, mode=mode,
        )

    # ---------------------------------------------------------------- 内部

    def _make_repair_hint(
        self, report, draft: Draft, evidence: list, mode: AnswerMode = "strict"
    ) -> str:
        """把自检失败翻译成给生成侧的修正指令。

        关键：要**具体**。"答案不准确"这种提示没有用；
        "数值 1.5 在证据中不存在，请只使用证据中出现的数值"才可执行。

        hybrid 多一条硬约束：正文与模型知识必须分开，补充段不得引用。
        修复只针对 doc_text——补充段不参与自检，也就不应进入修复目标。
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
            elif check.name == "doc_sentence_support" and mode == "hybrid":
                parts.append(
                    "文档正文的断言在所引证据中找不到逐字支撑；"
                    "请改为逐字引用证据原句/原词（可省略或拼接，但不得改写、"
                    "不得插入解释或推断）；仍无支撑的内容请移到"
                    f"{HYBRID_KNOWLEDGE_MARKER}段。"
                    "若文档确实没有规定，正文只写通用否定短句"
                    "（如「文档中未规定。」），具体说明放补充段。"
                )
        if not draft.citations:
            parts.append(
                "你的回答没有任何引用，请为每一句结论补充引用，"
                "格式必须与证据头括号内的定位格式一致"
                "（PDF 形如 [p.3 §3.6]，DOCX 形如 [§3.6 ¶4]）。"
            )
        if mode == "hybrid":
            parts.append(
                "正文与模型知识必须分开：文档能支持的内容放在正文并逐句引用；"
                f"文档没有的内容放在末尾 {HYBRID_KNOWLEDGE_MARKER} 之后，"
                "补充段不得包含任何引用标记（不得写 [p.N]/[§...]/[¶...]），"
                "也不得声称文档中写了。"
            )
            if draft.knowledge_citations_stripped:
                parts.append(
                    f"上一次的补充段含 {draft.knowledge_citations_stripped} 个引用标记，"
                    "已剥离；补充段一律不能引用。"
                )
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
        mode: AnswerMode = "strict",
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

        # 拒答时不返回模型知识：补充段不能替代被拒的答案。
        knowledge = None
        knowledge_used = False
        if not decision.refused and mode == "hybrid" and draft is not None:
            knowledge = draft.model_knowledge
            knowledge_used = draft.knowledge_used

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
            citation_template=self.profile.citation_template,
            docx_citation_template=self.profile.docx_citation_template,
            answer_mode=mode,
            model_knowledge=knowledge,
            knowledge_used=knowledge_used,
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
