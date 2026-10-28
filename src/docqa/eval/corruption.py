"""定向污染探针：噪声**命中答案字段**时，系统会不会"忠实于错误证据"。

要回答的问题
------------
`docs/evaluation.md` 第六节留下过一个明确的"仍未做"：

    随机噪声没有让准确率下滑，但这不能读成"抗噪"——注入的字符
    **未必落在答案所依赖的那几个数字上**。要回答"污染之下系统会怎样"，
    得在注入**命中答案字段**的条件下跑。

这个探针就是那个受控版本，且完全离线、可逐字节复现：

1. 对每道有数值答案事实的金标题，找到**答案所依赖的那个字段**；
2. 只改这一个字段（长度不变的数字替换，模拟一次系统性误读），
   并保证该取值在整份语料里只出现在这一个片段里——
   于是"原值从语料中消失"是确定的，答案是否错误不再有歧义；
3. 用 **NullProvider（抽取式）** 走一遍真实的 `QaAgent`：
   检索 → 起草 → 自检 → 拒答判定，与主评估同一条链路；
4. 记录：是否拒答、答案里出现的是原值还是被污染的值、四条 gate 是否全过。

对照组
------
对同一道题，再污染一个**不在答案字段上的**数值（优先同一片段的其他字段，
否则取本次检索到的其他片段），然后看答案是否仍然正确、gate 是否仍然全过。
没有对照组的话，"污染后答错"可能只是"污染这件事本身让系统失灵"；
有了它，才能说清楚失败是**由命中答案字段**引起的。

必须说清楚的边界
----------------
- **只测抽取式路径。** 抽取式答案字面上就是证据，所以数值落地**必然通过**——
  这恰恰是要演示的：`numeric_grounding` 校验的是**忠实度**，不是**真相**。
  换成生成模型并不会让它"知道"真值，只可能多一层改写。
- 这是**定向**污染，不是噪声强度扫描。它回答的是"命中答案字段时会不会静默答错"，
  不是"噪声到什么强度开始不可靠"——后者仍需长文档与真实识别。
- 样本量很小（合成件里可定向的题通常只有个位数），每个比例都附 Wilson 区间。
- 单字符替换不是真实 OCR 的形近字分布；它模拟的是"这个字段被系统性地读错"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from ..config import Config, DomainProfile
from ..index import HybridIndex
from ..llm import NullProvider
from ..models import AnswerBundle, Chunk
from ..qa.agent import QaAgent
from ..qa.verify import normalize
from .metrics import wilson_interval
from .runner import GoldenItem

# 字段里是否含数字。`12.5`、`IP54`、`-20` 都能用数字替换注入；
# 纯文字答案（"未规定"）与 `—` 不行，会被报告列为排除项。
_DIGIT_RE = re.compile(r"\d")


def bump_first_digit(value: str) -> str | None:
    """把第一个数字改成另一个数字。长度不变，注入后所有位置仍然有效。"""
    for i, ch in enumerate(value):
        if ch.isdigit():
            new = str((int(ch) + 1) % 10)
            if new != ch:
                return value[:i] + new + value[i + 1 :]
    return None


def alternate_values(value: str) -> list[str]:
    """同一长度下的其他取值：逐位替换一个数字，按"离原值近"排序。

    为什么不是只改首位：`1.0` 的 `2.0` / `3.0` 常已在语料里（别的条款或表格），
    碰撞时只能换一位——`1.1` 比 `0.0` 更像一次真实误读。

    为什么跳过 `0`：一个规格值被读成 `0.0` 太容易被人工复核一眼看出，
    会让"静默错误"的比例显得比真实情况乐观。
    """
    positions = [i for i, ch in enumerate(value) if ch.isdigit()]
    out: list[str] = []
    seen: set[str] = set()
    for delta in (1, -1, 2, -2, 3, -3, 4, -4, 5):
        for i in positions:
            d = (int(value[i]) + delta) % 10
            if d == int(value[i]) or d == 0:
                continue
            cand = value[:i] + str(d) + value[i + 1 :]
            if cand not in seen:
                seen.add(cand)
                out.append(cand)
    return out


def _token_re(token: str) -> re.Pattern[str]:
    """按数值边界匹配，避免 `4` 命中 `4.1`、`1.0` 命中 `11.0`。"""
    return re.compile(rf"(?<![\d.]){re.escape(token)}(?![\d.])")


def contains_token(text: str, token: str) -> bool:
    return bool(token) and _token_re(token).search(text or "") is not None


def _replace_token(text: str, old: str, new: str) -> str:
    return _token_re(old).sub(new, text or "")


def _corpus_chunks_containing(chunks: list[Chunk], token: str) -> list[str]:
    return [c.chunk_id for c in chunks if contains_token(c.text, token)]


@dataclass(frozen=True)
class Target:
    """一次可定向污染的答案字段。"""

    qid: str
    question: str
    field: str
    corrupted: str
    chunk_id: str


@dataclass(frozen=True)
class CorruptionSample:
    qid: str
    arm: str  # "treatment"（命中答案字段） / "control"（未命中）
    # 答案字段与它的污染形式。判据（含原值/含污染值）**始终针对答案字段**，
    # 这样对照组的 "preserved" 才有意义：它没有被污染，答案就该仍然正确。
    field: str
    corrupted: str
    # 本组实际改动的字段。处理组与上面相同；对照组是另一个字段。
    mutated_field: str
    mutated_to: str
    chunk_id: str
    refused: bool
    contains_original: bool
    contains_corrupted: bool
    # 本组改动的新值是否真的出现在答案里。对照组靠它区分"污染没进入答案"
    # （控制太弱）与"进入了答案但没影响答案字段"（这才是想证明的）。
    contains_mutated: bool
    gates_passed: bool
    gate_status: dict
    answer_excerpt: str

    @property
    def silent_wrong(self) -> bool:
        """带污染值作答、原值消失、且自检全过——系统会把它当正确答案放行。"""
        return (
            not self.refused
            and self.contains_corrupted
            and not self.contains_original
            and self.gates_passed
        )

    @property
    def preserved(self) -> bool:
        """对照口径：答案仍含原值且自检全过。"""
        return not self.refused and self.contains_original and self.gates_passed


def _match_answer_chunks(item: GoldenItem, chunks: list[Chunk]) -> list[Chunk]:
    """按 `relevant_chunk_ids` 的稳定后缀（`p1:c5`）找到答案片段。"""
    wanted = set(item.relevant_chunk_ids or [])
    if not wanted:
        return []
    return [
        c
        for c in chunks
        if c.chunk_id in wanted
        or any(c.chunk_id.endswith(":" + w) or c.chunk_id.endswith(w) for w in wanted)
    ]


def pick_target(item: GoldenItem, chunks: list[Chunk]) -> tuple[Target | None, str]:
    """挑一个**可无歧义判定对错**的答案字段。

    三个必要条件（缺一不可）：

    1. 字段出现在金标声明的答案片段里；
    2. 该取值在整份语料里**只出现在这个片段**——污染后原值彻底消失，
       答案里有没有它不再取决于"另一个片段是否恰好也写了同样的话"；
    3. 替换后的新值在语料里不存在——否则"答案含污染值"会在污染前就成立。

    不满足时返回 `(None, 原因)`，由报告如实列出被排除的题。
    """
    if item.expected_refusal:
        return None, "拒答题（不产生答案字段）"
    if not item.must_contain:
        return None, "金标没有 must_contain 事实"

    answer_chunks = _match_answer_chunks(item, chunks)
    if not answer_chunks:
        return None, "金标未标注 relevant_chunk_ids，无法定位答案片段"

    numeric_fields = [f for f in item.must_contain if _DIGIT_RE.search(f or "")]
    if not numeric_fields:
        return None, "答案事实不含数字（无法用数值替换注入）"

    found_in_answer = False
    for field in numeric_fields:
        for chunk in answer_chunks:
            if not contains_token(chunk.text, field):
                continue
            found_in_answer = True
            # 条件 2：整份语料里只有这个片段含该取值
            if _corpus_chunks_containing(chunks, field) != [chunk.chunk_id]:
                continue
            # 条件 3：新值在语料里不存在（条号、其他数值都算）
            corrupted = next(
                (
                    cand
                    for cand in alternate_values(field)
                    if not _corpus_chunks_containing(chunks, cand)
                ),
                None,
            )
            if corrupted is None:
                continue
            return (
                Target(
                    qid=item.qid,
                    question=item.question,
                    field=field,
                    corrupted=corrupted,
                    chunk_id=chunk.chunk_id,
                ),
                "",
            )
    if not found_in_answer:
        return None, "答案事实在标注的答案片段里找不到"
    return None, "答案字段在语料中不唯一，或所有替换值都与语料中的值冲突"


def _ask(
    chunks: list[Chunk], question: str, profile: DomainProfile, cfg: Config, doc_id: str
) -> AnswerBundle:
    """用与主评估同一条抽取式链路作答（不建模型、不触网）。"""
    index = HybridIndex(chunks, profile)
    agent = QaAgent(index, NullProvider(), cfg, profile, doc_id=doc_id)
    return agent.answer(question)


def _sample(
    target: Target,
    arm: str,
    bundle: AnswerBundle,
    *,
    mutated_field: str,
    mutated_to: str,
) -> CorruptionSample:
    answer = bundle.answer or ""
    return CorruptionSample(
        qid=target.qid,
        arm=arm,
        field=target.field,
        corrupted=target.corrupted,
        mutated_field=mutated_field,
        mutated_to=mutated_to,
        chunk_id=target.chunk_id,
        refused=bundle.refused,
        # 判据总是针对**答案字段**：对照组没有污染它，原值就该还在。
        contains_original=contains_token(answer, target.field),
        contains_corrupted=contains_token(answer, target.corrupted),
        contains_mutated=contains_token(answer, mutated_to),
        gates_passed=bool(bundle.verification.gates_passed),
        gate_status={c.name: bool(c.passed) for c in bundle.verification.checks},
        answer_excerpt=answer[:120],
    )


def _control_value(
    target: Target,
    answer_fields: set[str],
    chunks: list[Chunk],
    baseline: AnswerBundle,
    known_labels: set[str],
) -> tuple[str, str, str] | None:
    """挑一个非答案字段作对照：返回 (chunk_id, 原值, 污染值)。

    优先同一片段里的其他数值（控制得更紧：污染就发生在答案片段内，
    只是没落在答案依赖的那个字段上）；没有则退到本次检索到的其他片段。

    排除四类候选，否则对照会变成另一个实验：

    - **任何**答案事实（不只本次定向的那个字段）——多事实题（如温度上下限）
      改了另一个，答案其实也错了，只是本判据没盯着它；
    - 答案字段的字符区间（`54` 是 `IP54` 的子串，改它就是在改答案字段）；
    - 条号（改了会触发条号一致性检查，与"数值字段被读错"不是一回事）；
    - **题干里出现的数字**（例如 D4 的 `8 mm`）——改了它等于改掉问题的前提。
    """
    hit_ids = [h.chunk_id for h in baseline.hits]
    preferred = [c for c in chunks if c.chunk_id == target.chunk_id]
    others = [c for c in chunks if c.chunk_id in hit_ids and c.chunk_id != target.chunk_id]
    for chunk in preferred + others:
        protected: list[tuple[int, int]] = []
        for field in answer_fields:
            protected.extend(_token_spans(chunk.text, field))
        for tok, span in _numeric_tokens(chunk.text):
            if tok in answer_fields:
                continue
            if _overlaps(span, protected):
                continue
            if normalize(tok) in known_labels:
                continue
            if contains_token(target.question, tok):
                continue
            candidates = alternate_values(tok)
            if not candidates:
                continue
            # 边界匹配下的替换可能落空（例如 `1-20` 里取到 `-20`）。
            # 落空的对照改动等于什么都没做，不能让它在统计里冒充"没影响"。
            if _replace_token(chunk.text, tok, candidates[0]) == chunk.text:
                continue
            return chunk.chunk_id, tok, candidates[0]
    return None


_NUMBER_TOKEN_RE = re.compile(r"[±+\-]?\d+(?:\.\d+)?")


def _numeric_tokens(text: str) -> list[tuple[str, tuple[int, int]]]:
    return [(m.group(0), m.span()) for m in _NUMBER_TOKEN_RE.finditer(text or "")]


def _token_spans(text: str, token: str) -> list[tuple[int, int]]:
    return [m.span() for m in _token_re(token).finditer(text or "")]


def _overlaps(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    return any(not (span[1] <= s or span[0] >= e) for s, e in spans)


def _rate(count: int, total: int) -> dict:
    return {
        "count": count,
        "n": total,
        "rate": round(count / total, 4) if total else 0.0,
        "ci": list(wilson_interval(count, total)),
    }


def _corrupt_chunk(chunks: list[Chunk], chunk_id: str, old: str, new: str) -> list[Chunk]:
    """只替换指定片段里的取值，其余片段原样复用（Chunk 是 frozen 的）。"""
    return [
        replace(c, text=_replace_token(c.text, old, new)) if c.chunk_id == chunk_id else c
        for c in chunks
    ]


@dataclass(frozen=True)
class _ItemRun:
    samples: list[CorruptionSample]
    excluded_reason: str = ""
    control_unavailable: bool = False


def _run_item(
    item: GoldenItem,
    chunks: list[Chunk],
    *,
    profile: DomainProfile,
    cfg: Config,
    doc_id: str,
    known_labels: set[str],
) -> _ItemRun:
    """对一道题跑基线 + 处理组 + 对照组。"""
    target, reason = pick_target(item, chunks)
    if target is None:
        return _ItemRun([], excluded_reason=reason)

    baseline = _ask(chunks, item.question, profile, cfg, doc_id)
    baseline_ok = (
        not baseline.refused
        and contains_token(baseline.answer or "", target.field)
        and baseline.verification.gates_passed
    )
    if not baseline_ok:
        return _ItemRun(
            [],
            excluded_reason="未污染时就没有给出该字段并通过自检（污染实验无从对比）",
        )

    treatment = _ask(
        _corrupt_chunk(chunks, target.chunk_id, target.field, target.corrupted),
        item.question, profile, cfg, doc_id,
    )
    samples = [
        _sample(
            target, "treatment", treatment,
            mutated_field=target.field, mutated_to=target.corrupted,
        )
    ]

    control = _control_value(
        target, set(item.must_contain) | {target.field}, chunks, baseline, known_labels
    )
    if control is None:
        return _ItemRun(samples, control_unavailable=True)

    control_chunk_id, control_field, control_corrupted = control
    control_bundle = _ask(
        _corrupt_chunk(chunks, control_chunk_id, control_field, control_corrupted),
        item.question, profile, cfg, doc_id,
    )
    samples.append(
        _sample(
            target, "control", control_bundle,
            mutated_field=control_field, mutated_to=control_corrupted,
        )
    )
    return _ItemRun(samples)


def probe_targeted_corruption(
    chunks: list[Chunk],
    items: list[GoldenItem],
    *,
    profile: DomainProfile | None = None,
    cfg: Config | None = None,
    doc_id: str = "",
) -> dict:
    """跑定向污染实验：命中答案字段 vs 未命中（对照）。"""
    profile = profile or DomainProfile(name="probe")
    cfg = cfg or Config()
    known_labels = {normalize(c.clause_label) for c in chunks if c.clause_label}

    samples: list[CorruptionSample] = []
    excluded: list[dict] = []
    controls_unavailable: list[str] = []

    for item in items:
        run = _run_item(
            item, chunks, profile=profile, cfg=cfg,
            doc_id=doc_id, known_labels=known_labels,
        )
        if run.excluded_reason:
            excluded.append({"qid": item.qid, "reason": run.excluded_reason})
            continue
        samples.extend(run.samples)
        if run.control_unavailable:
            controls_unavailable.append(item.qid)

    treatment = [s for s in samples if s.arm == "treatment"]
    control = [s for s in samples if s.arm == "control"]
    silent = [s for s in treatment if s.silent_wrong]
    preserved = [s for s in control if s.preserved]
    answered = [s for s in treatment if not s.refused]
    grounding_passed_on_silent = [
        s for s in silent if s.gate_status.get("numeric_grounding", False)
    ]

    if treatment and len(silent) == len(treatment):
        verdict = (
            "**定向污染命中答案字段时，系统全部给出了「忠实但错误」的答案**："
            "答案照抄被污染的值、原值消失，而数值落地、引用有效、条号一致全部通过。"
            "这不是缺陷，是 `numeric_grounding` 的设计边界——它校验答案是否忠实于证据，"
            "不校验证据是否为真。工程应对只能是降低污染概率（更高 DPI / 第二引擎比对）"
            "与把高风险字段送人工复核，而不是指望某条 gate 自己发现真相。"
        )
    elif silent:
        verdict = (
            f"{len(silent)}/{len(treatment)} 道被定向污染的题给出了通过全部自检的错误答案。"
            "比例不到 100% 说明污染未必进入最终答案（检索或拼装可能换了片段），"
            "但每一次都意味着一次静默的错答。"
        )
    else:
        verdict = (
            "本轮没有出现「带污染值且通过全部自检」的答案——"
            "但样本量很小，不能据此认为这条路径安全；请检查逐题明细里的拒答与取值分布。"
        )

    return {
        "name": "targeted_corruption",
        "question": "OCR 噪声命中答案所依赖的字段时，系统会不会给出通过全部自检的错误答案？",
        "method": (
            "对每道有数值答案事实的金标题，定位答案片段里**语料中唯一**的该字段，"
            "改掉其中一个数字（长度不变、替换值不与语料中已有取值冲突），"
            "再用 NullProvider（抽取式）走一遍"
            "真实的 QaAgent：检索 → 起草 → 自检 → 拒答。"
            "对照组污染同一道题的非答案字段（优先同片段），用于证明失败来自命中答案字段。"
        ),
        "path": "extractive",
        "n_treatment": len(treatment),
        "n_control": len(control),
        "silent_wrong": _rate(len(silent), len(treatment)),
        "control_preserved": _rate(len(preserved), len(control)),
        # 对照组里改动真的进入了答案的比例。太低就说明对照太弱，
        # "没影响答案"可能只是因为这段文字根本没被引用。
        "control_reached_answer": _rate(
            sum(1 for s in control if s.contains_mutated), len(control)
        ),
        "answered_rate": _rate(len(answered), len(treatment)),
        "gate_pass_on_answered": _rate(
            sum(1 for s in answered if s.gates_passed), len(answered)
        ),
        "numeric_grounding_passed_on_silent": _rate(
            len(grounding_passed_on_silent), len(silent)
        ),
        "refused": sum(1 for s in treatment if s.refused),
        "conflicting": sum(
            1
            for s in treatment
            if s.contains_original and s.contains_corrupted
        ),
        "excluded": excluded,
        "controls_unavailable": controls_unavailable,
        "samples": [
            {
                "qid": s.qid,
                "arm": s.arm,
                "field": s.field,
                "corrupted": s.corrupted,
                "mutated_field": s.mutated_field,
                "mutated_to": s.mutated_to,
                "chunk_id": s.chunk_id,
                "refused": s.refused,
                "contains_original": s.contains_original,
                "contains_corrupted": s.contains_corrupted,
                "contains_mutated": s.contains_mutated,
                "gates_passed": s.gates_passed,
                "gate_status": s.gate_status,
                "silent_wrong": s.silent_wrong,
                "preserved": s.preserved,
                "answer_excerpt": s.answer_excerpt,
            }
            for s in samples
        ],
        "verdict": verdict,
        "limitations": [
            "只测抽取式路径：答案字面上就是证据，所以数值落地必然通过——"
            "这正是要演示的边界。换成生成模型并不会让它「知道」真值。",
            "这是定向污染，不是噪声强度扫描：它回答「命中答案字段时会不会静默答错」，"
            "不回答「噪声到什么强度开始不可靠」；后者仍需长文档与真实识别。",
            "单字符替换不是真实 OCR 的形近字分布；它模拟的是某个字段被系统性地读错。",
            "只有「语料中唯一且可数字替换」的答案字段能进入实验，被排除的题在报告里逐条列出。",
            "样本量小（合成件通常只有个位数题），每个比例都附 Wilson 区间，不能外推到真实文档。",
            "对照组优先在同片段污染，其次换片段；它证明的是「命中答案字段」这一差异，"
            "不是一套已标定的噪声阈值。",
        ],
    }


def render_section(report: dict) -> list[str]:
    """把定向污染结果渲染成 Markdown 小节。"""
    def pct(v: float) -> str:
        return f"{v:.1%}"

    def ci(pair) -> str:
        return f"[{pair[0]:.1%}, {pair[1]:.1%}]"

    sw = report["silent_wrong"]
    cp = report["control_preserved"]
    lines = [
        "## 3. 定向污染：噪声命中答案字段时会不会静默答错",
        "",
        report["method"],
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 处理组样本量（命中答案字段） | {report['n_treatment']} |",
        f"| 静默错误率（错误答案 + 全部门通过） | {pct(sw['rate'])} "
        f"（{sw['count']}/{sw['n']}，95% CI {ci(sw['ci'])}） |",
        f"| 其中数值落地通过的比例 | {pct(report['numeric_grounding_passed_on_silent']['rate'])} "
        f"（{report['numeric_grounding_passed_on_silent']['count']}/"
        f"{report['numeric_grounding_passed_on_silent']['n']}） |",
        f"| 处理组被拒答 | {report['refused']} |",
        f"| 处理组答案同时含原值与污染值 | {report['conflicting']} |",
        f"| 对照组样本量（未命中答案字段） | {report['n_control']} |",
        f"| 对照组改动真的进入答案的比例 | "
        f"{pct(report['control_reached_answer']['rate'])} "
        f"（{report['control_reached_answer']['count']}/"
        f"{report['control_reached_answer']['n']}） |",
        f"| 对照组答案保持正确且自检全过 | {pct(cp['rate'])} "
        f"（{cp['count']}/{cp['n']}，95% CI {ci(cp['ci'])}） |",
        "",
        "判据（含原值/含污染值）始终针对**答案字段**；对照组改的是另一个字段，"
        "所以它上面出现「含污染值 = 否」是预期结果。",
        "",
        "| 题号 | 组 | 本组改动 | 进入答案 | 答案字段 | 拒答 | 含原值 | 含污染值 | gate 全过 | 静默错误 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in report["samples"]:
        lines.append(
            f"| {s['qid']} | {s['arm']} | `{s['mutated_field']}` → `{s['mutated_to']}` | "
            f"{'是' if s['contains_mutated'] else '否'} | "
            f"`{s['field']}` | "
            f"{'是' if s['refused'] else '否'} | "
            f"{'是' if s['contains_original'] else '否'} | "
            f"{'是' if s['contains_corrupted'] else '否'} | "
            f"{'是' if s['gates_passed'] else '否'} | "
            f"{'**是**' if s['silent_wrong'] else '—'} |"
        )

    if report["excluded"]:
        lines += ["", "未进入实验的题（附原因，不静默丢弃）：", ""]
        lines += [
            f"- `{e['qid']}`：{e['reason']}" for e in report["excluded"]
        ]
    if report["controls_unavailable"]:
        lines += [
            "",
            "找不到对照字段的题（仍计入处理组，但不参与对照组比例）："
            f"`{'`、`'.join(report['controls_unavailable'])}`。",
        ]

    lines += ["", f"**结论**：{report['verdict']}", "", "**已知局限**", ""]
    lines += [f"- {item}" for item in report["limitations"]]
    return lines
