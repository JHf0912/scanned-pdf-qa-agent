"""DOCX 评估测试：段落覆盖、引用回环、N/A 口径与 runner 的双定位。

为什么这些指标是**硬门禁**
------------------------
`¶n` 与 `[§3.6 ¶4]` 是本项目对 DOCX 的全部承诺。两个数字必须同时成立：

- **段落覆盖率 = 1.0**：每个非空正文块都出现在至少一个片段里。
  它不是"质量指标"，而是"有没有内容被静默丢掉"的可执行形式——
  少一段就可能导致系统自信地回答"文档里没有"。
- **引用回环 = 1.0**：渲染出来的引用能**原样解析回同一个片段**。
  回到别的段落，读者点开看到的就是错的证据。

测试用的产物目录是**手写**的，不经过 ingest：评估函数只读产物
（profile.json / chunks.jsonl / docx_blocks.jsonl / manifest.json），
这一层要能脱离管线单独验证。
"""

from __future__ import annotations

import copy
import json
from dataclasses import fields
from pathlib import Path

import pytest

from docqa.errors import InputError
from docqa.eval.docx_eval import render_markdown, run_docx_eval
from docqa.eval.runner import GoldenItem, context_utilization, run_eval, score_item
from docqa.models import AnswerBundle, Citation, RetrievalHit

DOC_ID = "demo-b1"


# ---------------------------------------------------------------- 造产物


def _block(para_no: int, kind: str, text: str, **extra) -> dict:
    """一行 `docx_blocks.jsonl`。字段与 `DocxBlock.to_dict()` 对齐。"""
    data = {
        "block_id": f"b{para_no}",
        "body_index": para_no - 1,
        "para_no": para_no,
        "kind": kind,
        "label": None,
        "label_text": None,
        "title": "",
        "text": text,
        "level": None,
        "style_name": None,
        "table_id": None,
        "structure_parsed": False,
        "hidden_runs": 0,
        "rows": [],
    }
    data.update(extra)
    return data


def _chunk(
    ordinal: int,
    kind: str,
    text: str,
    *,
    label: str | None = None,
    section: str | None = None,
    para_no: int | None = None,
    para_end: int | None = None,
    table_id: str | None = None,
    block_ids: tuple[str, ...] = (),
) -> dict:
    """一行 `chunks.jsonl`。DOCX 一律 `locator_kind="paragraph"`、没有页码。"""
    return {
        "chunk_id": f"{DOC_ID}:b{ordinal}",
        "doc_id": DOC_ID,
        "page_no": None,
        "ordinal": ordinal,
        "kind": kind,
        "text": text,
        "clause_path": [],
        "clause_label": label,
        "table_id": table_id,
        "block_ids": list(block_ids),
        "char_len": len(text),
        "locator_kind": "paragraph",
        "section": section,
        "para_no": para_no,
        "para_end": para_no if para_end is None else para_end,
    }


BLOCKS = [
    _block(1, "heading", "3 技术要求", label="3", label_text="3", title="技术要求", level=1),
    _block(2, "heading", "3.6 斜度公差", label="3.6", label_text="3.6", title="斜度公差", level=2),
    _block(3, "paragraph", "斜度 1:100 的角度公差按 GB/T 11334—2005 中的 AT8 级选取。"),
    _block(
        4, "table",
        "检查项目 | 半圆键 | 楔键 | 键宽 b | 1.0 | 1.5",
        table_id="t1",
        rows=[["检查项目", "半圆键", "楔键"], ["键宽 b", "1.0", "1.5"]],
    ),
    _block(5, "paragraph", "包装箱内应附有合格证与中文说明书。"),
]

CHUNKS = [
    _chunk(1, "heading", "3 技术要求", label="3", section="3", para_no=1, para_end=1,
           block_ids=("b1",)),
    _chunk(
        2, "paragraph",
        "3.6 斜度公差\n斜度 1:100 的角度公差按 GB/T 11334—2005 中的 AT8 级选取。",
        label="3.6", section="3.6", para_no=2, para_end=3,
        block_ids=("b2", "b3"),
    ),
    _chunk(
        3, "table",
        "3.6 斜度公差\n检查项目 | 半圆键 | 楔键 | 键宽 b | 1.0 | 1.5",
        label="3.6", section="3.6", para_no=4, para_end=4, table_id="t1",
        block_ids=("b4",),
    ),
    _chunk(
        4, "paragraph",
        "3.6 斜度公差\n包装箱内应附有合格证与中文说明书。",
        label="3.6", section="3.6", para_no=5, para_end=5,
        block_ids=("b5",),
    ),
]

