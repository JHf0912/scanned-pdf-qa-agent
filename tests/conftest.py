"""共享测试夹具。

原则：**单元测试不碰网络、不碰模型、不碰真实文档。**
所有需要外部依赖的测试都标 `@pytest.mark.live` 并默认跳过——
一份跑得起来但结果随环境漂移的测试，比没有测试更糟。

## 关于 `live` 通道

这个标记曾经是**空的**：`pyproject.toml` 注册了它，本文件也这么写着，
但全仓没有一个用例打标——于是"需要真实端点的验证有专门通道"这句话
只是承诺，实际没有任何真实端点被验证过。

现在它是一条真的通道：

```powershell
uv run pytest -m live --live          # 有密钥时，真的打一次端点
uv run pytest -m live                 # 不加 --live 则全部 skip
uv run pytest -m "not live"           # 默认行为（CI 用这条）
```

`--live` 必须显式给：光有密钥不该让测试悄悄开始花钱。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from docqa.config import DomainProfile, load_config, load_domain
from docqa.models import Chunk, Citation, OcrBlock, OcrPage, Usage

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- live 通道

def pytest_addoption(parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="真的调用外部端点跑 @pytest.mark.live 用例（会产生费用）",
    )


def pytest_collection_modifyitems(config, items) -> None:
    """没有 `--live` 时，把 live 用例全部 skip 掉并说明原因。

    注意"没有密钥"与"没有加开关"是两件事，提示要分开写：
    否则用户在配了密钥之后仍然看到 skip，会以为密钥没生效。
    """
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(
        reason="需要真实端点：加 --live 才会执行（会产生 API 费用）"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


# ---------------------------------------------------------------- 临时目录
#
# 把 pytest 的临时目录换成一份**在当前进程里可枚举**的等价实现。
#
# 为什么需要（这不是洁癖，是真实踩过的坑）
# ---------------------------------------
# pytest 的 `tmp_path` 为防符号链接攻击，用 `mkdir(mode=0o700)` 建目录
# （见 `_pytest/tmpdir.py:getbasetemp` 与 `_pytest/pathlib.make_numbered_dir`）。
# 在 Windows 上，`0o700` 会让新目录拿到**受保护的 DACL**——
# 只有 `OWNER RIGHTS` / `SYSTEM` / `Administrators` 三项，且**不再继承父目录**。
#
# 在普通开发者机器上这没问题。但在**降权运行的沙箱或 CI 容器**里
# （进程本身在低完整性级别，还叠着一层写白名单），当前身份不算 OWNER RIGHTS，
# 于是 pytest 自己刚建出来的 basetemp **立刻就无法枚举**：
#
#   - 每个用到 `tmp_path` 的用例在 setup 阶段报 `PermissionError: WinError 5`；
#   - 收尾阶段 `cleanup_dead_symlinks` 再炸一次，连汇总行都来不及打印。
#
# 现象是"测试全挂了"，而代码一行都没动过。`--basetemp` 也救不了：
# pytest 会先 `rm_rf` 掉给定目录，再以 `mode=0o700` 重建。
#
# 所以这里覆盖 `tmp_path_factory` 与 `tmp_path` 两个**官方扩展点**，
# 用一样的语义（每个用例一个独立目录、会话级复用、不自动删除）建目录，
# 只是不收紧权限位。除权限位外的行为与 pytest 原生一致。

class _TmpPathFactory:
    """`tmp_path_factory` 的替身：语义相同，权限位不收紧。"""

    def __init__(self, base: Path) -> None:
        self._base = base
        self._counter = 0
        base.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self) -> Path:
        return self._base

    def getbasetemp(self) -> Path:
        return self._base

    def mktemp(self, name: str, numbered: bool = True) -> Path:
        # 用例名可能含 `[`、`]`、`/` 等非法字符，先洗掉。
        safe = "".join(ch if (ch.isalnum() or ch in "-_.") else "_" for ch in name)[:60]
        self._counter += 1
        prefix = f"{self._counter:03d}-" if numbered else ""
        candidate = self._base / f"{prefix}{safe}"
        suffix = 1
        # 重名不能直接复用：复用一个已有目录会让"本用例的干净目录"这个前提失效。
        while candidate.exists():
            suffix += 1
            candidate = self._base / f"{prefix}{safe}{suffix}"
        candidate.mkdir(parents=True)
        return candidate


def _tmp_base() -> Path:
    """选一个当前进程确实能枚举的临时根目录。

    先试仓库内（与源码同卷，PyMuPDF 打开临时 PDF 时不必跨卷），
    再退到系统临时目录（只读检出、容器只读挂载等场景下仓库不可写）。
    """
    candidates = [
        PROJECT_ROOT / ".pytest-tmp",
        Path(tempfile.gettempdir()) / "docqa-pytest",
    ]
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            os.listdir(candidate)  # 关键：自己确认可枚举，别把问题留到用例里
        except OSError:
            continue
        return candidate
    raise RuntimeError(
        "找不到可枚举的 pytest 临时目录。请检查进程对仓库或系统临时目录的写权限。"
    )


@pytest.fixture(scope="session")
def tmp_path_factory() -> _TmpPathFactory:
    return _TmpPathFactory(_tmp_base())


@pytest.fixture
def tmp_path(tmp_path_factory, request) -> Path:
    """与 pytest 原生 `tmp_path` 同语义：每个用例一个独立目录。"""
    return tmp_path_factory.mktemp(request.node.name)


@pytest.fixture
def tmpdir(tmp_path):
    """兼容 `tmpdir` 这个旧名字（个别测试按字符串路径使用）。"""
    return str(tmp_path)


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
