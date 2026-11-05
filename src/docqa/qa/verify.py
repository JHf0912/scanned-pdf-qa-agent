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
from ..models import (
    AnswerMode,
    CheckResult,
    CheckSeverity,
    Citation,
    VerificationReport,
)
from ..tables import find_cell  # noqa: F401  (便于调用方复用同一套定位逻辑)

_log = get_logger("qa.verify")

# 引用标记有六种形态，提取"答案里的数字"时必须先整体剔除：
#   PDF  ：[p.4 §3.6] / [p.4 ¶3] / [p.4]
#   DOCX ：[§3.6 ¶4] / [¶4] / [§3.6]
# 只认 PDF 形态时，DOCX 的 `[§3.6 ¶4]` 会让 3.6 与 4 进入数值断言与条号候选，
# 造成 `numeric_grounding` 假失败与 `clause_consistency` 噪声。
_CITATION_MARK_RE = re.compile(
    r"\[p\.\d+(?:\s*§[^\]]+?)?(?:\s*¶\d+)?\]"
    r"|\[§[^\]]+?\s*¶\d+\]"
    r"|\[¶\d+\]"
    r"|\[§[^\]]+?\]"
)

# hybrid 的文档正文采用**逐字引用**门禁：每个肯定子句归一化后必须是
# 所引证据归一化文本的子串，**且匹配必须落在段边界上**。这是有意的产品
# 取舍——牺牲改写流畅度换可追溯性；strict 仍保留正常改写（只有 advisory）。
#
# 归一化是**字符级 + token 级**的：
# - 去空白/表格 `|`/句读标点/引号括号（这些是**段边界**，保留在用于边界
#   检查的 source 里，但不进入用于子串匹配的 compact）；
# - 只删除**恰好等于** `的/为/和/按` 的 jieba token（`行为/按钮/目的/
#   总和/按照` 作为完整词保留）（NEW-12）；
# - 单位、符号、限定词、语态词一律保留：`±/%/‰/℃/μ/该/被/以上/不大于`
#   不能被省略或插入（NEW-8）。
_VERBATIM_CONNECTIVE_TOKENS = frozenset({"的", "为", "和", "按"})

# 段边界字符集。匹配的起点/终点必须紧邻这些字符（或文本首尾）：
# 紧邻中文/字母数字/单位符号（`±`、`%`、`℃`、`μ`、`千/百/毫/度` 等）
# 说明子句截断了完整段落/单元，不算逐字引用（NEW-8/NEW-11）。
_VERBATIM_BOUNDARY_CHARS = frozenset(
    " \t\n\r\f\v|"
    "。；;！？!?，,：、"
    "“”‘’「」『』《》()（）[]【】"
)

# 单位/符号连续体：子句以数字/ASCII 字母起止时，若**跳过空白/`|`**后的
# 最近实质字符属于这里，说明它截断了数值或单位（`80` + ` ℃`、`±` + `0.5 mm`）。
# NFKC 会把 `℃` 拆成 `°C`，所以列 `°` 与字母即可覆盖。
_UNIT_CONTINUATION_CHARS = frozenset(
    "0123456789"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    ".±%‰°μµ"
    "米厘毫微秒度帕牛克吨升瓦伏安欧赫焦卡倍"
)

# citation-like token（含空格/全角/大小写变体）。用于 NEW-9：扫描原始
# doc_text，要求每个标记都能绑定到已解析的 citation——不能因为是变体或
# 不存在的定位就被静默忽略。
_CITATION_LIKE_RE = re.compile(
    r"[\[［][^\[\]［］]*?"
    r"(?:[pPＰｐ]\s*[.．]\s*[0-9０-９]|[§¶])"
    r"[^\[\]［］]*?[\]］]"
)

# 句子里的引用标记 → locator 字段。`_CITATION_MARK_RE` 提取出的标记是
# 标准形态，这里只做结构化解析，不复用 answer._CITATION_RE 的召回逻辑。
_PAGE_MARK_PARSE_RE = re.compile(
    r"\[p\.(\d+)(?:\s*§([^\]]+?))?(?:\s*¶(\d+))?\]"
)
_DOCX_MARK_PARSE_RE = re.compile(
    r"\[§([^\]]+?)\s*¶(\d+)\]|\[¶(\d+)\]|\[§([^\]]+?)\]"
)

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

def _cited_chunk_ids(citation: Citation) -> tuple[str, ...]:
    """一条引用实际覆盖的 chunk 集合。

    DOCX 的超长段落会被切成多片，它们共享同一个 `[§1 ¶2]`；解析时把
    in-range 的全部片收进 `evidence_chunk_ids`。旧引用（或 PDF）没有这个
    字段，回退到 `chunk_id`，行为与升级前一致。
    """
    return citation.evidence_chunk_ids or (citation.chunk_id,)


def check_citation_validity(
    citations: list[Citation],
    retrieved_ids: set[str],
    known_ids: set[str],
    known_clause_labels: set[str] | None = None,
    allow_zero_citations: bool = False,
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

    **证据集合里的每个 chunk 都要校验**：DOCX 长段落切片共享一个引用串，
    若只校验主锚点，伪造的第二片会漏过去。

    Args:
        allow_zero_citations: 仅 hybrid 且**文档正文为空或明确否定**时由调用方置位。
            此时正文本来就没有可引用的依据，零引用不是"无出处"，而是"文档没写"。
            放行条件必须由调用方按正文内容判断——有内容的正文零引用仍失败。
    """
    offenders: list[str] = []
    if not citations:
        if allow_zero_citations:
            return CheckResult(
                name="citation_validity",
                passed=True,
                severity=CheckSeverity.GATE,
                detail="文档正文无引用，补充段不参与文档自检",
            )
        # 零引用不能算通过——那等于"这段结论没有任何出处"，
        # 与"引用都正确"是两回事。一个"有据可依"的系统必须要求出处存在。
        offenders.append("答案没有任何引用，无法追溯出处")
    for c in citations:
        # c.chunk_id 与 evidence_chunk_ids 都要查；主锚点去重避免同一问题报两遍。
        for cid in dict.fromkeys((c.chunk_id, *_cited_chunk_ids(c))):
            if cid not in known_ids:
                offenders.append(f"引用了不存在的片段 {cid}")
            elif cid not in retrieved_ids:
                offenders.append(f"引用了本次未召回的片段 {cid}")

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

    cited_evidence = " ".join(
        evidence_by_chunk.get(cid, "")
        for c in citations
        for cid in _cited_chunk_ids(c)
    )
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
    answer: str,
    citations: list[Citation],
    evidence_by_chunk: dict[str, str],
    *,
    allow_zero_citations: bool = False,
) -> tuple[CheckResult, float]:
    """句级支撑度。**默认只是咨询性指标，不是 gate。**

    为什么不当门：它依赖中文分词与一个阈值，而本项目没有足够标注数据
    去校准那个阈值。一个未校准的阈值产生的假警报会训练使用者忽略这个信号，
    那还不如把它降级成"上报但不拦截"。

    Args:
        allow_zero_citations: hybrid 且文档正文为空/明确否定时为 True。
            此时没有引用、没有可比对的证据，句级支撑度**不适用**——
            照常打分只会得到 0 分并把 advisory 标红，用户看到的是一个
            没有意义的红色告警。这里返回"不适用"而非"未通过"。
            strict（默认 False）的行为完全不变。

    Returns:
        (检查结果, 支撑度分数)。分数是"与证据有实质重叠的句子占比"。
    """
    if allow_zero_citations and not citations:
        return (
            CheckResult(
                name="sentence_support",
                passed=True,
                severity=CheckSeverity.ADVISORY,
                detail="文档正文无引用/无证据，句级支撑度不适用（hybrid 补充段不参与）",
            ),
            1.0,
        )

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

    evidence = normalize(" ".join(
        evidence_by_chunk.get(cid, "")
        for c in citations
        for cid in _cited_chunk_ids(c)
    ))
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


def _para_covered(citation: Citation, para_no: int) -> bool:
    start = citation.para_no
    if start is None:
        return False
    end = citation.para_end if citation.para_end is not None else start
    return start <= para_no <= end


def _citations_for_mark(mark: str, citations: list[Citation]) -> list[Citation]:
    """把一个引用标记映射到解析出的 Citation 集合。

    优先按字段匹配（页/条号/段落范围），而不是比较渲染串——场景配置可以
    自定义引用模板，渲染串未必等于模型写出的标记。
    """
    m = _PAGE_MARK_PARSE_RE.fullmatch(mark)
    if m:
        page = int(m.group(1))
        clause = normalize(m.group(2) or "")
        matches = [
            c for c in citations if c.locator_kind == "page" and c.page_no == page
        ]
        if clause:
            labelled = [
                c for c in matches
                if normalize(c.clause_label or "") == clause
            ]
            if labelled:
                return labelled
        return matches

    m = _DOCX_MARK_PARSE_RE.fullmatch(mark)
    if m:
        if m.group(1) is not None:
            section = normalize(m.group(1))
            para = int(m.group(2))
            return [
                c for c in citations
                if c.locator_kind == "paragraph"
                and normalize(c.section or c.clause_label or "") == section
                and _para_covered(c, para)
            ]
        if m.group(3) is not None:
            para = int(m.group(3))
            return [
                c for c in citations
                if c.locator_kind == "paragraph" and _para_covered(c, para)
            ]
        section = normalize(m.group(4))
        return [
            c for c in citations
            if c.locator_kind == "paragraph"
            and normalize(c.section or c.clause_label or "") == section
        ]
    return []


def _mark_binding(mark: str, citations: list[Citation]) -> list[Citation]:
    """把一个引用标记绑定到已解析的 Citation。

    先按 locator 字段匹配，再比对渲染串（自定义模板场景）。
    **绑不上就是绑不上**：调用方不得回退到“全部 citations”——那会让
    一个伪引用借用别的 chunk 的逐字文本通过（NEW-9）。
    """
    matched = _citations_for_mark(mark, citations)
    if matched:
        return matched
    norm = normalize(mark)
    return [c for c in citations if normalize(c.render()) == norm]


def _evidence_text_for_marks(
    marks: list[str], citations: list[Citation], evidence_by_chunk: dict[str, str]
) -> tuple[bool, str]:
    """子句所带引用对应的证据文本（多标记取并集）。

    拼接顺序是**确定性的**：按 marks → 匹配到的 citations → 各 citation 的
    `evidence_chunk_ids` 的首次出现顺序去重（`dict` 保序）。用 `set` 会让顺序
    依赖 `PYTHONHASHSEED`，同一答案/证据在不同进程里可能 pass 或 fail
    （SEC-T3-04：跨 chunk 并集与 DOCX 跨切片子句都会受影响）。

    Returns:
        (bound, evidence_text)。任一标记无法绑定到已解析 citation 时
        `bound=False`——**绝不回退到全部 citations**（NEW-9）。
    """
    ids: dict[str, None] = {}  # 有序去重（值不用，仅借用 dict 的插入顺序）
    for mark in marks:
        matched = _mark_binding(mark, citations)
        if not matched:
            return False, ""
        for citation in matched:
            for cid in _cited_chunk_ids(citation):
                ids.setdefault(cid, None)
    return True, " ".join(evidence_by_chunk.get(cid, "") for cid in ids)


# 句首引用标记：中文写法常把引用放在句号**之后**（`结论。[p.1 §3.1]`）。
# 直接按句号切分会把引用切到下一子句，让正确回答被误判成“没有引用”。
_LEADING_CITATION_RE = re.compile(
    rf"^(?:\s*(?:{_CITATION_MARK_RE.pattern})\s*)+"
)

# 子句分隔符。**必须细到逗号/分号/顿号**：否则
# “抗拉强度…590 MPa，通常采用调质处理”里的后半句会搭前半句的便车
# 通过 gate（NEW-2）。ASCII 的 `!?` 与中文 `！？` 同为句子边界，
# 一并切分，避免同类标点行为不一致。
_CLAUSE_SPLIT_RE = re.compile(
    r"([。！？!?；;，,：、\n\r\v\f\u2028\u2029\u0085])"
)

# 句子边界。引用覆盖的粒度是**句子**而不是子句：中文自然写法把引用放在
# 整句末尾（`A，B [§1 ¶2]`），同句的逗号/顿号/冒号/分号子句共用它。
# 只有句号/问号/叹号与**所有视觉换行**另起一句——**不跨句借用**。
# `\r`、U+2028（LINE SEPARATOR）、U+2029（PARAGRAPH SEPARATOR）、
# U+0085（NEL）、`\v`、`\f` 在渲染器里都可能断行；只认 `\n` 会把
# “引用在下一行”误当成同一句（SEC-T1-01）。
_SENTENCE_BOUNDARY_CHARS = frozenset("。！？!?\n\r\v\f\u2028\u2029\u0085")

# 句末的尾部引用标记串：同一句**最后一个带标记的子句**末尾的标记可被同句
# 借用。标记后允许 0–2 个闭合包裹字符（`A，B（[§1 ¶2]）。`、`A，B [§1 ¶2]”`
# 这类自然写法）。ASCII `.` 与 ASCII 引号 `"`/`'` 不支持：前者见
# `docs/hybrid-mode.md` §7 已知残余，后者不在 `_VERBATIM_BOUNDARY_CHARS`，
# 若算作闭合包裹会让「借用源成立、带标记子句自身却不逐字」稳定出现
# （SEC-T3-01），故直接不识别。
_TRAILING_CLOSERS = "）)】」』》”’"
_TRAILING_MARKS_RE = re.compile(
    rf"(?:\s*(?:{_CITATION_MARK_RE.pattern}))+"
    rf"(?:\s*[{re.escape(_TRAILING_CLOSERS)}]){{0,2}}\s*$"
)


def _split_doc_clauses(text: str) -> list[tuple[str, int]]:
    """按句读/逗号切子句，返回 `(子句, 句子编号)`。

    句子边界是 `。！？!?` 与**所有视觉换行**（`\n`/`\r`/`\v`/`\f`/
    U+2028/U+2029/U+0085）；`；;，,：、` 仍切子句但**不**另起一句。
    跟在分隔符后的引用标记归给**前一个子句**（中文常把引用写在句号之后），
    所以它属于前一个子句所在的句子。

    只用于 hybrid 的 `doc_sentence_support`；strict 的 advisory
    `check_sentence_support` 保持原有切分，行为不变。
    """
    parts = _CLAUSE_SPLIT_RE.split(text or "")
    clauses: list[tuple[str, int]] = []
    sentence = 0
    # re.split 带捕获组时，奇数位是分隔符；偶数位是子句内容。
    for idx in range(0, len(parts), 2):
        if idx and parts[idx - 1] in _SENTENCE_BOUNDARY_CHARS:
            sentence += 1
        chunk = parts[idx].strip()
        if not chunk:
            continue
        leading = _LEADING_CITATION_RE.match(chunk)
        if leading and clauses:
            prev, prev_sentence = clauses[-1]
            clauses[-1] = (f"{prev} {leading.group(0).strip()}", prev_sentence)
            chunk = chunk[leading.end():].strip()
        if chunk:
            clauses.append((chunk, sentence))
    return clauses


def _trailing_marks(clause: str) -> list[str]:
    """子句**末尾**的引用标记（句末引用）；没有则返回空列表。

    容忍标记后 0–2 个闭合包裹字符（`（[§1 ¶2]）`、`[§1 ¶2]”`）；
    ASCII `.`、ASCII 引号 `"`/`'` 与更多包裹字符不支持，属已知保守行为
    （ASCII 引号不在段边界字符集里，见 SEC-T3-01）。
    """
    match = _TRAILING_MARKS_RE.search(clause)
    if not match:
        return []
    return _CITATION_MARK_RE.findall(match.group(0))


def _verbatim_index(
    text: str,
) -> tuple[str, str, list[int], set[int]]:
    """把文本归一化成逐字校验用的 compact 核心，并保留边界检查所需信息。

    Returns:
        (compact, source, positions, transparent)
        - `source`：去引用 + NFKC 归一化后的原文（空白/标点/`|` 保留，
          用于检查匹配两侧是否紧邻段边界）；
        - `compact`：用于子串匹配的核心（去掉边界字符与恰好等于
          `的/为/和/按` 的 token）；
        - `positions[i]`：compact 第 i 个字符在 source 中的下标；
        - `transparent`：被删除的连接词 token 在 source 中的下标集合
          （边界检查时把它们当作透明，跳过继续看）。
    """
    import jieba

    source = normalize(_CITATION_MARK_RE.sub(" ", text or ""))
    chars: list[str] = []
    positions: list[int] = []
    transparent: set[int] = set()

    for word, start, end in jieba.tokenize(source):
        if word in _VERBATIM_CONNECTIVE_TOKENS:
            transparent.update(range(start, end))
            continue
        for idx in range(start, end):
            ch = source[idx]
            if ch in _VERBATIM_BOUNDARY_CHARS:
                continue  # 边界字符不进 compact，但留在 source 里供边界检查
            chars.append(ch)
            positions.append(idx)
    return "".join(chars), source, positions, transparent


def _verbatim_text(text: str) -> str:
    """逐字校验的 compact 核心（见 `_verbatim_index`）。"""
    return _verbatim_index(text)[0]


def _boundary_ok(
    source: str, transparent: set[int], index: int, *, step: int
) -> bool:
    """index 处（可跳过被删除的连接词）是否是文本首尾或段边界字符。"""
    while 0 <= index < len(source) and index in transparent:
        index += step
    if index < 0 or index >= len(source):
        return True
    return source[index] in _VERBATIM_BOUNDARY_CHARS


def _neighbor_substantive(
    source: str, transparent: set[int], index: int, *, step: int
) -> str | None:
    """跳过空白、`|` 与被删除的连接词后，最近的实质字符。"""
    while 0 <= index < len(source):
        if index in transparent or source[index].isspace() or source[index] == "|":
            index += step
            continue
        return source[index]
    return None


def _verbatim_match(
    clause_compact: str,
    evidence_compact: str,
    source: str,
    positions: list[int],
    transparent: set[int],
) -> bool:
    """子句 compact 是否以**段边界**为起止出现在证据 compact 中。

    只做子串还不够：证据 `不得使用铝` 的子串 `使用铝` 会反转极性
    （NEW-11）。这里要求匹配起点紧邻段边界（文本开头/空白/`|`/句读/
    引号括号），终点同理；紧邻的是中文/字母数字/单位符号一律失败。
    被删除的连接词（`的/为/和/按` token）在边界检查时透明。

    另外，空白不总是安全边界：`80 ℃` 里空白之后紧跟单位符号，说明
    子句截断了单位（NEW-8）。子句以数字/ASCII 字母起止时，跳过空白/
    `|` 后若最近实质字符属于单位/符号连续体，同样失败。
    """
    if not clause_compact or not positions:
        return False
    check_left_unit = clause_compact[0].isascii() and clause_compact[0].isalnum()
    check_right_unit = clause_compact[-1].isascii() and clause_compact[-1].isalnum()

    start = evidence_compact.find(clause_compact)
    while start >= 0:
        end = start + len(clause_compact)
        left_src = positions[start] - 1
        right_src = positions[end - 1] + 1
        left_ok = _boundary_ok(source, transparent, left_src, step=-1)
        right_ok = _boundary_ok(source, transparent, right_src, step=1)
        if left_ok and check_left_unit:
            left_ok = (
                _neighbor_substantive(source, transparent, left_src, step=-1)
                not in _UNIT_CONTINUATION_CHARS
            )
        if right_ok and check_right_unit:
            right_ok = (
                _neighbor_substantive(source, transparent, right_src, step=1)
                not in _UNIT_CONTINUATION_CHARS
            )
        if left_ok and right_ok:
            return True
        start = evidence_compact.find(clause_compact, start + 1)
    return False


def check_doc_sentence_support(
    answer: str,
    citations: list[Citation],
    evidence_by_chunk: dict[str, str],
    *,
    allow_zero_citations: bool = False,
) -> CheckResult:
    """hybrid 专用 **GATE**：文档正文必须**逐字**来自所引证据。

    为什么升级为逐字门禁
    --------------------
    `sentence_support` 只是 advisory，而 `parse_citations()` 只校验 locator
    是否真实、是否已召回——**不校验这句话是否被该 chunk 支持**。词面重叠
    阈值可以被"共享术语 + 未支撑尾巴/逗号拼接/短句"绕过（NEW-2），
    反对“任意否定词 + 情态词黑名单”的判定也可被正向谓词绕过（NEW-7）。
    改成子句级逐字包含后，这些变体都必然失败：

    - 每个肯定子句必须有至少一个引用标记（不再有 `<6 字` 豁免）；
    - 引用覆盖粒度是**句子**：子句自带标记时只用自己的标记；没有标记时
      只借用**同一句最后一个带尾部标记的子句**的引用（不跨句），借用后仍要
      逐字可证；找不到可借用的标记仍判“没有引用标记”；
    - 去掉引用后的子句归一化文本，必须是所引证据归一化文本的**段边界子串**：
      匹配起止必须紧邻段边界，紧邻中文/字母数字/单位符号一律失败
      （NEW-8 截断单位、NEW-11 删除否定前缀反转极性）；
    - 引用标记必须先绑定到已解析 citation，绑不上直接失败（NEW-9）；
    - 只跳过**通用否定白名单模板**命中的子句（NEW-7：不再接受
      “具体否定”或“肯定断言 + 否定后缀”）；
    - `allow_zero_citations=True` **且没有任何 citations** 时跳过整条 gate；
      只要存在 citations，即使含否定词也要跑。

    这是有意的产品取舍：hybrid 的文档正文牺牲改写流畅度换可追溯性，
    重度改写会被拒——正确做法是逐字引用，或把内容移到补充段。strict
    仍保留正常改写（只有 advisory）。
    """
    if allow_zero_citations and not citations:
        return CheckResult(
            name="doc_sentence_support",
            passed=True,
            severity=CheckSeverity.GATE,
            detail="文档正文为空或为通用否定，逐句支撑不适用（hybrid 补充段不参与）",
        )

    # 惰性导入：`policy` 模块级依赖本模块的 `normalize`/`_CITATION_MARK_RE`。
    from .policy import is_generic_negative

    clauses = _split_doc_clauses(answer)
    # 同一句**最后一个带尾部标记的子句**：同句的无标记子句可以借用它（不跨句）。
    # 不要求它必须是最后一个子句——`A，B [§1 ¶2]，文档中未规定。` 里末尾的
    # 通用否定不持有标记，但不应让整句失去借用源（F4）。
    sentence_tail_marks: dict[int, list[str]] = {}
    for clause, sentence in clauses:
        marks = _trailing_marks(clause)
        if marks:
            sentence_tail_marks[sentence] = marks

    evaluated = 0
    offenders: list[str] = []
    for clause, sentence in clauses:
        content = _CITATION_MARK_RE.sub(" ", clause).strip()
        if not content:
            continue  # 只有引用标记的子句没有可校验内容
        if is_generic_negative(clause):
            continue  # 只跳过通用否定白名单；具体否定仍要引用证据
        evaluated += 1
        marks = _CITATION_MARK_RE.findall(clause)
        if not marks:
            # 子句自己没有标记：借用同一句最后一个**带标记**子句的尾部引用。
            # 借来的引用仍走下面的逐字校验——覆盖范围变大，逐字门禁不变。
            marks = sentence_tail_marks.get(sentence, [])
        if not marks:
            offenders.append(f"子句「{clause[:40]}」没有引用标记")
            continue
        bound, evidence = _evidence_text_for_marks(
            marks, citations, evidence_by_chunk
        )
        if not bound:
            offenders.append(
                f"子句「{clause[:40]}」的引用标记无法绑定到已解析的引用"
            )
            continue
        clause_compact = _verbatim_text(content)
        if not clause_compact:
            # 内容全是边界/连接词（如只写「按」）——不是有意义引文，
            # 也不能因为 compact 为空就被当作“无需校验”放行。
            offenders.append(f"子句「{clause[:40]}」没有可校验的实质内容")
            continue
        ev_compact, ev_source, ev_positions, ev_transparent = _verbatim_index(
            evidence
        )
        if not _verbatim_match(
            clause_compact, ev_compact, ev_source, ev_positions, ev_transparent
        ):
            offenders.append(
                f"子句「{clause[:40]}」不是所引证据的逐字内容"
                "（hybrid 正文只允许引用证据原句/原词，改写或补充请放到模型知识补充段）"
            )

    if not evaluated:
        return CheckResult(
            name="doc_sentence_support",
            passed=True,
            severity=CheckSeverity.GATE,
            detail="答案中没有需要逐句校验的肯定子句",
        )

    return CheckResult(
        name="doc_sentence_support",
        passed=not offenders,
        severity=CheckSeverity.GATE,
        detail=(
            f"{evaluated} 个肯定子句均为所引证据的逐字内容"
            if not offenders
            else f"{len(offenders)}/{evaluated} 个子句缺少引用或不含逐字证据"
        ),
        offenders=offenders,
    )


def check_doc_citation_marks(
    answer: str, citations: list[Citation]
) -> CheckResult:
    """hybrid 专用 **GATE**：文档正文里的每个 citation-like token 都必须绑定到已解析引用。

    WHY
    ---
    `parse_citations()` 会静默丢弃无法映射的标记（`[p.99 §9.9]`、`[p. 3]` 变体），
    而 `citation_validity` 只看已解析的 citations，所以伪引用 token 既不被拒、
    也不会进入 `citations`；前端仍会把它渲染成一个（点不动的）chip（NEW-9）。
    这里直接扫描**原始 doc_text**，逐个绑定：

    - 能绑定 → 通过（合法、已召回的 locator）；
    - 绑不上（不存在的页/条号、变体形态、自定义模板对不上）→ GATE 失败，
      detail 点名具体 token；
    - 没有 citation-like token → 通过（通用否定的零引用路径由
      `doc_sentence_support` 的 allow_zero 处理）。

    本检查**不因 allow_zero_citations 跳过**：`文档中未规定。[p.99 §9.9]`
    即使正文是通用否定，伪引用也必须失败。
    """
    marks = _CITATION_LIKE_RE.findall(answer or "")
    offenders: list[str] = []
    for mark in marks:
        if not _mark_binding(mark, citations):
            offenders.append(f"引用标记「{mark}」无法绑定到已解析的引用")
    if not marks:
        return CheckResult(
            name="doc_citation_marks",
            passed=True,
            severity=CheckSeverity.GATE,
            detail="文档正文中没有引用标记",
        )
    return CheckResult(
        name="doc_citation_marks",
        passed=not offenders,
        severity=CheckSeverity.GATE,
        detail=(
            f"检查 {len(marks)} 个引用标记，全部绑定到已解析引用"
            if not offenders
            else f"{len(offenders)}/{len(marks)} 个引用标记无法绑定到已解析引用"
        ),
        offenders=offenders,
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


def check_knowledge_label(
    knowledge_used: bool, knowledge_citations_stripped: int = 0
) -> CheckResult:
    """hybrid 的补充段标注检查。**ADVISORY，永不阻断。**

    它回答的不是"答案对不对"，而是"模型有没有守住分离规则"：

    - 未使用补充段 → 无额外内容，通过；
    - 使用了补充段 → 已显式标注，通过；
    - 补充段里出现过引用标记 → 显示层已剥离（不会污染 `citations`），
      但说明 prompt 的"禁止引用"没被遵守。记为未通过以便发现 prompt 失效，
      **不改变放行结果**——剥离本身已经消除了伪引用这个危害。

    strict 模式不调用本检查，报告与旧版完全一致。

    **先看剥离计数，再看是否使用了补充段**（SEC-HYB-05）：模型在补充段里
    只写引用时，剥离后为空 → `knowledge_used=False`，但 `stripped>0` 是
    明确的 prompt 失效信号，不能报成“未使用模型知识”。
    """
    if knowledge_citations_stripped:
        return CheckResult(
            name="knowledge_label",
            passed=False,
            severity=CheckSeverity.ADVISORY,
            detail=(
                f"补充段含 {knowledge_citations_stripped} 个引用标记，"
                f"已剥离，不计入文档引用"
            ),
        )
    if not knowledge_used:
        return CheckResult(
            name="knowledge_label",
            passed=True,
            severity=CheckSeverity.ADVISORY,
            detail="未使用模型知识",
        )
    return CheckResult(
        name="knowledge_label",
        passed=True,
        severity=CheckSeverity.ADVISORY,
        detail="补充段已显式标注",
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
    answer_mode: AnswerMode = "strict",
    model_knowledge: str | None = None,
    knowledge_used: bool = False,
    knowledge_citations_stripped: int = 0,
    allow_zero_citations: bool = False,
) -> VerificationReport:
    """跑全部检查，汇总成报告。

    Args:
        support_gate: 若给定阈值，句级支撑度升格为 gate；默认 None（仅上报）。
            升格需要先有实测精确率支撑，见 docs/evaluation.md。
        known_clause_labels: 文档里真实存在的条号集合，供条号一致性检查区分
            "条号"与"数据值"（两者都是 `3.6` 这种形态）。
        answer_mode: `strict` 时报告与旧版一字不差；`hybrid` 额外追加
            `knowledge_label` advisory。
        model_knowledge: 补充段文本。**不参与任何文档自检**——本函数只检查
            调用方传入的 `answer`（hybrid 路径上即 doc_text）。参数保留给
            契约签名与后续日志，避免调用方以为"传了它就会被校验"。
        knowledge_used: 是否真的出现了非空补充段。
        knowledge_citations_stripped: 补充段里被剥离的引用标记数。
        allow_zero_citations: 仅 hybrid 且正文为空/明确否定时由调用方置位；
            此时引用有效性与句级支撑度按"不适用"处理。
    """
    checks = [
        check_citation_validity(
            citations, retrieved_ids, known_ids, known_clause_labels,
            allow_zero_citations=allow_zero_citations,
        ),
        check_numeric_grounding(answer, citations, evidence_by_chunk),
        check_clause_consistency(answer, citations, known_clause_labels),
    ]
    support_check, support_score = check_sentence_support(
        answer, citations, evidence_by_chunk,
        allow_zero_citations=allow_zero_citations,
    )
    if support_gate is not None:
        support_check = CheckResult(
            name=support_check.name,
            passed=support_score >= support_gate,
            severity=CheckSeverity.GATE,
            detail=support_check.detail + f"（已按阈值 {support_gate} 升格为 gate）",
            offenders=support_check.offenders,
        )
    checks.append(support_check)

    # strict 报告不新增任何检查项：以下三项都只在 hybrid 出现。
    if answer_mode == "hybrid":
        checks.append(
            check_doc_sentence_support(
                answer, citations, evidence_by_chunk,
                allow_zero_citations=allow_zero_citations,
            )
        )
        # 引用绑定不看 allow_zero：伪引用即使是通用否定也必须失败（NEW-9）。
        checks.append(check_doc_citation_marks(answer, citations))
        checks.append(check_knowledge_label(knowledge_used, knowledge_citations_stripped))

    report = VerificationReport(checks=checks, support_score=support_score)
    if not report.gates_passed:
        _log.info(
            "自检未通过",
            extra={"detail": "; ".join(f"{c.name}: {c.detail}" for c in report.failed_gates)},
        )
    return report