PROFILE = {
    "path": "demo.docx",
    "sha256": "0" * 8,
    "page_count": 0,
    "kind": "TEXT",
    "cross_engine": {},
    "source_type": "docx",
    "unit_count": 5,
    "pages": [],
}


def _profile(unit_count: int | None = 5) -> dict:
    """按需要块数造一份 profile；`None` 表示产物没声明 unit_count。"""
    profile = dict(PROFILE)
    if unit_count is None:
        profile.pop("unit_count")
    else:
        profile["unit_count"] = unit_count
    return profile

MANIFEST = {
    "source": {
        "path": "demo.docx",
        "sha256": "0" * 8,
        "source_type": "docx",
        "page_count": 0,
        "unit_count": 5,
    },
    "docx": {
        "paragraphs": 5,
        "headings": 2,
        "tables": 1,
        "tables_linearized": 1,
        "numbering": {
            "literal": 1,
            "computed": 1,
            "missing": 0,
            "unsupported_formats": [],
        },
        "unsupported": {"footnotes": 0, "text_boxes": 0, "equations": 0},
        "warnings": [],
        "unit_count": 5,
    },
    "chunk": {"chunks": 4, "structured": True, "citation_mode": "clause"},
    "ocr": {"not_applicable": "DOCX 原生文本，未调用 OCR"},
    "injection": {"scanned": True, "locator": "paragraph", "units_scanned": 5, "hits": []},
}


def _write_out_dir(
    root: Path,
    *,
    blocks: list[dict] | None = None,
    chunks: list[dict] | None = None,
    manifest: dict | None = None,
    profile: dict | None = None,
    omit: tuple[str, ...] = (),
) -> Path:
    """手写一份 DOCX 产物目录（与 C 的落盘契约一致）。"""
    root.mkdir(parents=True, exist_ok=True)
    payloads = {
        "profile.json": json.dumps(profile if profile is not None else PROFILE,
                                   ensure_ascii=False, indent=2),
        "chunks.jsonl": "\n".join(
            json.dumps(c, ensure_ascii=False) for c in (chunks if chunks is not None else CHUNKS)
        ),
        "docx_blocks.jsonl": "\n".join(
            json.dumps(b, ensure_ascii=False) for b in (blocks if blocks is not None else BLOCKS)
        ),
        "manifest.json": json.dumps(
            manifest if manifest is not None else MANIFEST, ensure_ascii=False, indent=2
        ),
        "ocr.json": "[]",
    }
    for name, text in payloads.items():
        if name in omit:
            continue
        (root / name).write_text(text, encoding="utf-8")
    return root


# ---------------------------------------------------------------- 段落覆盖


