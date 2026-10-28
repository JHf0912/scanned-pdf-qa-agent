"""拒答策略与歧义检测。

拒答为什么要分类
----------------
"无法回答"这一句话对使用者是没有用的。真正决定下一步动作的是**为什么**答不了：

- 文档里确实没有 → 换个问法，或者去别处找
- 这一页没识别出来 → 重扫一遍，或换更清晰的件
- 问题有歧义     → 用户补一句限定条件
- 自检没过       → 去看证据，人工判断
- 问的是别的文档 → 换一份材料
- 证据太弱       → 去看证据，确认是否真的相关

把六种情况混成一种，等于把"该做什么"这个信息丢掉了。所以
`RefusalReason` 是六个值，而不是一个布尔。

歧义必须是一个机制，不能只是一个测试类目
----------------------------------------
如果设计里没有歧义检测，一个歧义问题会被"排名第一的那个 chunk"悄悄答掉——
**一个沉默的错误答案比一次拒答糟糕得多**。所以这里要真的去比对候选：

当融合后的前两名来自**不同条款**、且各自给出的值**不一致**时，
返回 AMBIGUOUS_QUESTION 并把候选都列出来让用户选。

阈值是启发式的，这一点会在文档里写明，并用金标集里专门的歧义题来度量它。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import QaConfig
from ..logging import get_logger
from ..models import Chunk, Citation, ClarificationOption, RefusalReason
from .verify import normalize

_log = get_logger("qa.policy")

# 数值型槽位的比对：同一问题下两个候选给出的数字不同 -> 冲突
_SLOT_NUMBER_RE = re.compile(r"[±+\-]?\d+(?:\.\d+)?")


@dataclass
class Decision:
    refused: bool
    reason: RefusalReason | None = None
    detail: str | None = None
    candidates: list[ClarificationOption] | None = None


# 算覆盖率时剔除的词。分两类：
#   1. 疑问词与功能词——它们不承载检索信息；
#   2. **泛化的提问名词**——如"要求""规定""内容"。
#      第二类是实测加进来的：问"抗拉强度要求是多少"，"要求"二字
#      常常并不出现在证据里（原文写的是"应大于等于 590 MPa"），
#      于是覆盖率被无谓地拉低一半，相关的问题被判成不相关。
#      这类词信息量极低（几乎在任何文档里都常见），剔除它们
#      反而让覆盖率更贴近"用户在问的那个具体事物找到了没有"。
_QUESTION_STOPWORDS = frozenset(
    "什么 哪些 哪个 多少 怎么 如何 是否 有没有 请问 告诉 我 你 他 她 它 这 那 的 了 吗 呢 吧 是 有 在 和 与 及 或"
    " 要求 规定 内容 情况 方面 相关 具体 一般 主要 标准 文件 说明 描述 介绍 一下 怎样 什么样"
    " 时候 何时 哪里 哪儿 为什么 怎么 多久".split()
)

# "是否存在/是否规定"型问句。
# 这类问题的正确答案往往是"文档中未规定"——那是一个**有效答案**，不是拒答。
# 所以它们不能走"覆盖率低就拒答"的通道，否则系统会在该回答的地方说"我答不了"。
_EXISTENCE_RE = re.compile(
    r"(?:是否|有没有|有无|是不是)\s*(?:规定|包含|涉及|提及|明确|要求)|"
    r"(?:规定|包含|涉及|提及)了?\s*(?:哪些|什么|没有)"
)


def question_terms(question: str) -> set[str]:
    """抽取问题里的**实词**。

    只保留长度 >= 2 的词与数字/字母串。单字太常见（"的""是"），
    留着会让任何文本都显得"相关"，覆盖率就失去了区分度。
    """
    import jieba

    terms: set[str] = set()
    for tok in jieba.lcut(normalize(question)):
        tok = tok.strip()
        if not tok or tok in _QUESTION_STOPWORDS:
            continue
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-:/]*", tok):
            terms.add(tok)  # 型号、标准号、数值
        elif len(tok) >= 2 and re.search(r"[一-鿿]", tok):
            terms.add(tok)
    return terms


# 问"日期"的问法。答案在原文里往往是**日期字面量**（`2008-09-22 发布`），
# 词面上根本不会出现"日期"二字。
_DATE_ASKING = frozenset({"哪天", "日期", "什么时候", "何时", "年月日", "几号"})

# `normalize()` 已把各类破折号统一成 `-`，所以这里只需要认 `-` 与"年月"两种写法。
_DATE_LITERAL_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{4}年\d{1,2}月")


def _term_covered(term: str, haystack: str) -> bool:
    """某个实词是否出现在文本里（含形态变化的宽松匹配）。"""
    t = normalize(term)
    if t in haystack:
        return True
    # **答案的形态与提问的措辞本来就不必一致。** 这条判据要认得这一点，
    # 否则会把完全可答的问题判成"文档未涵盖"。已知的两种：
    #
    # 1. 形态变化：问"适用范围"，原文写的是"适用于"——整词匹配会漏；
    # 2. 换了表达：问"发布日期是哪天/日期"，原文写的是"2008-09-22 发布"，
    #    字面上没有"日期"二字。语料里这类"问法 ≠ 写法"的落差很常见
    #    （问"多少人"答"3 名"、问"多长"答"12 mm"），
    #    纯词面匹配遇到它们就误判成缺证据。
    #
    # 第 2 条只对**日期问法**开了一条窄路：仅当文本里确实存在日期字面量时才认。
    # 不把它做成通用的"数字→数量词"映射，是因为那会让覆盖率对任何含数字的
    # 文本都虚高，等于把这道闸门拆掉。
    if t in _DATE_ASKING and _DATE_LITERAL_RE.search(haystack):
        return True
    # 退一步用词首二字判断——对 3 字以上的实词，这两个字已足够 specific。
    return len(t) >= 3 and t[:2] in haystack


def query_term_coverage(question: str, text: str) -> float:
    """问题的实词有多大比例出现在**这一段**文本里。

    与 `evidence_coverage` 用同一套实词判据，只是作用范围从"全部证据"
    缩小到"单个候选"。歧义检测需要判断的是"这一段是否真的在回答这个问题"，
    那正是这个量在回答的事。
    """
    terms = question_terms(question)
    if not terms:
        return 1.0
    haystack = normalize(text)
    return sum(1 for t in terms if _term_covered(t, haystack)) / len(terms)


def evidence_coverage(question: str, chunks: list[Chunk]) -> float:
    """问题的实词有多大比例出现在召回的证据里。

    **为什么不用 RRF 融合分做拒答阈值**
    ----------------------------------
    这是实测踩出来的一个坑。RRF 的分数是 `Σ w/(k+rank)`，k=60 时
    排名第 1 与排名第 20 的分数只差 0.012（约 27%），而且这个差距还会
    被多臂求和的平滑效应进一步压缩。也就是说：

    **RRF 适合排序，但不适合表达置信度。** 拿它当拒答阈值，
    要么拦不住（阈值低），要么把相关结果也拦掉（阈值高）——
    因为分数分布本身就没有把"相关"与"不相关"分开。

    覆盖率则是另一回事：它直接问"用户问的那些词，在证据里找到了几个"。
    问"今天天气怎么样"时覆盖率接近 0，问"抗拉强度要求是多少"时很高，
    区分度是天然的，而且量纲可解释、可校准、不依赖任何模型。
    """
    terms = question_terms(question)
    if not terms:
        return 1.0  # 问题里没有实词（如"？"），无从判断，不因此拒答
    haystack = normalize(" ".join(c.text for c in chunks))
    return sum(1 for t in terms if _term_covered(t, haystack)) / len(terms)


def is_existence_question(question: str) -> bool:
    """是否属于"文档有没有规定 X"型问句。

    这类问句在合规与标准场景里占比很高，且它们的**正确答案常常是"未规定"**——
    一个正确、有据可引的答案。把它按"覆盖率低"拒掉，等于在该回答的地方说答不了。
    所以它们不走覆盖率预判，直接交给生成侧，由提示词明确要求回答"未规定"。
    """
    return bool(_EXISTENCE_RE.search(question or ""))


# 歧义的判定阈值。
#
# **为什么没有"分数接近度"这一条**
# ---------------------------------
# 最初的设计里有一条"前两名分数要接近（次/高 ≥ 0.85）"。实测发现它**毫无区分度**：
# 在 RRF 融合下，任意两个候选的分数比几乎恒在 0.95~1.00 之间——
# 因为 RRF 的分数就是 `Σ w/(k+rank)`，k=60 时排名差异被压得极小。
#
# 结果就是：这条判据对**每一对**候选都成立，等于没写；歧义检测因此
# 在 13 道金标题上误报 9 道，其中包括"额定载荷要求是多少"这种
# 证据唯一且明确的问题。**一个把正确答案判成歧义的检查，比没有更糟。**
#
# 这与拒答阈值踩的是同一个坑：**RRF 适合排序，不适合表达置信度或接近度**。
# 所以这里改用真正有区分度的信号——候选与问题的实词覆盖率，见下。
#
# 实测：真歧义 1.00；只是沾边的干扰项 0.33~0.67 —— 0.8 正好切在中间。
# 候选必须**几乎完整地**回答该问题，才有资格成为冲突方。
#
# 为什么要求这么高：阈值放到 0.6 时仍会误报。实测反例——问"示例件的工作温度
# 范围是多少"，第二名是"1 范围"，它含"范围""示例件"两个词，覆盖率 0.67，
# 于是被当成冲突方，一个证据唯一且明确的问题被判成歧义。
#
# 0.8 在实测样本上分得很干净：真正在回答问题的片段覆盖率 1.00，
# 只是"沾边"的干扰项 0.33~0.67。留一点余量（而非死卡 1.0）是为了
# 长问题（实词多）时少一个词不至于被一票否决。
AMBIGUITY_MIN_QUERY_OVERLAP = 0.8


def _query_overlap(query: str, text: str) -> float:
    """问题与候选文本的相关度。保留函数名以免调用方大改，实现已换成实词覆盖率。

    **为什么不用字符集合重叠**（原先的实现）
    ----------------------------------------
    字符集合太粗：真歧义（"报送截止时间为 9 月 10 日" vs "9 月 20 日"）与
    假阳性（"本文件引用了哪些规范性文件" 里的"1 范围"）**都落在 0.60~0.64**，
    这两类在数值上根本分不开，任何阈值都只能二选一地错。

    换成实词覆盖率后区分度就出来了：
    - 真歧义的两段都完整包含 {报送, 截止, 时间} → 1.00
    - "1 范围"里既没有"引用"也没有"规范性" → 0.00

    这也是全项目统一的做法：判断"这段文字是否在回答这个问题"，
    自始至终用同一个量，而不是每处再造一个临时指标。
    """
    return query_term_coverage(query, text)


def detect_ambiguity(
    question: str, hits: list[tuple[Chunk, float]], *, max_candidates: int = 2
) -> list[ClarificationOption]:
    """检测前两名候选之间是否存在**实质性冲突**。

    三个条件**同时**满足才算歧义，缺一不可：

    1. **两者都与问题实质相关**：各自与问题的实词重叠 ≥ 阈值。
       这是主力判据，也是唯一有区分度的那条。
    2. **来自不同条款**：同一段里出现多个数字不算冲突。
    3. **数值集合不一致**：取值相同说明说的是同一件事。

    这三条是实测调出来的，两轮都踩了坑：

    - 第一版只用"不同条款 + 数字不同"两条 → 几乎对每个问题都报歧义，
      因为任意三段文字的数字几乎必然不同；
    - 第二版加了"分数要接近" → **没有任何改善**，因为 RRF 下分数比
      恒在 0.95~1.00，这条对每对候选都成立。

    最终靠**实词重叠**把误报压下去。把这个过程记在这里，是因为
    "歧义检测误报率过高"是这个环节最典型的失败模式，
    而它每次都会伪装成"设计得不够严谨"而不是"判据选错了"。
    """
    if len(hits) < 2:
        return []

    candidates: list[ClarificationOption] = []
    for chunk, _score in hits[:max_candidates]:
        # 唯一有区分度的判据：候选必须与问题实质相关
        if _query_overlap(question, chunk.text) < AMBIGUITY_MIN_QUERY_OVERLAP:
            continue
        numbers = frozenset(_SLOT_NUMBER_RE.findall(normalize(chunk.text)))
        if not numbers:
            continue
        candidates.append(
            ClarificationOption(
                interpretation="；".join(p for p in chunk.clause_path if p)
                or f"第 {chunk.page_no} 页",
                value="、".join(sorted(numbers))[:80],
                citation=Citation(
                    page_no=chunk.page_no,
                    clause_label=chunk.clause_label,
                    chunk_id=chunk.chunk_id,
                    quote=chunk.text[:120],
                    para_no=chunk.ordinal,
                ),
            )
        )

    # 条件 3+4：至少两个候选、来自不同条款、且取值不同
    if len(candidates) < 2:
        return []
    labels = {c.citation.clause_label or c.citation.chunk_id for c in candidates}
    if len(labels) < 2 or len({c.value for c in candidates}) < 2:
        return []
    return candidates


def decide(
    *,
    cfg: QaConfig,
    top_score: float,
    second_score: float,
    gates_passed: bool,
    failed_gate_names: list[str],
    evidence_chunks: list[Chunk],
    failed_pages: set[tuple[str, int]],
    candidates: list[ClarificationOption],
    coverage: float = 1.0,
    existence_question: bool = False,
) -> Decision:
    """决定是否拒答，以及拒答的理由。

    判定顺序是有讲究的——**从"最确定的原因"到"最不确定的原因"**：
    先看证据是否触及了识别失败的页（这是硬事实），再看歧义（有具体候选），
    然后看自检（有具体失败项），最后才看分数阈值（纯统计判断）。
    顺序反了会把明确的问题报成模糊的问题。
    """
    # 1) 证据触及识别失败的页 —— 我们根本不知道那页写了什么
    #
    # **必须按 (doc_id, page_no) 比对，不能只比页码。**
    # 多文档检索时，不同文档的第 3 页是两回事：A 文档第 3 页识别失败、
    # B 文档第 3 页好好的，只比页码会让 B 的证据**误触发一次拒答**。
    # 拒答本身是安全侧的，但误拒答会让人不再信任拒答。
    bad_pages = sorted(
        c.page_no for c in evidence_chunks if (c.doc_id, c.page_no) in failed_pages
    )
    if bad_pages:
        return Decision(
            refused=True,
            reason=RefusalReason.EXTRACTION_FAILED,
            detail=(
                f"证据涉及未成功识别的页面：{bad_pages}。"
                "这不是「文档里没有」，而是「这一页没读出来」——"
                "请重新识别该页（或提供更清晰的扫描件）后再问。"
            ),
        )

    # 2) 有具体候选的歧义
    if candidates:
        return Decision(
            refused=True,
            reason=RefusalReason.AMBIGUOUS_QUESTION,
            detail=(
                f"检索到 {len(candidates)} 个取值不一致的候选，问题需要进一步限定。"
                f"请指明你问的是哪一个。"
            ),
            candidates=candidates,
        )

    # 3) 有证据但自检不过
    if not gates_passed:
        return Decision(
            refused=True,
            reason=RefusalReason.VERIFICATION_FAILED,
            detail=(
                f"生成的答案未通过自检：{'、'.join(failed_gate_names)}。"
                f"证据已附在下方，请人工核对。"
            ),
        )

    # 4) 完全没有证据
    if not evidence_chunks:
        return Decision(
            refused=True,
            reason=RefusalReason.NOT_IN_DOCUMENT,
            detail="检索未在文档中找到任何相关片段。可尝试换个问法，或确认问题是否属于本文档范围。",
        )

    # 5) 证据覆盖不足（主判据；见 evidence_coverage 的说明）
    #
    # 存在型问句（"是否规定了 X"）走豁免：它们的正确答案常常就是"未规定"，
    # 而"未规定"天然不会有高覆盖率——文档里根本没写，覆盖率为 0 恰恰是**预期**。
    # 不豁免的话，系统会在最该回答"未规定"的地方说"文档里没有"。
    #
    # 注意这个豁免原先只写在 agent 的预判里，`decide()` 又无条件地按覆盖率拒了一次——
    # 于是豁免被架空，G1 仍然被拒。**豁免必须落在最终裁决处，而不是某一处预判里。**
    if not existence_question and coverage < cfg.min_coverage:
        return Decision(
            refused=True,
            reason=RefusalReason.NOT_IN_DOCUMENT,
            detail=(
                f"问题中的关键词只有 {coverage:.0%} 出现在文档里"
                f"（阈值 {cfg.min_coverage:.0%}），判断为文档未涵盖该问题。"
                f"可尝试换个问法，或确认问题是否属于本文档范围。"
            ),
        )

    # 6) 融合分过低（辅助判据，放在最后）
    if top_score < cfg.min_top_score:
        return Decision(
            refused=True,
            reason=RefusalReason.LOW_CONFIDENCE,
            detail=(
                f"最高匹配分 {top_score:.4f} 低于阈值 {cfg.min_top_score}，"
                f"证据相关性不足。已附上最相关的片段供人工判断。"
            ),
        )

    # 7) 第一名与第二名咬得太紧（可选，默认关闭）
    #
    # 这一条解决的是**另一种**不确定性：分数都不低，但没有明确赢家。
    # 典型形态是两份文件对同一件事的规定不同，或同一份文档里
    # "3.4 允许倒圆"与"3.7 允许不倒圆"这类互相限定的条款。
    # 此时给一个答案比不给更危险——读者会以为系统已经判断过哪一条适用。
    #
    # `second_score` 在此之前是**传进来却没人读**的：索引层算好、四处调用点
    # 一路传参、`decide()` 收下就丢掉。而 `min_score_margin` 更彻底——
    # 在 config 与 YAML 里声明着，全仓零处读取。两个"看起来能用"的旋钮
    # 加起来等于没有。现在它们各自有明确分工：`min_top_score` 管绝对下限，
    # `min_score_margin` 管相对差距。
    if cfg.min_score_margin > 0 and second_score > 0:
        margin = top_score - second_score
        if margin < cfg.min_score_margin:
            return Decision(
                refused=True,
                reason=RefusalReason.LOW_CONFIDENCE,
                detail=(
                    f"最相关的两段证据分数接近（{top_score:.4f} vs {second_score:.4f}，"
                    f"差距 {margin:.4f} < {cfg.min_score_margin}），"
                    f"系统无法判断哪一段才是你要问的。"
                    f"建议补充限定条件（条款号、表格列名或文件版本）后重问。"
                ),
            )

    return Decision(refused=False)


def is_in_document_negative(answer_text: str) -> bool:
    """识别"文档明确说没有"这种否定型答案。

    它**不是拒答**：`"本标准未规定键的材料牌号"` 是一个正确、有据可引的答案。
    把它误判成拒答，会让系统在该给答案的地方说"我答不了"——
    这是 RAG 系统里一个很常见、也很容易被忽略的失败模式。
    """
    if not answer_text:
        return False
    markers = ("未规定", "没有规定", "未涉及", "不涉及", "未作规定", "并未规定", "未包含", "不包含")
    return any(m in answer_text for m in markers)
