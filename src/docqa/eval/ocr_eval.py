"""索引层（上下文可信度）评估：把人工转写基准与 OCR 产物对上，算出 OCR 质量。

为什么这一层要单独算，而且要**先报**
--------------------------------------
检索层与生成层的指标，全都建立在"检索到的内容是对的"这个前提上。
扫描件场景里这个前提**恰恰最不牢靠**——`DA/T 77—2019` 之所以要为 OCR 准确率
单独立标准，正是因为下游的一切都建在它上面。

所以 `docqa eval` 的报告里，这一层排在最前面。而在此之前，
`metrics.py` 里的 CER / 单元格准确率 / 数值字段准确率**都已实现且有单测**，
只是从没被接进评估——参考基准 `data/golden/transcription.md` 也只被文档引用。

对齐这件事要讲清楚
------------------
参考基准是**按行抽样的节选**（只挑信息密度最高或 OCR 风险最高的片段），
而 OCR 产物是**整页全文**。直接比会把整页多出来的部分全算成"插入错误"，
CER 高得毫无意义。

而且参考在原文里**根本不是连续的一段**：抽样时会跳过中间条款，
例如第 3 页的条款序列是 3.1 → 3.2 → **3.4** → 3.5 → 3.6 → 4.2（跳过了 3.3）。
所以任何"整段滑窗"都不可能匹配上——实测这样做会算出 44.5% 这种离谱的 CER。

这里改用**逐行对齐**：每一行单独去整页里定位最接近的窗口，再按行长加权聚合。
定位本身是**粗到细**两段（字符二元组粗扫 → 局部精修），
详见 `_locate` 的注释。

方法本身是启发式的（不是严格的最优子串匹配），取值依据写在
`docs/evaluation.md`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..logging import get_logger
from ..models import OcrPage, TableGrid
from ..tables import find_cell
from .metrics import cer, normalize_for_cer, numeric_field_accuracy, strip_symbols

_log = get_logger("eval.ocr")

# 窗口搜索的放宽幅度：在参考长度的 ±25% 之间找最佳对齐。
# 放宽是因为模型可能多读/漏读几个字，等长窗口会错位。
WINDOW_TOLERANCE = 0.25

_PAGE_RE = re.compile(r"^###\s*第\s*(\d+)\s*页", re.MULTILINE)
_FENCE_RE = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)


@dataclass
class TranscriptionRef:
    """人工转写基准：按页的正文节选，以及（可选的）表格网格。"""

    text_by_page: dict[int, str] = field(default_factory=dict)
    table_by_page: dict[int, list[list[str]]] = field(default_factory=dict)

    @property
    def pages(self) -> list[int]:
        return sorted(set(self.text_by_page) | set(self.table_by_page))


def _parse_md_table(block: str) -> list[list[str]]:
    """把 markdown 表格解析成矩形网格（跳过 `|---|` 分隔行）。"""
    rows: list[list[str]] = []
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(set(c) <= set("-: ") for c in cells):  # 分隔行
            continue
        rows.append(cells)
    return rows


def parse_transcription(path: Path) -> TranscriptionRef:
    """解析 `data/golden/transcription.md`。

    期望结构：`### 第 N 页（说明）` 后面跟一个代码块（正文节选）
    或一个 markdown 表格（表格页的网格基准）。
    """
    text = path.read_text(encoding="utf-8")
    ref = TranscriptionRef()

    marks = list(_PAGE_RE.finditer(text))
    for i, m in enumerate(marks):
        page_no = int(m.group(1))
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        section = text[m.end() : end]

        fenced = _FENCE_RE.search(section)
        if fenced:
            ref.text_by_page[page_no] = fenced.group(1).strip()
            continue

        grid = _parse_md_table(section)
        if grid:
            ref.table_by_page[page_no] = grid

    return ref


def _best_window_cer(line: str, ref_strict: str, hay_strict: str) -> tuple[float, float]:
    """在整页 OCR 文本里找与**一行参考**最接近的窗口。

    Returns:
        (归一化 CER, 该窗口上的符号敏感 CER)。

    **两个 CER 必须在同一个窗口上算。** 符号敏感的那个如果拿短行去比整页，
    编辑距离会被整页多出来的内容撑爆——实测跑出过 `14.0` 这种大于 1 的"错误率"，
    任何错误率都不该超过 1，那本身就是个信号。
    """
    ref = strip_symbols(ref_strict)
    if not ref:
        return 0.0, 0.0
    if len(hay_strict) <= len(ref):
        r = cer(line, hay_strict)
        return r.normalized, r.strict

    # 两个口径必须在**同一个定位窗口**上算。
    #
    # 不能各自搜一次：归一化会把标点去掉、串变短，两次搜出的位置可能不同，
    # 结果就是"符号敏感 CER 反而比归一化 CER 还低"这种反常——
    # 保留标点只可能暴露更多错误，不可能更少。**这个不等式本身就是校验。**
    start, end = _locate(hay_strict, ref_strict)
    norm_v = cer(strip_symbols(hay_strict[start:end]), strip_symbols(ref_strict)).normalized
    strict_v = cer(hay_strict[start:end], ref_strict).strict
    return norm_v, strict_v


def _locate(text: str, ref: str) -> tuple[int, int]:
    """在 text 里定位与 ref 对应的窗口，返回 (start, end)。

    粗到细两段：
      1. **粗扫**用字符二元组重叠 —— 集合运算，O(n)，很便宜；
      2. **精修**只在粗扫最优位置附近逐字符试，编辑距离只算几十次。

    为什么不直接上 difflib 的 `find_longest_match`：它找的是"最长公共块"，
    而最长的那一块**未必对应用户抽样的那一行**——实测在封面上就锚错了位置，
    把 CER 从 1.8% 抬到 59%。也不用暴力滑窗：那是 O(窗口数 × 编辑距离)，
    千字符页面上直接跑超时。粗到细两者都避开了。
    """
    n, m = len(text), len(ref)
    if m == 0 or n <= m:
        return 0, n

    def bigrams(s: str) -> set[str]:
        return {s[i : i + 2] for i in range(len(s) - 1)}

    ref_bg = bigrams(ref)
    coarse = max(1, m // 10)
    best_pos, best_score = 0, -1.0
    for start in range(0, n - m + 1, coarse):
        w = bigrams(text[start : start + m])
        if not ref_bg:
            break
        score = len(ref_bg & w) / len(ref_bg)
        if score > best_score:
            best_score, best_pos = score, start

    # 精修：在粗扫最优点 ±coarse 内逐字符试，同时允许 ±25% 的长度浮动
    lo = max(0, best_pos - coarse)
    hi = min(n, best_pos + coarse + m)
    best = (1e9, best_pos, best_pos + m)
    for size in range(max(1, int(m * (1 - WINDOW_TOLERANCE))),
                      min(n - lo, int(m * (1 + WINDOW_TOLERANCE))) + 1,
                      max(1, m // 20)):
        for start in range(lo, min(hi, n - size) + 1, max(1, m // 20)):
            d = cer(text[start : start + size], ref).normalized
            if d < best[0]:
                best = (d, start, start + size)
        if best[0] == 0.0:
            break
    return best[1], best[2]


def _anchored_cer(text: str, ref: str) -> float:
    """把参考定位到 text 上，在那一小段上算 CER（归一化口径）。"""
    if not ref or not text:
        return 1.0
    start, end = _locate(text, ref)
    return cer(text[start:end], ref).normalized


def best_aligned_cer(reference: str, ocr_text: str) -> tuple[float, float, int]:
    """逐行对齐后聚合，返回 (归一化 CER, 符号敏感 CER, 参考字符数)。

    **为什么按行对齐而不是整段滑窗**
    ----------------------------------
    参考基准是**按行抽样的节选**——为了覆盖信息密度与 OCR 风险最高的位置，
    抽样时会跳过中间的条款。例如第 3 页那段的条款序列是
    3.1 → 3.2 → **3.4** → 3.5 → 3.6 → 4.2（**跳过了 3.3**）。

    所以参考在原文里**根本不是连续的一段**，任何连续滑窗都不可能匹配上，
    实测会算出 44.5% 这种离谱的 CER。按行对齐才对得上——
    每一行单独去整页里找最接近的窗口，再按行长加权聚合。
    """
    # 保留标点与单位的版本：定位与符号敏感 CER 都在它上面做
    hay_strict = normalize_for_cer(ocr_text)
    lines = [ln.strip() for ln in reference.splitlines() if ln.strip()]
    if not lines:
        return 0.0, 0.0, 0

    total_chars = 0
    weighted = 0.0
    strict_num = 0.0
    for line in lines:
        ref_strict = normalize_for_cer(line)
        n = len(strip_symbols(ref_strict))
        if n < 4:      # 太短的行（页码、单字标题）不具统计意义，跳过
            continue
        d, s = _best_window_cer(line, ref_strict, hay_strict)
        total_chars += n
        weighted += d * n
        strict_num += s * n

    if total_chars == 0:
        return 0.0, 0.0, 0
    return weighted / total_chars, strict_num / total_chars, total_chars


def compare_tables_semantically(expected: list[list[str]], grid: TableGrid) -> dict:
    """按**语义位置**比对两张表，而不是逐格比。

    为什么不能逐格比
    ----------------
    参考基准里那张表是**人读的折叠形式**（4 列：检查项目 / 平键 / 半圆键 / 楔键），
    而 OCR 交出来的是**展开后的矩形网格**（9×8，合并单元格摊平、表头分层）。
    两者形状天然不同，逐格比会得到 14% 这种毫无意义的数字。

    真正该问的是：**「键宽 b 那一行、半圆键那一列」的值对不对**——
    这是语义位置，不是网格坐标。所以用 `tables.find_cell` 去定位。
    """
    if not expected or len(expected) < 2:
        return {"comparable": False, "reason": "参考表结构不足（需表头 + 至少一行数据）"}

    header = expected[0]
    col_labels = header[1:]          # 平键 / 半圆键 / 楔键
    correct = compared = 0
    details: list[dict] = []

    for row in expected[1:]:
        if not row:
            continue
        row_label = row[0]
        for ci, col_label in enumerate(col_labels, start=1):
            if ci >= len(row):
                continue
            exp_val = row[ci].strip()
            hit = find_cell(grid, row_label=row_label, col_label=col_label)
            got_val = hit[0].strip() if hit else None
            compared += 1
            # 归一化后比较：全角/半角、空白差异不算错
            ok = got_val is not None and (
                normalize_for_cer(exp_val) == normalize_for_cer(got_val)
            )
            correct += int(ok)
            if not ok:
                details.append({"row": row_label, "col": col_label,
                                "expected": exp_val, "got": got_val})

    return {
        "comparable": True,
        "cells_correct": correct,
        "cells_compared": compared,
        "cell_accuracy": round(correct / compared, 4) if compared else 0.0,
        "mismatches": details[:10],
        "ocr_shape": [grid.n_rows, grid.n_cols],
    }


def evaluate_ocr(pages: list[OcrPage], ref: TranscriptionRef) -> dict:
    """对每页算索引层指标。

    只对**参考基准覆盖到的页**计算——没有基准就没有度量，
    没覆盖的页如实标注为"未评估"，而不是记一个 0 分拉低整体。
    """
    per_page: list[dict] = []
    text_pages: list[int] = []
    table_pages: list[int] = []

    by_page = {p.page_no: p for p in pages}

    for page_no in ref.pages:
        page = by_page.get(page_no)
        if page is None:
            per_page.append({"page_no": page_no, "evaluated": False,
                             "reason": "该页不在 OCR 产物里"})
            continue
        if page.failed:
            per_page.append({"page_no": page_no, "evaluated": False,
                             "reason": "该页识别失败，无从比对"})
            continue

        entry: dict = {"page_no": page_no, "evaluated": True}

        if page_no in ref.text_by_page:
            reference = ref.text_by_page[page_no]
            ocr_text = page.plain_text()
            cer_n, cer_s, _ = best_aligned_cer(reference, ocr_text)
            nf = numeric_field_accuracy(reference, ocr_text)
            entry.update({
                "kind": "text",
                "ref_chars": len(strip_symbols(normalize_for_cer(reference))),
                "cer_normalized": round(cer_n, 4),
                "cer_strict": round(cer_s, 4),
                "character_accuracy": round(max(0.0, 1 - cer_n), 4),
                "numeric_field_recall": round(nf["field_recall"], 4),
                "missing_numbers": nf["missing"][:8],
            })
            text_pages.append(page_no)

        elif page_no in ref.table_by_page:
            expected = ref.table_by_page[page_no]
            grid: TableGrid | None = page.tables[0] if page.tables else None
            if grid is None:
                entry.update({"kind": "table", "evaluated": False,
                              "reason": "参考基准里有表格，但 OCR 未产出表格"})
            else:
                tc = compare_tables_semantically(expected, grid)
                entry.update({"kind": "table", **tc})
                if tc.get("comparable"):
                    table_pages.append(page_no)

        per_page.append(entry)

    # --- 汇总：**只对评估过的页取平均**，未评估的不算 0 分 ---
    text_entries = [e for e in per_page if e.get("kind") == "text" and e.get("evaluated")]
    table_entries = [e for e in per_page if e.get("kind") == "table" and e.get("evaluated")]

    summary: dict = {
        "method": "最佳窗口对齐（参考为节选，OCR 为整页）",
        "pages_evaluated": len(text_entries) + len(table_entries),
        "pages_not_evaluated": [e["page_no"] for e in per_page if not e.get("evaluated")],
        "per_page": per_page,
    }
    if text_entries:
        summary["cer_normalized"] = round(
            sum(e["cer_normalized"] for e in text_entries) / len(text_entries), 4)
        summary["cer_strict"] = round(
            sum(e["cer_strict"] for e in text_entries) / len(text_entries), 4)
        summary["character_accuracy"] = round(
            sum(e["character_accuracy"] for e in text_entries) / len(text_entries), 4)
        summary["numeric_field_recall"] = round(
            sum(e["numeric_field_recall"] for e in text_entries) / len(text_entries), 4)
    if table_entries:
        summary["table_cell_accuracy"] = round(
            sum(e["cell_accuracy"] for e in table_entries) / len(table_entries), 4)
    return summary