class TestParagraphCoverage:
    def test_full_coverage_passes(self, tmp_path: Path):
        rep = run_docx_eval(_write_out_dir(tmp_path))
        cov = rep["metrics"]["paragraph_coverage"]
        assert cov["covered"] == cov["total"] == 5
        assert cov["ratio"] == 1.0
        assert cov["offenders"] == []
        assert rep["gates_passed"] is True

    def test_uncovered_block_fails_and_lists_block_id(self, tmp_path: Path):
        # 删掉唯一含有第 5 段文本的片段
        chunks = [c for c in CHUNKS if c["chunk_id"] != f"{DOC_ID}:b4"]
        rep = run_docx_eval(_write_out_dir(tmp_path, chunks=chunks))
        cov = rep["metrics"]["paragraph_coverage"]
        assert rep["gates_passed"] is False
        assert cov["covered"] == 4 and cov["total"] == 5
        offenders = {o["block_id"] for o in cov["offenders"]}
        assert "b5" in offenders
        # offender 要带足够定位的预览，否则读者不知道该去原文哪里找
        assert any("包装箱" in o["preview"] for o in cov["offenders"])
        assert any("paragraph_coverage" in f for f in rep["failures"])

    def test_table_block_covered_by_cells_when_linearized_text_differs(self, tmp_path: Path):
        """表格口径：整表 text 不在片段里时，逐格判断；全部非空单元格出现即算覆盖。"""
        blocks = [
            _block(
                1, "table",
                "键宽 b | 1.0 | 长度 L | 2.0",
                table_id="t1",
                rows=[["键宽 b", "1.0"], ["长度 L", "2.0"]],
            )
        ]
        chunks = [
            _chunk(1, "table", "表 1 键宽 b 1.0 长度 L 2.0",
                   label="1", section="1", para_no=1, para_end=1, table_id="t1",
                   block_ids=("b1",))
        ]
        rep = run_docx_eval(
            _write_out_dir(tmp_path, blocks=blocks, chunks=chunks, profile=_profile(1))
        )
        assert rep["metrics"]["paragraph_coverage"]["ratio"] == 1.0

    def test_empty_text_blocks_are_not_counted(self, tmp_path: Path):
        blocks = [*BLOCKS, _block(6, "paragraph", "   ")]
        rep = run_docx_eval(_write_out_dir(tmp_path, blocks=blocks))
        assert rep["metrics"]["paragraph_coverage"]["total"] == 5

    def test_duplicate_text_does_not_mask_a_dropped_block(self, tmp_path: Path):
        """两段文字相同、删掉覆盖第二段的片段。

        以"任意片段文本包含"为判据时这里会 2/2 通过——正是审计指的掩盖效应；
        以 `chunk.block_ids` 为成员依据后必须失败。
        """
        blocks = [
            _block(1, "paragraph", "重复的段落内容。"),
            _block(2, "paragraph", "重复的段落内容。"),
        ]
        chunks = [
            _chunk(1, "paragraph", "重复的段落内容。", label="1", section="1",
                   para_no=1, para_end=1, block_ids=("b1",))
        ]
        rep = run_docx_eval(
            _write_out_dir(tmp_path, blocks=blocks, chunks=chunks, profile=_profile(2))
        )
        cov = rep["metrics"]["paragraph_coverage"]
        assert rep["gates_passed"] is False
        assert cov["covered"] == 1 and cov["total"] == 2
        assert [o["block_id"] for o in cov["offenders"]] == ["b2"]
        assert "block_ids" in cov["offenders"][0]["reason"]

    def test_text_in_a_non_covering_chunk_does_not_count(self, tmp_path: Path):
        """块文本出现在别的片段里不算覆盖：必须由声明覆盖它的片段给出。"""
        blocks = [_block(1, "paragraph", "目标段落文本。")]
        chunks = [
            _chunk(1, "paragraph", "无关内容。", label="1", section="1",
                   para_no=1, para_end=1, block_ids=("b1",)),
            _chunk(2, "paragraph", "目标段落文本。", label="2", section="2",
                   para_no=2, para_end=2, block_ids=("b9",)),
        ]
        rep = run_docx_eval(
            _write_out_dir(tmp_path, blocks=blocks, chunks=chunks, profile=_profile(1))
        )
        assert rep["gates_passed"] is False
        offenders = rep["metrics"]["paragraph_coverage"]["offenders"]
        assert [o["block_id"] for o in offenders] == ["b1"]

    def test_long_block_split_across_chunks_is_covered_by_sentences(self, tmp_path: Path):
        """一个正文块被切成多片：整块文本不在任何单片里，但每句都在，算覆盖。"""
        sentences = [f"第{i}句说明性内容，用于凑长段落。" for i in range(1, 7)]
        blocks = [_block(1, "paragraph", "".join(sentences))]
        chunks = [
            _chunk(1, "paragraph", "1 范围\n" + "".join(sentences[:2]),
                   label="1", section="1", para_no=1, para_end=1, block_ids=("b1",)),
            _chunk(2, "paragraph", "1 范围\n" + "".join(sentences[2:4]),
                   label="1", section="1", para_no=1, para_end=1, block_ids=("b1",)),
            _chunk(3, "paragraph", "1 范围\n" + "".join(sentences[4:]),
                   label="1", section="1", para_no=1, para_end=1, block_ids=("b1",)),
        ]
        rep = run_docx_eval(
            _write_out_dir(tmp_path, blocks=blocks, chunks=chunks, profile=_profile(1))
        )
        assert rep["metrics"]["paragraph_coverage"]["ratio"] == 1.0


# ---------------------------------------------------------------- 引用回环


class TestCitationRoundtrip:
    def test_every_chunk_roundtrips(self, tmp_path: Path):
        rep = run_docx_eval(_write_out_dir(tmp_path))
        rt = rep["metrics"]["citation_roundtrip"]
        assert rt["ok"] == rt["total"] == len(CHUNKS)
        assert rt["ratio"] == 1.0
        assert rt["offenders"] == []

    def test_unlocatable_chunk_is_offender(self, tmp_path: Path):
        # 没有条号、没有段落号 -> 渲染成 [?]，无法回环
        chunks = [*CHUNKS, _chunk(9, "paragraph", "没有任何定位信息", para_no=None)]
        rep = run_docx_eval(_write_out_dir(tmp_path, chunks=chunks))
        rt = rep["metrics"]["citation_roundtrip"]
        assert rep["gates_passed"] is False
        assert rt["ratio"] < 1.0
        assert any(o["chunk_id"] == f"{DOC_ID}:b9" for o in rt["offenders"])
        assert "p.None" not in json.dumps(rep, ensure_ascii=False)


# ---------------------------------------------------------------- N/A 与结构


