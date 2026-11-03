# DOCX 原生支持 Phase 1 —— 设计漏洞审查与冻结契约

- 日期：2026-10-29
- 状态：已批准，实施前冻结
- 范围：原生文本 DOCX（Phase 1）；表格线性化；图片/扫描页留 Phase 3
- 关联计划：`C:\Users\26775\.claude\plans\pasted-content-id-3244-phase-1-polished-beacon.md`
- 实施方式：主会话为 leader，按 workstream 分派 Agent；完成后独立审计（只审不改），再修复、再验证

---

## 1. 为什么做这件事

系统当前只接受 PDF：`ingest` → `detect.profile_pdf` → 渲染/OCR/文本层 → `chunk_document` → 索引 → 问答。
DOCX 是原生文本，不需要检测、渲染与 OCR；但 DOCX 没有固定“页”的概念，Word 的页码取决于排版，不能假装有页码。因此 Phase 1 的目标不是“把 DOCX 当 PDF 处理”，而是：

> `docqa ingest x.docx` / 网页上传 DOCX → 标题与段落切分 → 检索 → 生成带 `[§3.6 ¶4]` 引用的答案 → 同一套自检、拒答、Web 工作台。

Phase 1 只做文本 DOCX。表格在 Phase 1 **线性化**以保住数值（不静默丢弃），结构化表格（`gridSpan`/`vMerge`）留 Phase 2；内嵌图片/扫描页留 Phase 3。

---

## 2. 审查方法

审查覆盖了以下文件的真实调用链，而不是只看设计文档：

- 数据模型：`src/docqa/models.py`
- 切分/检索：`src/docqa/chunker.py`、`src/docqa/index/lexical.py`、`src/docqa/index/store.py`
- 管线：`src/docqa/pipeline.py`、`src/docqa/detect.py`、`src/docqa/render.py`
- 问答/自检：`src/docqa/qa/answer.py`、`qa/verify.py`、`qa/agent.py`、`qa/policy.py`
- API/CLI：`src/docqa/api/app.py`、`src/docqa/cli.py`
- 评估/报告：`src/docqa/eval/runner.py`、`src/docqa/report.py`
- 前端：`web/src/types.ts`、`api.ts`、`format.ts`、`AnswerCard.tsx`、`EvidencePanel.tsx`、`DocRail.tsx`、`Overview.tsx`
- 依赖与测试：`pyproject.toml`、`tests/`

结论：原设计（只改 `Citation.page_no` + `SYSTEM_PROMPT` + 前端正则 + 上传校验）方向正确，但**定位抽象的影响面被低估**，且 DOCX 解析的若干结构问题会直接导致“回答看似有据、实际丢内容”。

---

## 3. 漏洞清单

### 3.1 P0：不改就会错或直接崩

