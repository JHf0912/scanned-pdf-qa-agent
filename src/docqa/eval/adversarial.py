"""对抗变异套件：验证自检**真的能抓住错误**。

为什么这是全套交付里性价比最高的一项
------------------------------------
"我做了自检"是一句无法证伪的话。要让它可证伪，就得**造出已知的错误，
看自检抓不抓得住**，并报出按变异类型的检出率。

这件事还有一个别的测试做不到的性质：**它不受语料规模限制**。
本项目主文档只有 4 页，检索指标没有统计意义；但一页素材就能生成
20 个以上的变异样本，样本量是够的。

变异类型刻意对应真实的失败模式
------------------------------
- `number`：把 590 改成 950 —— 最常见的数值幻觉
- `unit`：把 MPa 改成 kPa —— 单位幻觉，读起来完全合理
- `docid`：把 GB/T 11334—2005 改成 2006 —— 文号幻觉
- `citation`：删掉引用 —— 无出处的结论
- `clause`：把 3.6 说成 3.5 —— 串条
- `fabricate`：凭空加一句证据里没有的话

**检出率低不一定说明自检差，也可能是变异本身超出了自检的设计范围**
（例如只靠数值落地的机制抓不住 `fabricate`）。报告必须按类型分开列，
并说明每类是由哪条检查负责的——这样才能看出"缺口在哪"。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from ..logging import get_logger
from ..models import Citation, VerificationReport
from ..qa.verify import verify

_log = get_logger("eval.adversarial")

FABRICATED_SENTENCES = (
    "此外，本标准还规定了产品的包装材料应为可降解材料。",
    "同时要求生产企业必须通过 ISO 9001 质量管理体系认证。",
    "该条款自 2010 年起已被修订，现行要求更为严格。",
)


@dataclass
class Mutation:
    kind: str
    description: str
    answer: str
    citations: list[Citation]


@dataclass
class DetectionResult:
    kind: str
    detected: int = 0
    total: int = 0
    misses: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.detected / self.total if self.total else 0.0


_NUM_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])")
_DOCID_RE = re.compile(r"((?:GB/T|GB|ISO|DA/T)\s*\d+(?:\.\d+)*[—\-])(\d{4})")


def _mutate_number(answer: str, rng: random.Random) -> str | None:
    """改一个数字。避开引用标记里的页码与条号。"""
    body = re.sub(r"\[p\.\d+[^\]]*\]", " ", answer)
    matches = list(_NUM_RE.finditer(body))
    if not matches:
        return None
    m = rng.choice(matches)
    old = m.group(1)
    try:
        val = float(old)
    except ValueError:
        return None
    new = str(int(val) + rng.choice([1, 9, 10])) if val >= 1 else f"{val + 0.5:.1f}"
    if new == old:
        return None
    return body[: m.start(1)] + new + body[m.end(1) :]


def _mutate_docid(answer: str, rng: random.Random) -> str | None:
    m = _DOCID_RE.search(answer)
    if not m:
        return None
    year = int(m.group(2)) + rng.choice([-4, 1, 3])
    return answer[: m.start(2)] + str(year) + answer[m.end(2) :]


def _mutate_unit(answer: str) -> str | None:
    for old, new in (("MPa", "kPa"), ("mm", "cm"), ("%", "‰")):
        if old in answer:
            return answer.replace(old, new, 1)
    return None


def _mutate_clause(answer: str, rng: random.Random) -> str | None:
    """把答案里提到的条号改掉。"""
    matches = list(re.finditer(r"(?<![\d.])(\d+\.\d+)(?![\d.])", answer))
    if not matches:
        return None
    m = rng.choice(matches)
    old = m.group(1)
    new = f"{old[:-1]}{int(old[-1]) + rng.choice([1, 2])}"
    return answer[: m.start(1)] + new + answer[m.end(1) :]


def _drop_citations(answer: str) -> str | None:
    stripped = re.sub(r"\[p\.\d+[^\]]*\]", "", answer).strip()
    return stripped if stripped != answer.strip() else None


def _fabricate(answer: str, rng: random.Random) -> str:
    return answer.rstrip() + " " + rng.choice(FABRICATED_SENTENCES)


def generate_mutations(
    answer: str, citations: list[Citation], *, seed: int = 0, per_kind: int = 4
) -> list[Mutation]:
    """从一条正确回答生成一批已知错误的变异。"""
    rng = random.Random(seed)
    out: list[Mutation] = []

    for _ in range(per_kind):
        if (m := _mutate_number(answer, rng)) and m != answer:
            out.append(Mutation("number", "改动了数值", m, citations))
    for _ in range(per_kind):
        if (m := _mutate_docid(answer, rng)) and m != answer:
            out.append(Mutation("docid", "改动了标准号年份", m, citations))
    if (m := _mutate_unit(answer)) and m != answer:
        out.append(Mutation("unit", "替换了单位", m, citations))
    for _ in range(per_kind):
        if (m := _mutate_clause(answer, rng)) and m != answer:
            out.append(Mutation("clause", "改动了条号", m, citations))
    if (m := _drop_citations(answer)) is not None:
        out.append(Mutation("citation", "删除了全部引用", m, []))
    out.append(Mutation("fabricate", "凭空添加了一句证据中没有的话", _fabricate(answer, rng), citations))

    return out


def run_adversarial(
    samples: list[tuple[str, list[Citation]]],
    *,
    retrieved_ids: set[str],
    known_ids: set[str],
    evidence_by_chunk: dict[str, str],
    support_gate: float | None = None,
    seed: int = 0,
) -> dict:
    """对一批正确回答做变异，统计自检的检出率。

    Args:
        samples: [(正确答案, 其引用), ...]

    Returns:
        按变异类型分组的检出率报告。
    """
    by_kind: dict[str, DetectionResult] = {}
    total_mutations = 0

    for answer, citations in samples:
        for mut in generate_mutations(answer, citations, seed=seed):
            total_mutations += 1
            result = by_kind.setdefault(mut.kind, DetectionResult(kind=mut.kind))
            result.total += 1

            report: VerificationReport = verify(
                mut.answer,
                mut.citations,
                retrieved_ids=retrieved_ids,
                known_ids=known_ids,
                evidence_by_chunk=evidence_by_chunk,
                support_gate=support_gate,
            )
            if not report.gates_passed:
                result.detected += 1
            else:
                result.misses.append(mut.description)

    overall_detected = sum(r.detected for r in by_kind.values())
    return {
        "total_mutations": total_mutations,
        "overall_detection_rate": overall_detected / total_mutations if total_mutations else 0.0,
        "by_kind": {
            kind: {
                "detected": r.detected,
                "total": r.total,
                "rate": round(r.rate, 3),
                "misses": r.misses[:3],
            }
            for kind, r in sorted(by_kind.items())
        },
        # 哪条检查负责哪类变异——写清楚才能看出缺口在哪
        "responsibility": {
            "number": "numeric_grounding",
            "docid": "numeric_grounding",
            "unit": "numeric_grounding",
            "clause": "clause_consistency / numeric_grounding",
            "citation": "citation_validity",
            "fabricate": "（无专门机制——纯文字编造需要句级支撑度或人工复核）",
        },
    }
