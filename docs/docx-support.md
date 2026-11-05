# DOCX 原生支持（Phase 1）

- 状态：**Phase 1 已实现** —— 原生文本 DOCX 的解析、切分、检索、问答与评估。
- 契约来源：[`docs/reviews/2026-10-29-docx-gap-review.md`](reviews/2026-10-29-docx-gap-review.md)（已冻结）。
- 定位设计：[`docs/design.md`](design.md) 第七节（为什么 DOCX 没有"页"）。

Phase 1 的目标不是"把 DOCX 当 PDF 处理"，而是：

> `docqa ingest x.docx` → 标题与段落切分 → 检索 → 生成带 `[§3.6 ¶4]` 引用的答案
> → 同一套自检、拒答与 Web 工作台。

DOCX 是原生文本，**不需要类型判定、页图渲染与 OCR**。因此它也不该被要求配置
视觉模型：一台没有 OCR 端点的机器上，`docqa ingest x.docx` 必须能跑通。

---

## 1. 支持范围

| 能做 | 说明 |
|---|---|
| 正文段落 | 按 `w:body` 顺序抽取，含超链接文字、`w:tab`/`w:br`、`w:sdt`（内容控件）正文 |
| 标题 | `w:outlineLvl` → 样式链（`basedOn`）→ 无 Heading 样式时用 `DomainProfile.clause_patterns` 兜底**短段落** |
| 条款号 | 文本里的字面条号（`3.6`、`第三章`、`（二）`、`一、`）+ 有界解析 `numbering.xml` 的自动编号 |
| 表格 | **线性化**：每行按 `格 | 格 | 格` 拼成文本，保留数值与阅读顺序 |
| 修订 | 保留 `w:ins`（插入），排除 `w:del`/`w:moveFrom`/`w:delText`/`w:instrText` |
| 隐藏文字 | run 直接 `w:vanish`、`w:rStyle` 字符样式链、`w:pStyle` 段落样式链（均沿 `basedOn`）任一判为隐藏，即**整棵子树跳过并计数**：不进入正文、证据与注入扫描 |
| 引用 | `[§3.6 ¶4]` / `[¶4]` / `[§3.6]`，解析回同一个片段 |
| 注入扫描 | 逐正文块调用 `scan_text()`，manifest 记 `injection`（`locator="paragraph"`） |
| Web | 上传 `.docx`、跨文档检索、证据与自检展示（不新增端点，只扩展现有字段） |

---

## 2. 定位：`¶n` 与引用格式

### 2.1 `¶n` 的定义

> `¶n` = 文档顺序第 **n** 个**非空正文块**（段落或线性化表格；标题计入；空段落不计）。

- 它**不是** Word 的"段落数"：Word 的段落计数包含空段落，两者对不上是预期行为。
- 每个正文块同时记 `body_index`（它在 `w:body` 子元素里的原始下标），
  排查时可以回到原始位置。
- 每个片段带 `[para_no, para_end]` 区间；引用里的 `¶4` 落在区间内即映射到该片段。
- 每个片段还带 `block_ids`（它覆盖的块 ID 列表）。覆盖率的**成员依据**就是它：
  一个块的 `block_id` 不在任何片段的 `block_ids` 里，就算文本恰好在别处出现，
  也判为未覆盖。
- 产物里的块 ID 是 `b{para_no}`（如 `b4`），与 `docx_blocks.jsonl` 的 `block_id` 一致。
- **别把块 ID 与片段 ID 混了**：`chunk_id` 形如 `<doc_id>:b7`，其中的 `7` 是
  **片段序号**（ordinal），不是 `¶n`；块 ID 的 `b{para_no}` 才是 `¶n`。
  两者数值不同、用途不同（前者用于检索与引用回链，后者用于正文覆盖核对）。

### 2.2 引用格式与证据头