class TestNotApplicableAndStructure:
    def test_ocr_metrics_are_explicitly_not_applicable(self, tmp_path: Path):
        rep = run_docx_eval(_write_out_dir(tmp_path))
        ocr = rep["metrics"]["ocr"]
        assert ocr["not_applicable"] is True
        assert "DOCX" in ocr["reason"]
        # 必须不是 0：0 会被读成"识别全错"，而不是"不适用"
        assert "cer" not in ocr
        assert "character_accuracy" not in ocr

    def test_structure_is_read_from_manifest(self, tmp_path: Path):
        rep = run_docx_eval(_write_out_dir(tmp_path))
        st = rep["metrics"]["structure"]
        assert st["paragraphs"] == 5
        assert st["headings"] == 2
        assert st["tables"] == 1
        assert st["tables_linearized"] == 1

    def test_numbering_missing_is_warning_not_gate(self, tmp_path: Path):
        manifest = copy.deepcopy(MANIFEST)
        manifest["docx"]["numbering"]["missing"] = 2
        rep = run_docx_eval(_write_out_dir(tmp_path, manifest=manifest))
        assert rep["gates_passed"] is True
        assert rep["metrics"]["numbering"]["missing"] == 2
        assert any("编号" in w for w in rep["warnings"])

    def test_unsupported_counts_are_reported(self, tmp_path: Path):
        manifest = copy.deepcopy(MANIFEST)
        manifest["docx"]["unsupported"]["footnotes"] = 2
        rep = run_docx_eval(_write_out_dir(tmp_path, manifest=manifest))
        assert rep["metrics"]["unsupported"]["footnotes"] == 2
        # 不支持的构造**计数上报**，但不设为门禁：Phase 1 的已知限制
        assert rep["gates_passed"] is True

    def test_source_type_and_unit_count_come_from_profile(self, tmp_path: Path):
        rep = run_docx_eval(_write_out_dir(tmp_path))
        assert rep["source_type"] == "docx"
        assert rep["unit_count"] == 5
        assert rep["doc_id"] == DOC_ID  # 从 chunks 的 doc_id 推得


# ---------------------------------------------------------------- Markdown


class TestMarkdown:
    def test_contains_key_fields(self, tmp_path: Path):
        md = render_markdown(run_docx_eval(_write_out_dir(tmp_path)))
        assert md.startswith("# DOCX 评估报告")
        assert DOC_ID in md
        assert "docx" in md
        assert "非空正文块" in md
        assert "段落覆盖率" in md
        assert "引用回环" in md
        assert "unit_count 对账" in md
        assert "不适用：DOCX 原生文本，无 OCR 层" in md
        assert "未支持构造" in md
        assert "p.None" not in md

    def test_failures_are_surfaced(self, tmp_path: Path):
        chunks = [c for c in CHUNKS if c["chunk_id"] != f"{DOC_ID}:b4"]
        md = render_markdown(run_docx_eval(_write_out_dir(tmp_path, chunks=chunks)))
        assert "未通过" in md
        assert "paragraph_coverage" in md
        assert "b5" in md


# ---------------------------------------------------------------- 产物校验


class TestArtifacts:
    @pytest.mark.parametrize("missing", ["profile.json", "chunks.jsonl", "docx_blocks.jsonl", "manifest.json"])
    def test_missing_artifact_raises_actionable_error(self, tmp_path: Path, missing: str):
        _write_out_dir(tmp_path, omit=(missing,))
        with pytest.raises(InputError) as exc:
            run_docx_eval(tmp_path)
        assert missing in str(exc.value)
        assert "ingest" in str(exc.value)

    def test_accepts_ingest_result_like_object(self, tmp_path: Path):
        """CLI 手里是 `IngestResult` 而不是目录：取其 `.out_dir`，不要靠猜签名。"""
        out_dir = _write_out_dir(tmp_path)

        class _Result:
            out_dir = None

        result = _Result()
        result.out_dir = out_dir
        assert run_docx_eval(result)["gates_passed"] is True

    def test_rejects_non_path_input(self):
        with pytest.raises(InputError) as exc:
            run_docx_eval(42)
        assert "产物目录" in str(exc.value)


class TestArtifactRobustness:
    """产物损坏时要**报错**，不能静默算出一个漂亮的 1.0。"""

    def test_malformed_profile_json_is_input_error(self, tmp_path: Path):
        _write_out_dir(tmp_path)
        (tmp_path / "profile.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(InputError) as exc:
            run_docx_eval(tmp_path)
        assert "profile.json" in str(exc.value)

    def test_non_object_json_is_input_error(self, tmp_path: Path):
        _write_out_dir(tmp_path)
        (tmp_path / "manifest.json").write_text("[]", encoding="utf-8")
        with pytest.raises(InputError) as exc:
            run_docx_eval(tmp_path)
        assert "顶层" in str(exc.value)

    def test_malformed_jsonl_line_reports_lineno(self, tmp_path: Path):
        _write_out_dir(tmp_path)
        path = tmp_path / "chunks.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "\n{broken\n", encoding="utf-8")
        with pytest.raises(InputError) as exc:
            run_docx_eval(tmp_path)
        assert "chunks.jsonl:5" in str(exc.value)

    def test_chunk_without_id_is_skipped_not_counted(self, tmp_path: Path):
        chunks = [*CHUNKS, {"doc_id": DOC_ID, "text": "没有 chunk_id 的行"}]
        rep = run_docx_eval(_write_out_dir(tmp_path, chunks=chunks))
        assert rep["metrics"]["chunks"] == len(CHUNKS)
        assert rep["gates_passed"] is True

    def test_missing_block_id_falls_back_to_para_no(self, tmp_path: Path):
        block = _block(5, "paragraph", "包装箱内应附有合格证与中文说明书。")
        block.pop("block_id")
        blocks = [b for b in BLOCKS if b["para_no"] != 5] + [block]
        chunks = [c for c in CHUNKS if c["chunk_id"] != f"{DOC_ID}:b4"]
        rep = run_docx_eval(_write_out_dir(tmp_path, blocks=blocks, chunks=chunks))
        offenders = rep["metrics"]["paragraph_coverage"]["offenders"]
        assert [o["block_id"] for o in offenders] == ["b5"]

    def test_table_rows_with_odd_shapes_do_not_crash(self, tmp_path: Path):
        blocks = [
            _block(
                1, "table", "键宽 b | 1.0",
                table_id="t1",
                rows=[["键宽 b", "1.0"], "长度 L", 42],
            )
        ]
        chunks = [
            _chunk(1, "table", "键宽 b 1.0 长度 L", label="1", section="1",
                   para_no=1, para_end=1, table_id="t1", block_ids=("b1",))
        ]
        rep = run_docx_eval(
            _write_out_dir(tmp_path, blocks=blocks, chunks=chunks, profile=_profile(1))
        )
        assert rep["metrics"]["paragraph_coverage"]["ratio"] == 1.0

    def test_whitespace_only_blocks_fail_with_clear_failure(self, tmp_path: Path):
        rep = run_docx_eval(
            _write_out_dir(
                tmp_path,
                blocks=[_block(1, "paragraph", "   ")],
                chunks=[],
                profile=_profile(0),
            )
        )
        assert rep["gates_passed"] is False
        assert any("没有非空正文块" in f for f in rep["failures"])

    def test_empty_chunks_fail_with_clear_failure(self, tmp_path: Path):
        rep = run_docx_eval(_write_out_dir(tmp_path, chunks=[]))
        assert rep["gates_passed"] is False
        assert any("chunks.jsonl 为空" in f for f in rep["failures"])

    def test_numbering_unsupported_formats_warn(self, tmp_path: Path):
        manifest = copy.deepcopy(MANIFEST)
        manifest["docx"]["numbering"]["unsupported_formats"] = ["chineseLegalSimplified"]
        rep = run_docx_eval(_write_out_dir(tmp_path, manifest=manifest))
        assert any("chineseLegalSimplified" in w for w in rep["warnings"])
        assert rep["gates_passed"] is True

    def test_docx_meta_not_a_dict_degrades_to_zeros(self, tmp_path: Path):
        manifest = {"source": {"source_type": "docx", "unit_count": 5}, "docx": "oops"}
        rep = run_docx_eval(_write_out_dir(tmp_path, manifest=manifest))
        assert rep["metrics"]["structure"]["paragraphs"] == 0
        assert rep["metrics"]["unsupported"] == {}
        assert rep["metrics"]["numbering"]["missing"] == 0
        assert rep["doc_id"] == DOC_ID

    def test_doc_id_falls_back_to_manifest_source_stem(self, tmp_path: Path):
        chunks = [dict(c, doc_id="") for c in CHUNKS]
        rep = run_docx_eval(_write_out_dir(tmp_path, chunks=chunks))
        assert rep["doc_id"] == "demo"

    def test_doc_id_falls_back_to_out_dir_name(self, tmp_path: Path):
        chunks = [dict(c, doc_id="") for c in CHUNKS]
        profile = {k: v for k, v in PROFILE.items() if k != "path"}
        manifest = {"source": {}, "docx": {}}
        rep = run_docx_eval(
            _write_out_dir(tmp_path, chunks=chunks, manifest=manifest, profile=profile)
        )
        assert rep["doc_id"] == tmp_path.name

    def test_unit_count_falls_back_to_manifest_when_profile_lacks_it(self, tmp_path: Path):
        """profile 没写时读 manifest；两边一致才算通过（不是"回退到数块数"）。"""
        rep = run_docx_eval(_write_out_dir(tmp_path, profile=_profile(None)))
        assert rep["declared_unit_count"] == 5  # 来自 manifest.source.unit_count
        assert rep["unit_count"] == 5
        assert rep["gates_passed"] is True


class TestUnitCountGate:
    """`unit_count` 是"块文件本身完整"的交叉核对，不是装饰字段。"""

    def test_unit_count_must_match_nonempty_block_rows(self, tmp_path: Path):
        blocks = [_block(1, "paragraph", "第一段。"), _block(2, "paragraph", "第二段。")]
        chunks = [
            _chunk(1, "paragraph", "第一段。\n第二段。", label="1", section="1",
                   para_no=1, para_end=2, block_ids=("b1", "b2"))
        ]
        rep = run_docx_eval(
            _write_out_dir(tmp_path, blocks=blocks, chunks=chunks, profile=_profile(3))
        )
        assert rep["gates_passed"] is False
        assert rep["unit_count"] == 2
        assert rep["declared_unit_count"] == 3
        assert any(
            "unit_count" in f and "3" in f and "2" in f for f in rep["failures"]
        )

    def test_missing_unit_count_is_a_failure_not_a_free_pass(self, tmp_path: Path):
        blocks = [_block(1, "paragraph", "第一段。")]
        chunks = [
            _chunk(1, "paragraph", "第一段。", label="1", section="1",
                   para_no=1, para_end=1, block_ids=("b1",))
        ]
        manifest = copy.deepcopy(MANIFEST)
        manifest["source"].pop("unit_count")
        manifest["docx"].pop("unit_count")
        rep = run_docx_eval(
            _write_out_dir(
                tmp_path, blocks=blocks, chunks=chunks,
                profile=_profile(None), manifest=manifest,
            )
        )
        assert rep["gates_passed"] is False
        assert rep["declared_unit_count"] is None
        assert any("unit_count" in f for f in rep["failures"])


class _ParsedCitation:
    """模拟 `parse_citations()` 的返回（A 的 `Citation.evidence_chunk_ids`）。"""

    def __init__(self, chunk_id: str, evidence: tuple[str, ...] = ()) -> None:
        self.chunk_id = chunk_id
        self.evidence_chunk_ids = evidence


def _split_paragraph_artifacts() -> tuple[list[dict], list[dict]]:
    """一个正文块被切成三片、三片渲染出同一个 `[§1 ¶1]`。"""
    blocks = [_block(1, "paragraph", "第一句。第二句。第三句。")]
    chunks = [
        _chunk(2, "paragraph", "1 范围\n第一句。", label="1", section="1",
               para_no=1, para_end=1, block_ids=("b1",)),
        _chunk(3, "paragraph", "1 范围\n第二句。", label="1", section="1",
               para_no=1, para_end=1, block_ids=("b1",)),
        _chunk(4, "paragraph", "1 范围\n第三句。", label="1", section="1",
               para_no=1, para_end=1, block_ids=("b1",)),
    ]
    return blocks, chunks


class TestRoundtripEvidenceSet:
    """长段落被切成多片时，引用回的是"证据集合"而不是单个 chunk_id。"""

    def test_evidence_chunk_ids_cover_all_pieces(self, tmp_path: Path, monkeypatch):
        blocks, chunks = _split_paragraph_artifacts()
        ids = tuple(c["chunk_id"] for c in chunks)

        def fake_parse(rendered, all_chunks):
            return [_ParsedCitation(chunks[0]["chunk_id"], evidence=ids)]

        monkeypatch.setattr("docqa.eval.docx_eval.parse_citations", fake_parse)
        rep = run_docx_eval(
            _write_out_dir(tmp_path, blocks=blocks, chunks=chunks, profile=_profile(1))
        )
        rt = rep["metrics"]["citation_roundtrip"]
        assert rt["ok"] == rt["total"] == 3
        assert rep["gates_passed"] is True

    def test_empty_evidence_falls_back_to_chunk_id(self, tmp_path: Path, monkeypatch):
        blocks, chunks = _split_paragraph_artifacts()

        def fake_parse(rendered, all_chunks):
            return [_ParsedCitation(chunks[0]["chunk_id"])]

        monkeypatch.setattr("docqa.eval.docx_eval.parse_citations", fake_parse)
        rep = run_docx_eval(
            _write_out_dir(tmp_path, blocks=blocks, chunks=chunks, profile=_profile(1))
        )
        rt = rep["metrics"]["citation_roundtrip"]
        assert rt["ok"] == 1 and rt["total"] == 3
        assert rep["gates_passed"] is False

    @pytest.mark.skipif(
        "evidence_chunk_ids" not in {f.name for f in fields(Citation)},
        reason="等待 A 给 Citation 加 evidence_chunk_ids（落地后本用例自动启用）",
    )
    def test_real_parse_citations_covers_all_split_chunks(self, tmp_path: Path):
        blocks, chunks = _split_paragraph_artifacts()
        rep = run_docx_eval(
            _write_out_dir(tmp_path, blocks=blocks, chunks=chunks, profile=_profile(1))
        )
        assert rep["metrics"]["citation_roundtrip"]["ratio"] == 1.0


class TestCustomTemplate:
    """场景配置了自定义 DOCX 模板时，回环必须按同一模板判。"""

    def test_custom_docx_template_is_used_for_roundtrip(self, tmp_path: Path):
        blocks, chunks = _split_paragraph_artifacts()
        # 一个 parse_citations 不认识的形态：只要评估用的是自定义模板，
        # 回环就必须失败，并在 offender 里露出**自定义模板渲染出的串**。
        custom = "<<§{section} ¶{para}>>"
        rep = run_docx_eval(
            _write_out_dir(tmp_path, blocks=blocks, chunks=chunks, profile=_profile(1)),
            docx_template=custom,
        )
        rt = rep["metrics"]["citation_roundtrip"]
        assert rep["docx_template"] == custom
        assert rt["ratio"] == 0.0
        assert all("<<§1" in offender["rendered"] for offender in rt["offenders"])

    def test_none_keeps_the_default_template(self, tmp_path: Path):
        blocks, chunks = _split_paragraph_artifacts()
        rep = run_docx_eval(
            _write_out_dir(tmp_path, blocks=blocks, chunks=chunks, profile=_profile(1)),
            docx_template=None,
        )
        assert rep["metrics"]["citation_roundtrip"]["ratio"] == 1.0


class TestMarkdownEdges:
    def test_clean_report_has_no_failure_or_warning_sections(self, tmp_path: Path):
        blocks = [b for b in BLOCKS if b["kind"] != "table"]
        chunks = [c for c in CHUNKS if c["kind"] != "table"]
        md = render_markdown(
            run_docx_eval(
                _write_out_dir(
                    tmp_path, blocks=blocks, chunks=chunks, profile=_profile(4)
                )
            )
        )
        assert "## 失败项" not in md
        assert "## 警告（不设门禁）" not in md
        assert "## 未覆盖的正文块" not in md

    def test_roundtrip_offenders_get_their_own_section(self, tmp_path: Path):
        chunks = [*CHUNKS, _chunk(9, "paragraph", "没有任何定位信息", para_no=None)]
        md = render_markdown(run_docx_eval(_write_out_dir(tmp_path, chunks=chunks)))
        assert "## 无法回环的片段" in md
        assert f"{DOC_ID}:b9" in md

    def test_missing_unsupported_stats_are_stated(self, tmp_path: Path):
        manifest = {"source": {"source_type": "docx"}, "docx": {"numbering": {}}}
        md = render_markdown(run_docx_eval(_write_out_dir(tmp_path, manifest=manifest)))
        assert "产物未提供" in md


# ---------------------------------------------------------------- 端到端


class TestEndToEndWithPipeline:
    """产物契约的集成检查：`ingest()` 写出的东西，评估端必须读得懂。"""

    def test_artifacts_from_real_ingest_pass_both_gates(self, tmp_path: Path):
        from docqa.config import Config
        from docqa.pipeline import ingest
        from docx import Document

        path = tmp_path / "sample.docx"
        document = Document()
        document.add_paragraph("1 总则", style="Heading 1")
        document.add_paragraph("本文件规定了演示样件的技术要求。")
        document.add_paragraph("2.1 抗压强度", style="Heading 2")
        document.add_paragraph("抗压强度应大于等于 42 MPa。")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "项目"
        table.cell(0, 1).text = "指标"
        table.cell(1, 0).text = "抗压强度"
        table.cell(1, 1).text = "42 MPa"
        document.save(path)

        cfg = Config(cache_dir=tmp_path / "derived" / "cache")
        result = ingest(path, cfg=cfg, domain="standard", out_root=tmp_path / "out")

        rep = run_docx_eval(result)  # IngestResult，不是目录
        assert rep["gates_passed"] is True
        assert rep["source_type"] == "docx"
        assert rep["unit_count"] == result.profile.unit_count
        assert rep["metrics"]["paragraph_coverage"]["ratio"] == 1.0
        assert rep["metrics"]["citation_roundtrip"]["ratio"] == 1.0
        assert rep["metrics"]["structure"]["tables"] == 1
        assert rep["metrics"]["ocr"]["not_applicable"] is True


# ---------------------------------------------------------------- runner 双定位


def _pdf_citation() -> Citation:
    return Citation(
        page_no=3, clause_label="3.6", chunk_id="d:p3:c2", quote="",
        para_no=12, locator_kind="page",
    )


def _docx_citation(para_no: int = 4) -> Citation:
    return Citation(
        page_no=None, clause_label="3.6", chunk_id="d:b2", quote="",
        para_no=para_no, locator_kind="paragraph", section="3.6",
    )


def _bundle(*citations: Citation) -> AnswerBundle:
    return AnswerBundle(
        question="斜度公差是多少",
        answer="按 AT8 级选取。",
        citations=list(citations),
        hits=[RetrievalHit(chunk_id="d:b2", score=1.0, arm="bm25_word", rank=1)],
    )


class TestRunnerDocxLocator:
    def test_citation_pages_skip_none_and_do_not_crash(self):
        """混合 PDF+DOCX 时 `sorted({c.page_no})` 会因 None 抛 TypeError。"""
        detail = score_item(
            GoldenItem(qid="q", question="q", category="clause"),
            _bundle(_pdf_citation(), _docx_citation()),
        )
        assert detail["citation_pages"] == [3]
        assert "[§3.6 ¶4]" in detail["citation_locators"]
        assert "[p.3 §3.6]" in detail["citation_locators"]
        assert all("p.None" not in loc for loc in detail["citation_locators"])

    def test_page_metric_is_na_when_no_citation_carries_a_page(self):
        item = GoldenItem(
            qid="q", question="q", category="clause", citation_pages=[1], must_contain=["AT8"]
        )
        detail = score_item(item, _bundle(_docx_citation()))
        assert detail["citation_page_ok"] is None  # N/A，而不是 False
        assert detail["passed"] is True

    def test_pdf_zero_citations_is_a_failure_not_na(self):
        """PDF 题完全没有引用：以前判 False，改成三态后不能退化成"没量"。"""
        item = GoldenItem(qid="q", question="q", category="clause", citation_pages=[1])
        detail = score_item(item, AnswerBundle(question="q", answer="内容。", citations=[]))
        assert detail["citation_page_ok"] is False
        assert detail["passed"] is False

    def test_context_utilization_counts_evidence_chunks(self):
        """一个引用指向多个证据片段时，它们都算"被引用的 chunk"。"""
        cite = _ParsedCitation("d:b2", evidence=("d:b2", "d:b3"))
        bundle = AnswerBundle(
            question="q", answer="a", citations=[cite],
            hits=[
                RetrievalHit(chunk_id="d:b2", score=1.0, arm="bm25_word", rank=1),
                RetrievalHit(chunk_id="d:b3", score=0.9, arm="bm25_word", rank=2),
                RetrievalHit(chunk_id="d:b9", score=0.1, arm="bm25_word", rank=3),
            ],
        )
        assert context_utilization(bundle) == pytest.approx(2 / 3)

    def test_locator_match_controls_pass(self):
        ok_item = GoldenItem(
            qid="q", question="q", category="clause", must_contain=["AT8"],
            citation_locators=["[§3.6 ¶4]"],
        )
        assert score_item(ok_item, _bundle(_docx_citation()))["citation_locator_ok"] is True
        assert score_item(ok_item, _bundle(_docx_citation()))["passed"] is True

        bad_item = GoldenItem(
            qid="q", question="q", category="clause", must_contain=["AT8"],
            citation_locators=["[§9.9 ¶9]"],
        )
        detail = score_item(bad_item, _bundle(_docx_citation()))
        assert detail["citation_locator_ok"] is False
        assert detail["passed"] is False

    def test_golden_item_field_is_optional(self):
        assert GoldenItem.from_dict({"question": "q"}).citation_locators == []
        item = GoldenItem.from_dict({"question": "q", "citation_locators": ["[§3.6 ¶4]"]})
        assert item.citation_locators == ["[§3.6 ¶4]"]

    def test_run_eval_reads_source_type_from_manifest(self):
        def ask(question: str) -> AnswerBundle:
            return AnswerBundle(question=question, answer="")

        docx_rep = run_eval(
            [], ask, engine="docx", doc_id="d", domain="standard", manifest=MANIFEST
        )
        assert docx_rep.source_type == "docx"
        assert docx_rep.docx["paragraphs"] == 5

        # 旧 PDF 调用（不传 manifest）保持原口径
        pdf_rep = run_eval([], ask, engine="t", doc_id="d", domain="standard")
        assert pdf_rep.source_type == "pdf"
        assert pdf_rep.docx == {}
