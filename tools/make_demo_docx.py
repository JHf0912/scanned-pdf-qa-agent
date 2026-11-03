"""生成 DOCX 演示样例（合成内容，无版权、无个人信息）。

为什么需要一个"造"出来的样例
----------------------------
DOCX 解析的测试必须在 `tmp_path` 里现场生成（见 `tests/docx/docx_fixtures.py`），
但人工检查、演示、以及给评估用的固定输入需要一份**可提交的**文件。
随手拿一份真实合同/标准放进来会有两个问题：版权，以及里面一定有真实姓名、
单位、金额——这正是本项目坚持"测试与样例全部合成"的原因。

这份样例覆盖 Phase 1 的主路径：

- 多级标题（`Heading 1` / `Heading 2`），带字面条号；
- Word 自动编号（`numbering.xml` + `w:numPr`，两级），文本里**没有**条号；
- 普通正文段落；
- 简单表格（Phase 1 线性化，不解析合并网格）。

用法
----
    uv run python tools/make_demo_docx.py
    uv run python tools/make_demo_docx.py --out artifacts/docx/demo.docx

注意：`uv run` 若报 `docqa.exe` 被占用（本机 Web 服务正在跑），
加 `--no-sync` 跳过环境同步即可，与本次生成无关。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls

from docx import Document

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "artifacts" / "docx" / "demo.docx"

# 自动编号的定义：第 1 级起于 3（演示"编号不在文本里"），第 2 级是 3.1。
NUMBERING_XML = (
    '<w:abstractNum {ns} w:abstractNumId="90">'
    '<w:lvl w:ilvl="0"><w:start w:val="3"/>'
    '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1"/></w:lvl>'
    '<w:lvl w:ilvl="1"><w:start w:val="1"/>'
    '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2"/></w:lvl>'
    "</w:abstractNum>"
)
NUMBER_INSTANCE_XML = (
    '<w:num {ns} w:numId="90"><w:abstractNumId w:val="90"/></w:num>'
)


def _add_numbering(document: Document) -> None:
    numbering = document.part.numbering_part.element
    numbering.append(parse_xml(NUMBERING_XML.format(ns=nsdecls("w"))))
    numbering.append(parse_xml(NUMBER_INSTANCE_XML.format(ns=nsdecls("w"))))


def _number(paragraph, ilvl: int, num_id: int = 90) -> None:
    pPr = paragraph._p.get_or_add_pPr()
    pPr.append(
        parse_xml(
            f'<w:numPr {nsdecls("w")}>'
            f'<w:ilvl w:val="{ilvl}"/><w:numId w:val="{num_id}"/>'
            "</w:numPr>"
        )
    )


def build_demo() -> Document:
    """构造演示文档。内容全部是合成数据。"""
    document = Document()
    core = document.core_properties
    core.title = "DOCX Phase 1 合成演示文档"
    core.author = "docqa demo generator"
    core.comments = "合成样例：无版权内容、无个人信息。"
    _add_numbering(document)

    document.add_paragraph("1 总则", style="Heading 1")
    document.add_paragraph(
        "本文件规定了演示样件的技术要求与试验方法，用于验证 DOCX 原生文本的解析链路。"
    )
    document.add_paragraph("1.1 适用范围", style="Heading 2")
    document.add_paragraph("适用于 Phase 1 的演示与人工检查。")

    document.add_paragraph("2 技术要求", style="Heading 1")
    document.add_paragraph("2.1 外观", style="Heading 2")
    document.add_paragraph("样件表面应无裂纹、无划痕，颜色均匀。")

    table = document.add_table(rows=3, cols=3)
    for column, header in enumerate(("项目", "指标", "备注")):
        table.cell(0, column).text = header
    for row, values in enumerate(
        (("抗压强度", "42 MPa", "合成数据"), ("外观", "无裂纹", "合成数据")), start=1
    ):
        for column, value in enumerate(values):
            table.cell(row, column).text = value

    # 下面两个标题的条号只存在于 numbering.xml 里：
    # paragraph.text 是"试验方法"/“步骤”，解析器要把 "3" / "3.1" 算回来。
    chapter = document.add_paragraph("试验方法", style="Heading 1")
    _number(chapter, ilvl=0)
    document.add_paragraph("按下列步骤进行试验，记录全部观测结果。")
    step = document.add_paragraph("步骤", style="Heading 2")
    _number(step, ilvl=1)
    document.add_paragraph("将样件置于标准环境中静置 24 小时后测量。")
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 DOCX 演示样例（合成内容）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出路径")
    args = parser.parse_args(argv)
    out: Path = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    build_demo().save(out)
    sys.stdout.write(f"已生成 {out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
