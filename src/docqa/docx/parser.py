"""DOCX 正文解析（Phase 1：按文档顺序、不丢内容）。

为什么不走 `document.paragraphs` + `document.tables`
----------------------------------------------------
python-docx 的这两个便捷属性各自按类型返回，**拿不到交错顺序**：先 3 段正文、
再 1 张表、再 2 段正文的文档，用它们重建会得到"5 段正文 + 1 张表"，
表格跑到最后——引用定位与阅读顺序同时错位。

`document.paragraphs` 还有三个坑：

1. `paragraph.text` 不含超链接里的文字；
2. 不含 `w:tab` / `w:br` / `w:noBreakHyphen`；
3. 看不到 `w:sdt`（内容控件）里的正文。

所以这里按 `body.iterchildren()` 走 `CT_P` / `CT_Tbl`，下钻 `w:sdtContent`，
用一个显式的 run walker 拼文本。这也让"排除删除文本、保留修订插入"这类
取舍变成一行可读的规则，而不是解析器内部的隐式行为。

不可信的文本仍然不可信
----------------------
DOCX 的正文同样可能夹带提示注入（隐藏文字、批注、文本框）。本模块只负责
**如实抽取并计数**；注入扫描由 `docqa.injection.scan_text()` 在管线层逐块做。
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import parse_xml

from docqa.config import DomainProfile
from docqa.docx.numbering import (
    NON_NUMBERING_FORMATS,
    NumberingLabel,
    NumberingResolver,
)
from docqa.docx.security import DocxSecurity, check_parse_limits, validate_docx
from docqa.errors import InputError
from docx import Document

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_V = "{urn:schemas-microsoft-com:vml}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"


def _w(tag: str) -> str:
    return f"{_W}{tag}"


def _m(tag: str) -> str:
    return f"{_M}{tag}"


# Phase 1 明确不支持的构造。**每一项都必须出现在 manifest 里**（值为 0 也要有），
# 否则"没计数"与"计数为 0"在产物里长得一样，退化就不可见了。
UNSUPPORTED_KEYS = (
    "footnotes",
    "endnotes",
    "text_boxes",
    "equations",
    "comments",
    "headers",
    "footers",
    "hidden_runs",
    "deleted_runs",
    "images",
    "embedded_objects",
    "content_controls",
    "fields",
    "nested_tables",
    # 这两种构造的内容不在正文 run 里：`altChunk` 指向包内另一份
    # HTML/RTF，`mc:AlternateContent` 是兼容性分支。计数是底线——
    # "没计数"和"没有这种构造"在 manifest 里必须能区分。
    "alt_chunks",
    "alternate_content",
)

# 警告最多留这么多条：一份畸形文档可能触发几万条同类警告，
# 全留会把 manifest 撑爆；留前 N-1 条明细 + 一条汇总。
MAX_WARNINGS = 20

# 没有 Heading 样式时，只有**短段落**才可能被条款正则识别成标题。
# 这个阈值同时是"不要把每个以数字开头的段落都当标题"的实现：
# 真正的标题通常很短，正文段落一长就不再兜底。
MAX_FALLBACK_HEADING_CHARS = 60

_HEADING_STYLE_RE = re.compile(r"^(?:Heading|标题)\s*([1-9])$", re.IGNORECASE)

# 句末标点：以这些结尾的是句子，不是标题。
_SENTENCE_ENDINGS = ("。", "！", "？", "；", ".", "!", "?", ";")

# "数值 + 单位"是数据句，不是条号。`42 MPa 是最小抗压强度。` 曾被兜底逻辑
# 误判成一级标题并重置章节栈。
_MEASUREMENT_RE = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*(?:"
    r"%|℃|°C|MPa|kPa|GPa|Pa|mm|cm|km|kg|mg|kN|kW|mV|mA|kHz|MHz|Hz|dB|"
    r"min|ms|"
    r"万元|亿元|元|毫米|厘米|米|千克|克|吨|兆帕|千帕|帕|伏|安|瓦|小时|分钟|秒"
    r")",
    re.IGNORECASE,
)

# 标题开头条号。四种真实形态：
#   3.6 / 3.6.1      —— 标准条款号
#   第三章            —— 中文"第 X 章"
#   （二） / (二)    —— 中文括号序号
#   一、              —— 中文顿号序号
_LABEL_PATTERNS = (
    re.compile(r"^\s*(?P<label>\d+(?:\.\d+){0,8})\.?(?=\s|$|[、．，])"),
    re.compile(r"^\s*(?P<label>第[一二三四五六七八九十百零〇两\d]+[章节条篇部分])"),
    re.compile(r"^\s*(?P<label>[（(][一二三四五六七八九十百零〇两\d]+[）)])"),
    re.compile(r"^\s*(?P<label>[一二三四五六七八九十百零〇两]+、)"),
)

# 这些子树在拼接"正文文本"时整棵跳过：
# - `w:del` / `w:moveFrom`：修订删除与移走的内容，不属于当前版本；
# - `w:instrText` / `w:delText`：域指令与删除文本；
# - `w:drawing` / `w:pict` / `w:object`：图片、文本框、嵌入对象（计数上报）；
# - `w:txbxContent`：文本框正文（计数上报，不当正文）；
# - `w:pPr`：段落属性，里面没有正文。
_SKIP_SUBTREES = frozenset(
    _w(tag)
    for tag in (
        "del",
        "moveFrom",
        "instrText",
        "delText",
        "drawing",
        "pict",
        "object",
        "txbxContent",
        "pPr",
    )
)


@dataclass(frozen=True)
class DocxBlock:
    """一个非空正文块（段落或线性化表格）。

    `para_no` 就是冻结契约里的 `¶n`：文档顺序第 n 个非空正文块
    （标题计入、空段落不计、表格占一个号）。`body_index` 是它在
    `w:body` 子元素里的原始下标——`¶n` 经过过滤，排查时往往需要回到原始位置。
    """

    body_index: int
    para_no: int
    kind: Literal["heading", "paragraph", "table"]
    text: str
    title: str = ""
    level: int | None = None
    style_name: str | None = None
    # 裸条号（"3" / "3.6" / "三"）：引用一致性比对用这个。
    clause_label: str | None = None
    # 展示条号（"第3章" / "第三章" / "3.6"）：`section` 与阅读用这个。
    label_text: str | None = None
    table_id: str | None = None
    rows: tuple[tuple[str, ...], ...] = ()
    structure_parsed: bool = False
    hidden_runs: int = 0

    def to_dict(self) -> dict:
        """块级序列化，供管线写 `docx_blocks.jsonl`（C 负责落盘）。

        保留 `body_index` 与 `rows`：出问题时需要回到原始 body 位置、
        或者核对表格线性化到底丢了什么。
        """
        return {
            "body_index": self.body_index,
            "para_no": self.para_no,
            "kind": self.kind,
            "label": self.clause_label,
            "label_text": self.label_text,
            "title": self.title,
            "text": self.text,
            "level": self.level,
            "style_name": self.style_name,
            "table_id": self.table_id,
            "structure_parsed": self.structure_parsed,
            "hidden_runs": self.hidden_runs,
            "rows": [list(row) for row in self.rows],
        }


@dataclass(frozen=True)
class DocxStats:
    """进 manifest 的统计。"""

    paragraphs: int = 0
    headings: int = 0
    tables: int = 0
    tables_linearized: int = 0
    numbering_literal: int = 0
    numbering_computed: int = 0
    numbering_missing: int = 0
    numbering_unsupported_formats: tuple[str, ...] = ()
    unsupported: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "paragraphs": self.paragraphs,
            "headings": self.headings,
            "tables": self.tables,
            "tables_linearized": self.tables_linearized,
            "numbering": {
                "literal": self.numbering_literal,
                "computed": self.numbering_computed,
                "missing": self.numbering_missing,
                "unsupported_formats": list(self.numbering_unsupported_formats),
            },
            "unsupported": dict(self.unsupported),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class DocxDocument:
    """一次解析的完整产物。"""

    blocks: tuple[DocxBlock, ...]
    stats: DocxStats
    security: DocxSecurity
    source_name: str | None = None

    @property
    def unit_count(self) -> int:
        """非空正文块数，与冻结契约的 `SourceProfile.unit_count` 对齐。"""
        return len(self.blocks)


def parse_docx(
    source: bytes | Path,
    *,
    filename: str | None = None,
    profile: DomainProfile | None = None,
) -> DocxDocument:
    """解析 DOCX。

    Args:
        source: 文件字节或路径。**只读，不落盘解压**。
        filename: 原始文件名，用于按扩展名拦截 `.docm` / `.dotx` / `.doc`。
            传路径时默认取 `source.name`。
        profile: 业务场景配置。只有 `clause_patterns` 参与解析：中文 Word 里
            大量条款直接写成正文段落（没有 Heading 样式），短段落靠这些正则
            兜底识别成标题。为 None 时不做这层兜底。

    Raises:
        InputError: 包不安全、结构损坏、没有正文，或规模超限。
    """
    if isinstance(source, Path):
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise InputError(
                f"无法读取 DOCX 文件：{source}（{exc}）。请确认文件存在且当前用户可读。"
            ) from exc
        source_name = filename or source.name
    else:
        data = source
        source_name = filename

    report = validate_docx(data, filename=source_name)
    try:
        document = Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - 输入边界：任何异常都转成可操作的 InputError
        raise InputError(
            f"DOCX 解析失败：{type(exc).__name__}: {exc}。\n"
            "包结构可能已损坏或含不支持的部件；请用 Word/WPS 重新另存为 .docx。"
        ) from exc
    return _parse_document(document, report, source_name, profile)


# ---------------------------------------------------------------- 主流程


def _parse_document(
    document: Any,
    report: DocxSecurity,
    source_name: str | None,
    profile: DomainProfile | None,
) -> DocxDocument:
    body = document.element.body
    style_defs = _style_defs(document)
    resolver = NumberingResolver(_numbering_element(document))
    patterns = _compile_patterns(profile)

    blocks: list[DocxBlock] = []
    warnings: list[str] = []
    paragraph_total = 0
    table_total = 0
    heading_total = 0
    char_total = 0
    literal_total = 0
    computed_total = 0
    missing_total = 0
    formats: list[str] = []

    for body_index, element in _iter_body_containers(body):
        if element.tag == _w("tbl"):
            table_total += 1
            text, rows = _linearize_table(element, style_defs)
            char_total += len(text)
            if text.strip():
                blocks.append(
                    DocxBlock(
                        body_index=body_index,
                        para_no=len(blocks) + 1,
                        kind="table",
                        text=text,
                        table_id=f"t{table_total}",
                        rows=rows,
                        structure_parsed=False,
                    )
                )
            continue

        paragraph_total += 1
        text, hidden_runs = _paragraph_text(element, style_defs)
        char_total += len(text)
        label_from_text, title_from_text = _split_label(text)
        level = _heading_level(element, style_defs)
        # 显式的 `w:outlineLvl`（含 9 = 正文文本）是作者的明确表态：
        # 即使段落以 "3.6" 开头，也不再拿条款正则去猜它是标题。
        if level is None and not _has_outline(element) and patterns:
            fallback = _fallback_heading(text, patterns)
            if fallback is not None:
                level, label_from_text, title_from_text = fallback
        numbering = _numbering_props(element)

        # 空段落不占 ¶n，但**编号照样推进**：Word 里空编号段落会消耗一个号，
        # 不推进会让后续所有条号整体错位。
        result: NumberingLabel | None = None
        if numbering is not None:
            result = resolver.resolve(*numbering)
            formats.extend(result.unsupported_formats)
        if not text.strip():
            continue

        label: str | None = None
        label_text: str | None = None
        title = ""
        if level is not None:
            heading_total += 1
            if label_from_text:
                literal_total += 1
                label_text = label_from_text
                label = bare_clause_label(label_from_text)
                title = title_from_text
            elif result is not None and result.number:
                computed_total += 1
                label, label_text, title = result.number, result.text, text.strip()
            elif result is not None and not _is_non_numbering(result) and result.text is None:
                missing_total += 1
                _warn(
                    warnings,
                    f"有一个标题段落带自动编号但无法解析（numId={numbering[0]}, "
                    f"ilvl={numbering[1]}），引用将退化为段落号",
                )
            else:
                title = text.strip()
        elif numbering is not None and result is not None:
            if label_from_text:
                literal_total += 1
                label_text = label_from_text
                label = bare_clause_label(label_from_text)
            elif result.number:
                computed_total += 1
                label, label_text = result.number, result.text
            elif not _is_non_numbering(result) and result.text is None:
                missing_total += 1
                _warn(
                    warnings,
                    f"有一个段落带自动编号但无法解析（numId={numbering[0]}, "
                    f"ilvl={numbering[1]}），引用将退化为段落号",
                )

        blocks.append(
            DocxBlock(
                body_index=body_index,
                para_no=len(blocks) + 1,
                kind="heading" if level is not None else "paragraph",
                text=text,
                title=title,
                level=level,
                style_name=_style_name(element, style_defs),
                clause_label=label,
                label_text=label_text,
                hidden_runs=hidden_runs,
            )
        )

    check_parse_limits(paragraph_total, char_total)
    if not blocks:
        raise InputError(
            "DOCX 没有可解析的正文：正文里没有非空段落或表格。\n"
            "可能是空文档、只有图片的文档，或内容全部在文本框/页眉页脚里"
            "（Phase 1 不支持这些构造）。"
        )

    if missing_total > len(warnings):
        warnings.append(
            f"…等共 {missing_total} 个段落/标题的自动编号无法解析"
            f"（已省略 {missing_total - len(warnings)} 条明细）"
        )

    unsupported = _count_unsupported(body, document, style_defs)
    stats = DocxStats(
        paragraphs=paragraph_total,
        headings=heading_total,
        tables=table_total,
        tables_linearized=table_total,
        numbering_literal=literal_total,
        numbering_computed=computed_total,
        numbering_missing=missing_total,
        numbering_unsupported_formats=tuple(dict.fromkeys(formats)),
        unsupported=unsupported,
        warnings=tuple(warnings),
    )
    return DocxDocument(
        blocks=tuple(blocks),
        stats=stats,
        security=report,
        source_name=source_name,
    )


# ---------------------------------------------------------------- body 遍历


def _iter_body_containers(body: Any):
    """按 body 顺序产出 (body_index, 段落/表格元素)，并下钻 `w:sdtContent`。"""
    for index, child in enumerate(body.iterchildren()):
        yield from _container_children(index, child)


def _container_children(index: int, element: Any):
    tag = element.tag
    if tag in (_w("p"), _w("tbl")):
        yield index, element
        return
    branch = _branch_of(element)
    if branch is not None:
        for child in branch.iterchildren():
            yield from _container_children(index, child)


def _branch_of(element: Any) -> Any | None:
    """把"包装元素"映射到真正装正文的那一层。

    - `w:sdt` → `w:sdtContent`；
    - `w:customXml` → 自身；
    - `mc:AlternateContent` → 第一个 `mc:Choice`，否则 `mc:Fallback`。
      只走一个分支：两个分支都走会把同一段文字算两遍。
    """
    tag = element.tag
    if tag == _w("sdt"):
        return element.find(_w("sdtContent"))
    if tag == _w("customXml"):
        return element
    if tag == f"{_MC}AlternateContent":
        choice = element.find(f"{_MC}Choice")
        if choice is not None:
            return choice
        return element.find(f"{_MC}Fallback")
    return None


# ---------------------------------------------------------------- 文本抽取


def _paragraph_text(paragraph: Any, style_defs: dict[str, _StyleDef]) -> tuple[str, int]:
    """拼接段落正文，返回 (文本, 隐藏 run 数)。"""
    parts: list[str] = []
    hidden = 0
    paragraph_style = _paragraph_style_id(paragraph)
    for child in paragraph.iterchildren():
        hidden += _walk(child, parts, style_defs, paragraph_style)
    return "".join(parts), hidden


def _walk(
    element: Any,
    parts: list[str],
    style_defs: dict[str, _StyleDef],
    paragraph_style: str,
) -> int:
    # 隐藏文字（直接 vanish / 字符样式 / 段落样式）**整棵子树丢弃并计数**：
    # 冻结契约明确要求它不进入正文/证据。保留它的文本等于让不可见内容成为
    # 答案依据，而隐藏层恰恰是提示注入最常用的藏身处。
    if element.tag == _w("r") and _run_is_hidden(element, style_defs, paragraph_style):
        return 1
    hidden = 0
    for child in element.iterchildren():
        tag = child.tag
        if tag in _SKIP_SUBTREES:
            continue
        if tag == f"{_MC}AlternateContent":
            branch = _branch_of(child)
            if branch is not None:
                hidden += _walk(branch, parts, style_defs, paragraph_style)
            continue
        if tag in (_w("t"), _m("t")):
            parts.append(child.text or "")
        elif tag == _w("tab"):
            parts.append("\t")
        elif tag in (_w("br"), _w("cr")):
            parts.append("\n")
        elif tag == _w("noBreakHyphen"):
            parts.append("-")
        else:
            hidden += _walk(child, parts, style_defs, paragraph_style)
    return hidden


def _vanish_hidden(vanish: Any) -> bool:
    """`w:vanish` 的三态语义：缺省/1/true/on = 隐藏，0/false/off = 可见。"""
    value = vanish.get(_w("val"))
    return value is None or value.lower() not in ("0", "false", "off")


def _run_is_hidden(
    run: Any, style_defs: dict[str, _StyleDef], paragraph_style: str
) -> bool:
    """判定 run 是否隐藏。优先级：直接 vanish > 字符样式链 > 段落样式链。

    只认直接 `w:rPr/w:vanish` 会漏掉通过字符样式隐藏的文字——Word 里
    "设为隐藏"经常是样式级设置，文本照样会进正文，形成"可见的隐藏文字"。
    """
    rPr = run.find(_w("rPr"))
    if rPr is not None:
        vanish = rPr.find(_w("vanish"))
        if vanish is not None:
            return _vanish_hidden(vanish)
        run_style = rPr.find(_w("rStyle"))
        if run_style is not None:
            hidden = _style_hidden(run_style.get(_w("val")) or "", style_defs)
            if hidden is not None:
                return hidden
    return bool(_style_hidden(paragraph_style, style_defs))


def _style_hidden(style_id: str, style_defs: dict[str, _StyleDef]) -> bool | None:
    """沿 `basedOn` 查找样式链上的 vanish。限深防环。"""
    seen: set[str] = set()
    current: str | None = style_id
    while current and current not in seen and len(seen) < 10:
        seen.add(current)
        definition = style_defs.get(current)
        if definition is None:
            return None
        if definition.hidden is not None:
            return definition.hidden
        current = definition.based_on
    return None


def _paragraph_style_id(paragraph: Any) -> str:
    pPr = paragraph.find(_w("pPr"))
    if pPr is None:
        return ""
    style_el = pPr.find(_w("pStyle"))
    if style_el is None:
        return ""
    return style_el.get(_w("val")) or ""


def _run_paragraph_style(run: Any) -> str:
    parent = run.getparent()
    while parent is not None and parent.tag != _w("p"):
        parent = parent.getparent()
    return _paragraph_style_id(parent) if parent is not None else ""


# ---------------------------------------------------------------- 标题与条号


@dataclass(frozen=True)
class _StyleDef:
    """样式链的一节：名称、outlineLvl、父样式、隐藏标记。"""

    name: str = ""
    outline_level: int | None = None
    based_on: str | None = None
    # `w:rPr/w:vanish` 的三态：None = 样式未表态，True/False = 显式隐藏/可见。
    hidden: bool | None = None


def _style_defs(document: Any) -> dict[str, _StyleDef]:
    try:
        styles = document.styles.element
    except (KeyError, NotImplementedError, AttributeError):
        return {}
    defs: dict[str, _StyleDef] = {}
    for style in styles.findall(_w("style")):
        style_id = style.get(_w("styleId"))
        if not style_id:
            continue
        name_el = style.find(_w("name"))
        based_el = style.find(_w("basedOn"))
        outline_el = style.find(f"{_w('pPr')}/{_w('outlineLvl')}")
        vanish = style.find(f"{_w('rPr')}/{_w('vanish')}")
        defs[style_id] = _StyleDef(
            name=(name_el.get(_w("val")) if name_el is not None else "") or "",
            outline_level=_as_int(outline_el.get(_w("val"))) if outline_el is not None else None,
            based_on=based_el.get(_w("val")) if based_el is not None else None,
            hidden=_vanish_hidden(vanish) if vanish is not None else None,
        )
    return defs


def _style_name(paragraph: Any, style_defs: dict[str, _StyleDef]) -> str | None:
    pPr = paragraph.find(_w("pPr"))
    style_el = pPr.find(_w("pStyle")) if pPr is not None else None
    if style_el is None:
        return None
    style_id = style_el.get(_w("val")) or ""
    definition = style_defs.get(style_id)
    return (definition.name if definition is not None else style_id) or None


def _heading_level(paragraph: Any, style_defs: dict[str, _StyleDef]) -> int | None:
    """标题判定：`w:outlineLvl` → 样式链 → 非标题。

    样式链而不是只看当前样式名：中文文档常见"我的标题"这类自定义样式，
    `basedOn` 指向 `Heading 1`/`标题 1`，outline 层级在父样式上。
    只匹配样式名会把这类文档整体判成"无结构"。
    """
    pPr = paragraph.find(_w("pPr"))
    if pPr is None:
        return None
    outline = pPr.find(_w("outlineLvl"))
    if outline is not None:
        return _outline_to_level(_as_int(outline.get(_w("val"))))
    style_el = pPr.find(_w("pStyle"))
    if style_el is None:
        return None
    return _style_chain_level(style_el.get(_w("val")) or "", style_defs)


def _has_outline(paragraph: Any) -> bool:
    pPr = paragraph.find(_w("pPr"))
    return pPr is not None and pPr.find(_w("outlineLvl")) is not None


def _outline_to_level(value: int | None) -> int | None:
    if value is None:
        return None
    # 0..8 是 1..9 级标题；9 显式表示"正文文本"。
    return value + 1 if 0 <= value <= 8 else None


def _style_chain_level(style_id: str, style_defs: dict[str, _StyleDef]) -> int | None:
    """沿 `basedOn` 向上找 outlineLvl 或 Heading N 样式名。有界，防环。"""
    seen: set[str] = set()
    current: str | None = style_id
    while current and current not in seen and len(seen) < 10:
        seen.add(current)
        definition = style_defs.get(current)
        if definition is None:
            match = _HEADING_STYLE_RE.match(current.strip())
            return int(match.group(1)) if match else None
        if definition.outline_level is not None:
            return _outline_to_level(definition.outline_level)
        match = _HEADING_STYLE_RE.match(definition.name.strip())
        if match:
            return int(match.group(1))
        current = definition.based_on
    return None


def _split_label(text: str) -> tuple[str | None, str]:
    """把标题开头的条号拆出来，返回 (展示条号, 标题余下部分)。"""
    for pattern in _LABEL_PATTERNS:
        match = pattern.match(text)
        if match:
            label = match.group("label").strip()
            rest = text[match.end() :].lstrip("、．.,，:：)） 　").strip()
            return label, rest
    return None, text.strip()


def bare_clause_label(label: str) -> str:
    """把展示条号清洗成裸条号。

    `第3章 → 3`、`（二）→ 二`、`一、→ 一`、`3.6 → 3.6`。
    引用一致性校验比对的是这个值——答案里写 `[§3 ¶4]` 时，证据的 `第3章`
    不能被判成不一致。
    """
    text = label.strip()
    chapter = re.match(r"^第([一二三四五六七八九十百零〇两\d]+)[章节条篇部分]$", text)
    if chapter:
        return chapter.group(1)
    parenthesised = re.match(r"^[（(]([一二三四五六七八九十百零〇两\d]+)[）)]$", text)
    if parenthesised:
        return parenthesised.group(1)
    if re.fullmatch(r"[一二三四五六七八九十百零〇两\d]+、", text):
        return text[:-1]
    return text


def _compile_patterns(profile: DomainProfile | None) -> list[re.Pattern[str]]:
    if profile is None:
        return []
    compiled: list[re.Pattern[str]] = []
    for raw in profile.clause_patterns:
        try:
            compiled.append(re.compile(raw))
        except re.error:
            # 场景配置里的坏正则不该让整篇文档解析失败；跳过即可，
            # 退化到"没有这层兜底"。
            continue
    return compiled


def _fallback_heading(text: str, patterns: list[re.Pattern[str]]) -> tuple[int, str, str] | None:
    """没有 Heading 样式时，用场景条款正则识别**短段落**的条号开头。

    中文 Word 文档里大量条款就是普通正文段落（`3.6 斜度公差……`），
    没有 outlineLvl / Heading 样式。若一律不认，整篇会退化成一个章节，
    引用只能给 `[¶n]`。

    但也不能"以数字开头就是标题"：`2024 年度报告`、`42 MPa 是最小抗压强度。`
    都是正文。所以以下条件同时成立才认：

    1. 段落足够短（`MAX_FALLBACK_HEADING_CHARS`）——标题短、正文长；
    2. 不以句末标点结尾——标题不是句子；
    3. 不像"数值 + 单位"（`42 MPa`、`3.5 万元`）——这是数据句，不是条号；
    4. 命中**场景配置的**条款正则（`DomainProfile.clause_patterns`）；
    5. 标签不是单个 ASCII 字母（`a)` 是列项，不单独成条）。
    """
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_FALLBACK_HEADING_CHARS:
        return None
    if stripped.endswith(_SENTENCE_ENDINGS):
        return None
    if _MEASUREMENT_RE.match(stripped):
        return None
    for pattern in patterns:
        match = pattern.match(stripped)
        if match is None:
            continue
        groups = match.groupdict()
        label = (groups.get("label") or "").strip()
        if not label:
            continue
        # `a) 项目`（standard.yaml 的列项模式）挂在所属条下，不是独立标题。
        if re.fullmatch(r"[A-Za-z]", label):
            continue
        title = (groups.get("title") or "").strip()
        if not title:
            title = stripped[match.end() :].lstrip("、．.,，:：)） 　").strip()
        return _label_depth(label), label, title
    return None


def _label_depth(label: str) -> int:
    """由裸条号推断标题层级，用于标题栈。

    `3.6 → 2`、`第3章 → 1`、`第3节 → 2`、`（二）→ 2`、`一、→ 1`。
    推断错了最坏情况是 `clause_path` 少一层/多一层，不会丢内容。
    """
    text = label.strip()
    if re.fullmatch(r"\d+(?:\.\d+)+", text):
        return text.count(".") + 1
    if text.startswith("第") and text.endswith(("章", "篇", "部分")):
        return 1
    if text.startswith("第") and text.endswith("节"):
        return 2
    if text.startswith(("（", "(")):
        return 2
    if text.startswith("第"):
        return 3
    return 1


def _numbering_props(paragraph: Any) -> tuple[int, int] | None:
    """读 `w:numPr`，返回 (numId, ilvl)。numId=0 表示显式取消编号。"""
    pPr = paragraph.find(_w("pPr"))
    if pPr is None:
        return None
    numPr = pPr.find(_w("numPr"))
    if numPr is None:
        return None
    num_id_el = numPr.find(_w("numId"))
    ilvl_el = numPr.find(_w("ilvl"))
    num_id = _as_int(num_id_el.get(_w("val"))) if num_id_el is not None else None
    ilvl = _as_int(ilvl_el.get(_w("val"))) if ilvl_el is not None else None
    if num_id is None or num_id == 0:
        return None
    return num_id, ilvl if ilvl is not None and ilvl >= 0 else 0


def _is_non_numbering(label: NumberingLabel) -> bool:
    """`bullet`/`none` 不是"解析失败"，而是"本来就不是条号"。"""
    return any(fmt in NON_NUMBERING_FORMATS for fmt in label.unsupported_formats)


# ---------------------------------------------------------------- 表格线性化


def _linearize_table(
    table: Any, style_defs: dict[str, _StyleDef]
) -> tuple[str, tuple[tuple[str, ...], ...]]:
    """把表格压成一行文本：单元格与行都用 ` | ` 连接。

    Phase 1 只保住数值与阅读顺序，不解析矩形网格（`structure_parsed=False`）。
    合并单元格的处理是**结构化**的：

    - `w:gridSpan`：一个 `w:tc` 本来就只出现一次，不展开成多列；
    - `w:vMerge` / `w:hMerge` 的 continuation 单元格直接跳过；
    - **相邻同值不做去重**——一列里两格都写"是"是合法数据，
      按值去重会静默删掉值，正是本项目最不能接受的那类 bug。
    """
    rows: list[tuple[str, ...]] = []
    for row in _iter_wrapped(table, _w("tr")):
        cells: list[str] = []
        for cell in _iter_wrapped(row, _w("tc")):
            if _is_merge_continuation(cell):
                continue
            cells.append(_cell_text(cell, style_defs))
        rows.append(tuple(cells))
    text = " | ".join(
        " | ".join(row) for row in rows if any(cell.strip() for cell in row)
    )
    return text, tuple(rows)


def _iter_wrapped(parent: Any, tag: str):
    """在 `parent` 的直接子元素里找 `tag`，穿透 sdt / customXml / AlternateContent。

    表格行与单元格都可能是内容控件（`w:sdt`）包裹的；只 `findall` 会让
    整行或整格静默消失——数值消失而 manifest 只多一个 `content_controls=1`，
    没有任何"内容丢了"的信号。
    """
    for child in parent.iterchildren():
        if child.tag == tag:
            yield child
            continue
        branch = _branch_of(child)
        if branch is not None:
            yield from _iter_wrapped(branch, tag)


def _is_merge_continuation(cell: Any) -> bool:
    tcPr = cell.find(_w("tcPr"))
    if tcPr is None:
        return False
    for tag in ("vMerge", "hMerge"):
        merge = tcPr.find(_w(tag))
        if merge is not None and (merge.get(_w("val")) or "continue") != "restart":
            return True
    return False


def _cell_text(cell: Any, style_defs: dict[str, _StyleDef]) -> str:
    """单元格文本：把它下面所有段落的文本压成单行。"""
    texts: list[str] = []
    for paragraph in cell.iter(_w("p")):
        text, _ = _paragraph_text(paragraph, style_defs)
        if text.strip():
            texts.append(text)
    return re.sub(r"\s+", " ", " ".join(texts)).strip()


# ---------------------------------------------------------------- 不支持构造计数


def _count_unsupported(
    body: Any, document: Any, style_defs: dict[str, _StyleDef]
) -> dict[str, int]:
    counts = {key: 0 for key in UNSUPPORTED_KEYS}
    counts["text_boxes"] = _count_iter(body, _w("txbxContent"))
    counts["equations"] = _count_iter(body, _m("oMath"))
    counts["content_controls"] = _count_iter(body, _w("sdt"))
    counts["deleted_runs"] = _count_iter(body, _w("del"))
    counts["embedded_objects"] = _count_iter(body, _w("object"))
    counts["images"] = _count_iter(body, f"{_A}blip") + _count_iter(body, f"{_V}imagedata")
    counts["fields"] = _count_iter(body, _w("fldSimple")) + sum(
        1
        for element in body.iter(_w("fldChar"))
        if (element.get(_w("fldCharType")) or "").lower() == "begin"
    )
    counts["hidden_runs"] = sum(
        1
        for run in body.iter(_w("r"))
        if _run_is_hidden(run, style_defs, _run_paragraph_style(run))
    )
    counts["nested_tables"] = sum(
        1 for table in body.iter(_w("tbl")) if _has_table_ancestor(table)
    )
    counts["alt_chunks"] = _count_iter(body, _w("altChunk"))
    counts["alternate_content"] = _count_iter(body, f"{_MC}AlternateContent")
    counts["comments"] = _part_item_count(document, RT.COMMENTS, "comment")
    counts["footnotes"] = _part_item_count(
        document,
        RT.FOOTNOTES,
        "footnote",
        excluded={"separator", "continuationSeparator", "continuationNotice"},
    )
    counts["endnotes"] = _part_item_count(
        document,
        RT.ENDNOTES,
        "endnote",
        excluded={"separator", "continuationSeparator", "continuationNotice"},
    )
    parts = _part_names(document)
    counts["headers"] = sum(1 for name in parts if re.search(r"/word/header\d*\.xml$", name))
    counts["footers"] = sum(1 for name in parts if re.search(r"/word/footer\d*\.xml$", name))
    return counts


def _count_iter(element: Any, tag: str) -> int:
    return sum(1 for _ in element.iter(tag))


def _has_table_ancestor(element: Any) -> bool:
    parent = element.getparent()
    while parent is not None:
        if parent.tag == _w("tbl"):
            return True
        parent = parent.getparent()
    return False


def _numbering_element(document: Any) -> Any | None:
    try:
        return document.part.numbering_part.element
    except (KeyError, NotImplementedError, AttributeError, ValueError):
        return None


def _part_names(document: Any) -> list[str]:
    try:
        return [str(part.partname) for part in document.part.package.parts]
    except (AttributeError, KeyError, ValueError):
        return []


def _part_item_count(
    document: Any, relation_type: str, tag: str, *, excluded: set[str] | None = None
) -> int:
    try:
        part = document.part.part_related_by(relation_type)
    except (KeyError, ValueError, NotImplementedError):
        return 0
    element = getattr(part, "element", None)
    if element is None:
        blob = getattr(part, "blob", None)
        if not blob:
            return 0
        try:
            element = parse_xml(blob)
        except Exception:  # noqa: BLE001 - 附属部件坏了不该让整篇解析失败
            return 0
    skipped = excluded or set()
    count = 0
    for item in element.findall(_w(tag)):
        if (item.get(_w("type")) or "normal") in skipped:
            continue
        count += 1
    return count


# ---------------------------------------------------------------- 工具


def _as_int(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _warn(warnings: list[str], message: str) -> None:
    if len(warnings) < MAX_WARNINGS - 1:
        warnings.append(message)
