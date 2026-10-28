"""程序化自检。

最重要的一段话，先说清楚
------------------------
**"数值落地"防不住 OCR 错误，这是两件不同的事。**

- 数值落地校验的是"答案里的数字是否出现在它所引用的证据里"——它保证
  **答案忠实于证据**，不保证**证据本身是对的**。
- 如果 OCR 把 `学工〔2026〕15号` 读成 `13号`，模型照抄 `13号`，
  数值落地**会通过**，系统于是自信地给出一个错的文号。

所以幻觉与 OCR 污染需要两套机制：

| 威胁 | 机制 | 落在哪 |
|---|---|---|
| 模型编造不存在的内容 | 引用有效性 + 数值落地 + 条款一致性 | 本模块（gate） |
| 证据本身被识别错 | 跨引擎交叉校验 + `uncertain` 标记 + `EXTRACTION_FAILED` 拒答 | ocr/spotcheck + policy |

把这两件事混为一谈，是"看起来做了自检、实际没防住"的典型原因。

为什么自检不交给模型打分
------------------------
让模型给自己的答案打分是自证。本模块的每一条 gate 都是**确定性**的：
给定同样的答案与证据，结果必然一样，可复现、可单元测试、
也能被对抗变异套件（eval/adversarial.py）验证其检出率。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from ..logging import get_logger
from ..models import CheckResult, CheckSeverity, Citation, VerificationReport
from ..tables import find_cell  # noqa: F401  (便于调用方复用同一套定位逻辑)

_log = get_logger("qa.verify")

# 引用标记形如 [p.4 §3.6] / [p.1 ¶3]。提取"答案里的数字"时必须先剔除它，
# 否则页码与条号会被当成待校验的数值，产生大量假警报。
_CITATION_MARK_RE = re.compile(r"\[p\.\d+(?:\s*§[^\]]+|\s*¶\d+)?\]")

# 单位。必须和数值一起构成"量"再比对——**只校验数字是防不住单位错误的**：
# 把 `590 MPa` 改成 `590 kPa`，数字 590 照样能在证据里找到，检查会通过，
# 而答案已经完全错了。这个缺口是被对抗变异套件实测出来的（unit 类检出率 0%）。
_UNIT_ALT = (
    r"GPa|MPa|kPa|Pa|kN|N|mm|cm|km|kg|mg|g|μm|um|℃|°C|°|%|"
    r"级|号|次|年|月|日|个|件|批"
)

# 待校验的"量"token：数值 + 可选单位
#   1.0 / 590 / 1:100 / ±AT8/2 / 590 MPa / 2.0 mm / 12.5 kN
_NUMBER_RE = re.compile(
    r"[±+\-]?\d+(?:\.\d+)?(?:\s*[:/]\s*\d+(?:\.\d+)?)*"  # 590、1.0、1:100
    r"(?:\s*(?:" + _UNIT_ALT + r"))?"                      # 可选单位，如 MPa
)
_DOC_ID_RE = re.compile(r"(?:GB/T|GB|JB/T|ISO|DA/T)\s*\d+(?:\.\d+)*(?:[—\-]\d{4})?")
_CLAUSE_REF_RE = re.compile(r"(?:第\s*)?(\d+(?:\.\d+){1,4})\s*(?:条|款)?")

# 中文数字 -> 阿拉伯数字，用于跨写法比对（"一年内" vs "1 年内"）
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100, "千": 1000}


def normalize(text: str) -> str:
    """归一化，让"同一个值的不同写法"能比对得上。

    处理三类差异，它们都会造成假的不匹配：
    1. 全角/半角（`１５６８` vs `1568`）——扫描件与 OCR 都可能产出全角；
    2. 各类破折号（`—` `–` `－` `‑`）——国标文号里用的是长破折号；
    3. 空白（含不换行空格）。
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    out = out.replace(" ", " ").replace("　", " ")
    for dash in "—–－‑‒―":
        out = out.replace(dash, "-")
    out = re.sub(r"\s+", " ", out)
    return out.strip()


def cn_to_arabic(text: str) -> str:
    """把常见的中文数字写法折算成阿拉伯数字，仅用于比对。"""
    def repl(m: re.Match[str]) -> str:
        word = m.group(0)
        if word == "十":
            return "10"
        if len(word) == 1:
            return str(_CN_DIGITS.get(word, word))
        # 形如 十五 / 二十 / 三十六
        total, section = 0, 0
        for ch in word:
            v = _CN_DIGITS.get(ch)
            if v is None:
                return word
            if v >= 10:
                section = (section or 1) * v
                total += section
                section = 0
            else:
                section = v
        return str(total + section)

    return re.sub(r"[零一二两三四五六七八九十百千]+", repl, text)


