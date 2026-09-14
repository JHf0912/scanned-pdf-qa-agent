"""评估指标。全部是确定性实现，不依赖评委模型。

为什么不用现成的 RAG 评估框架
------------------------------
调研里有一条对我们**特别关键**的结论：RAGAS 一类基于 LLM-as-judge 的框架，
在**金融长文本/表格场景下 Context Recall 的误判率报告高达 37%**，
而且不同工具的"Faithfulness"定义互不等价。

我们的文档正是表格密集的。所以主指标一律做成确定性的：
给定同样的输入，结果必然一样，可以单元测试，也可以在报告里写清公式。
LLM 判定只作交叉参考，且必须标注其局限。

中文 CER 的两个坑
-----------------
1. **jiwer 默认按空格分词**——对中文是错的，整句话会被当成一个 token，
   CER 恒为 0 或 1。必须用字符级变换 `SentencesToListOfCharacters()`。
2. **繁简差异会被算成替换错误**。OCR 可能输出繁体变体，不归一就虚高 CER。
   所以先过 `zhconv` 做繁简归一。

另外报告**两个 CER**：归一化后的（可与公开数字比较）与
**符号/数字敏感**的（在原始文本上算）。国标里 `≤`、`%`、`±AT8/2`、`1:100`
都是承载语义的，去标点的 CER 恰好会掩盖问答系统最需要抓住的那类错误。
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

from ..logging import get_logger

_log = get_logger("eval.metrics")


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """二项比例的 Wilson 置信区间。

    为什么不用 success/total 直接报：本项目的样本量很小（金标集十几题、
    每类只有几题），一个"100%"很可能只是 n=3 的假象。Wilson 区间在
    小样本与极端比例下都比正态近似可靠，能如实表达"这个数字有多不确定"。
    """
    if total == 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def normalize_for_cer(text: str) -> str:
    """CER 归一化：繁简统一 + 全角转半角 + 压空白。"""
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    try:
        from zhconv import convert

        out = convert(out, "zh-cn")
    except ImportError:  # pragma: no cover - 依赖在 eval extra 里
        _log.warning("未安装 zhconv，CER 未做繁简归一（会虚高）")
    out = re.sub(r"\s+", "", out)
    return out


def strip_symbols(text: str) -> str:
    """去掉标点与符号，只留文字、数字与字母。用于"归一化 CER"。"""
    return re.sub(r"[^\w一-鿿]", "", text)


@dataclass
class CerResult:
    normalized: float  # 归一化 CER（去标点，可与公开数字比较）
    strict: float  # 符号/数字敏感 CER（保留标点与单位）
    ref_len: int
    hyp_len: int
    edits: int
    available: bool = True
    note: str = ""


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein 距离。自己实现是为了不引入 jiwer 这个硬依赖——
    评估指标的核心算法应当是可见、可测的，而不是藏在一个黑盒包里。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(reference: str, hypothesis: str) -> CerResult:
    """字符错误率。中文没有词边界，按字符算才是正确做法。"""
    ref_norm = strip_symbols(normalize_for_cer(reference))
    hyp_norm = strip_symbols(normalize_for_cer(hypothesis))
    ref_strict = normalize_for_cer(reference)
    hyp_strict = normalize_for_cer(hypothesis)

    if not ref_norm and not ref_strict:
        return CerResult(0.0, 0.0, 0, 0, 0, available=False, note="参考文本为空")

    edits_norm = _edit_distance(ref_norm, hyp_norm)
    edits_strict = _edit_distance(ref_strict, hyp_strict)
    return CerResult(
        normalized=edits_norm / len(ref_norm) if ref_norm else 0.0,
        strict=edits_strict / len(ref_strict) if ref_strict else 0.0,
        ref_len=len(ref_norm),
        hyp_len=len(hyp_norm),
        edits=edits_norm,
    )


def character_accuracy(cer_value: float) -> float:
    """字符准确率 = 1 - CER。国标的验收参照值见 docs/evaluation.md：
    档案行业标准 DA/T 77—2019 要求印刷体中文 ≥95%，招标常见要求 ≥99%。"""
    return max(0.0, 1.0 - cer_value)


# ---------------------------------------------------------------- 检索指标

def recall_at_k(ranked_ids: list[str], relevant_ids: set[str], k: int) -> float:
    if not relevant_ids:
        return 0.0
    return len(set(ranked_ids[:k]) & relevant_ids) / len(relevant_ids)


def reciprocal_rank(ranked_ids: list[str], relevant_ids: set[str]) -> float:
    for i, cid in enumerate(ranked_ids, start=1):
        if cid in relevant_ids:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranked_ids: list[str], relevant_ids: set[str], k: int) -> float:
    dcg = sum(
        1.0 / math.log2(i + 1)
        for i, cid in enumerate(ranked_ids[:k], start=1)
        if cid in relevant_ids
    )
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant_ids), k) + 1))
    return dcg / ideal if ideal > 0 else 0.0


# ---------------------------------------------------------------- 表格指标

def table_cell_accuracy(expected: list[list[str]], actual: list[list[str]]) -> dict:
    """表格单元格级准确率。

    行列数不一致时不强行对齐——那只会产生一个好看但无意义的数字。
    改为按"能对上的部分"计算，并**如实报告维度不匹配**。
    """
    if not expected:
        return {"cell_accuracy": 0.0, "shape_match": False, "expected_shape": (0, 0), "actual_shape": (0, 0)}
    exp_rows, exp_cols = len(expected), max(len(r) for r in expected)
    act_rows = len(actual)
    act_cols = max((len(r) for r in actual), default=0)

    correct = 0
    compared = 0
    for r in range(min(exp_rows, act_rows)):
        for c in range(min(len(expected[r]), len(actual[r]) if r < act_rows else 0)):
            compared += 1
            if normalize_for_cer(expected[r][c]) == normalize_for_cer(actual[r][c]):
                correct += 1
    return {
        "cell_accuracy": correct / compared if compared else 0.0,
        "cells_correct": correct,
        "cells_compared": compared,
        "shape_match": (exp_rows, exp_cols) == (act_rows, act_cols),
        "expected_shape": (exp_rows, exp_cols),
        "actual_shape": (act_rows, act_cols),
    }


# ---------------------------------------------------------------- 数值字段

_NUM_FIELD_RE = re.compile(r"[±+\-]?\d+(?:[.:/]\d+)*\s*(?:MPa|mm|%)?", re.IGNORECASE)


def _normalize_keep_breaks(text: str) -> str:
    """数值提取专用的归一化：繁简统一 + 全角转半角 + 折叠空白，**但保留换行**。

    为什么不直接用 `normalize_for_cer`
    ----------------------------------
    它用 `re.sub(r"\\s+", "", ...)` 把**换行也压掉了**。对 CER 来说这是对的
    （比对时不该被换行影响），但对**数值提取**是灾难：

    - 跨行：`… Q/EXAMPLE 001—2026` 换行后接 `1  范围`，
      压完变成 `001—20261`，正则把 `20261` 当成**一个**数——
      于是参考里的 `1` 被报成"漏掉"，结果里多出一个 `20261`；
    - 同行：`1.0  1.0  1.5` 压成 `1.01.01.5`，而正则的小数部分允许 `.`，
      整串被吃成**一个**数。

    两种情况下，召回率都会被无端拉低——而它看起来只是个"识别得不准"的数字，
    不会有人怀疑是度量本身错了。

    保留空白与换行后，数字之间的边界就还在，同时也还能捕获随后的单位
    （`590 MPa` 仍是一个完整字段）。
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    try:
        from zhconv import convert

        out = convert(out, "zh-cn")
    except ImportError:  # pragma: no cover - 依赖在 eval extra 里
        pass
    return re.sub(r"[^\S\n]+", " ", out)


def numeric_field_accuracy(reference: str, hypothesis: str) -> dict:
    """数值字段准确率：参考文本里出现的每个数值，是否在结果中原样出现。

    这是比 CER 更贴近业务的一层——一个字符之差（1.0 与 1.5）
    在 CER 上只值 1/50，在业务上却是"完全错误的答案"。
    """
    ref_nums = {m.group(0).strip()
                for m in _NUM_FIELD_RE.finditer(_normalize_keep_breaks(reference))}
    hyp_nums = {m.group(0).strip()
                for m in _NUM_FIELD_RE.finditer(_normalize_keep_breaks(hypothesis))}
    if not ref_nums:
        return {"field_recall": 1.0, "field_precision": 1.0, "missing": [], "spurious": []}
    missing = sorted(ref_nums - hyp_nums)
    spurious = sorted(hyp_nums - ref_nums)
    return {
        "field_recall": len(ref_nums & hyp_nums) / len(ref_nums),
        "field_precision": len(ref_nums & hyp_nums) / len(hyp_nums) if hyp_nums else 0.0,
        "missing": missing,
        "spurious": spurious,
    }