| # | 漏洞 | 证据 / 后果 | 修正 |
|---|------|------------|------|
| 1 | 只改 `Citation.page_no` 不够 | `Chunk.page_no` 也是 `int`，并流入 chunk_id（`{doc_id}:p{page}:c{n}`）、证据头、`/api/chunks`、eval、report、前端 | `Chunk.page_no: int \| None`；新增 `locator_kind/section/para_no/para_end`；DOCX chunk_id 用 `{doc_id}:b{ordinal}` |
| 2 | `build_evidence_block()` 写死“第 N 页” | `qa/answer.py:75`；DOCX 会输出“第 None 页”，模型照抄证据头，引用在源头就坏了 | 证据头按 chunk locator 渲染：PDF `（第 3 页 §3.6）`，DOCX `（§3.6 ¶4）` |
| 3 | `verify.py` 不是“不用改” | `qa/verify.py:42` 的 `_CITATION_MARK_RE` 只剔除 `[p....]`；`[§3.6 ¶4]` 会让 3.6/4 进入数值断言 → `numeric_grounding` 假失败、`clause_consistency` 噪声 | 正则覆盖 `[p....]`、`[§... ¶...]`、`[¶...]`、`[§...]` 四种形态 |
| 4 | `parse_citations()` 只认 `[p....]` 且按页分组 | `qa/answer.py:83-124`；DOCX 没有页码，永远解析不到引用 | 双格式解析；DOCX 按 `(section, para_no)` 映射，`para_no` 落在 `[para_no, para_end]` |
| 5 | 提示词与修复提示写死 PDF 格式 | `SYSTEM_PROMPT` 第 2 条；`_make_repair_hint()` 的“补 [p.页码 §条款号]” | 提示词改为“引用格式必须与证据头一致”；混合 PDF+DOCX 时逐 chunk 生效 |
| 6 | `Citation.render()` 的退化模板只有页码 | `FALLBACK_CITATION_TEMPLATE`/`BARE_CITATION_TEMPLATE` 都含 `{page}` | 增加 `[§{section} ¶{para}]`、`[¶{para}]`、`[§{section}]`；按 `locator_kind` 分支 |
| 7 | 上传链路只认 PDF | `api/app.py:296-307`；`web/src/api.ts:69-78` 写死 `Content-Type: application/pdf`；`DocRail.tsx` 的 `accept` 与文案只写 PDF | 扩展名 + magic 分派；前端按文件类型发 Content-Type；接受 `.docx` |
| 8 | `load_result()` 假定 PDF 产物 | `pipeline.py:436` 重建 `PdfProfile` 且读 `ocr.json` | `SourceProfile` 加 `source_type/unit_count`；DOCX 写 `profile.json`、`ocr.json=[]`、`pages=[]`、`page_count=0` |
| 9 | `/api/status`、`_discover()`、`Overview`、`DocRail` 只报页数 | DOCX 显示“0 页” | 状态与产物加 `source_type/unit_count`；UI 显示“DOCX · 原生文本 · N 段” |
| 10 | CLI `ingest` 强制要 OCR 配置 | `cli.py:379` `_cfg(require_ocr=(engine == "vlm"))`，且总会构造 OCR 引擎 | DOCX 分支 `require_ocr=False`、不构造引擎；`--engine` 只提示“不适用” |
| 11 | `pipeline.ingest()` 先跑 `profile_pdf`/render/OCR | DOCX 进 PyMuPDF 会崩 | `assert_not_golden` 之后、detect 之前分派 `_ingest_pdf`/`_ingest_docx` |
| 12 | DOCX 正文顺序与文本提取会丢内容 | `document.paragraphs` + `.tables` 丢失交错顺序；`paragraph.text` 不含超链接、tab/br、字段；`w:sdt` 正文看不到 | `body.iterchildren()` 遍历 `CT_P`/`CT_Tbl`，下钻 `w:sdtContent`；run walker 含 `w:ins/w:hyperlink/fldSimple`，排除 `w:del/instrText/delText` |
| 13 | Word 自动编号不在文本里 | 标题用 `numPr` 时 `paragraph.text` 没有“3.6”，`clause_label` 为空 → 全部退化 `[¶n]`，条号一致性失效 | 有界解析 `numbering.xml`（decimal/中文/字母/罗马 + lvlText + start/lvlRestart）；无法解析时退回标题并记 manifest 警告 |
| 14 | 表格“Phase 1 不做”等于静默丢数值 | 标准/合同的关键值常在表格；检索会回答“未提及” | Phase 1 线性化表格为原子 table chunk；manifest 标 `structure_parsed=false`；Phase 2 再上 `gridSpan/vMerge` |
| 15 | ZIP/XML 校验不足 | 只查 magic + `word/document.xml` 挡不住加密条目、docm/dotx/旧 .doc、zip bomb、DTD/ENTITY、外部关系 | 独立 `security.py`：大小/条目/解压比、加密位、路径穿越、DTD/ENTITY、`document.xml` 上限、外部关系不解析；不落盘解压 |
| 16 | DOCX 文本同样不可信，漏了注入扫描 | 现有 `injection.scan_pages()` 是 PDF 形状 | DOCX 逐块 `scan_text()`；manifest 写 `injection`；报告文案用“段”而非“页” |
| 17 | 评估/报告把 DOCX 当 PDF | `eval/runner.py` 用 `citation_pages`；`report.py` 是“PDF 解析结果”/按页 | 页码指标对 DOCX 显式 N/A（不是 0）；新增 DOCX 结构/覆盖/引用回环指标；报告分支 |

