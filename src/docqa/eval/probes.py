"""离线探针：把评估层"未知/未测量"的两项变成可复现的数字。

要回答的问题
------------
`docs/evaluation.md` 与 README 里有几处诚实的"未知"：

1. **句级支撑度是启发式的**——它按"答案句子与证据的字符重叠"打分，
   但**没有实测过精确率**，所以默认只上报、不当 gate。
2. **跨引擎一致性校验的精确率未知**——机制已接进 `ingest`，
   但它标出的可疑字段里有多少是真的错，没有数字。

这两项都不需要模型、不需要真实文档，**在合成件上就能离线测量**：

- 句级支撑度：程序化构造"应当有支撑"与"不应当有支撑"的答案样本，
  直接调用 `qa.verify.check_sentence_support`，按"把它当门"的口径
  算精确率/召回率；
- 跨引擎校验：把同一页文本当成引擎 A，**定点改写一个高风险字段**
  当作引擎 B 的识别错误，再用 `ocr.spotcheck.diff_claims` 判断是否报警；
  另配若干"只改无关字符"的对照样本，用来量假警报。

必须说清楚的边界
----------------
这些数字衡量的是**评估机制本身**，不是 OCR 精度，也不是真实文档上的表现：

- 合成件的识别结果是构造的，字段分布与真实扫描件不同；
- 注入的错误只落在检测器覆盖的字段类型（数值/文号/日期）内，
  对字符级 OCR 错误不报警——那是设计上的窄域，不是回归；
- 句级支撑度的正例来自证据原句，词面重叠最高，所以测出的精确率是**上限**，
  真实答案一旦改述，假阳性只会更多。

结论写进 `docs/evaluation.md`，命令是 `uv run docqa eval-probes`。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from ..models import Chunk, Citation, OcrPage
from ..ocr.spotcheck import diff_claims, iter_high_risk_field_spans
from ..qa.verify import check_sentence_support
from .metrics import wilson_interval

# 与 `check_sentence_support` 内部一致：句子最短长度与"判定有支撑"的阈值。
SENTENCE_MIN_LEN = 6
SUPPORT_THRESHOLD = 0.5

# 一条启发式要升格为 gate，至少需要跨过的门槛。
# 这些值不是行业标准，而是本项目的立场：**未达标的门禁会把用户训练成忽略告警**。
GATE_PRECISION_BAR = 0.9
GATE_RECALL_BAR = 0.9
GATE_MIN_SAMPLES = 20

_SENTENCE_SPLIT_RE = re.compile(r"[。！？；\n]")

# 改述样本用的替换表。刻意保守：只换同义表达，不改变语义。
# 它用来暴露句级支撑度的真实弱点——**词面重叠不等于语义支撑**。
PARAPHRASE_PAIRS: tuple[tuple[str, str], ...] = (
    ("应不低于", "不得低于"),
    ("应大于等于", "不得小于"),
    ("本文件", "本标准"),
    ("适用于", "适用对象为"),
    ("规定了", "对……作出了规定"),
    ("要求为", "要求是"),
)

# 这些句子与合成件主题无关，用来构造"证据里根本没有"的答案。
# 与 `eval.adversarial.FABRICATED_SENTENCES` 同源，但这里不 import 它——
# 探针需要的是一个**稳定、可解释**的负例集合，而不是随对抗套件演进而变。
UNSUPPORTED_SENTENCES: tuple[str, ...] = (
    "此外，本标准还规定了产品的包装材料应为可降解材料。",
    "同时要求生产企业必须通过 ISO 9001 质量管理体系认证。",
    "该条款自 2010 年起已被修订，现行要求更为严格。",
    "包装箱内应附有中文说明书与合格证，并注明生产批号。",
)


# ---------------------------------------------------------------- 句级支撑度


@dataclass(frozen=True)
class SupportSample:
    kind: str
    expected_supported: bool
    answer: str
    chunk_id: str


@dataclass
class _SupportRun:
    sample: SupportSample
    score: float
    flagged: bool  # True = 启发式认为"支撑不足"


def _split_sentences(text: str) -> list[str]:
    return [
        s.strip()
        for s in _SENTENCE_SPLIT_RE.split(text or "")
        if len(s.strip()) >= SENTENCE_MIN_LEN
    ]


def _cite(chunk: Chunk) -> Citation:
    return Citation(
        page_no=chunk.page_no,
        clause_label=chunk.clause_label,
        chunk_id=chunk.chunk_id,
        quote="",
        doc_id=chunk.doc_id,
    )


def _paraphrase(sentence: str) -> str | None:
    for old, new in PARAPHRASE_PAIRS:
        if old in sentence:
            return sentence.replace(old, new, 1)
    return None


def _build_support_samples(chunks: list[Chunk], *, seed: int = 0) -> list[SupportSample]:
    """从语料里构造带标签的样本。

    正例（应判"有支撑"）：
      - `extractive`：证据原句；
      - `reordered`：同一证据里的两句，调换顺序后拼接（顺序不应影响支撑度）；
      - `paraphrase`：同义替换后的原句（**语义仍有支撑，但词面重叠下降**）。

    负例（应判"支撑不足"）：
      - `fabricated`：与文档无关的整句；
      - `mixed`：一句真 + 一句假（边界样本：2 句里 1 句有支撑，分数正好 0.5）；
      - `other_chunk`：引用了 A 片段，答案却取自 B 片段（串内容）。
    """
    rng = random.Random(seed)
    eligible = sorted(
        (c for c in chunks if c.kind != "table" and len(c.text) >= 30),
        key=lambda c: c.chunk_id,
    )
    samples: list[SupportSample] = []

    for i, chunk in enumerate(eligible):
        sentences = _split_sentences(chunk.text)
        if not sentences:
            continue
        true_sentence = max(sentences, key=len)

        samples.append(SupportSample("extractive", True, true_sentence, chunk.chunk_id))

        if len(sentences) >= 2:
            samples.append(
                SupportSample(
                    "reordered", True,
                    f"{sentences[1]}。{sentences[0]}", chunk.chunk_id,
                )
            )

        if (para := _paraphrase(true_sentence)) and para != true_sentence:
            samples.append(SupportSample("paraphrase", True, para, chunk.chunk_id))

        fake = rng.choice(UNSUPPORTED_SENTENCES)
        samples.append(SupportSample("fabricated", False, fake, chunk.chunk_id))
        samples.append(
            SupportSample("mixed", False, f"{true_sentence}。{fake}", chunk.chunk_id)
        )

        others = [c for c in eligible if c.chunk_id != chunk.chunk_id]
        if others:
            other_sentences = _split_sentences(others[(i + 1) % len(others)].text)
            if other_sentences:
                samples.append(
                    SupportSample(
                        "other_chunk", False, max(other_sentences, key=len), chunk.chunk_id
                    )
                )

    return samples


def _confusion(runs: list[_SupportRun]) -> dict:
    tp = sum(1 for r in runs if not r.sample.expected_supported and r.flagged)
    fp = sum(1 for r in runs if r.sample.expected_supported and r.flagged)
    fn = sum(1 for r in runs if not r.sample.expected_supported and not r.flagged)
    tn = sum(1 for r in runs if r.sample.expected_supported and not r.flagged)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "specificity": round(tn / (tn + fp), 4) if (tn + fp) else 0.0,
        "precision_ci": list(wilson_interval(tp, tp + fp)),
        "recall_ci": list(wilson_interval(tp, tp + fn)),
    }


def _gate_recommendation(*, n: int, precision: float, recall: float) -> str:
    gaps: list[str] = []
    if n < GATE_MIN_SAMPLES:
        gaps.append(f"样本量 {n} < {GATE_MIN_SAMPLES}")
    if precision < GATE_PRECISION_BAR:
        gaps.append(f"精确率 {precision:.1%} < {GATE_PRECISION_BAR:.0%}")
    if recall < GATE_RECALL_BAR:
        gaps.append(f"召回率 {recall:.1%} < {GATE_RECALL_BAR:.0%}")
    if gaps:
        return (
            "**不建议升格为 gate**（" + "；".join(gaps) + "）。"
            "维持咨询性指标：上报但不拦截。一个未达标的门禁会把用户训练成忽略告警。"
        )
    return (
        f"在该样本集上达到建议门槛（n≥{GATE_MIN_SAMPLES}，精确率/召回率均 ≥"
        f"{GATE_PRECISION_BAR:.0%}）。**仍不据此直接升格**：本实验的正例是证据原句，"
        "精确率是上限，需要在真实文档的人工标注答案上复测。"
    )


def probe_sentence_support(chunks: list[Chunk], *, seed: int = 0) -> dict:
    """测句级支撑度作为门时的精确率/召回率。"""
    samples = _build_support_samples(chunks, seed=seed)
    evidence = {c.chunk_id: c.text for c in chunks}
    by_id = {c.chunk_id: c for c in chunks}

    runs: list[_SupportRun] = []
    for sample in samples:
        chunk = by_id[sample.chunk_id]
        check, score = check_sentence_support(
            sample.answer, [_cite(chunk)], evidence
        )
        runs.append(_SupportRun(sample=sample, score=round(score, 4), flagged=not check.passed))

    confusion = _confusion(runs)

    by_kind: dict[str, dict] = {}
    for run in runs:
        entry = by_kind.setdefault(
            run.sample.kind,
            {
                "n": 0,
                "expected": "supported" if run.sample.expected_supported else "unsupported",
                "flagged": 0,
                "missed": 0,
                "false_alarm": 0,
                "mean_score": 0.0,
                "_sum_score": 0.0,
                "examples": [],
            },
        )
        entry["n"] += 1
        entry["_sum_score"] += run.score
        if run.flagged:
            entry["flagged"] += 1
            if run.sample.expected_supported:
                entry["false_alarm"] += 1
            if len(entry["examples"]) < 2:
                entry["examples"].append(run.sample.answer[:50])
        elif not run.sample.expected_supported:
            entry["missed"] += 1
            if len(entry["examples"]) < 2:
                entry["examples"].append(run.sample.answer[:50])

    for entry in by_kind.values():
        entry["mean_score"] = round(entry.pop("_sum_score") / entry["n"], 4)

    sweep = []
    for threshold in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
        tp = sum(1 for r in runs if not r.sample.expected_supported and r.score < threshold)
        fp = sum(1 for r in runs if r.sample.expected_supported and r.score < threshold)
        fn = sum(1 for r in runs if not r.sample.expected_supported and r.score >= threshold)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        sweep.append({
            "threshold": threshold,
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "flagged": tp + fp,
        })

    return {
        "name": "sentence_support",
        "question": "句级支撑度若升格为 gate，精确率与召回率是多少？",
        "method": (
            "在合成件片段上程序化构造带标签样本，直接调用 "
            "qa.verify.check_sentence_support；flagged = 分数 < "
            f"{SUPPORT_THRESHOLD}（即当前实现判为『支撑不足』）。"
            "正例含证据原句、调序拼接与同义改述；负例含无关整句、真假混合与跨片段串内容。"
        ),
        "threshold": SUPPORT_THRESHOLD,
        "n": len(runs),
        "confusion": confusion,
        "by_kind": dict(sorted(by_kind.items())),
        "threshold_sweep": sweep,
        "gate_recommendation": _gate_recommendation(
            n=len(runs), precision=confusion["precision"], recall=confusion["recall"]
        ),
        "limitations": [
            "正例来自证据原句（词面重叠最高），所以精确率是**上限**；真实答案会改述，假阳性只会更多。",
            "改述只有一张手工替换表，覆盖面远小于真实生成模型的表达方式。",
            "样本量小且全部来自 2 页合成件，结论不能外推到真实文档；真实文档上应重跑同一探针。",
        ],
    }


# ---------------------------------------------------------------- 跨引擎校验


@dataclass(frozen=True)
class SpotcheckSample:
    sample_id: str
    page_no: int
    kind: str
    truth: bool  # True = 确实注入了一个已知的高风险字段错误
    flagged: bool
    old: str = ""
    new: str = ""
    conflicts: int = 0


def _bump_first_digit(value: str) -> str | None:
    """把第一个数字改成另一个数字。长度不变，因此注入后所有 span 位置仍然有效。"""
    for i, ch in enumerate(value):
        if ch.isdigit():
            new = str((int(ch) + 1) % 10)
            if new != ch:
                return value[:i] + new + value[i + 1 :]
    return None


def _mutate_value_groups(text: str, page_no: int) -> list[SpotcheckSample]:
    """对页内每个**不同的高风险字段取值**，把所有出现位置一起改掉。

    为什么按"取值组"而不是按单次出现：
    当前比对是**集合差**。只改一处时，旧值仍可能留在集合里；更糟的是，
    如果新值恰好已在页内别处出现，集合差会**完全看不见**这次改动。
    即便报警，也只能说明"这个取值有问题"，无法定位到具体哪一处。
    所以这里模拟的是"引擎把该值系统性地读错"：所有出现一起变。这个边界
    本身也是探针要暴露的事实，而不是需要绕开的实现细节。
    """
    spans = iter_high_risk_field_spans(text)
    groups: dict[str, list] = {}
    for span in spans:
        groups.setdefault(span.value, []).append(span)

    samples: list[SpotcheckSample] = []
    for value, group in sorted(groups.items(), key=lambda kv: kv[1][0].start):
        new_value = _bump_first_digit(value)
        if new_value is None:
            continue  # 人名等无数字字段无法用"改数字"注入，跳过并如实体现在样本量里
        mutated = text
        for span in sorted(group, key=lambda s: s.start, reverse=True):
            mutated = mutated[: span.start] + new_value + mutated[span.end :]
        diff = diff_claims(text, mutated, engine_a="A", engine_b="B")
        kinds = {s.kind for s in group}
        samples.append(
            SpotcheckSample(
                sample_id=f"p{page_no}:inject:{value}",
                page_no=page_no,
                kind=kinds.pop() if len(kinds) == 1 else "mixed",
                truth=True,
                flagged=not diff.agreed,
                old=value,
                new=new_value,
                conflicts=diff.n_conflicts,
            )
        )
    return samples


def _clean_variants(text: str) -> list[tuple[str, str]]:
    """只改排版、不改任何高风险字段的对照文本。

    这些样本的"真值"是**没有错误**。任何一次报警都是假阳性，
    而假阳性正是跨引擎校验最大的落地风险：噪声一多，用户就会关掉它。
    """
    variants: list[tuple[str, str]] = [
        ("blank_lines", text.replace("\n", "\n\n")),
        ("extra_text", text + "\n附加说明：本页无新增技术内容。"),
        ("punctuation", re.sub(r"([，；：])\s*", r"\1 ", text)),
    ]
    if "示例" in text:
        variants.append(("confusable_char", text.replace("示例", "示列")))
    return [(name, variant) for name, variant in variants if variant != text]


def _spot_confusion(samples: list[SpotcheckSample]) -> dict:
    tp = sum(1 for s in samples if s.truth and s.flagged)
    fp = sum(1 for s in samples if not s.truth and s.flagged)
    fn = sum(1 for s in samples if s.truth and not s.flagged)
    tn = sum(1 for s in samples if not s.truth and not s.flagged)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "specificity": round(tn / (tn + fp), 4) if (tn + fp) else 0.0,
        "precision_ci": list(wilson_interval(tp, tp + fp)),
        "recall_ci": list(wilson_interval(tp, tp + fn)),
    }


def probe_cross_engine(pages: list[OcrPage]) -> dict:
    """测跨引擎一致性校验的标记精确率/召回率。

    每一页取 `plain_text()` 当作引擎 A；引擎 B 由两类样本构成：

    - **注入样本**：把某一个高风险字段取值的所有出现一起改成另一个数字；
    - **对照样本**：只改空行、标点、追加无字段文本、替换一个非字段词。
    """
    samples: list[SpotcheckSample] = []
    for page in sorted(pages, key=lambda p: p.page_no):
        if page.failed:
            continue
        text = page.plain_text()
        if not text.strip():
            continue
        samples.extend(_mutate_value_groups(text, page.page_no))
        for name, variant in _clean_variants(text):
            diff = diff_claims(text, variant, engine_a="A", engine_b="B")
            samples.append(
                SpotcheckSample(
                    sample_id=f"p{page.page_no}:clean:{name}",
                    page_no=page.page_no,
                    kind=f"clean_{name}",
                    truth=False,
                    flagged=not diff.agreed,
                    conflicts=diff.n_conflicts,
                )
            )

    confusion = _spot_confusion(samples)

    by_kind: dict[str, dict] = {}
    for sample in samples:
        entry = by_kind.setdefault(
            sample.kind,
            {"n": 0, "truth": "injected" if sample.truth else "clean",
             "flagged": 0, "detected": 0, "missed": 0, "false_alarm": 0},
        )
        entry["n"] += 1
        if sample.flagged:
            entry["flagged"] += 1
            if sample.truth:
                entry["detected"] += 1
            else:
                entry["false_alarm"] += 1
        elif sample.truth:
            entry["missed"] += 1

    missed = [
        s for s in samples if s.truth and not s.flagged
    ][:5]
    false_alarms = [
        s for s in samples if not s.truth and s.flagged
    ][:5]

    exercised = sorted({s.kind for s in samples if s.truth})
    not_exercised = [k for k in ("docid", "date", "name") if k not in exercised]

    if confusion["precision"] >= 0.95 and confusion["recall"] >= 0.95:
        verdict = (
            "在该字段域内，标记可用于**送人工复核**：注入的字段错误全部被捕获，"
            "对照样本没有产生假警报。它的定位仍是『可疑，请复核』，不是『确认有错』。"
        )
    else:
        verdict = (
            "标记的精确率或召回率未达 95%，**不足以单独承担放行/拦截判定**。"
            "应只作为人工复核的排序信号，且必须与不确定度标记、识别失败拒答配合使用。"
        )

    return {
        "name": "cross_engine_spotcheck",
        "question": "跨引擎一致性校验标出的可疑字段里，有多少是真的错？",
        "method": (
            "同一页文本作引擎 A；引擎 B 分两类：把每个不同高风险字段取值的**所有出现**"
            "一起改掉（已知错误），以及只改空行/标点/追加无字段文本/替换非字段词（对照）。"
            "报警判据即 diff_claims 的集合差非空。"
        ),
        "n": len(samples),
        "confusion": confusion,
        "by_kind": dict(sorted(by_kind.items())),
        "kinds_exercised": exercised,
        "kinds_not_exercised": not_exercised,
        "missed_examples": [
            {"sample": s.sample_id, "old": s.old, "new": s.new} for s in missed
        ],
        "false_alarm_examples": [
            {"sample": s.sample_id, "conflicts": s.conflicts} for s in false_alarms
        ],
        "mean_conflicts_on_flagged": round(
            sum(s.conflicts for s in samples if s.flagged)
            / max(1, sum(1 for s in samples if s.flagged)),
            3,
        ),
        "verdict": verdict,
        "limitations": [
            "注入错误只落在检测器覆盖的字段类型（数值/文号/日期）内；字符级 OCR 错误"
            "（人名、术语错字）不报警——这是窄域设计，不是回归。",
            "按『取值组』整体替换，模拟的是系统性读错；单点差异若把旧值改成一个"
            "页内已存在的值，集合差会完全看不见——这是当前实现的已知盲区，"
            "也意味着报警无法定位到具体出现位置。",
            "合成件的字段分布与真实扫描件不同；本实验测的是比对机制，不是任何引擎的识别精度。",
            "页面级报警不等于字段级定位准确；本探针未对单个冲突做人工真值标注。",
        ],
    }


# ---------------------------------------------------------------- 汇总与渲染


def run_probes(
    pages: list[OcrPage], chunks: list[Chunk], *, seed: int = 0
) -> dict:
    """跑全部已接入的离线探针。"""
    return {
        "seed": seed,
        "probes": {
            "sentence_support": probe_sentence_support(chunks, seed=seed),
            "cross_engine_spotcheck": probe_cross_engine(pages),
        },
    }


def _pct(value: float) -> str:
    return f"{value:.1%}"


def _ci(pair: list[float]) -> str:
    return f"[{pair[0]:.1%}, {pair[1]:.1%}]"


def _support_table(report: dict) -> list[str]:
    lines = [
        "| 指标 | 值 |",
        "|---|---|",
        f"| 样本量 | {report['n']} |",
        f"| TP / FP / FN / TN | {report['confusion']['tp']} / {report['confusion']['fp']} / "
        f"{report['confusion']['fn']} / {report['confusion']['tn']} |",
        f"| 精确率（报警里真无支撑的比例） | {_pct(report['confusion']['precision'])} "
        f"（95% CI {_ci(report['confusion']['precision_ci'])}） |",
        f"| 召回率（无支撑答案被抓住的比例） | {_pct(report['confusion']['recall'])} "
        f"（95% CI {_ci(report['confusion']['recall_ci'])}） |",
        f"| F1 | {report['confusion']['f1']:.3f} |",
        "",
        "| 样本类型 | 真值 | n | 报警 | 漏报 | 误报 | 平均分 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for kind, entry in report["by_kind"].items():
        lines.append(
            f"| `{kind}` | {entry['expected']} | {entry['n']} | {entry['flagged']} | "
            f"{entry['missed']} | {entry['false_alarm']} | {entry['mean_score']:.3f} |"
        )
    lines += [
        "",
        "阈值扫描（把报警判据从分数 < 0.5 挪到其他值时会怎样）：",
        "",
        "| 阈值 | 精确率 | 召回率 | 报警数 |",
        "|---:|---:|---:|---:|",
    ]
    for row in report["threshold_sweep"]:
        lines.append(
            f"| {row['threshold']:.1f} | {_pct(row['precision'])} | "
            f"{_pct(row['recall'])} | {row['flagged']} |"
        )
    return lines


def _spot_table(report: dict) -> list[str]:
    lines = [
        "| 指标 | 值 |",
        "|---|---|",
        f"| 样本量 | {report['n']} |",
        f"| TP / FP / FN / TN | {report['confusion']['tp']} / {report['confusion']['fp']} / "
        f"{report['confusion']['fn']} / {report['confusion']['tn']} |",
        f"| 精确率（报警里真有字段错误的比例） | {_pct(report['confusion']['precision'])} "
        f"（95% CI {_ci(report['confusion']['precision_ci'])}） |",
        f"| 召回率（注入错误被捕获的比例） | {_pct(report['confusion']['recall'])} "
        f"（95% CI {_ci(report['confusion']['recall_ci'])}） |",
        f"| F1 | {report['confusion']['f1']:.3f} |",
        f"| 报警样本平均冲突数 | {report['mean_conflicts_on_flagged']} |",
        "",
        "| 样本类型 | 真值 | n | 报警 | 检出 | 漏报 | 误报 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for kind, entry in report["by_kind"].items():
        lines.append(
            f"| `{kind}` | {entry['truth']} | {entry['n']} | {entry['flagged']} | "
            f"{entry['detected']} | {entry['missed']} | {entry['false_alarm']} |"
        )
    return lines


def render_markdown(report: dict) -> str:
    """把探针结果渲染成可入库、可 diff 的 Markdown 报告。"""
    support = report["probes"]["sentence_support"]
    spot = report["probes"]["cross_engine_spotcheck"]
    meta = report.get("metadata", {})

    lines = [
        "# 离线探针报告：评估层两项“未知”的实测",
        "",
        "> 本报告由 `uv run docqa eval-probes` 生成。输入是仓库自带的合成件与 fixture 产物，",
        "> **不调用任何模型、不触网**；数字衡量的是评估机制本身，不是 OCR 精度。",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 文档 | `{meta.get('doc_id', '')}` |",
        f"| 引擎 | `{meta.get('engine', '')}` |",
        f"| 域 | `{meta.get('domain', '')}` |",
        f"| 随机种子 | {report.get('seed', 0)} |",
        "",
        "## 1. 句级支撑度：启发式能不能升格为 gate",
        "",
        support["method"],
        "",
        *_support_table(support),
        "",
        f"**结论**：{support['gate_recommendation']}",
        "",
        "**已知局限**",
        "",
    ]
    lines += [f"- {item}" for item in support["limitations"]]
    lines += [
        "",
        "## 2. 跨引擎一致性校验：标记的精确率 / 召回率",
        "",
        spot["method"],
        "",
        *_spot_table(spot),
        "",
        f"本轮注入样本覆盖的字段类型：`{'`、`'.join(spot['kinds_exercised'])}`；"
        f"未覆盖：`{'`、`'.join(spot['kinds_not_exercised']) or '无'}`"
        "（该合成件里没有对应形态的字段，或形态不匹配检测器正则）。",
        "",
        f"**结论**：{spot['verdict']}",
        "",
        "**已知局限**",
        "",
    ]
    lines += [f"- {item}" for item in spot["limitations"]]
    if spot["missed_examples"]:
        lines += ["", "漏报样例（前 5 条）：", ""]
        lines += [
            f"- `{e['sample']}`：{e['old']} → {e['new']}" for e in spot["missed_examples"]
        ]
    if spot["false_alarm_examples"]:
        lines += ["", "假警报样例（前 5 条）：", ""]
        lines += [
            f"- `{e['sample']}`：{e['conflicts']} 处冲突" for e in spot["false_alarm_examples"]
        ]

    lines += ["", "## 复现", "", "```powershell", "uv run docqa eval-probes", "```", ""]
    notes = report.get("notes") or []
    if notes:
        lines += ["**报告口径提醒**", ""]
        lines += [f"- {note}" for note in notes]
        lines.append("")
    return "\n".join(lines)
