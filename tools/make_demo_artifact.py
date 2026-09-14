"""生成**合成演示件**及其完整溯源链，供评审无密钥复现。

为什么需要这个
--------------
原设计打算把 OCR 溯源链（页图 + 识别产物 + Provenance + 提示词）提交入库，
让评审能独立核对。但本项目的两份真实文档**都不能公开**：

- GB/T 1568-2008 取自官方公开预览，已承诺**不再分发**；
- 另一份真实文档含个人身份信息，不便公开。

于是那条路根本走不通。改为**自造一份文档**：内容完全自撰，
因此可以自由公开；把它渲染成无文本层的扫描件走完整条管线，
产物一并提交。评审于是可以：

1. 重跑 `render.py`，核对页图哈希；
2. 看到溯源链的真实格式；
3. 用 `--engine fixture` 复现检索引擎/问答/自检/评估整条下游。

**必须说清楚的一点**：这份 fixture 的识别结果是**从同一份源文本程序化生成的**，
因此它**零 OCR 误差是构造出来的，不是识别出来的**。
这正是不允许用 fixture 运行声明任何 OCR 精度指标的原因——
它演示的是**管线与溯源链的格式**，不是识别能力。

用法
----
    uv run python tools/make_demo_artifact.py
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = ROOT / "artifacts" / "demo"
RAW_DIR = ROOT / "data" / "raw"

# 页面尺寸（A4 @150dpi）与字体
PAGE_W, PAGE_H = 1240, 1754
MARGIN = 110
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
)

# ---------------------------------------------------------------- 合成文档内容
#
# **全部自撰**，与任何真实标准或公文无关。刻意模仿"条款 + 表格"的形态，
# 好让管线里那些针对结构化文档的路径（条款切分、表格原子化、跨列定位）都被走到。

TITLE = "示例技术规范"
DOC_NO = "Q/EXAMPLE 001—2026"

PAGES: list[list[tuple[str, str]]] = [
    # 第 1 页：条款正文
    [
        ("title", TITLE),
        ("subtitle", DOC_NO),
        ("h2", "1  范围"),
        ("p", "本文件规定了示例件的术语、技术要求与验收规则。"),
        ("p", "本文件适用于示例件的设计、制造与验收。"),
        ("h2", "2  规范性引用文件"),
        ("p", "Q/EXAMPLE 100—2025  示例件通用要求"),
        ("p", "Q/EXAMPLE 200—2024  示例件检验方法"),
        ("h2", "3  技术要求"),
        ("clause", "3.1  示例件的额定载荷应大于等于 12.5 kN。"),
        ("clause", "3.2  示例件表面不允许有裂纹、锈蚀与毛刺。"),
        ("clause", "3.3  示例件的工作温度范围为 -20 ℃ ~ 60 ℃。"),
        ("clause", "3.4  示例件的密封等级应不低于 IP54。"),
        ("clause", "3.5  当公称直径 d 大于 8 mm 时，壁厚 t 应按 Q/EXAMPLE 100—2025 "
                   "的规定，d ≤ 20 mm 取 2.0 mm；d > 20 mm 取 3.0 mm。"),
    ],
    # 第 2 页：表格 + 后续条款
    [
        ("h2", "4  验收规则"),
        ("p", "示例件的检验项目与合格质量水平见表 1。"),
        ("table_caption", "表 1  检验项目与合格质量水平 AQL"),
        ("table", ""),  # 占位，实际表格由 build_page 绘制
        ("clause", "4.1  按 Q/EXAMPLE 200—2024 的规定进行抽样检验。"),
        ("clause", "4.2  样本量 n 为 8，合格判定数为 Ac。"),
        ("h2", "5  标志与包装"),
        ("clause", "5.1  包装箱外表面应标明制造厂名、产品名称与出厂日期。"),
        ("clause", "5.2  产品应涂防锈剂，自出厂之日起一年内不生锈。"),
    ],
]

# 表 1：两级表头 + 横向合并（刻意复刻国标表格的形态）
TABLE_HEADER = [
    [("检验项目", 2, 1), ("合格质量水平 AQL", 1, 3)],
    [("", 0, 0), ("甲型", 1, 1), ("乙型", 1, 1), ("丙型", 1, 1)],
]
TABLE_BODY = [
    ["外径", "1.0", "1.0", "1.5"],
    ["壁厚", "2.5", "2.5", "2.5"],
    ["长度", "4", "—", "4"],
    ["密封等级", "—", "2.5", "—"],
]

# 供 fixture 使用的结构化块（由上面的源文本程序化生成，故零 OCR 误差）
FIXTURE_PAGE1 = [
    {"type": "heading", "order": 0, "clause_label": None, "text": TITLE},
    {"type": "paragraph", "order": 1, "clause_label": None, "text": DOC_NO},
    {"type": "clause", "order": 2, "clause_label": "1", "text": "1  范围"},
    {"type": "paragraph", "order": 3, "clause_label": None,
     "text": "本文件规定了示例件的术语、技术要求与验收规则。"},
    {"type": "paragraph", "order": 4, "clause_label": None,
     "text": "本文件适用于示例件的设计、制造与验收。"},
    {"type": "clause", "order": 5, "clause_label": "2", "text": "2  规范性引用文件"},
    {"type": "paragraph", "order": 6, "clause_label": None,
     "text": "Q/EXAMPLE 100—2025  示例件通用要求"},
    {"type": "paragraph", "order": 7, "clause_label": None,
     "text": "Q/EXAMPLE 200—2024  示例件检验方法"},
    {"type": "clause", "order": 8, "clause_label": "3", "text": "3  技术要求"},
    {"type": "clause", "order": 9, "clause_label": "3.1",
     "text": "3.1  示例件的额定载荷应大于等于 12.5 kN。"},
    {"type": "clause", "order": 10, "clause_label": "3.2",
     "text": "3.2  示例件表面不允许有裂纹、锈蚀与毛刺。"},
    {"type": "clause", "order": 11, "clause_label": "3.3",
     "text": "3.3  示例件的工作温度范围为 -20 ℃ ~ 60 ℃。"},
    {"type": "clause", "order": 12, "clause_label": "3.4",
     "text": "3.4  示例件的密封等级应不低于 IP54。"},
    {"type": "clause", "order": 13, "clause_label": "3.5",
     "text": "3.5  当公称直径 d 大于 8 mm 时，壁厚 t 应按 Q/EXAMPLE 100—2025 的规定，"
             "d ≤ 20 mm 取 2.0 mm；d > 20 mm 取 3.0 mm。"},
]

_TABLE_HTML = """<table>
<thead>
<tr><th rowspan="2">检验项目</th><th colspan="3">合格质量水平 AQL</th></tr>
<tr><th>甲型</th><th>乙型</th><th>丙型</th></tr>
</thead>
<tbody>
<tr><td>外径</td><td>1.0</td><td>1.0</td><td>1.5</td></tr>
<tr><td>壁厚</td><td>2.5</td><td>2.5</td><td>2.5</td></tr>
<tr><td>长度</td><td>4</td><td>—</td><td>4</td></tr>
<tr><td>密封等级</td><td>—</td><td>2.5</td><td>—</td></tr>
</tbody></table>"""

FIXTURE_PAGE2 = [
    {"type": "clause", "order": 0, "clause_label": "4", "text": "4  验收规则"},
    {"type": "paragraph", "order": 1, "clause_label": None,
     "text": "示例件的检验项目与合格质量水平见表 1。"},
    {"type": "table", "order": 2, "clause_label": "表 1", "text": "",
     "table": {"caption": "表 1  检验项目与合格质量水平 AQL", "html": _TABLE_HTML}},
    {"type": "clause", "order": 3, "clause_label": "4.1",
     "text": "4.1  按 Q/EXAMPLE 200—2024 的规定进行抽样检验。"},
    {"type": "clause", "order": 4, "clause_label": "4.2",
     "text": "4.2  样本量 n 为 8，合格判定数为 Ac。"},
    {"type": "clause", "order": 5, "clause_label": "5", "text": "5  标志与包装"},
    {"type": "clause", "order": 6, "clause_label": "5.1",
     "text": "5.1  包装箱外表面应标明制造厂名、产品名称与出厂日期。"},
    {"type": "clause", "order": 7, "clause_label": "5.2",
     "text": "5.2  产品应涂防锈剂，自出厂之日起一年内不生锈。"},
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    raise RuntimeError(f"找不到可用的中文字体，试过：{FONT_CANDIDATES}")


def _draw_table(draw: ImageDraw.ImageDraw, y: int, font) -> int:
    """画表 1，并返回绘制后的 y 坐标。"""
    col_w = [220, 180, 180, 180]
    row_h = 62
    x0 = MARGIN
    n_rows = len(TABLE_HEADER) + len(TABLE_BODY)
    total_w = sum(col_w)
    bottom = y + row_h * n_rows

    # 外框与横线
    draw.rectangle([x0, y, x0 + total_w, bottom], outline=(0, 0, 0), width=2)
    for i in range(1, n_rows):
        # 第 1 列纵跨两行表头，所以表头之间那条横线**不能穿过它**——
        # 否则"检验项目"会被一条线从中间划开，看起来像两个格子。
        left = x0 + col_w[0] if i == 1 else x0
        draw.line([left, y + i * row_h, x0 + total_w, y + i * row_h],
                  fill=(0, 0, 0), width=1)
    # 竖线：表头第 2 行起才分列，所以上两行按合并结构画
    xs = [x0]
    for w in col_w:
        xs.append(xs[-1] + w)
    draw.line([xs[1], y, xs[1], bottom], fill=(0, 0, 0), width=1)  # 第 1 列始终分隔
    for x in xs[2:-1]:
        draw.line([x, y + row_h, x, bottom], fill=(0, 0, 0), width=1)

    def centred(text: str, cx: int, cy: int) -> None:
        if not text:
            return
        box = draw.textbbox((0, 0), text, font=font)
        draw.text((cx - (box[2] - box[0]) / 2, cy - (box[3] - box[1]) / 2 - box[1]),
                  text, fill=(0, 0, 0), font=font)

    # 表头。"检验项目"纵跨两行，所以它要居中在**整个两行块**里，
    # 而不是第一行——否则横线会从文字中间穿过去。
    centred("检验项目", x0 + col_w[0] / 2, y + row_h)  # 两行块的中心 = 1×row_h
    centred("合格质量水平 AQL", x0 + col_w[0] + sum(col_w[1:]) / 2, y + row_h / 2)
    for i, name in enumerate(("甲型", "乙型", "丙型")):
        centred(name, x0 + col_w[0] + col_w[1] * i + col_w[1] / 2, y + row_h * 1.5)

    # 数据行
    for r, row in enumerate(TABLE_BODY):
        cy = y + (len(TABLE_HEADER) + r) * row_h + row_h / 2
        for c, value in enumerate(row):
            centred(value, x0 + sum(col_w[:c]) + col_w[c] / 2, cy)

    return bottom


def build_page(page_idx: int, spec: list[tuple[str, str]]) -> Image.Image:
    img = Image.new("RGB", (PAGE_W, PAGE_H), "white")
    draw = ImageDraw.Draw(img)
    y = MARGIN

    f_title, f_h2, f_body = _font(46, True), _font(30, True), _font(26)

    for kind, text in spec:
        if kind == "table":
            y = _draw_table(draw, y + 10, f_body) + 40
            continue
        if kind == "table_caption":
            box = draw.textbbox((0, 0), text, font=f_body)
            draw.text(((PAGE_W - (box[2] - box[0])) / 2, y), text,
                      fill=(0, 0, 0), font=f_body)
            y += 52
            continue

        font = {"title": f_title, "subtitle": f_body, "h2": f_h2}.get(kind, f_body)
        if kind == "title":
            box = draw.textbbox((0, 0), text, font=font)
            draw.text(((PAGE_W - (box[2] - box[0])) / 2, y), text, fill=(0, 0, 0), font=font)
            y += 78
            continue
        if kind == "subtitle":
            box = draw.textbbox((0, 0), text, font=font)
            draw.text(((PAGE_W - (box[2] - box[0])) / 2, y), text,
                      fill=(60, 60, 60), font=font)
            y += 60
            continue

        # 正文按可用宽度折行
        avail = PAGE_W - 2 * MARGIN
        line = ""
        for ch in text:
            if draw.textlength(line + ch, font=font) > avail:
                draw.text((MARGIN, y), line, fill=(0, 0, 0), font=font)
                y += 42
                line = ch
            else:
                line += ch
        if line:
            draw.text((MARGIN, y), line, fill=(0, 0, 0), font=font)
            y += 52 if kind == "h2" else 42

    # 页脚页码
    draw.text((PAGE_W / 2 - 8, PAGE_H - 80), str(page_idx + 1), fill=(0, 0, 0), font=f_body)
    return img


def main() -> int:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    pages_dir = DEMO_DIR / "pages"
    pages_dir.mkdir(exist_ok=True)

    # 1) 画页图并打包成**无文本层**的扫描件式 PDF
    doc = pymupdf.open()
    for i, spec in enumerate(PAGES):
        png = pages_dir / f"p{i + 1:04d}.png"
        build_page(i, spec).save(png)
        page = doc.new_page(width=PAGE_W / 150 * 72, height=PAGE_H / 150 * 72)
        page.insert_image(page.rect, filename=str(png))
    pdf_path = RAW_DIR / "demo_scan.pdf"
    doc.save(pdf_path, garbage=4, deflate=True)
    doc.close()
    print(f"已生成 {pdf_path}（{len(PAGES)} 页，无文本层）")

    # 2) 写 fixture 识别结果（由源文本程序化生成 —— 零误差是构造出来的）
    ocr_dir = DEMO_DIR / "ocr"
    ocr_dir.mkdir(exist_ok=True)
    for i, blocks in enumerate((FIXTURE_PAGE1, FIXTURE_PAGE2), start=1):
        (ocr_dir / f"p{i:04d}.json").write_text(
            json.dumps({"blocks": blocks}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    (ocr_dir / "README.md").write_text(
        "# 这份 fixture 是构造出来的，不是识别出来的\n\n"
        "其中的结构化块由 `tools/make_demo_artifact.py` **从同一份源文本程序化生成**，\n"
        "因此它**零 OCR 误差是构造的结果**。\n\n"
        "它的用途是让评审在**没有任何模型密钥**的前提下复现整条下游链路\n"
        "（切分 → 检索 → 问答 → 自检 → 拒答 → 评估），并看到溯源链的真实格式。\n\n"
        "**不得据此声明任何 OCR 精度指标**——`eval` 也会拒绝把 fixture 运行\n"
        "与实跑基线相比（引擎名不同即拒绝，没有绕过开关）。\n",
        encoding="utf-8",
    )
    print(f"已生成 fixture 识别结果 -> {ocr_dir}")

    # 3) 提示词副本（溯源链的一环：没有它就说不清产物是哪版提示词产生的）
    shutil.copy2(ROOT / "prompts" / "ocr_page.v1.md", DEMO_DIR / "prompt.ocr_page.v1.md")
    print("已复制提示词副本")
    return 0


if __name__ == "__main__":
    sys.exit(main())