### 3.2 P1：会显著影响可用性/可维护性

| # | 问题 | 修正 |
|---|------|------|
| 18 | `Citation.para_no` 已存在，原设计又加 `paragraph` 会重复 | 复用 `para_no`，不新增同义字段 |
| 19 | `section` 与 `clause_label` 语义混淆 | `clause_label` = 裸条号；`section` = 裸条号优先、否则标题；完整标题路径在 `clause_path`；引用校验仍用 `clause_label` |
| 20 | 多文档 PDF+DOCX 混合 | 渲染/证据头/歧义回退按 chunk locator 分支；`failed_pages` 仍只对 PDF 生效，`page_no=None` 跳过 |
| 21 | 旧产物兼容 | 新增字段全部带默认值；`load_result` 用 `.get()`；旧 `chunks.jsonl` 仍可读 |
| 22 | 段落号定义不清 | `¶n` = 文档顺序第 n 个**非空正文块**（段落或线性化表格；标题计入；空段落不计）；同时记 `body_index` |
| 23 | 文本丢失无法发现 | 硬不变量：每个非空正文块必须出现在 ≥1 个 chunk 中；`eval-docx` 覆盖不足直接失败并列 offenders |
| 24 | `PdfProfile` 名字对 DOCX 是假话 | 机械重命名为 `SourceProfile`，保留 `PdfProfile = SourceProfile` 兼容别名 |
| 25 | 依赖未声明 | `pyproject.toml` 加 `python-docx>=1.1.2` 并 `uv lock`；MIT，不引入 AGPL |

### 3.3 P2：明确留到后续阶段

- Phase 2：`gridSpan`/`vMerge` → `TableGrid` 矩形网格；单元格级引用 `[表 1 行 2 列 3]`。
- Phase 3：内嵌图片/扫描页走 VLM OCR；装饰图跳过；`needs_review`。
- Phase 1 明确不支持并**计数上报**：脚注/尾注、文本框/形状、OMML 公式（尽力抽 `m:t`）、批注、页眉页脚、隐藏文字、修订删除、嵌入对象。绝不静默丢。

---

## 4. 冻结契约

### 4.1 数据模型

```python
LocatorKind = Literal["page", "paragraph"]

@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    page_no: int | None
    ordinal: int
    kind: Literal["clause", "paragraph", "table", "heading"]
    text: str
    clause_path: tuple[str, ...] = ()
    clause_label: str | None = None   # 裸条号，如 "3.6"
    table_id: str | None = None
    block_ids: tuple[str, ...] = ()
    char_len: int = 0
    locator_kind: LocatorKind = "page"
    section: str | None = None        # 裸条号 "3.6"；无条号时为标题
    para_no: int | None = None
    para_end: int | None = None

@dataclass(frozen=True)
class Citation:
    page_no: int | None
    clause_label: str | None
    chunk_id: str
    quote: str
    para_no: int | None = None
    doc_id: str | None = None
    locator_kind: LocatorKind = "page"
    section: str | None = None
```

`SourceProfile`：

```python
@dataclass
class SourceProfile:
    path: Path
    sha256: str
    page_count: int
    pages: list[PageProfile]
    kind: PageKind
    cross_engine: dict[str, float] = field(default_factory=dict)
    source_type: Literal["pdf", "docx"] = "pdf"
    unit_count: int = 0          # DOCX 非空正文块数；PDF 为 0
PdfProfile = SourceProfile       # 兼容别名
```

`DomainProfile.docx_citation_template: str = "[§{section} ¶{para}]"`；`AnswerBundle` 增同名字段。

### 4.2 引用格式与证据头

| 来源 | 证据头 | 模型应输出 |
|------|--------|-----------|
| PDF 有条号 | `[doc:p3:c2] （第 3 页 §3.6）` | `[p.3 §3.6]` |
| PDF 无条号 | `[doc:p3:c2] （第 3 页 ¶12）` | `[p.3 ¶12]` |
| DOCX 有标题/条号 | `[doc:b2] （§3.6 ¶4）` | `[§3.6 ¶4]` |
| DOCX 无条号 | `[doc:b2] （¶4）` | `[¶4]` |

解析正则（草案）：

```python
_CITATION_RE = re.compile(
    r"\[p\.(?P<page>\d+)"
    r"(?:\s*§(?P<clause>[^\]]+?))?"
    r"(?:\s*¶(?P<para>\d+))?\]"
    r"|\[§(?P<section>[^\]]+?)\s*¶(?P<dpara>\d+)\]"
    r"|\[¶(?P<only_para>\d+)\]"
    r"|\[§(?P<section_only>[^\]]+?)\]"
)
```

**匹配规则**：原始 section 等于某 chunk 的 `clause_label` → 命中；含真实裸条号 token → 命中并记裸条号；否则按 `para_no` 回退并保留原始串（让 `citation_validity` 抓编造条号）；纯标题 → `clause_label=None`。去重键 `chunk_id|locator`。

### 4.3 段落序号 `¶n`

- `¶n` = 文档顺序第 n 个**非空正文块**（段落或线性化表格；标题计入；空段落不计）。
- 同时记 `body_index`（原始 body 子元素下标）到 block 内部，便于精确回溯。
- 每个 chunk 带 `para_no`/`para_end`；`[§3.6 ¶4]` 的 4 落在范围内即可映射。
- 文档固定写清定义，避免读者按 Word 段落数对不上。

### 4.4 DOCX 产物与 manifest

```text
<out_dir>/
  profile.json    # source_type=docx, kind=TEXT, page_count=0, unit_count=N, pages=[]
  ocr.json        # []
  pages.json      # []
  chunks.jsonl    # locator_kind=paragraph, section, para_no/para_end
  manifest.json
```

manifest 关键字段：

```json
{
  "source": {"path": "...", "sha256": "...", "source_type": "docx", "page_count": 0, "unit_count": 123},
  "detect": {"kind": "TEXT", "not_applicable": "DOCX 原生文本，无需 PDF 类型判定"},
  "render": {"not_applicable": "DOCX 无页图"},
  "ocr": {"not_applicable": "DOCX 原生文本，未调用 OCR", "engine": null, "pages_total": 0, "pages_failed": []},
  "docx": {
    "paragraphs": 123, "headings": 18, "tables": 4, "tables_linearized": 4,
    "numbering": {"literal": 10, "computed": 8, "missing": 0, "unsupported_formats": []},
    "unsupported": {"footnotes": 2, "text_boxes": 0, "equations": 0}
  },
  "chunk": {"chunks": 42, "structured": true, "used_sentence_fallback": false,
            "tables": 4, "citation_mode": "clause"},
  "injection": {"scanned": true, "locator": "paragraph", "pages_scanned": 0,
                "units_scanned": 123, "hits": []},
  "index": {},
  "usage_total": {"tokens_in": 0, "tokens_out": 0, "est_cost_usd": 0.0, "latency_ms": 0, "pages": 0},
  "elapsed_s": 0.4
}
```

`load_result()`：`SourceProfile` 用 `.get("source_type","pdf")`/`.get("unit_count",0)`；chunk 用 `.get("locator_kind","page")`、`.get("section")`、`.get("para_no")`、`.get("para_end")`；`page_no` 允许 `None`。旧产物零改动可读。

---

## 5. 目录与文件所有权

```text
src/docqa/docx/
  __init__.py        # parse_docx / is_docx / DocxDocument 再导出
  security.py        # ZIP/XML/大小/加密/DTD/路径校验
  numbering.py       # numPr -> 条号标签（有界）
  parser.py          # body 顺序、run walker、标题、表格线性化、unsupported 计数
  chunks.py          # 标题栈 -> Chunk；复用 chunker.split_sentences/split_long

tests/docx/
  conftest.py
  test_security.py
  test_parser.py
  test_numbering.py
  test_chunks.py
  test_pipeline.py
  test_eval.py

tools/make_demo_docx.py
artifacts/docx/{README.md,demo.docx}

docs/docx-support.md
docs/reviews/2026-10-29-docx-gap-review.md
docs/reviews/2026-10-29-docx-audit.md
docs/verification/2026-10-29-docx-phase1.md
```