| 来源 | locator | 模型应输出 | 证据头 |
|---|---|---|---|
| PDF 有条号 | `page` | `[p.3 §3.6]` | `[doc:p3:c2] （第 3 页 §3.6）` |
| PDF 无条号 | `page` | `[p.3 ¶12]` | `[doc:p3:c2] （第 3 页 ¶12）` |
| DOCX 有条号/标题 | `paragraph` | `[§3.6 ¶4]` | `[doc:b2] （§3.6 ¶4）` |
| DOCX 无条号 | `paragraph` | `[¶4]` | `[doc:b2] （¶4）` |

DOCX **永远不出现页码**。`Citation.render()` 在 `page_no=None` 时降级到段落模板；
连条号与段落号都没有时渲染 `[?]`——绝不产出 `p.None`。一个看起来像引用、
实际不可定位的字符串，比明说"不知道"更糟。

场景配置里的 `docx_citation_template`（自定义模板）**必须生成上述规范形态之一**，
带字面前缀的模板能渲染、但解析不回来（见 [5.5](#55-自定义引用模板的约束可执行限制)）。

### 2.3 解析规则（冻结）

`parse_citations()` 认六种形态（PDF 三种 + DOCX 三种）：`[p.N]`、`[p.N §x]`、
`[p.N ¶n]`、`[§x ¶n]`、`[¶n]`、`[§x]`。
DOCX 引用的映射顺序：

1. 原始 section 等于某片段的 `clause_label` 或 `section` → 命中；
2. section 串**开头**含真实存在的裸条号 token → 命中并记裸条号
   （不使用"任意数字串"：`[§表 1 ¶4]` 不能被误判成 `§1`）；
3. 否则按 `para_no` 回退，并**保留原始串**，让 `citation_validity` 抓编造条号；
4. 纯标题（不含数字 token）→ `clause_label=None`，不参与条号一致性校验。

`sanitize_section()` 会剔除 `]` 与换行，避免破坏 `[...]` 语法；原始标题仍保留在
`clause_path` / block 里。

---

## 3. 命令

```powershell
uv run docqa ingest data/raw/x.docx --domain standard   # 产物目录会打印在面板里
uv run docqa ask "3.6 的斜度公差是多少？" --doc out/<doc_id>
uv run docqa eval-docx data/raw/x.docx                  # 现场 ingest 到临时目录再评估
uv run docqa eval-docx data/raw/x.docx --json           # 结构化输出
uv run docqa eval-docx data/raw/x.docx --out out/docx_eval.md
uv run docqa serve                                      # Web 工作台，可直接上传 .docx
```

- `--engine` / `--force` 对 DOCX **不适用**（会提示并忽略）；`--fixtures-dir` 也不会被使用。
- `docqa eval-docx` 的退出码：门禁不通过时 `6`（与 `docqa eval` 的回归门同一套语义，
  可直接挂 CI）。
- 没有 OCR 配置也能跑：DOCX 路径不构造 OCR 引擎，`--engine` 只做提示。

---

## 4. 产物

### 4.1 目录结构

```text
<out_dir>/                     # 例如 out/<stem>-<sha8>/（CLI ingest）
                               # Web 上传：<stem>-<sha256[:32]><ext>（单段 128-bit 哈希）
  profile.json                 # SourceProfile.to_dict()
                               #   source_type="docx", kind="TEXT",
                               #   page_count=0, unit_count=N, pages=[]
  pages.json                   # []
  ocr.json                     # []
  chunks.jsonl                 # Chunk.to_dict()，locator_kind="paragraph"
  docx_blocks.jsonl            # 每个非空正文块一行（含 block_id）
  manifest.json                # 管线清单（见下）
```

`doc_id` 就是产物目录名：CLI 直接 `ingest` 为 `<stem>-<sha8>`；Web 上传的存储名
已经是 `<stem>-<sha256[:32]><ext>`，**不再追加 8 位哈希**（否则前端 `docTitle()`
会显示重复哈希）。旧产物回读不变——`load_result()` 只读 manifest 里的 `doc_id`，
不按目录名重新推导。

`docx_blocks.jsonl` 的每行：

```json
{"block_id": "b4", "body_index": 3, "para_no": 4, "kind": "paragraph",
 "label": "3.6", "label_text": "3.6", "title": "斜度公差", "text": "……",
 "level": 2, "style_name": "Heading 2", "table_id": null,
 "structure_parsed": false, "hidden_runs": 0, "rows": []}
```

表格块额外带 `table_id`（`t1`、`t2`）与 `rows`（每个表格行的单元格文本；
合并延续格已跳过，见 8.2 的去重规则）。
`structure_parsed` 在 Phase 1 恒为 `false`：表格只线性化，不建矩形网格。

### 4.2 manifest 关键字段

```json
{
  "source": {"path": "…/x.docx", "sha256": "…", "source_type": "docx",
             "page_count": 0, "unit_count": 123},
  "detect": {"kind": "TEXT", "not_applicable": "DOCX 原生文本，无需 PDF 类型判定"},
  "render": {"not_applicable": "DOCX 无页图"},
  "ocr": {"not_applicable": "DOCX 原生文本，未调用 OCR", "engine": null,
          "pages_total": 0, "pages_failed": []},
  "docx": {
    "paragraphs": 123, "headings": 18, "tables": 4, "tables_linearized": 4,
    "numbering": {"literal": 10, "computed": 8, "missing": 0, "unsupported_formats": []},
    "unsupported": {"footnotes": 2, "text_boxes": 0, "equations": 0, "…": 0},
    "security": {"entries": 12, "compressed_bytes": 4567, "uncompressed_bytes": 23456,
                  "document_xml_bytes": 12345, "external_relationships": 0,
                  "content_type": "…"},
    "unit_count": 123
  },
  "chunk": {"chunks": 42, "structured": true, "used_sentence_fallback": false,
             "tables": 4, "citation_mode": "clause"},
  "injection": {"scanned": true, "locator": "paragraph", "pages_scanned": 0,
                 "units_scanned": 123, "hits": []},
  "usage_total": {"tokens_in": 0, "tokens_out": 0, "est_cost_usd": 0.0,
                   "latency_ms": 0, "pages": 0}
}
```

`unsupported` 里**每个键都必须出现（值为 0 也要有）**：否则"没计数"与"计数为 0"
在产物里长得一样，退化就不可见了。

### 4.3 正文条号提升（`chunks.py::_clause_start_display`）

普通段落自带的条号（来自文本或自动编号）在"看起来像条号"时**提升为独立的
clause chunk**，引用才能写成 `[§3.6.1 ¶7]` 而不是退化成 `[¶7]`：

- 显示条号（`label_text`）含 `.`（`3.6`、`3.6.1`）；或
- 以「第」开头、或以「条 / 章 / 节 / 篇 / 部分」结尾（`第一条`、`第三章`）；或
- 形如「（一）」/「(一)」。

扁平列表号 `1`、`一` **不提升**——否则每个列表项都会变成伪造的条款引用。
`DocxBlock.to_dict()` 同时给出 `label`（裸条号，用于一致性比对）与 `label_text`
（展示条号，提升与阅读用）。

---

## 5. 评估：`docqa eval-docx`

### 5.1 三条硬门禁

| 指标 | 定义 | 门禁 |
|---|---|---|
| `paragraph_coverage` | 每个非空正文块的 `block_id`（`b{para_no}`）必须出现在至少一个片段的 `block_ids` 中，且内容由**这些片段**核对 | **必须 1.0** |
| `citation_roundtrip` | 每条片段按**场景配置的 DOCX 引用模板**（`docx_template`，缺省为 `DOCX_CITATION_TEMPLATE`；必须是规范形态，见 5.5）渲染后，`parse_citations()` 结果的 `evidence_chunk_ids`（为空时退回 `chunk_id`）必须包含该 `chunk_id` | **必须 1.0** |
| `unit_count` 对账 | 产物自称的 `unit_count`（profile → `manifest.source` → `manifest.docx`）必须等于 `docx_blocks.jsonl` 的非空块行数 | **必须相等** |

覆盖口径（写死在报告里，读者不必猜）：

- **成员依据**：`block_id` 必须出现在某片段的 `block_ids` 里。只靠"任意片段文本包含"
  会掩盖丢块——两个内容相同的段落，删掉覆盖第二段的片段后，文本判据仍会通过。
- **文本依据**：由**声明覆盖该块的片段**核对，不是任意片段。
  - 非表格块：先判整块归一化文本；长块被切成多片（片间有重叠）时**逐句核对**，
    每句都必须出现（切分器按同一套句子边界切，所以每句必然完整落在某一片里）。
  - 表格块：整表不在时，逐个判断非空单元格，**全部出现**才算覆盖。只要求
    "部分出现"会把整列丢失判成覆盖——而丢一列数值，问答照样会自信地给出半张表。

`unit_count` 对账防的是另一种丢失：parser/写盘阶段丢掉一个块、且块文件里也没有
它的记录。只核对"块文件里的块有没有被覆盖"是**产物自洽**，不是内容完整。
产物没有声明 `unit_count` 时同样判失败——无法核对不等于没问题。

不足 1.0 / 不相等时报告列出 `offenders`：正文块给 `block_id`（如 `b5`）、`para_no`、
前 40 字预览与原因（"没有片段声明覆盖"或"片段声明覆盖但文本不在其中"）；
片段给 `chunk_id` 与它渲染出的引用串。

评估**只读产物、不重新 ingest**：它量的是"产物本身自洽不自洽"。CLI 负责把
ingest 的输出（通常是临时目录）传进来。

### 5.2 其它指标（从 manifest 读，不设门禁）

- `structure`：`paragraphs` / `headings` / `tables` / `tables_linearized`
- `tables`：`total` / `linearized` / `structure_parsed`（Phase 1 恒为 `false`）
- `numbering`：`literal` / `computed` / `missing` / `unsupported_formats`；
  `missing > 0` 给**警告**，不设门禁（编号解析是有界实现，失败不等于内容错）
- `unsupported`：未抽取构造的计数（脚注、文本框、公式、批注、页眉页脚、图片……）
- `chunks`：片段数

### 5.3 N/A 指标口径

| 指标 | DOCX 上的值 |
|---|---|
| 字符准确率 / CER / 表格单元格准确率 / 数值字段召回 | **不适用** |
| 页数 / 页图 / 识别失败页 | **不适用** |
| `citation_page_ok`（金标按页判引用） | `None`（N/A），不是 `False` |

规则只有一条：**"不适用"绝不写成 0**。`0%` 会被读成"识别全错"，而 DOCX 根本没有
识别环节。`eval-docx` 输出：

```json
"ocr": {"not_applicable": true, "reason": "DOCX 原生文本，无 OCR 层"}
```

HTML 报告里对应写「不适用：DOCX 原生文本，无 OCR 层」，不会出现
「未提供人工转写基准」这种 PDF 口径——那会把读者引向一个错误的动作。

### 5.4 报告

`docqa eval-docx` 默认打印 Markdown（`--out` 可落盘，`--json` 输出原始 dict）。
Markdown 含：文档 / 来源类型 / 非空正文块（实际行数）与声明的 `unit_count` /
片段数 / **实际使用的引用模板** / 结论、三条门禁的值与口径、结构与解析统计、
未覆盖块与无法回环片段、失败项、警告、未支持构造计数。

`docqa eval <产物目录>` 的 HTML 报告按 `source_type` 分支：DOCX 渲染「DOCX 解析结果」
（正文块 / 标题 / 表格 / 线性化 / 未支持构造），**不渲染页图、页数、识别失败页**；
逐题卡片的「引用页码」改为「引用出处」，显示渲染后的 `[§3.6 ¶4]`。旧 PDF 报告输出
保持原样。

### 5.5 自定义引用模板的约束（可执行限制）

`DomainProfile.docx_citation_template` **不是自由文本**：它必须生成下列**规范形态**
之一（占位符顺序与间距可调，**字面前缀不行**）：

| 形态 | 模板示例 | 渲染结果 |
|---|---|---|
| 条号 + 段落号（默认） | `[§{section} ¶{para}]` | `[§3.6 ¶4]` |
| 仅段落号 | `[¶{para}]` | `[¶4]` |
| 仅条号 | `[§{section}]` | `[§3.6]` |
| PDF 页码系列（DOCX 不应使用） | `[p.{page} §{clause}]` 等 | `[p.3 §3.6]` |

原因：`parse_citations()` 与引用有效性校验只认这些形态（PDF 三种 + DOCX 三种，
见 [2.3](#23-解析规则冻结)）。模板带字面前缀时**能渲染，但解析不回来**：

- **正例**：`docx_citation_template: "[¶{para}]"` → 每条片段的引用都能解析回自身，
  `citation_roundtrip = 1.0`（合成件实测 12/12）。
- **反例**：`docx_citation_template: "[DOCX §{section} ¶{para}]"` → 渲染为
  `[DOCX §3.6 ¶4]`，`parse_citations()` 认不出；`citation_roundtrip < 1.0`、
  `gates_passed=false`、`docqa eval-docx` 退出码 **6**（合成件实测 0/12）。

这是**有意为之**的可执行限制，不是 bug：一个能渲染却无法回环的模板，等于让每条
引用都变成不可核验的装饰。评估按"与真实问答同一个模板"渲染（CLI 传入
`profile.docx_citation_template`），所以模板不合法会在 `eval-docx` 当场暴露，
而不是让线上引用悄悄失去可解析性。若产品确实需要字面前缀，正确做法是扩展
`parse_citations()`（解析层）并补测试，而不是让评估静默按默认模板放行。

---

## 6. 明确不支持（Phase 1）

| 项 | 处理方式 |
|---|---|
| 脚注 / 尾注 | 不抽取，`unsupported.footnotes/endnotes` 计数 |
| 文本框 / 形状 | 不抽取，`unsupported.text_boxes` 计数 |
| 公式（OMML） | 尽力抽 `m:t` 文本，**不保证结构**；`unsupported.equations` 计数 |
| 批注 | 不抽取，`unsupported.comments` 计数 |
| 页眉 / 页脚 | 不抽取，`unsupported.headers/footers` 计数 |
| 隐藏文字 | 按样式链判定后整棵 run 子树跳过（不进入正文/证据/注入扫描）：run 直接 `w:vanish` **优先**，其次 `w:rStyle` 指向的字符样式、最后 `w:pStyle` 指向的段落样式（都沿 `basedOn` 派生链）；block 级 `hidden_runs` 与 `stats.unsupported.hidden_runs` 都保留计数 |
| `w:altChunk`（外部嵌入内容） | 只计数（`unsupported.alt_chunks`），不抽取 |
| `mc:AlternateContent` | 只走**一个**分支（`mc:Choice` 优先，否则 `mc:Fallback`）抽取，`unsupported.alternate_content` 计数——两个分支都走会把同一段文字算两遍 |
| 修订删除 | 排除，`unsupported.deleted_runs` 计数；插入（`w:ins`）保留 |
| 嵌入对象 | 不抽取，`unsupported.embedded_objects` 计数 |
| 内嵌图片 / 扫描页 | **不做 OCR**，`unsupported.images` 计数（Phase 3） |
| 表格合并单元格网格 | 不建矩形网格（`structure_parsed=false`），只线性化（Phase 2 的 `gridSpan`/`vMerge`） |
| 单元格级引用 | 不支持 `[表 1 行 2 列 3]`（Phase 2） |
| `.doc` | OLE 复合文档，拒绝并提示「另存为 .docx」 |
| `.docm` / `.dotm` / `.dotx` | 拒绝（宏 / 模板），提示另存 |
| 加密 DOCX | OLE 包头被识别并拒绝，提示确认文件未加密 |
| Word 渲染页码 | **不模拟**；引用一律 `[§… ¶n]` |

**隐藏文字的判定是样式链的，不只看 run 上的直接标记。** 识别范围：

1. run 自身的 `w:rPr/w:vanish`（优先级最高）；
2. `w:rPr/w:rStyle` 指向的字符样式，及其 `basedOn` 派生链；
3. `w:pStyle` 指向的段落样式，及其 `basedOn` 派生链。

优先级为 **直接设置 > 字符样式 > 段落样式**。`w:vanish` 的 `val` 三态：
缺省 / `1` / `true` / `on` = 隐藏；`0` / `false` / `off` = 可见。
命中后整棵 run 子树被跳过（文字不进入正文、证据与注入扫描），
计数同时保留在 block 级 `hidden_runs` 与 `stats.unsupported.hidden_runs` 里。

**绝不静默丢弃**：所有不支持的构造都进入 `unsupported` 计数，`0` 也会出现。

---

## 7. 安全限制

DOCX 是不可信输入（用户上传的 ZIP 包）。校验在**解析之前**完成，
校验阶段**不落盘解压**（上传层把原始字节落盘是另一回事，见 7.2）。

### 7.1 包校验（`docx/security.py`）

- **容器层**：文件头必须是 ZIP；OLE 包头（旧 `.doc` / 加密包）与空 ZIP 拒绝；
  **多卷 ZIP** 拒绝（EOCD 的 disk 字段非 0，Zip64 的 0xFFFF 占位除外）；
  条目数 ≤ 5,000；**重复条目名**拒绝（zip confusion）；**加密位**
  （`flag_bits & 0x1`）拒绝；**符号链接 / 设备 / FIFO / socket 条目**按
  `external_attr` 的 mode 位拒绝。
- **路径**：条目名同时检查 `ZipInfo.filename` 与 **`orig_filename`**：`zipfile` 会在
  NUL 处截断名字、在 Windows 上把反斜杠归一成 `/`，只看归一后的名字会漏掉原始名里的
  攻击特征。含 **NUL / 控制字符**、反斜杠、绝对路径（`/` 开头或盘符）或 `..` 一律拒绝
  （`evil\x00.xml`、`..\evil.xml` 端到端都是 `InputError`）。NUL 尤其重要：不同 ZIP
  实现会在 NUL 处截断文件名，同一份包可能在校验器与解压器眼里是两个不同的文件。
- **大小与压缩比**：单条目声明解压 ≤ 100 MB；总声明解压 ≤ 500 MB；
  压缩比按 **`解压后 ≥ 1 MiB 且 ratio > 200`** 判 zip bomb
  （阈值用 `>=`：把体积压到阈值下方两字节不能绕过）。
- **元数据部件**：`[Content_Types].xml` 与所有 `.rels`（**含孤立 rels**）上限
  **8 MiB**，并且在 `_scan_xml_part` **之前按声明大小预检**——9 MiB 的孤立 rels
  不扫描即拒绝，7 MiB 的合法关系表正常通过；`word/document.xml` 100 MiB；
  其它 XML 100 MiB。
- **主文档身份**：`[Content_Types].xml` 必须把 `/word/document.xml` 声明为
  WordprocessingML 主文档（宏 / 模板类型拒绝）；`_rels/.rels` 的 `officeDocument`
  目标必须**就是** `word/document.xml`——只检查"包里存在该文件"不能保证
  被解析的就是它。声明为 XML 内容（`+xml` / `application/xml` / `text/xml`）的
  部件，扩展名必须是 `.xml` / `.rels`，否则会绕过 DTD 扫描却被解析器打开。
- **XML 编码**：**只接受 UTF-8**（允许 UTF-8 BOM）。UTF-16/32（含无 BOM 的
  自动识别形态）、含 NUL、无法按 UTF-8 解码的 XML 一律拒绝——其它编码下
  字节级 `<!DOCTYPE` 扫描会失效，而解析器仍会照常解析。
- **DTD / ENTITY**：扫描集合是**所有** `.xml` / `.rels`（含没有被任何部件引用的
  孤立 rels），在**剥离 BOM 并解码之后**再扫；命中 `<!DOCTYPE` / `<!ENTITY` 即拒绝。
  顺序是安全属性：**先扫完所有 XML，再解析** `[Content_Types].xml` 与根关系。
- **外部关系**：只**计数**、不请求（`external_relationships` 进 manifest）；
  解析 `.rels` 仅用于数出 `TargetMode="external"` 的条目。
- **规模**：`word/document.xml` 实际解压 ≤ 100 MB；解析后段落 ≤ 100,000、
  正文字符 ≤ 20,000,000。
- **注入**：正文逐块 `scan_text()`（隐藏文字不进入正文，因此也不进入扫描）；
  命中写 manifest `injection`，报告文案用"段"而非"页"。
- **错误统一**：`zipfile` / `zlib` / `struct` / `EOFError` 等异常统一转
  `InputError`（退出码 3），不把 traceback 或异常文本漏给调用方。

### 7.2 上传层（`api/app.py`）

- **大小**：`Content-Length` 声明超限 → 立即 413，**一个字节都不读**；
  无长度或声明撒谎 → 边读边累计，超过上限立即 413（峰值不随声明膨胀）。
  上限 `MAX_UPLOAD_BYTES = 200 MB`。
- **存储名**：净化后的 stem + **内容 sha256 前 32 位（128-bit）** + 扩展名，
  例如 `<stem>-<32 hex>.docx`；它同时就是产物的 `doc_id`（见 4.1）。用户文件名
  不参与路径拼接（同名覆盖、Windows ADS、设备名、超长名都被挡掉），原始文件名
  只进响应的 `original_filename` 与日志。
- **幂等与冲突**：同内容重复上传命中已存在的普通文件时**不重写**（幂等）；目标已存在
  但**完整 sha256** 与本次不一致（包括预置的符号链接）→ **409**，不写穿目标、
  不留残留文件。
- **原子写**：先写同目录随机临时文件，再 `os.replace` 原子替换；写盘失败清理临时文件，
  500 只回通用文案（详情只进服务端日志）。验证失败只删**本次新建**的文件——
  同内容重复上传时，已存在的有效源文件不会被顺手删掉。
- **目录权限**：`data/raw/` 是上传落点，**不得被不可信的本地用户或共享进程可写**。
  预置同名文件/符号链接不会导致写穿（409 拒绝），但仍构成可用性与拒绝服务风险；
  服务应以专用账号运行，并让该目录只对它可写。
- **并发**：上传并发 1（超出 429，防止同时推高内存与磁盘）；问答并发 2
  （`QaAgent` 非线程安全）。
- **错误回显**：500 只回「服务端内部错误，详情见服务端日志」，不回显路径、
  异常类型或内部状态。

### 7.3 部署边界

`docqa serve` 默认只绑 **127.0.0.1**。服务**没有认证机制**：改成 `--host 0.0.0.0`
等于把「上传 → 解析」与生成模型的配额开放给整个网络。容器里必须绑 0.0.0.0 时，
请自行加反向代理认证与限流，详见 [`deployment.md`](deployment.md)。

### 7.4 残余风险（已接受）

- 孤立 `.rels` 仍会被安全解析（用于计数外部关系），受 8 MiB / 500 MiB 上限约束；
- **未做关系图可达性裁剪**：不可达部件不解析，但仍在扫描与计数范围内；
- **多 EOCD 未单独检测**：只按"注释长度与文件末尾吻合"定位最后一个有效 EOCD；
- 上传上限固定 200 MB；`ingest` 在请求内**同步**执行，长任务会占用一个请求线程。
- **SEC-09（未闭环）**：注入扫描的 manifest **只有截断日志**，没有
  `truncated` / `hits_total` 字段。一次命中超过展示上限（当前每页/每块保留前 20 条）时，
  报告只能看到被保留的条目，**无法说明"还有多少条被截断"**——截断事实在日志里，
  不在产物里。补这两个字段需要改 `injection.py` 的序列化契约，尚未做。

校验失败一律是 `InputError`（退出码 3），消息说明原因与可操作的处理方式
（`另存为 .docx` / 确认文件未加密 / 拆分文档）。

---

## 8. 三个实现决策（容易做错的地方）

### 8.1 `bullet` / `none` 不伪造条号

Word 的自动编号不在段落文本里，需要从 `numbering.xml` 算回来。但
`numFmt=bullet`（项目符号）与 `numFmt=none` **不是条号**：把它们当条号，会让每个
列表项都变成伪造的条款引用，比没有标签更糟。所以：

- `bullet` / `none` → 直接返回"无标签"；
- 其它数字格式：支持 decimal / decimalZero / chineseCounting / chineseCountingThousand /
  upperLetter / lowerLetter / upperRoman / lowerRoman 与 `%1..%9` 的 `lvlText`；
  不支持的格式**回退十进制并记录格式名**（进 `numbering.unsupported_formats`）；
- 解析不出来就返回 `None`，**绝不编一个条号**——引用退化为 `[¶n]` 并记警告。

### 8.2 表格结构化去重

线性化时的去重是**结构化**的，不是按值：

- `w:gridSpan`：一个 `w:tc` 本来就只出现一次，**不展开成多列**；
- `w:vMerge` / `w:hMerge` 的 continuation 单元格**直接跳过**（它不是内容，
  是同一个内容的延续）；
- **相邻同值不去重**——一列里两格都写"是"是合法数据，按值去重会静默删掉值，
  正是本项目最不能接受的那类 bug。

整行为空的行不参与拼接。`rows` 是**跳过合并延续格之后**的单元格；
`gridSpan` / `vMerge` 的原始合并信息不在 Phase 1 产物里保留，
Phase 2 重建网格时需要重新读 `document.xml`（这是"只线性化"的直接代价）。

### 8.3 正文段落编号不作标题锚点

带 `numPr` 的普通正文段落（例如列表项"1. 检查外观"）会拿到 `clause_label`，
但**不会进入标题栈**，也不会成为 `clause_path` 的层级锚点：

- 标题判定只看 `w:outlineLvl`、样式链，或"短段落 + `DomainProfile.clause_patterns`"
  这层兜底；
- 否则整篇文档会变成几十个假章节，`clause_path` 与条号一致性全部失真；
- 普通段落的编号只影响它自己所在片段的 `clause_label` / `section` 字段。

---

## 9. 已知限制与后续阶段

- **Phase 2**：`gridSpan`/`vMerge` → 矩形网格；单元格级引用 `[表 1 行 2 列 3]`。
- **Phase 3**：内嵌图片 / 扫描页走 VLM OCR；装饰图跳过；`needs_review` 标记。
- 纯标题引用（`[§不存在的标题 ¶4]`）不会被 `citation_validity` 抓住：后续计划加
  ADVISORY 的 `locator_validity`，不做 gate。
- 自动编号的展示串可能是「第3章」，但 `clause_label` 始终是裸 `3`：
  一致性比对用裸条号，阅读用展示串。
- `section` 含 `]` / 换行会破坏引用语法：展示时清洗，原始标题保留在 `clause_path`。

---

## 10. 测试与复现

```powershell
uv run pytest tests/docx -q                          # 解析 / 编号 / 切分 / 管线 / 评估
uv run pytest tests/docx/test_eval.py -q             # 本文件的指标
uv run docqa eval-docx artifacts/docx/demo.docx      # 合成件上的完整评估
```

`tests/docx/test_eval.py` 覆盖：覆盖率 100% / 回环 100%、删掉一个片段后门禁失败并列出
`block_id`、**重复文本不能掩盖丢块**、**块文本出现在非覆盖片段里不算覆盖**、
长块跨片按句核对、`unit_count` 与块行数不一致/缺失时失败、**自定义 DOCX 模板**
参与回环、表格按单元格判定、OCR 指标 N/A、`render_markdown` 关键字段、
`IngestResult` 直传，以及一次真实的 `pipeline.ingest()` 端到端
（含 `evidence_chunk_ids` 的跨片回环）。
