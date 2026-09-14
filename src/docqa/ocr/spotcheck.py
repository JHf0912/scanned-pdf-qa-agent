"""跨引擎一致性校验：防的是**证据本身被识别错**。

它补的是哪块缺口
----------------
`qa/verify.py` 里的数值落地校验的是"答案是否忠实于证据"，**不是"证据是否为真"**。
如果 OCR 把 `〔2026〕15号` 读成 `13号`，模型照抄 `13号`，
所有 gate 都会通过，系统自信地给出一个错文号。

这是两件不同的事，需要两套机制：

| 威胁 | 由谁负责 |
|---|---|
| 模型编造内容 | `qa/verify.py`（引用有效性 / 数值落地 / 条款一致性） |
| **证据被识别错** | **本模块**（跨引擎比对）+ `uncertain` 标记 + `EXTRACTION_FAILED` 拒答 |

为什么不做"多模型投票"
----------------------
投票需要一个能被检验的正确性判据，否则只是把一个不确定的数字变成三个。
所以这里的做法是**窄域重抽取 + 差异比对**：

- 只比 `数值 / 标准号 / 人名 / 日期` 这几类**高风险字段**，不做全文比对；
- 输出的是**可评估的标记**（不一致处置 `needs_review`），
  它的精确率/召回率会在 `eval/` 里对着人工转写实测；
- **实测不达标就不上线**，并在文档里写明"测过、没上、原因是什么"。

一个无法被证伪的标记——不报错也不报对的数字——比没有标记更糟。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..logging import get_logger
from ..models import OcrPage

_log = get_logger("ocr.spotcheck")

# 高风险字段：识别错了代价最大、且最不容易被肉眼发现的那些
_DOC_ID_RE = re.compile(r"(?:GB/T|GB|JB/T|ISO|DA/T|Q/)[\s]*\d+(?:\.\d+)*(?:[—\-]\d{4})?")
_DATE_RE = re.compile(r"\d{4}\s*[-—年]\s*\d{1,2}\s*[-—月]\s*\d{1,2}\s*日?")
_NUMBER_RE = re.compile(r"[±+\-]?\d+(?:\.\d+)?(?:\s*[:/]\s*\d+(?:\.\d+)?)*")
# 姓名：中文 2~4 字，仅在人名标记后出现时才算
_NAME_RE = re.compile(r"(?:组长|组员|成员|签名|起草人|负责人|联系人)\s*[：:]\s*([一-鿿]{2,4})")


@dataclass
class ClaimDiff:
    """两份识别结果在高风险字段上的差异。"""

    agreed: bool
    only_in_a: list[str] = field(default_factory=list)
    only_in_b: list[str] = field(default_factory=list)
    engine_a: str = ""
    engine_b: str = ""

    @property
    def n_conflicts(self) -> int:
        return len(self.only_in_a) + len(self.only_in_b)

    def describe(self) -> str:
        if self.agreed:
            return f"两个引擎在 {self.engine_a} / {self.engine_b} 上一致"
        return (
            f"{self.engine_a} 独有 {self.only_in_a}；"
            f"{self.engine_b} 独有 {self.only_in_b}"
        )


def extract_high_risk_fields(text: str) -> set[str]:
    """抽出高风险字段。刻意做得**窄**——宁可比少了，也不要引入噪声差异。

    宽泛的比对会把标点、空白、断行差异都算成"不一致"，
    产生大量假警报，最终使用者会忽略这个标记。
    """
    if not text:
        return set()
    fields: set[str] = set()
    fields.update(m.group(0).replace(" ", "") for m in _DOC_ID_RE.finditer(text))
    fields.update(m.group(0).replace(" ", "") for m in _DATE_RE.finditer(text))
    fields.update(m.group(0).replace(" ", "") for m in _NUMBER_RE.finditer(text))
    fields.update(m.group(1) for m in _NAME_RE.finditer(text))
    return {f for f in fields if f}


def diff_claims(text_a: str, text_b: str, *, engine_a: str = "A", engine_b: str = "B") -> ClaimDiff:
    """比对两份识别文本的高风险字段。"""
    fa = extract_high_risk_fields(text_a)
    fb = extract_high_risk_fields(text_b)
    only_a = sorted(fa - fb)
    only_b = sorted(fb - fa)
    return ClaimDiff(
        agreed=not only_a and not only_b,
        only_in_a=only_a,
        only_in_b=only_b,
        engine_a=engine_a,
        engine_b=engine_b,
    )


def spotcheck_page(page: OcrPage, other: OcrPage) -> ClaimDiff:
    """比对同一页由两个引擎产生的识别结果，并把差异写回 `needs_review`。

    标记落在**块级**而非页级：页级标记只能告诉用户"这页可能有问题"，
    块级才能直接指到"就是这一行的这个数字存疑"。
    """
    diff = diff_claims(
        page.plain_text(),
        other.plain_text(),
        engine_a=page.provider or page.model,
        engine_b=other.provider or other.model,
    )

    if not diff.agreed:
        conflicting = set(diff.only_in_a) | set(diff.only_in_b)
        for block in page.blocks:
            if any(f in block.text for f in conflicting):
                block.needs_review = True
        page.warnings.append(f"跨引擎校验发现 {diff.n_conflicts} 处字段不一致：{diff.describe()}")
        _log.warning(
            "跨引擎校验不一致",
            extra={"page": page.page_no, "detail": diff.describe()[:300]},
        )

    return diff


def evaluate_spotcheck(
    agreements: list[tuple[bool, bool]],
) -> dict:
    """评估这个标记本身值不值得信。

    Args:
        agreements: [(标记为不一致, 人工核对确实有错), ...]

    Returns:
        精确率 / 召回率 / F1。**只有在实测达标后才应当把它接入流水线**；
        不达标就写"测过、没上、原因是什么"，那比一个半成品得分更高。
    """
    tp = sum(1 for flagged, truly_wrong in agreements if flagged and truly_wrong)
    fp = sum(1 for flagged, truly_wrong in agreements if flagged and not truly_wrong)
    fn = sum(1 for flagged, truly_wrong in agreements if not flagged and truly_wrong)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "n": len(agreements),
        "tp": tp, "fp": fp, "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }
