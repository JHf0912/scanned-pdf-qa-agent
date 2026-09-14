"""共享测试夹具。

原则：**单元测试不碰网络、不碰模型、不碰真实文档。**
所有需要外部依赖的测试都标 `@pytest.mark.live` 并默认跳过——
一份跑得起来但结果随环境漂移的测试，比没有测试更糟。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docqa.config import DomainProfile, load_config, load_domain
from docqa.models import Chunk, Citation, OcrBlock, OcrPage, Usage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def cfg():
    return load_config()


@pytest.fixture(scope="session")
def standard_profile(cfg) -> DomainProfile:
    return load_domain("standard", cfg)


@pytest.fixture(scope="session")
def notice_profile(cfg) -> DomainProfile:
    return load_domain("notice", cfg)


def make_block(idx: int, btype: str, text: str, label: str | None = None,
               page: int = 1) -> OcrBlock:
    return OcrBlock(
        block_id=f"p{page}-b{idx}", page_no=page, type=btype, order=idx,
        text=text, clause_label=label,
    )


def make_page(blocks: list[OcrBlock], page_no: int = 1) -> OcrPage:
    return OcrPage(
        page_no=page_no, blocks=blocks, tables=[],
        image_sha256=f"sha{page_no}", provider="test", model="test",
        prompt_version="v1", usage=Usage(),
    )


@pytest.fixture
def structured_doc() -> OcrPage:
    """一份**结构化的合成文档**，条款号形态与国标一致，正文为自撰占位内容。

    刻意不使用真实标准原文：单元测试应当与具体文档解耦，
    否则换个文档就要重写测试。真实文档由评估（data/golden/）覆盖。
    """
    return make_page([
        make_block(0, "heading", "示例技术条件"),
        make_block(1, "clause", "1 范围", "1"),
        make_block(2, "paragraph", "本文件规定了示例件的技术要求与验收检查。"),
        make_block(3, "clause", "3 技术要求", "3"),
        make_block(4, "clause", "3.1 抗拉强度", "3.1"),
        make_block(5, "paragraph", "抗拉强度应大于等于 590 MPa。"),
        make_block(6, "clause", "3.6 斜度公差", "3.6"),
        make_block(7, "paragraph",
                   "斜度 1:100 的角度公差按 GB/T 11334—2005 中的 AT8 级选取，极限偏差为 ±AT8/2。"),
        make_block(8, "table", "| 检查项目 | AQL |\n|---|---|\n| 键宽 b | 1.0 |", "表 1"),
    ])


@pytest.fixture
def flat_doc() -> OcrPage:
    """一份**没有任何结构**的连续正文，用于验证句子窗口退化路径。"""
    return make_page([
        make_block(i, "paragraph", t) for i, t in enumerate([
            "为做好有关工作，根据上级文件精神，现成立工作小组。",
            "组长由某某担任，组员包括若干人，具体名单见附件。",
            "请各单位遵照执行，如有问题及时反馈。",
            "特此通知。",
        ])
    ])


@pytest.fixture
def evidence_map() -> dict[str, str]:
    return {
        "c1": "3.6 楔键斜度 1:100 的角度公差按 GB/T 11334—2005 中的 AT8 级选取，其极限偏差为 ±AT8/2。",
        "c2": "3.1 键的抗拉强度应大于等于 590 MPa。",
    }


@pytest.fixture
def good_citation() -> Citation:
    return Citation(page_no=3, clause_label="3.6", chunk_id="c1", quote="楔键斜度…")


@pytest.fixture
def chunks() -> list[Chunk]:
    return [
        Chunk(chunk_id="c1", doc_id="d", page_no=3, ordinal=1, kind="clause",
              text="3.6 楔键斜度 1:100 的角度公差按 GB/T 11334—2005 中的 AT8 级选取，"
                   "其极限偏差为 ±AT8/2。",
              clause_path=("斜度公差", "3.6"), clause_label="3.6"),
        Chunk(chunk_id="c2", doc_id="d", page_no=3, ordinal=2, kind="clause",
              text="3.1 键的抗拉强度应大于等于 590 MPa。",
              clause_path=("抗拉强度", "3.1"), clause_label="3.1"),
        Chunk(chunk_id="c3", doc_id="d", page_no=4, ordinal=3, kind="table",
              text="| 检查项目 | 半圆键 |\n|---|---|\n| 键宽 b | 1.0 |",
              clause_path=("表 1",), clause_label="表 1", table_id="p4-t1"),
    ]


# ---------------------------------------------------------------- 共用夹具
#
# 放在 conftest 而不是某个测试文件里：跨文件引用夹具是无效的
# （pytest 只在 conftest 与本文件内查找），曾因此让一整个测试文件报 ERROR。

@pytest.fixture
def agent(structured_doc, standard_profile, cfg):
    """走抽取式（无模型）的问答 Agent，供多个测试文件共用。"""
    from docqa.chunker import chunk_document
    from docqa.index import HybridIndex
    from docqa.llm import NullProvider
    from docqa.qa import QaAgent

    chunks, _ = chunk_document([structured_doc], "d", standard_profile)
    return QaAgent(HybridIndex(chunks, standard_profile), NullProvider(), cfg,
                   standard_profile, doc_id="d")


@pytest.fixture
def golden_file(tmp_path) -> Path:
    """两题的最小金标集：一道应答题 + 一道无答案题。"""
    import json

    p = tmp_path / "qa.jsonl"
    items = [
        {"qid": "1", "question": "抗拉强度要求是多少", "category": "clause",
         "must_contain": ["590"], "citation_pages": [1]},
        {"qid": "2", "question": "今天天气怎么样", "category": "no_answer",
         "expected_refusal": True, "expected_reason": "not_in_document"},
    ]
    p.write_text(
        "\n".join(json.dumps(i, ensure_ascii=False) for i in items), encoding="utf-8"
    )
    return p