@dataclass
class Claims:
    """从一段文本里抽出的、可被程序校验的断言。"""

    numbers: list[str] = field(default_factory=list)
    doc_ids: list[str] = field(default_factory=list)
    clause_refs: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.numbers or self.doc_ids or self.clause_refs)


def extract_claims(text: str) -> Claims:
    """抽出答案里的数值、文号、条款号。

    先剔除引用标记，否则 `[p.4 §3.6]` 里的 4 和 3.6 会被当作待校验的数字。
    """
    stripped = _CITATION_MARK_RE.sub(" ", text or "")
    norm = normalize(stripped)

    doc_ids = _DOC_ID_RE.findall(norm)
    # 文号里的数字不再单独校验（已被 doc_id 整体覆盖）
    without_docids = _DOC_ID_RE.sub(" ", norm)

    numbers: list[str] = []
    for m in _NUMBER_RE.finditer(without_docids):
        tok = m.group(0).strip()
        # 过滤掉纯标点与过短的噪声
        if tok and re.search(r"\d", tok):
            numbers.append(tok)

    clause_refs = [
        m.group(1) for m in _CLAUSE_REF_RE.finditer(normalize(stripped)) if "." in m.group(1)
    ]
    return Claims(numbers=numbers, doc_ids=doc_ids, clause_refs=clause_refs)


def _number_in_evidence(token: str, evidence: str) -> bool:
    """某个数值是否在证据里出现过。

    比对前把两边都归一化，并额外尝试中文数字写法，
    避免"一年"与"1 年"这种差异被误判为幻觉。

    **必须按数字边界匹配，不能用裸子串。**
    -------------------------------------
    早先的实现是 `t in e`，于是：
        证据 `圆角半径 r=0.5 mm～1.5 mm`，答案写 `5 mm` → 判"落地"（因为 "5" 是 "1.5" 的子串）。
    这正是"数值落地"这套机制存在的意义所在——它专门用来抓"答案里的数字在证据里不存在"，
    而子串匹配会让**最像真话的那类数值幻觉**（少写一位小数）整片漏过。
    实测对抗变异里 `number` 类的漏检就落在这里。
    """
    t = normalize(token)
    e = normalize(evidence)
    if not t:
        return False

    # 1) 原文命中，且命中位置的左右都不是数字/小数点/冒号——边界干净才算数。
    if _contains_with_boundary(t, e):
        return True
    # 2) 数值与单位之间的空格常被模型写丢（`590MPa` vs `590 MPa`），
    #    这类差异不该判为幻觉，所以去掉空格后再按同样规则比一次。
    if _contains_with_boundary(t.replace(" ", ""), e.replace(" ", "")):
        return True
    # 3) 中文数字写法（"一年" vs "1 年"）
    ct, ce = cn_to_arabic(t), cn_to_arabic(e)
    return ct != t and _contains_with_boundary(ct, ce)


# 数字字符：ASCII 数字 + 小数点/冒号/斜杠（`1:100`、`1.5` 属于同一个"量"的一部分）。
_DIGIT_CHARS = frozenset("0123456789.:／/％‰")


def _is_digit_char(ch: str) -> bool:
    return ch in _DIGIT_CHARS or ch.isdigit()


def _contains_with_boundary(needle: str, haystack: str) -> bool:
    """在 haystack 里找 needle，但要求两侧不紧邻数字字符。

    这样 `5` 不会命中 `1.5`、`4` 不会命中 `40`、`10` 不会命中 `1.0`。
    两侧若紧邻的是单位或中文（`590` 对 `590MPa`），仍算命中——那是同一个量。
    """
    if not needle:
        return False
    start = 0
    n = len(needle)
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            return False
        before = haystack[idx - 1] if idx > 0 else ""
        after = haystack[idx + n] if idx + n < len(haystack) else ""
        # 只有 needle 的首/尾是数字时才需要检查对应边界。
        left_ok = not (_is_digit_char(needle[0]) and before and _is_digit_char(before))
        right_ok = not (_is_digit_char(needle[-1]) and after and _is_digit_char(after))
        if left_ok and right_ok:
            return True
        start = idx + 1


# ---------------------------------------------------------------- 各项检查