| Workstream | 文件所有权 |
|-----------|-----------|
| A 模型/QA | `src/docqa/models.py`、`detect.py`（改名兼容）、`qa/answer.py`、`qa/verify.py`、`qa/policy.py`、`qa/agent.py`、`config.py`、`injection.py`、`docs/design.md` |
| B DOCX 解析 | `src/docqa/docx/*`、`pyproject.toml`、`uv.lock`、`tools/make_demo_docx.py`、`artifacts/docx/*`、`tests/docx/test_{security,parser,numbering,chunks}.py` |
| C 管线/CLI/API | `pipeline.py`、`cli.py`、`api/app.py`、`errors.py`、`tests/docx/test_pipeline.py`、`tests/test_cli_api.py` |
| D Web | `web/src/**`、`web/index.html`、`src/docqa/web/**`（构建产物）、E2E 脚本 |
| E 评估/文档 | `eval/docx_eval.py`、`eval/runner.py`、`report.py`、`tests/docx/test_eval.py`、`docs/docx-support.md`、`README.md` |

文件所有权互斥；A 改 `models.py` 后 B 不碰；C 依赖 A/B 接口；D 只改 `web/`；E 只改 `eval/report/docs`。

---

## 6. 验收标准

1. 无 OCR 配置的机器上，`docqa ingest x.docx` 成功，产物可 `ask`。
2. DOCX 答案引用形如 `[§3.6 ¶4]`，点击能定位到证据；PDF 仍为 `[p.N §...]`。
3. `eval-docx` 的 `paragraph_coverage=1.0`、`citation_roundtrip=1.0`。
4. 现有 PDF 全量测试与行为不变；coverage ≥ 84%；ruff 干净。
5. Web 三种视口（1440/768/390）无横向滚动、无 console error；DOCX 上传/问答/证据/自检全流程可用。
6. 独立审计报告中的 P0 全部关闭；P1 有明确处置（修复或写入限制）。

---

## 7. 明确不做 / 已知限制（Phase 1）

- 不做 DOCX 表格的合并单元格网格与单元格级定位；表格只线性化，`structure_parsed=false`。
- 不做 DOCX 内嵌图片/扫描页 OCR；图片计数但不识别。
- 不提取脚注/尾注、文本框/形状、批注、页眉页脚；公式尽力抽 `m:t` 但不保证结构。
- 不用 Word 渲染出的页码；DOCX 引用一律 `[§... ¶n]`，`¶n` 按本文定义。
- 不新增 Web API 端点；只扩展现有六个端点的字段。
- 不修改 `.env`、不打印/复制/提交密钥；测试全程离线；不提交、不推送（除非用户明确要求）。

---

## 8. 交付与审计

- 实施：A–E workstream，按 `docs/reviews/2026-10-29-docx-gap-review.md` 的契约开工。
- 审计：`code-reviewer` + `security-reviewer` 只审不改，写 `docs/reviews/2026-10-29-docx-audit.md`。
- 验证：`e2e-runner` + leader 全量测试，写 `docs/verification/2026-10-29-docx-phase1.md`。
- 长期文档：`docs/docx-support.md`（格式契约、限制、示例）。

---

## 9. 复核补充（2026-10-29 Plan agent 二次审查）

以下是在 A/B 已开始实现后，独立规划 Agent 对真实调用链复核出的补充项。已通过消息分派到对应 workstream，纳入 T1/T2/T3/T5 的验收。

### 9.1 新增 P0

