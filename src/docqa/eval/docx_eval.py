"""DOCX 评估：段落覆盖率与引用回环（`docqa eval-docx` 的指标实现）。

为什么单列一条评估通道
--------------------
PDF 的评估围绕"页"与"OCR 识别质量"展开；DOCX 没有页、没有识别，
用同一套指标会问错问题（例如把 `page_count=0` 报成"解析出 0 页"）。
DOCX 的硬承诺只有三条，它们都能被**独立复算**：

1. **段落覆盖率 = 1.0**
   每个 `text.strip()` 非空的正文块，都必须被至少一个片段的 `block_ids` 声明覆盖，
   且内容能由这些片段核对出来。只靠"某片段文本包含这段文字"会掩盖丢块——
   两个内容相同的段落，删掉覆盖第二段的那一片，文本判据仍会通过。
   这是"没有内容被静默丢掉"的可执行形式：少一段，系统就可能自信地回答
   "文档里没有"——那比报错危险得多。
2. **引用回环 = 1.0**
   每条片段用 `DOCX_CITATION_TEMPLATE` 渲染出的引用，必须能被
   `parse_citations()` 解析回**同一个** `chunk_id`（长段落切成的多片共享同一个
   定位，靠 `evidence_chunk_ids` 集合判定）。
   回到别的段落，读者点开看到的就是错的证据。
3. **`unit_count` 对账**
   产物自称的块数（profile/manifest）必须等于 `docx_blocks.jsonl` 的非空行数。
   只核对"块文件里的块有没有被覆盖"是产物自洽，不是内容完整：写盘阶段丢一个
   块、块文件里也没有它，覆盖率仍会显示 1.0。

只读产物，不重新 ingest
----------------------
本模块不解析 DOCX、不重建索引：它只读管线已经落盘的
`profile.json` / `chunks.jsonl` / `docx_blocks.jsonl` / `manifest.json`。
这样评估量的是"产物本身自洽不自洽"，而不是"评估时重新跑一遍"。
CLI 负责把 ingest 的输出（通常是临时目录）传进来。

`¶n` 的口径
-----------
文档顺序第 n 个**非空正文块**（段落或线性化表格；标题计入；空段落不计）。
表格的覆盖判定：先判整表线性化文本是否出现；整表不在时，逐个判断非空单元格，
**全部出现**才算覆盖（见 `TABLE_COVERAGE_CALIBER`）。这个口径写进报告，
读者不需要猜。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..chunker import split_sentences
from ..errors import InputError
from ..models import DOCX_CITATION_TEMPLATE, Chunk, Citation
from ..qa.answer import parse_citations
from ..qa.verify import normalize

# 评估所依赖的产物。缺任何一个都无法给出可信结论，因此直接报错，
# 而不是"能算多少算多少"——半份产物算出来的 1.0 是最危险的假象。
REQUIRED_ARTIFACTS = {
    "profile.json": "来源画像（SourceProfile.to_dict()）",
    "chunks.jsonl": "片段（Chunk.to_dict()，每行一条）",
    "docx_blocks.jsonl": "正文块（DocxBlock.to_dict()，每行一条）",
    "manifest.json": "管线清单",
}

OCR_NOT_APPLICABLE = "DOCX 原生文本，无 OCR 层"

# 覆盖判据的两层，顺序不能反：
#   1. **成员依据**：block_id 必须出现在某片段的 `block_ids` 里。
#      只靠"某片段文本包含这段文字"会掩盖丢块——两个内容相同的段落，
#      删掉覆盖第二段的那一片，文本判据仍会通过。
#   2. **文本依据**：由**覆盖它的那些片段**核对内容，而不是任意片段。
#      非表格块先判整块文本；长块被切成多片时逐句核对（每句必须出现）。
#      表格块整表不在时，逐个判断非空单元格，全部出现才算覆盖。
TABLE_COVERAGE_CALIBER = (
    "成员依据：block_id 必须出现在至少一个片段的 block_ids 中。"
    "文本依据：由这些片段核对——非表格块整块文本须出现在其拼接中（跨片时逐句核对）；"
    "表格块整表不在时，逐个判断非空单元格，全部出现才算覆盖。"
)

ROUNDTRIP_CALIBER = (
    "每条片段按场景配置的 DOCX 引用模板（默认 DOCX_CITATION_TEMPLATE）渲染引用，"
    "parse_citations() 解析结果的 evidence_chunk_ids（为空时退回 chunk_id）"
    "必须包含该 chunk_id。"
)

# offender 预览长度：够认出是哪一段，又不至于把整篇文档塞进报告。
PREVIEW_CHARS = 40


# ---------------------------------------------------------------- 入口


def run_docx_eval(out_dir: Path, *, docx_template: str | None = None) -> dict:
    """读取 DOCX 产物目录，产出评估报告（dict，可直接 JSON 序列化）。

    Args:
        out_dir: `docqa ingest <file.docx>` 的输出目录。
            也接受 `pipeline.IngestResult`（取其 `.out_dir`）：
            CLI 手里拿到的就是那个对象，让它少写一行 `.out_dir` 比让
            调用方靠捕获 `TypeError` 猜签名更可靠。
        docx_template: 场景配置的 DOCX 引用模板（`DomainProfile.docx_citation_template`）。
            为 None 时用 `DOCX_CITATION_TEMPLATE`。**必须与真实问答用同一个模板**：
            否则配置了自定义模板的文档，问答按自定义格式引用、评估却按默认格式判定，
            门禁会在真实链路上失效而评估仍然通过。

    Raises:
        InputError: 产物缺失或损坏。消息里写明缺哪个文件、下一步该做什么。
    """
    out_dir = _resolve_out_dir(out_dir)
    template = docx_template or DOCX_CITATION_TEMPLATE
    _require_artifacts(out_dir)
    profile = _read_json(out_dir / "profile.json")
    manifest = _read_json(out_dir / "manifest.json")
    chunk_dicts = _read_jsonl(out_dir / "chunks.jsonl")
    block_dicts = _read_jsonl(out_dir / "docx_blocks.jsonl")

    chunks = [c for c in (_chunk_from_dict(d) for d in chunk_dicts) if c is not None]
    docx_meta = manifest.get("docx")
    if not isinstance(docx_meta, dict):
        docx_meta = {}

    coverage = _paragraph_coverage(chunks, block_dicts)
    roundtrip = _citation_roundtrip(chunks, template)
    unit = _unit_count_check(profile, manifest, block_dicts)
    numbering = _numbering_metrics(docx_meta)
    unsupported = _unsupported_metrics(docx_meta)

    failures = _failures(coverage, roundtrip, unit)
    warnings = _warnings(numbering, unsupported, docx_meta, block_dicts)

    return {
        "doc_id": _resolve_doc_id(profile, chunks, manifest, out_dir),
        "source_type": "docx",
        # `unit_count` = 产物里**实际**有多少个非空正文块；
        # `declared_unit_count` = 产物自称有多少个。两者不等说明写盘阶段丢过内容，
        # 这是一个**门禁**（见 `_failures`），不是展示字段。
        "unit_count": unit["actual"],
        "declared_unit_count": unit["declared"],
        # 回环判据用的模板写进报告：配置了自定义模板的文档，读者要能核对
        # "评估和问答是不是同一个引用格式"。
        "docx_template": template,
        "out_dir": str(out_dir),
        "metrics": {
            "paragraph_coverage": coverage,
            "citation_roundtrip": roundtrip,
            "structure": _structure_metrics(docx_meta),
            "tables": _table_metrics(docx_meta, block_dicts),
            "numbering": numbering,
            "unsupported": unsupported,
            # **不适用，不是 0**：0 会被读成"识别全错"，而 DOCX 根本没有识别层。
            "ocr": {"not_applicable": True, "reason": OCR_NOT_APPLICABLE},
            "chunks": len(chunks),
        },
        "gates_passed": not failures,
        "failures": failures,
        "warnings": warnings,
        "notes": [
            "本报告只读 ingest 产物（profile/chunks/docx_blocks/manifest），不重新解析 DOCX。",
            "PDF 口径的指标（页码、页图、CER、识别失败页）在 DOCX 上不适用，报告里不出现。",
        ],
    }


# ---------------------------------------------------------------- 产物读取


def _resolve_out_dir(source: Any) -> Path:
    """接受产物目录或 `IngestResult`（有 `.out_dir`），统一成目录。"""
    if isinstance(source, str | Path):
        return Path(source)
    out_dir = getattr(source, "out_dir", None)
    if out_dir is not None:
        return Path(out_dir)
    raise InputError(
        f"run_docx_eval 需要 ingest 产物目录或 IngestResult，收到 {type(source).__name__}。"
        "请传入 `docqa ingest <file.docx>` 打印的「产物目录」。"
    )


def _require_artifacts(out_dir: Path) -> None:
    missing = [
        name for name in REQUIRED_ARTIFACTS if not (out_dir / name).is_file()
    ]
    if not missing:
        return
    detail = "、".join(f"{name}（{REQUIRED_ARTIFACTS[name]}）" for name in missing)
    raise InputError(
        f"DOCX 评估产物不完整，缺少：{detail}。目录：{out_dir}\n"
        "评估只读产物、不重新 ingest。请先运行 `docqa ingest <file.docx>`，"
        "把命令打印的「产物目录」原样交给 `docqa eval-docx`。"
    )


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(
            f"无法解析评估产物 {path.name}：{exc}。请重新 ingest 生成完整产物。"
        ) from exc
    if not isinstance(data, dict):
        raise InputError(
            f"评估产物 {path.name} 的顶层不是对象（实际是 {type(data).__name__}）。"
            "请重新 ingest 生成完整产物。"
        )
    return data


def _read_jsonl(path: Path) -> list[dict]:
    """逐行读 JSONL；坏行直接报错并给出行号。

    **不跳过坏行**：少读一行块，覆盖率就可能显示 1.0——
    那正好掩盖了要抓的问题。
    """
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise InputError(f"无法读取评估产物 {path.name}：{exc}") from exc
    for lineno, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InputError(
                f"评估产物 {path.name}:{lineno} 不是合法 JSON：{exc}。"
                "请重新 ingest 生成完整产物。"
            ) from exc
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _chunk_from_dict(data: dict) -> Chunk | None:
    """按冻结契约构造 `Chunk`；字段全部用 `.get()`，旧产物零改动可读。"""
    chunk_id = str(data.get("chunk_id") or "")
    if not chunk_id:
        return None
    text = str(data.get("text") or "")
    return Chunk(
        chunk_id=chunk_id,
        doc_id=str(data.get("doc_id") or ""),
        page_no=data.get("page_no"),
        ordinal=_as_int(data.get("ordinal")),
        kind=data.get("kind") or "paragraph",
        text=text,
        clause_path=tuple(data.get("clause_path") or ()),
        clause_label=data.get("clause_label"),
        table_id=data.get("table_id"),
        block_ids=tuple(data.get("block_ids") or ()),
        char_len=_as_int(data.get("char_len")) or len(text),
        locator_kind=data.get("locator_kind") or "paragraph",
        section=data.get("section"),
        para_no=_as_int_or_none(data.get("para_no")),
        para_end=_as_int_or_none(data.get("para_end")),
    )


# ---------------------------------------------------------------- 指标：覆盖


def _paragraph_coverage(chunks: list[Chunk], blocks: list[dict]) -> dict:
    index = _coverage_index(chunks)
    total = 0
    covered = 0
    offenders: list[dict] = []
    for block in blocks:
        raw = str(block.get("text") or "")
        if not raw.strip():
            continue
        total += 1
        block_id = _block_id(block)
        texts = index.get(block_id) or []
        if not texts:
            # 没有任何片段声明覆盖它：**只凭"别处有同样的文字"不算覆盖**。
            # 这正是审计里"删掉覆盖第二段的片段、覆盖率仍 2/2"的修法。
            offenders.append(
                _offender(block, raw, "没有任何片段的 block_ids 含该块")
            )
            continue
        if _block_covered(raw, block, texts):
            covered += 1
            continue
        offenders.append(
            _offender(block, raw, "片段声明覆盖该块，但块文本不在其文本中")
        )
    return {
        "covered": covered,
        "total": total,
        "ratio": round(covered / total, 6) if total else 0.0,
        "gate": 1.0,
        "caliber": TABLE_COVERAGE_CALIBER,
        "offenders": offenders,
    }


def _coverage_index(chunks: list[Chunk]) -> dict[str, list[str]]:
    """block_id -> 声明覆盖它的片段文本（已归一化）。"""
    index: dict[str, list[str]] = {}
    for chunk in chunks:
        text = normalize(chunk.text)
        for block_id in chunk.block_ids:
            index.setdefault(str(block_id), []).append(text)
    return index


def _block_covered(raw: str, block: dict, texts: list[str]) -> bool:
    """在**覆盖该块的片段**里核对内容，而不是在任意片段里。

    长正文块会被切成多片、片间有重叠，整块文本通常不是任何单片、也不是
    "简单拼接"的连续子串（重叠段会重复），所以退到**逐句核对**：B 的切分器
    按同一套句子边界切，每句必然完整落在某一片里。丢一句就是没覆盖。
    """
    combined = "".join(texts)
    full = normalize(raw)
    if full and full in combined:
        return True
    if block.get("kind") == "table":
        cells = _table_cells(block)
        return bool(cells) and all(cell in combined for cell in cells)
    sentences = [normalize(sentence) for sentence in split_sentences(raw)]
    sentences = [sentence for sentence in sentences if sentence]
    if len(sentences) <= 1:
        # 单句块整块都不在，没有任何"逐句"可退——就是没覆盖。
        return False
    # 全部句子都要出现。只要求"部分出现"会把整句丢失判成覆盖——
    # 而丢一句数值，问答照样会自信地给出错的结论。
    return all(sentence in combined for sentence in sentences)


def _table_cells(block: dict) -> list[str]:
    cells: list[str] = []
    for row in block.get("rows") or []:
        if isinstance(row, str):
            row = [row]
        if not isinstance(row, list | tuple):
            continue
        for cell in row:
            value = normalize(str(cell))
            if value:
                cells.append(value)
    return cells


def _block_id(block: dict) -> str:
    explicit = str(block.get("block_id") or "")
    if explicit:
        return explicit
    return f"b{block.get('para_no')}"


def _preview(text: str) -> str:
    return " ".join(text.split())[:PREVIEW_CHARS]


def _offender(block: dict, raw: str, reason: str) -> dict:
    return {
        "block_id": _block_id(block),
        "para_no": block.get("para_no"),
        "preview": _preview(raw),
        "reason": reason,
    }


# ---------------------------------------------------------------- 指标：回环


def _citation_roundtrip(chunks: list[Chunk], docx_template: str) -> dict:
    ok = 0
    offenders: list[dict] = []
    for chunk in chunks:
        rendered = _render(chunk, docx_template)
        parsed = parse_citations(rendered, chunks)
        if any(_citation_covers(citation, chunk.chunk_id) for citation in parsed):
            ok += 1
            continue
        offenders.append({"chunk_id": chunk.chunk_id, "rendered": rendered})
    total = len(chunks)
    return {
        "ok": ok,
        "total": total,
        "ratio": round(ok / total, 6) if total else 0.0,
        "gate": 1.0,
        "caliber": ROUNDTRIP_CALIBER,
        "offenders": offenders,
    }


def _citation_covers(citation: Citation, chunk_id: str) -> bool:
    """解析出的引用是否覆盖给定片段。

    优先看 `evidence_chunk_ids`：长段落被切成 b2/b3/b4，三片共享同一个
    `[§1 ¶2]`，解析只会产出**一条**引用，但它的证据集合包含全部三片。
    只认 `chunk_id` 会把后两片判成"回环失败"——那是评估错了，不是切分错了。
    字段为空时（旧产物/单 chunk）退回 `chunk_id`。
    """
    evidence = tuple(getattr(citation, "evidence_chunk_ids", ()) or ())
    if evidence:
        return chunk_id in evidence
    return citation.chunk_id == chunk_id


def _render(chunk: Chunk, docx_template: str) -> str:
    """用**与真实问答相同**的 DOCX 模板渲染一条引用。"""
    return Citation(
        page_no=None,
        clause_label=chunk.clause_label,
        chunk_id=chunk.chunk_id,
        quote="",
        para_no=chunk.para_no,
        locator_kind="paragraph",
        section=chunk.section,
    ).render(docx_template=docx_template)


# ---------------------------------------------------------------- 指标：结构


def _structure_metrics(docx: dict) -> dict:
    return {
        "paragraphs": _as_int(docx.get("paragraphs")),
        "headings": _as_int(docx.get("headings")),
        "tables": _as_int(docx.get("tables")),
        "tables_linearized": _as_int(docx.get("tables_linearized")),
    }


def _table_metrics(docx: dict, blocks: list[dict]) -> dict:
    return {
        "total": _as_int(docx.get("tables")),
        "linearized": _as_int(docx.get("tables_linearized")),
        # Phase 1 表格只线性化：合并单元格网格与单元格级引用不在范围内。
        # 这里如实读产物，而不是写死 False。
        "structure_parsed": any(bool(block.get("structure_parsed")) for block in blocks),
    }


def _numbering_metrics(docx: dict) -> dict:
    raw = docx.get("numbering")
    if not isinstance(raw, dict):
        raw = {}
    formats = raw.get("unsupported_formats")
    return {
        "literal": _as_int(raw.get("literal")),
        "computed": _as_int(raw.get("computed")),
        "missing": _as_int(raw.get("missing")),
        "unsupported_formats": [str(item) for item in (formats or [])],
    }


def _unsupported_metrics(docx: dict) -> dict:
    raw = docx.get("unsupported")
    if not isinstance(raw, dict):
        return {}
    return {str(key): _as_int(value) for key, value in raw.items()}


# ---------------------------------------------------------------- 结论


def _failures(coverage: dict, roundtrip: dict, unit: dict) -> list[str]:
    failures: list[str] = []
    # --- 产物完整性：块文件本身有没有丢，与"块是否被片段覆盖"是两件事 ---
    #
    # 只核对"块文件里的块有没有被覆盖"是**产物自洽**，不是内容完整：
    # parser/写盘阶段丢一个块、且同时从块文件里消失时，覆盖率仍会显示 1.0。
    # `unit_count` 是源文档解析出的块数，拿它和块文件行数对账才能发现这种情况。
    if unit["declared"] is None:
        failures.append(
            "产物未声明 unit_count（profile/manifest 都没有）："
            "无法核对 docx_blocks.jsonl 是否在写盘前丢过正文块"
        )
    elif unit["declared"] != unit["actual"]:
        failures.append(
            f"unit_count={unit['declared']} 与 docx_blocks.jsonl 的非空正文块数 "
            f"{unit['actual']} 不一致：有正文块在写盘前丢失，或产物被裁剪"
        )
    if not coverage["total"]:
        failures.append(
            "docx_blocks.jsonl 里没有非空正文块：覆盖率无从核对，产物可能已损坏"
        )
    elif coverage["covered"] != coverage["total"]:
        missing = coverage["total"] - coverage["covered"]
        failures.append(
            f"paragraph_coverage={coverage['ratio']:.4f} < 1.0："
            f"{missing} 个正文块没有被任何片段按 block_ids 覆盖"
        )
    if not roundtrip["total"]:
        failures.append("chunks.jsonl 为空：引用回环无从核对，产物可能已损坏")
    elif roundtrip["ok"] != roundtrip["total"]:
        broken = roundtrip["total"] - roundtrip["ok"]
        failures.append(
            f"citation_roundtrip={roundtrip['ratio']:.4f} < 1.0："
            f"{broken} 个片段的引用无法解析回自身（含 evidence 集合）"
        )
    return failures


def _unit_count_check(profile: dict, manifest: dict, blocks: list[dict]) -> dict:
    """对账：产物**自称**的块数 vs `docx_blocks.jsonl` 的**非空块行数**。"""
    actual = sum(1 for block in blocks if str(block.get("text") or "").strip())
    declared = _declared_unit_count(profile, manifest)
    return {"declared": declared, "actual": actual, "matches": declared == actual}


def _declared_unit_count(profile: dict, manifest: dict) -> int | None:
    containers: list = [profile]
    source = manifest.get("source")
    if isinstance(source, dict):
        containers.append(source)
    docx_meta = manifest.get("docx")
    if isinstance(docx_meta, dict):
        containers.append(docx_meta)
    for container in containers:
        if isinstance(container, dict) and container.get("unit_count") is not None:
            return _as_int_or_none(container.get("unit_count"))
    return None


def _warnings(
    numbering: dict, unsupported: dict, docx: dict, blocks: list[dict]
) -> list[str]:
    warnings: list[str] = []
    if numbering["missing"] > 0:
        warnings.append(
            f"自动编号有 {numbering['missing']} 处无法解析：这些标题/段落退化为段落号引用。"
            "按契约只提示、不设门禁（编号解析是有界实现，失败不等于内容错）"
        )
    if numbering["unsupported_formats"]:
        warnings.append(
            "自动编号格式不在支持列表内："
            + "、".join(numbering["unsupported_formats"])
        )
    if any(
        block.get("kind") == "table" and not block.get("structure_parsed")
        for block in blocks
    ):
        warnings.append(
            "表格仅线性化（structure_parsed=false）：合并单元格网格与单元格级引用"
            "不在 Phase 1 范围"
        )
    nonzero = {key: value for key, value in unsupported.items() if value}
    if nonzero:
        detail = "、".join(f"{key}={value}" for key, value in sorted(nonzero.items()))
        warnings.append(f"存在未抽取的不支持构造（已计数上报，不静默丢弃）：{detail}")
    parser_warnings = docx.get("warnings")
    if isinstance(parser_warnings, list):
        warnings.extend(f"解析器警告：{item}" for item in parser_warnings[:20])
    return warnings


def _resolve_doc_id(profile: dict, chunks: list[Chunk], manifest: dict, out_dir: Path) -> str:
    for chunk in chunks:
        if chunk.doc_id:
            return chunk.doc_id
    docx_meta = manifest.get("docx")
    if isinstance(docx_meta, dict) and docx_meta.get("doc_id"):
        return str(docx_meta["doc_id"])
    chunk_meta = manifest.get("chunk")
    if isinstance(chunk_meta, dict) and chunk_meta.get("doc_id"):
        return str(chunk_meta["doc_id"])
    source_meta = manifest.get("source")
    if isinstance(source_meta, dict) and source_meta.get("path"):
        return Path(str(source_meta["path"])).stem
    if profile.get("path"):
        return Path(str(profile["path"])).stem
    return out_dir.name


# ---------------------------------------------------------------- Markdown


def render_markdown(report: dict) -> str:
    """把评估结果渲染成可入库、可 diff 的 Markdown。"""
    m = report.get("metrics") or {}
    coverage = m.get("paragraph_coverage") or {}
    roundtrip = m.get("citation_roundtrip") or {}
    structure = m.get("structure") or {}
    tables = m.get("tables") or {}
    numbering = m.get("numbering") or {}
    unsupported = m.get("unsupported") or {}
    ocr = m.get("ocr") or {}

    lines = [
        f"# DOCX 评估报告：`{report.get('doc_id', '')}`",
        "",
        "> 由 `docqa eval-docx` 生成。**只读 ingest 产物，不重新解析 DOCX**；",
        "> 段落覆盖率、引用回环与 unit_count 对账都是硬门禁，任一不满足即整体未通过。",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 文档 | `{report.get('doc_id', '')}` |",
        f"| 来源类型 | `{report.get('source_type', '')}`（原生文本，无页、无 OCR 层） |",
        f"| 非空正文块 | {report.get('unit_count', 0)} |",
        f"| 声明的 unit_count | "
        f"{report.get('declared_unit_count') if report.get('declared_unit_count') is not None else '未声明'} |",
        f"| 片段数 | {m.get('chunks', 0)} |",
        f"| 引用模板 | `{report.get('docx_template', DOCX_CITATION_TEMPLATE)}` |",
        f"| 产物目录 | `{report.get('out_dir', '')}` |",
        f"| 结论 | {'通过' if report.get('gates_passed') else '未通过'} |",
        "",
        "## 硬门禁指标",
        "",
        "| 指标 | 值 | 口径 |",
        "|---|---|---|",
        f"| 段落覆盖率 | {_pct(coverage.get('ratio', 0))}"
        f"（{coverage.get('covered', 0)}/{coverage.get('total', 0)}） | "
        f"{coverage.get('caliber', '')} |",
        f"| 引用回环 | {_pct(roundtrip.get('ratio', 0))}"
        f"（{roundtrip.get('ok', 0)}/{roundtrip.get('total', 0)}） | "
        f"{roundtrip.get('caliber', '')} |",
        f"| `unit_count` 对账 | {_unit_count_text(report)} | "
        "产物自称的块数 vs `docx_blocks.jsonl` 的非空行数 |",
        "",
        "## 结构与解析统计",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 段落 | {structure.get('paragraphs', 0)} |",
        f"| 标题 | {structure.get('headings', 0)} |",
        f"| 表格 | {structure.get('tables', 0)} |",
        f"| 表格已线性化 | {structure.get('tables_linearized', 0)} |",
        f"| 表格结构解析（网格） | {'是' if tables.get('structure_parsed') else '否（Phase 1 只线性化）'} |",
        f"| 自动编号：字面/计算/缺失 | {numbering.get('literal', 0)} / "
        f"{numbering.get('computed', 0)} / {numbering.get('missing', 0)} |",
        f"| OCR 指标 | {'不适用：' + str(ocr.get('reason', '')) if ocr.get('not_applicable') else '未提供'} |",
        "",
    ]

    lines += _offender_lines(coverage, roundtrip)
    lines += _failure_lines(report)
    lines += _warning_lines(report)
    lines += _unsupported_lines(unsupported)

    lines += [
        "## 复现",
        "",
        "```powershell",
        "uv run docqa ingest <file.docx>              # 产物目录：out/<doc_id>/",
        "uv run docqa eval-docx <file.docx> --out out/docx_eval.md",
        "```",
        "",
    ]
    notes = report.get("notes") or []
    if notes:
        lines += ["**口径提醒**", ""]
        lines += [f"- {note}" for note in notes]
        lines.append("")
    return "\n".join(lines)


def _unit_count_text(report: dict) -> str:
    declared = report.get("declared_unit_count")
    actual = report.get("unit_count", 0)
    if declared is None:
        return "未声明（无法核对）"
    verdict = "一致" if declared == actual else "不一致"
    return f"{verdict}（声明 {declared} / 实际 {actual}）"


def _offender_lines(coverage: dict, roundtrip: dict) -> list[str]:
    lines: list[str] = []
    blocks = coverage.get("offenders") or []
    if blocks:
        lines += ["## 未覆盖的正文块", ""]
        for item in blocks:
            reason = item.get("reason") or ""
            suffix = f"　原因：{reason}" if reason else ""
            lines.append(
                f"- `{item.get('block_id', '')}`（¶{item.get('para_no', '?')}）："
                f"{item.get('preview', '')}{suffix}"
            )
        lines.append("")
    chunks = roundtrip.get("offenders") or []
    if chunks:
        lines += ["## 无法回环的片段", ""]
        for item in chunks:
            lines.append(
                f"- `{item.get('chunk_id', '')}` 渲染为 `{item.get('rendered', '')}`，"
                "解析不回自身"
            )
        lines.append("")
    return lines


def _failure_lines(report: dict) -> list[str]:
    failures = report.get("failures") or []
    if not failures:
        return []
    lines = ["## 失败项", ""]
    lines += [f"- {failure}" for failure in failures]
    lines.append("")
    return lines


def _warning_lines(report: dict) -> list[str]:
    warnings = report.get("warnings") or []
    if not warnings:
        return []
    lines = ["## 警告（不设门禁）", ""]
    lines += [f"- {warning}" for warning in warnings]
    lines.append("")
    return lines


def _unsupported_lines(unsupported: dict) -> list[str]:
    lines = ["## 未支持构造（计数上报）", ""]
    if not unsupported:
        lines += ["产物未提供 `docx.unsupported` 统计。", ""]
        return lines
    lines += ["| 构造 | 数量 |", "|---|---:|"]
    for key, value in sorted(unsupported.items()):
        lines.append(f"| `{key}` | {value} |")
    lines.append("")
    lines.append(
        "这些构造在 Phase 1 **不抽取、只计数**：脚注/尾注、文本框/形状、公式结构、"
        "批注、页眉页脚、图片 OCR 都不在范围内。计数为 0 也会出现在表里，"
        "否则「没计数」与「计数为 0」在产物里长得一样。"
    )
    lines.append("")
    return lines


# ---------------------------------------------------------------- 工具


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "0.0%"


def _as_int(raw: Any) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _as_int_or_none(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