def check_citation_validity(
    citations: list[Citation],
    retrieved_ids: set[str],
    known_ids: set[str],
    known_clause_labels: set[str] | None = None,
) -> CheckResult:
    """每条引用都必须指向真实存在、且本次确实召回过的 chunk。

    这条挡的是"编造出处"：模型给出一个格式漂亮但根本不存在的引用。

    **条号本身也要校验。**
    ----------------------
    只校验 chunk_id 存在是不够的：`parse_citations()` 在条号对不上时会把引用
    落到该页第一个 chunk 上，**但保留原样写出的条号**。于是答案里出现
    `[p.1 §9.9]`（第 1 页根本没有 9.9 条）时，chunk_id 是真实的、检查通过——
    一个凭空捏造的条款出处就混了过去。实测这条能全绿通过所有四项检查。

    给了 `known_clause_labels` 时，额外要求：引用里的条号必须是文档中真实存在的条号。
    （不给就跳过这一半校验，并在结论里明说，避免"静默失效的闸门"。）
    """
    offenders: list[str] = []
    if not citations:
        # 零引用不能算通过——那等于"这段结论没有任何出处"，
        # 与"引用都正确"是两回事。一个"有据可依"的系统必须要求出处存在。
        offenders.append("答案没有任何引用，无法追溯出处")
    for c in citations:
        if c.chunk_id not in known_ids:
            offenders.append(f"引用了不存在的片段 {c.chunk_id}")
        elif c.chunk_id not in retrieved_ids:
            offenders.append(f"引用了本次未召回的片段 {c.chunk_id}")

    # --- 条号真实性 ---
    labels = known_clause_labels or set()
    checked_labels = 0
    unmatched_labels: list[str] = []
    if labels:
        for c in citations:
            raw = normalize(c.clause_label or "")
            if not raw:
                continue
            checked_labels += 1
            if raw not in labels:
                unmatched_labels.append(raw)
        for raw in sorted(set(unmatched_labels)):
            offenders.append(f"引用标注了文档中不存在的条款 {raw}")

    if not offenders:
        detail = f"检查 {len(citations)} 条引用，全部指向真实且已召回的片段"
        if checked_labels:
            detail += f"，且 {checked_labels} 处条号均为文档中真实存在的条号"
    elif not citations:
        detail = "答案未提供任何引用"
    elif unmatched_labels:
        detail = f"引用的条号与文档不符：{'、'.join(sorted(set(unmatched_labels)))}"
    else:
        detail = f"{len(offenders)}/{len(citations)} 条引用无效"

    return CheckResult(
        name="citation_validity",
        passed=not offenders,
        severity=CheckSeverity.GATE,
        detail=detail,
        offenders=offenders,
    )


def check_numeric_grounding(
    answer: str, citations: list[Citation], evidence_by_chunk: dict[str, str]
) -> CheckResult:
    """答案里的每个数值都必须能在**它所引用的证据**里找到。

    这是数值幻觉最有效的探测器：伪造一个尺寸、公差或文号，
    答案读起来毫无破绽，但那个数字在证据里根本不存在。

    **它不检测 OCR 污染**——证据本身错了，答案照抄，这条会通过。
    见模块开头的说明。
    """
    claims = extract_claims(answer)
    if claims.is_empty():
        return CheckResult(
            name="numeric_grounding",
            passed=True,
            severity=CheckSeverity.GATE,
            detail="答案中未出现需校验的数值或文号",
        )

    cited_evidence = " ".join(evidence_by_chunk.get(c.chunk_id, "") for c in citations)
    if not cited_evidence.strip():
        # 答案里有数值，却拿不出任何可比对的证据 —— 这**必须判失败**。
        # 通过的话就等于"只要不引用，数值就永远校验通过"，等于把这道闸门关掉了。
        return CheckResult(
            name="numeric_grounding",
            passed=False,
            severity=CheckSeverity.GATE,
            detail="答案含数值，但没有任何可用的引用证据可供比对（无证据即无法证成）",
            offenders=claims.numbers + claims.doc_ids,
        )

    offenders: list[str] = []
    for tok in claims.numbers:
        if not _number_in_evidence(tok, cited_evidence):
            offenders.append(f"数值 {tok} 未出现在所引证据中")
    for tok in claims.doc_ids:
        if not _number_in_evidence(tok, cited_evidence):
            offenders.append(f"文号 {tok} 未出现在所引证据中")

    return CheckResult(
        name="numeric_grounding",
        passed=not offenders,
        severity=CheckSeverity.GATE,
        detail=(
            f"校验 {len(claims.numbers) + len(claims.doc_ids)} 个数值/文号，全部落地"
            if not offenders
            else f"{len(offenders)} 个数值/文号在所引证据中找不到"
        ),
        offenders=offenders,
    )