| # | 问题 | 修正 | Owner |
|---|------|------|-------|
| 26 | `AnswerBundle.to_dict()` 的 citations 缺少 `locator_kind/section/para_no/para_end`，前端 locator 分支拿不到字段 | 补齐并加序列化断言 | A |
| 27 | `InjectionHit` 只有 `page_no`，DOCX 命中显示 `page_no: 0` | 加 `locator`/`para_no`，`scan_text` 支持，`describe()/to_dict()` 分支；PDF 默认不变 | A |
| 28 | `parse_citations` 的裸条号 token 用“任意数字串”，`[§表 1 ¶4]` 会误判成 `§1` | 锚定条号位置（`第N条/款/章/节`、整串或空白边界），加回归测试 | A |
| 29 | `security.py` 先解析 `[Content_Types].xml` 再扫 DTD，DTD/ENTITY 可能先被解析 | 先扫所有 XML part，再做 content-type/external rels；加 DTD 在 `[Content_Types].xml` 的测试 | B |
| 30 | UTF-16 BOM 的 XML 绕过 UTF-8 字节扫描 | 拒绝 UTF-16 BOM XML 或按 BOM 解码后扫；捕获 `NotImplementedError` | B |
| 31 | 文本丢失不变量没有可独立复算的产物 | 新增 `docx_blocks.jsonl`（`block_id/body_index/para_no/kind/label/title/text`）；`chunks.py` 构建后断言每个非空 block 被覆盖，否则 `InputError` | B（产出）、C（落盘）、E（评估） |
| 32 | 长章节切成多片后，后几片没有 `3.6`，答案写“见 3.6 条”会触发 `numeric_grounding` 假失败 | 每片 `Chunk.text` 前缀紧凑节头（`label title\n`） | B |
| 33 | `eval/runner.py` 的 `sorted({c.page_no})` 在混合 PDF+DOCX 时遇 `None` 会 `TypeError`；report 会渲染 `p.None` | E 过滤 `None`，新增 `citation_locators`；报告用 locator 而非页码 | E |
| 34 | DOCX 的 OCR 指标/解析报告仍按 PDF 渲染 | E 分支：`ocr_metrics` N/A（不是 0）、“DOCX 解析结果”、引用出处；`EvalReport.source_type` | E |

### 9.2 新增 P1

- 纯标题引用（`[§不存在的标题 ¶4]`）不会被 `citation_validity` 抓到：加 ADVISORY 的 `locator_validity`，不做 gate；文档写明限制。
- 自动编号的展示串可能是“第3章”，但 `clause_label` 必须是裸 `3`；B 明确 bare/number 字段。
- 无 Heading 样式但正文写 `3.6 斜度公差` 的中文 Word 文档：标题判定加 `DomainProfile.clause_patterns` 对短段落的兜底；不要把每个数字开头的段落都当标题。
- `section` 含 `]`/换行会破坏引用语法：展示时清洗，原始标题保留在 `clause_path`。
- `build_evidence_block` 的 DOCX 头用 `chunk.section or chunk.clause_label`。
- 混合语料 `/api/status` 报第一份文档：C 加 `source_type="mixed"`、`unit_count=sum`、只有全 PDF 才报 `page_count`。
- `_discover()` 读 `source.source_type/unit_count` 与 `docx.unsupported`；DOCX 的 `pages` 不展示。
- CLI `doctor`/`missing_ocr_message` 明确“DOCX 不需要 OCR”；`detect`/`render` 对 DOCX 早退并给可操作提示。
- `eval` 对 DOCX 用 `engine="docx"`；`--transcription/--noise-sweep` 明确 PDF/OCR-only。
- `UploadResult` 加 `source_type/unit_count`；前端 copy 收尾。
- `tests/test_locator_contract.py` 补 `AnswerBundle.to_dict` 与 `[§表 1 ¶4]` 用例。

### 9.3 范围决策

- **采纳**：`docx_blocks.jsonl`、切分片节头前缀、`citation_locators`、DOCX 报告 N/A 分支、`InjectionHit` locator。
- **保留**：三视口 E2E。现有 `web/node_modules` 已有 `playwright-core` + Chrome，不是新依赖；T8 仍跑 1440/768/390 作为验收，不因“可省”而省。
- **不做**：Phase 1 不实现 `gridSpan/vMerge` 网格、单元格级引用、脚注/文本框/公式结构、Word 页码模拟、`.doc/.docm/.dotx/加密 DOCX`。
- **不提交**：`web/` 源码目前未跟踪；是否纳入提交由用户决定，leader 不在未授权时 `git add/commit`。