def _is_clause_reference(ref: str, known_labels: set[str]) -> bool:
    """判断一个"带小数点的数字"到底是不是条号引用。

    **这一步不能省。** 条号的形态是 `3.6`，而**表格里的数值也是这个形态**——
    AQL 值 `2.5`、`1.0`，公差 `3.0 mm`，载荷 `12.5 kN`。
    不加区分的话，答案里每个小数都会被当成条号声明，然后被判"与证据不符"。

    实测代价：一条完全正确的表格问答会被 `clause_consistency` 拦下并最终拒答。
    **一个把正确回答判成错误的检查，比没有这个检查更糟**——它会训练使用者
    忽略所有告警。

    判据用"这个数字是否是文档里真实存在的条号"：
    - 是 → 它是一处条号声明，纳入一致性校验；
    - 否 → 它是数据值，不是条号声明，不归本检查管。

    那"编造一个文档里不存在的条号"怎么办？由 `numeric_grounding` 负责——
    凭空出现的数字逃不过数值落地。两者分工明确，不会留缺口。
    """
    return normalize(ref) in known_labels


def check_clause_consistency(
    answer: str, citations: list[Citation], known_labels: set[str] | None = None
) -> CheckResult:
    """答案里提到的条号，必须与所引证据的条号自洽。

    挡的是"串条"：答案内容来自 3.6，却说成是 3.5。这类错误在人工抽查时
    很难发现（两个条号长得像、内容也相关），但会让追溯失效。

    Args:
        known_labels: 文档里真实存在的条号集合。**必传**——否则每个小数
            都会被当成条号（见 `_is_clause_reference`）。
    """
    claims = extract_claims(answer)
    labels = known_labels or set()
    if not labels and claims.clause_refs:
        # 不传条号集合时，本检查会退化成空操作（因为无法区分条号与数据值）。
        # **静默失效的闸门比没有闸门更危险**——所以这里明确报出来。
        _log.warning(
            "未提供文档条号集合，条号一致性检查已跳过",
            extra={"detail": f"答案中有 {len(claims.clause_refs)} 处小数点数字，无法判定是否为条号"},
        )
    refs = [r for r in claims.clause_refs if _is_clause_reference(r, labels)]

    if not refs:
        skipped = len(claims.clause_refs)
        if skipped == 0:
            detail = "答案中未提及条号"
        elif not labels:
            # 退化的原因要写在**结论里**，不能只写进日志——
            # 评审看的是报告，不是 stderr。静默失效的闸门比没有闸门更危险。
            detail = (
                f"⚠ 未提供文档条号集合，本项检查已跳过"
                f"（答案中有 {skipped} 处小数点数字，无法判定是条号还是数据值）"
            )
        else:
            detail = f"答案中的 {skipped} 处小数均非文档条号（应为数据值），不纳入条号校验"
        return CheckResult(
            name="clause_consistency",
            passed=True,
            severity=CheckSeverity.GATE,
            detail=detail,
        )

    cited_labels = {normalize(c.clause_label) for c in citations if c.clause_label}
    offenders: list[str] = []
    for ref in refs:
        n = normalize(ref)
        if not any(n == lab or n in lab or lab in n for lab in cited_labels if lab):
            offenders.append(f"答案提到条款 {ref}，但所引证据的条号是 {sorted(cited_labels) or '（无）'}")

    return CheckResult(
        name="clause_consistency",
        passed=not offenders,
        severity=CheckSeverity.GATE,
        detail=(
            f"答案提及 {len(refs)} 处条号，与证据一致"
            if not offenders
            else f"{len(offenders)} 处条号与证据不符"
        ),
        offenders=offenders,
    )


def check_sentence_support(
    answer: str, citations: list[Citation], evidence_by_chunk: dict[str, str]
) -> tuple[CheckResult, float]:
    """句级支撑度。**默认只是咨询性指标，不是 gate。**

    为什么不当门：它依赖中文分词与一个阈值，而本项目没有足够标注数据
    去校准那个阈值。一个未校准的阈值产生的假警报会训练使用者忽略这个信号，
    那还不如把它降级成"上报但不拦截"。

    Returns:
        (检查结果, 支撑度分数)。分数是"与证据有实质重叠的句子占比"。
    """
    sentences = [s.strip() for s in re.split(r"[。！？；\n]", answer) if len(s.strip()) >= 6]
    if not sentences:
        return (
            CheckResult(
                name="sentence_support",
                passed=True,
                severity=CheckSeverity.ADVISORY,
                detail="答案过短，无法评估句级支撑度",
            ),
            1.0,
        )

    evidence = normalize(" ".join(evidence_by_chunk.get(c.chunk_id, "") for c in citations))
    ev_chars = set(re.findall(r"[一-鿿]|[A-Za-z0-9]+", evidence))

    supported = 0
    weak: list[str] = []
    for sent in sentences:
        s_chars = set(re.findall(r"[一-鿿]|[A-Za-z0-9]+", normalize(sent)))
        if not s_chars:
            continue
        overlap = len(s_chars & ev_chars) / len(s_chars)
        if overlap >= 0.5:
            supported += 1
        else:
            weak.append(sent[:40])

    score = supported / len(sentences)
    return (
        CheckResult(
            name="sentence_support",
            passed=score >= 0.5,
            severity=CheckSeverity.ADVISORY,
            detail=f"{supported}/{len(sentences)} 句与证据有实质重叠（咨询性指标，不作放行判定）",
            offenders=weak,
        ),
        score,
    )


def check_extraction_health(evidence_chunks, failed_pages=None) -> CheckResult:
    """证据是否涉及识别失败的页面。

    这条对应 OCR 污染那一侧：只要证据涉及任何一页识别失败，
    这次回答就不该放行——不是"答案错了"，而是"我们根本不知道这页写了什么"。

    注意：主链路上这道判定在 `qa.policy.decide()` 里（那里能拿到
    `(doc_id, page_no)` 形态的失败页集合，且发生在放行之前）。
    本函数提供给**不走 decide 的调用方**（例如自定义评估流程）复用同一判据。

    （此前的实现是一个永远不 append 的循环：`failed` 恒为空、恒返回 passed=True，
    是个**永远不会失败的检查**。这类空壳比没有检查更糟——它会被当成一路保障。）
    """
    failed_for = failed_pages or set()
    bad: list[str] = []
    for c in evidence_chunks:
        doc_id = getattr(c, "doc_id", "") or ""
        page = getattr(c, "page_no", None)
        if page is None:
            continue
        # 失败页集合支持两种形态：裸页码（单文档）与 (doc_id, page_no)（多文档）。
        if page in failed_for or (doc_id, page) in failed_for:
            bad.append(f"{doc_id or '?'} 第 {page} 页")

    return CheckResult(
        name="extraction_health",
        passed=not bad,
        severity=CheckSeverity.GATE,
        detail=(
            "证据所涉页面均已成功识别"
            if not bad
            else f"证据涉及 {len(bad)} 处识别失败的页面"
        ),
        offenders=bad,
    )


def verify(
    answer: str,
    citations: list[Citation],
    *,
    retrieved_ids: set[str],
    known_ids: set[str],
    evidence_by_chunk: dict[str, str],
    support_gate: float | None = None,
    known_clause_labels: set[str] | None = None,
) -> VerificationReport:
    """跑全部检查，汇总成报告。

    Args:
        support_gate: 若给定阈值，句级支撑度升格为 gate；默认 None（仅上报）。
            升格需要先有实测精确率支撑，见 docs/evaluation.md。
        known_clause_labels: 文档里真实存在的条号集合，供条号一致性检查区分
            "条号"与"数据值"（两者都是 `3.6` 这种形态）。
    """
    checks = [
        check_citation_validity(citations, retrieved_ids, known_ids, known_clause_labels),
        check_numeric_grounding(answer, citations, evidence_by_chunk),
        check_clause_consistency(answer, citations, known_clause_labels),
    ]
    support_check, support_score = check_sentence_support(answer, citations, evidence_by_chunk)
    if support_gate is not None:
        support_check = CheckResult(
            name=support_check.name,
            passed=support_score >= support_gate,
            severity=CheckSeverity.GATE,
            detail=support_check.detail + f"（已按阈值 {support_gate} 升格为 gate）",
            offenders=support_check.offenders,
        )
    checks.append(support_check)

    report = VerificationReport(checks=checks, support_score=support_score)
    if not report.gates_passed:
        _log.info(
            "自检未通过",
            extra={"detail": "; ".join(f"{c.name}: {c.detail}" for c in report.failed_gates)},
        )
    return report
