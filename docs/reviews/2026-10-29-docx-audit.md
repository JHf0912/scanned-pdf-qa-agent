# DOCX 原生支持 Phase 1 —— 独立代码审计（只读）

- 日期：2026-10-30（任务标注 2026-10-29）
- 仓库：`D:\github\scanned-pdf-qa-agent`，分支 `feat/web-workbench`
- 审计范围：`src/docqa/docx/*`、模型/QA/管线/CLI/API/评估/报告、`web/src/*` 中与 DOCX 相关的契约
- 审计方式：`git status` / `git diff` / 全量读代码 + 在系统临时目录构造 DOCX 载荷实测；**未修改任何仓库代码、测试、配置或数据**；未读 `.env`、未调用真实模型端点
- 关联文档：`docs/reviews/2026-10-29-docx-gap-review.md`（冻结契约）、`docs/docx-support.md`、并行安全审计 `docs/reviews/2026-10-29-docx-security-audit.md`
- 基线验证：`uv run --no-sync pytest tests -q`（734 collected，全绿，4 skipped）；`uv run --no-sync pytest tests/docx -q --cov=docqa.docx`（92.40%，门槛 84%）；`uv run --no-sync ruff check src tests tools`（通过）；`web` 下 `npm run typecheck`（通过）。

---

## 1. 结论摘要

**结论：不可放行。**

| 严重度 | 数量 | 说明 |
|---|---:|---|
| P0 | 3 | 长正文块引用定位歧义导致**正确答案被自检误判为幻觉并拒答**；表格行/单元格被内容控件包裹时**数值静默丢失**；XML 扫描按扩展名/BOM 判断存在已实测绕过（与并行安全审计 SEC-DOCX-01/02 重合）。 |
| P1 | 5 | `paragraph_coverage` 用文本子串而非 `block_ids`，重复文本可掩盖丢块；PDF 评测零引用答案的页码判定从 False 变成 N/A（PDF 行为回归）；自动编号的**普通段落**条号在切分阶段被丢弃；无样式兜底把“42 MPa 是最小值”这类短正文判成一级标题并重置章节栈；`zlib.error` 逃逸出 `validate_docx`（API 500 / CLI 打印堆栈）。 |
| P2 | 9 | 样式级 `numPr` 静默失效；`eval-docx` 无法发现“块文件本身丢了一行”；非 XML 部件声明大小可伪造；对抗变异对 DOCX 引用形态失明；前端把 `mixed` 误判为 DOCX 且 unsupported 标签键不匹配；PDF 证据头缺 `page_no=None` 兜底；中文数字条号与附录/无空格条号提取缺口；`eval-docx` 忽略场景自定义 DOCX 模板；DOCX 注入命中仍序列化 `page_no: 0`。 |

已确认正常/无回归的部分：

- DOCX 正文顺序、超链接、`w:ins`、`w:del`、`w:instrText`、正文级 `w:sdt`、tab/br、隐藏文字保留并计数等行为与文档一致，且有测试覆盖。
- DOCX 产物（`profile.json` / `ocr.json=[]` / `pages.json=[]` / `chunks.jsonl` / `docx_blocks.jsonl` / `manifest.json`）完整；旧 PDF 产物回读零改动可读（有专门测试）。
- `source_type_of` 的扩展名 + magic 分派、`_ingest_docx` 不构造 OCR 引擎、CLI/API 的 DOCX 分支、`eval-docx` 的 N/A 口径基本正确。
- `Citation.render()` 自身不会产出 `p.None`；所有 `sorted({c.page_no})`、失败页集合、报告分页渲染均过滤了 `None`。唯一缺口在 PDF 证据头（P2-06）。
- 未发现硬编码密钥、SQL/命令注入、路径穿越（上传文件名已 `Path.name` 归一，包内条目路径有校验）。

---

## 2. Findings 表（按严重度排序）

> `file:line` 为仓库内当前工作区快照。P0 的完整复现见第 3 节。表格内用 `｜` 代替竖线以免破坏 Markdown。

| ID | 严重度 | 文件:行 | 问题 | 失败场景 | 建议 |
|---|---|---|---|---|---|
| **P0-01** | P0 | `src/docqa/docx/chunks.py:175`、`src/docqa/qa/answer.py:264,284-286`、`src/docqa/eval/docx_eval.py:302-320` | 一个超长正文块被切成多片后，所有片共享同一个 `para_no/para_end` 与同一个渲染串 `[§1 ¶2]`；`_resolve_docx_target` 只取 `in_range` 中**召回顺序第一个** | 生成式答案的事实来自第 3 片（如 `42 MPa`），但解析把 `[§1 ¶2]` 落到第 1 片；`numeric_grounding` 在已引证据里找不到 42 → 修复两轮后 `verification_failed` 拒答。`eval-docx` 对同一文档同时报 `paragraph_coverage=0.5`、`citation_roundtrip=2/4`，退出码 6。 | 让每个切分片有可区分的定位（扩展引用语法带片号），或对同一段落号解析到**全部** `in_range` chunk 并合并证据；`eval-docx` 的 roundtrip 按同一规则判定。补“>max_chars 单段 + 脚本化 provider”的端到端测试。 |
| **P0-02** | P0 | `src/docqa/docx/parser.py:696-718`（`_linearize_table` 只 `findall(w:tr)` / `findall(w:tc)`） | 表格行或单元格被 `w:sdt`（内容控件）包裹时不被遍历；文档却宣称支持 `w:sdt` 正文 | 2×2 表第二行包进 `w:sdt`：线性化只剩 `项目 ｜ 指标`，`42 MPa` 整行丢失；单元格包 `w:sdt` 时该格丢失。`content_controls=1` 只说明存在控件，没有任何“行/格内容丢失”信号。 | 用与 `_iter_body_containers` 同样的容器下钻遍历 `w:tr`/`w:tc`（`w:sdt/w:sdtContent`、`w:customXml`），并补行/单元格级 `w:sdt` 回归测试。 |
| **P0-03** | P0 | `src/docqa/docx/security.py:308-344`、`347-377`、`408-424`；`src/docqa/docx/parser.py:250-258` | XML 扫描按**扩展名 + BOM** 判断，而实际解析目标由 root rels / content-type 决定；`.rels` 又用 stdlib ET 解析 | (a) decoy `word/document.xml` + root rels 指向 `word/document.bin`：`validate_docx` 通过，`parse_docx` 只返回 `前`，实体后的 `隐藏内容/后` 静默消失（扫描从未看到 `.bin`）。(b) 无 BOM UTF-16LE 的孤立 `.rels` 含 DTD/ENTITY：扩展名扫描漏掉；`_count_external_relationships` 解析并展开实体（实测 5 级 validate 通过、峰值 5.2MB；6 级在 51MB 后报 InputError；并行安全审计测得更大载荷约 560MB 且 ingest 成功）。 | 解析 root rels 的 officeDocument target 并要求它等于 `word/document.xml`；按 content-type 找 XML 部件（或拒绝 XML content-type 却非 `.xml/.rels` 的部件）；UTF-16/非 UTF-8 一律拒绝或解码后扫描；用安全解析器（defusedxml/lxml 安全配置）解析 rels，并限制聚合大小。 |
| **P1-01** | P1 | `src/docqa/eval/docx_eval.py:230-271`、`454-463` | `paragraph_coverage` 用“块文本是否为某片段文本的子串”判定，完全不看 `chunk.block_ids`；`unit_count` 与块文件行数不做一致性门禁 | 两个内容相同的段落，删掉覆盖第二段的整个 chunk：`paragraph_coverage` 仍 4/4、`gates_passed=true`（第一段文本顶替匹配）。反向：长块被切分时全文字串不可能出现在单片，覆盖率必然 <1（见 P0-01）。另外 `unit_count=3` 而 `docx_blocks.jsonl` 只有 2 行时门禁仍通过（P2-02）。 | 以 `block_ids` 为成员依据、以文本为内容校验；对切分块按块拼接后校验；加 `unit_count == 非空块数` 门禁；报告同时给出两个数。 |
| **P1-02** | P1 | `src/docqa/eval/runner.py:196-199` | PDF 金标题带 `citation_pages`、答案零引用时，`page_ok` 由旧实现的 `False` 变成 `None`，`passed` 因此为真 | `score_item(GoldenItem(citation_pages=[1], must_contain=["590"]), AnswerBundle(answer="590 MPa", citations=[]))` 现在返回 `citation_page_ok=None, passed=True`；旧代码 `page_ok = bool(cited & set(...))` 返回 False。这会把“模型没给任何引用”的 PDF 题算成答对，抬高 accuracy，破坏“PDF 行为不变”。 | 区分“完全没有引用”（应 False）与“有引用但都不是页定位”（可 None，用于 DOCX/混合）；补 PDF 零引用 + `citation_pages` 的回归测试。 |
| **P1-03** | P1 | `src/docqa/docx/chunks.py:155-163`、`src/docqa/docx/parser.py:349-363`；`docs/docx-support.md:302` | 带 `w:numPr` 的**普通正文段落**在 parser 里算出了 `clause_label/label_text`，但 `emit_group` 只从标题栈取 `section/clause_label`，不读块自身的编号，也不把 `label_text` 写进 chunk 文本 | 兼容场景：`numFmt=chineseCounting`、`lvlText=第%1条` 的普通段落，块标签为 `一/二`、`numbering.computed=2`，但 chunk 的 `clause_label/section` 均为 None，文本里没有“第一条”。引用退化为 `[¶n]`；用户问“第一条”时问题词在证据里不存在，覆盖率预判可能直接拒答。文档声称“普通段落的编号只影响它自己所在片段”，实现与此不符。 | 在 `emit_group`/`emit` 中传播块级 `clause_label/section` 并把 `label_text` 加入文本前缀；或明确把这类段落提升为节锚点。补“普通编号段落”的 chunk 级测试。 |
| **P1-04** | P1 | `src/docqa/docx/parser.py:619-648`；`config/domains/standard.yaml:13-14` | 无样式兜底只看“短段落 + 场景正则”，而生产场景正则允许**裸整数**标签；没有单位/数值黑名单，也没有层级合理性判断 | 标准场景下普通段落 `42 MPa 是最小抗压强度。`（≤60 字）被判成一级标题，`label=42`；其后本属 3.6 的段落 chunk 变成 `section=42`、`clause_label=42`，引用与条号校验都指向错误章节。测试里的 `FALLBACK_PROFILE` 要求带小数点（`\d+(?:\.\d+)+`），因此没有暴露生产配置的误判。 | 收紧兜底：拒绝“标签 + 计量单位/纯数值”形态；裸整数仅在上下文可解释为章节时使用；或要求标签带点/“第X章/条”；补生产 YAML 形态的负例测试。 |
| **P1-05** | P1 | `src/docqa/docx/security.py:393-396`、`443-444`；`src/docqa/api/app.py:413-421`；`src/docqa/cli.py:504-506,1361-1367` | `zlib.error` 不在 ZIP 读取异常元组内 | 翻转 `word/document.xml` 的 DEFLATE 字节：`validate_docx` 直接抛 `zlib error Error -3 ...`，不是 `InputError`。API 落到兜底 500 并把异常文本回给客户端；CLI 不在 `_fail` 分支，打印 traceback、退出码 1（契约应为输入错误 3）。 | 在校验入口统一捕获 `zlib.error`、`struct.error`、`EOFError`、`OverflowError`、`ValueError` 等并转 `InputError`；补损坏 deflate/CRC/截断测试。 |
| **P2-01** | P2 | `src/docqa/docx/parser.py:671-685`、`494-512` | `_numbering_props` 只读段落 `w:pPr/w:numPr`；`_style_defs` 不采集样式里的 `numPr` | Word“多级列表链接到标题样式”把 `numPr` 写在 `styles.xml` 的 Heading 样式里：`Heading 1` 段落解析后 `clause_label=None`、`numbering.missing=0`、无警告。自动编号标题整篇退化为 `[¶n]`。 | 沿 `basedOn` 链解析样式级 `numPr`（有界），或在检测到样式含编号却无法解析时计数并警告。 |
| **P2-02** | P2 | `src/docqa/eval/docx_eval.py:96-101`、`381-401`、`454-463` | 评估只校验“块文件里的块是否被片段覆盖”，不校验块文件本身是否完整；`unit_count` 与块行数不做比较 | 模拟 parser/写盘阶段丢掉一个正文块（同时从 `docx_blocks.jsonl` 与覆盖它的 chunk 中删掉）：`unit_count=3`、`paragraph_coverage.total=2`，`gates_passed=true`。所谓“独立复算”仍只到产物自洽，无法发现“块根本没被产出”。 | 增加 `unit_count == 非空块行数` 门禁；如要真正独立，需重新解析 `document.xml` 或用 manifest 的 `docx.paragraphs/tables` 与块数交叉核对，并在不一致时失败。 |
| **P2-03** | P2 | `src/docqa/docx/security.py:269-298`、`347-405` | 条目大小/压缩比/总解压量都信任中央目录的声明值；`_check_xml_parts` 只对 XML/rels 做实际字节读取并注明“声明大小可能说谎” | 把被引用的 `word/media/huge.bin` 声明解压大小改为 1 字节，实际 40MB：`validate_docx` 通过；python-docx 读取该部件并解压后才因 CRC 失败（实测约 0.04s/40MB）。更大条目会在被拒绝前持续分配内存。 | 对所有会被 python-docx 读取的部件做实际字节上限校验（或校验 local header 与中央目录一致），再做压缩比/总量判断。 |
| **P2-04** | P2 | `src/docqa/eval/adversarial.py:69-76`、`102-116` | 变异正则只识别/剔除 `[p....]` 引用，不认识 `[§3.6 ¶4]` 等 DOCX 形态 | DOCX 答案的 `number`/`clause` 变异可能选中引用里的 `3.6`/`4`，改成 `[§4.6 ¶4]` 后 `verify._CITATION_MARK_RE` 又把整个标记剔除，变异被记为“未检出”；`_drop_citations` 只删 `[p....]`，DOCX 上根本不会产出 `citation` 类变异。DOCX 的对抗检出率因此系统性失真。 | 复用/抽出 `verify._CITATION_MARK_RE`（六种形态）做变异前剔除与删引用；补 DOCX 答案的对抗变异测试。 |
| **P2-05** | P2 | `web/src/format.ts:76-86`；`web/src/components/Overview.tsx:8-18,49`；`web/src/types.ts:134-139` | `isDocxSource` 对 `source_type="mixed"` 且 `unit_count>0` 返回 true；`UNSUPPORTED_LABELS` 的键与后端 `UNSUPPORTED_KEYS` 不一致；`UploadResult` 未声明后端已返回的 `source_type/unit_count` | PDF + DOCX 混选时 `/api/status` 返回 `mixed`，Overview 却显示“DOCX / 原生文本 / 未调用 OCR / 不适用”，并隐藏 PDF 页数；`unsupported` 里 `headers/footers/hidden_runs/deleted_runs` 等显示为英文原键（映射表写的是 `headers_footers/hidden_text/tracked_deletions`）。 | 给 `mixed` 单独分支（显示 PDF+DOCX）；按后端键名对齐映射；补 `UploadResult` 字段。 |
| **P2-06** | P2 | `src/docqa/qa/answer.py:116-118` | PDF 证据头直接插值 `chunk.page_no`，没有 `None` 兜底 | `Chunk` 或手改/裁剪产物中出现 `locator_kind="page"` 且 `page_no=null` 时，证据头变成“第 None 页”，模型可能照抄。`Citation.render` 本身有兜底，但证据头这条路径没有。 | `page_no is None` 时退化到 `（¶{ordinal}）` 或标记不可定位；在 `load_result`/构造处校验 `locator_kind="page"` 必须带页码。 |
| **P2-07** | P2 | `src/docqa/qa/answer.py:135-141`；`src/docqa/docx/parser.py:95-100`、`586-602` | 条号提取/回环只覆盖 ASCII 数字与少量分隔符；中文数字条号无法与阿拉伯数字等价；`A.1` 标题样式、`3.6斜度`（无空格）、`3.6：…` 不被拆出标签 | 合规文档 `第一条` 的 chunk 是 `clause_label=一`：模型写 `[§1 ¶1]` 时 `_clause_token` 取到 `1`、`known_labels` 只有 `一` → `citation_validity` 报“引用标注了文档中不存在的条款 1”（实测）；附录 `A.1` 作为 Heading 2 时 `clause_label=None`，无法参与条号校验；无空格标题整段变成 section 标题、`clause_label=None`。 | 条号 token 支持中文数字并用 `cn_to_arabic` 归一后比对；`_LABEL_PATTERNS` 增加 `[A-Z](\.\d+)*`、可接受无空格/冒号分隔；补对应回环测试。 |
| **P2-08** | P2 | `src/docqa/eval/docx_eval.py:323-333`；`src/docqa/config.py:172-175` | `eval-docx` 的 roundtrip 硬编码 `DOCX_CITATION_TEMPLATE`，不使用场景配置的 `docx_citation_template` | 场景配置了自定义 DOCX 模板（例如带 `{doc}` 或额外文本）时，真实问答按自定义模板渲染，`eval-docx` 却按默认模板判定 1.0；若自定义模板不可被 `parse_citations` 解析，门禁在真实链路上失效而评估仍通过。 | `run_docx_eval` 接收 `DomainProfile`（或模板字符串），用与 `Citation.render` 相同的模板做回环；CLI 传入已加载的 profile。 |
| **P2-09** | P2 | `src/docqa/pipeline.py:540-542`；`src/docqa/injection.py:73-101` | DOCX 注入命中仍以 `page_no=0` 序列化，`locator="paragraph"` 只是附加字段 | manifest 的 `injection.hits[].page_no` 对 DOCX 是 0；任何按 `page_no` 读取的消费者会看到“第 0 页”，与“DOCX 没有页”矛盾。 | `page_no` 改为 `int｜None`，paragraph 定位时写 null（或从 `to_dict` 省略）；保留 `para_no/locator` 作为定位。 |

---

## 3. P0 复现步骤

> 以下均只在系统临时目录生成输入，未写入仓库。使用默认 `Config` 或 `standard` 场景（`max_chars=600`）。

### 3.1 P0-01：长正文块被切分后，引用落到错误片段，正确答案被拒答

构造（任选 Python 脚本）：

```python
from docx import Document
d = Document()
d.add_paragraph("1 技术要求", style="Heading 1")
filler = "".join(f"这是第{i}条用于填充长度的说明性句子。" for i in range(1, 61))
d.add_paragraph(filler + "抗压强度应大于等于 42 MPa。")
d.save("longq.docx")
```

步骤：

1. `docqa ingest longq.docx`：得到 4 个片段：`b1` 标题、`b2/b3/b4` 同属 `¶2`（标准场景 `max_chars=600`）。
2. 用一个**脚本化 provider**（非真实端点）返回正确答案 `抗压强度应大于等于 42 MPa [§1 ¶2]。`；问题要能同时召回 `b2/b3/b4`，例如 `第10条 第20条 说明性句子`。
3. `QaAgent.answer()` 观测：`hits=[b2(1.0), b3(0.984), b4(0.968)]`；`parse_citations` 把 `[§1 ¶2]` 落到 `b2`；`numeric_grounding` 在 `b2` 中找不到 `42 MPa` → 两轮修复后 `refused=True, reason=verification_failed`，`citations=[]`。
4. 评测侧无模型即可复现：`docqa eval-docx longq.docx` 对“标题 + 一个 >max_chars 段落”的文档输出 `paragraph_coverage=0.5`、`citation_roundtrip=2/4`，退出码 6（门禁失败）。

根因：`src/docqa/docx/chunks.py:175` 对同一个 `DocxBlock` 的每个 `split_long()` 片都发 `para_no=para_end=block.para_no`；`src/docqa/qa/answer.py:264,284-286` 在多个同定位片段中取召回顺序第一个；`src/docqa/eval/docx_eval.py:307` 又要求解析回**同一个** `chunk_id`。三者对“一个块多个片段”没有共同约定。

### 3.2 P0-02：表格行/单元格包在内容控件中时静默丢数值

```python
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
d = Document()
t = d.add_table(rows=2, cols=2)
t.cell(0,0).text = "项目"; t.cell(0,1).text = "指标"
t.cell(1,0).text = "抗压"; t.cell(1,1).text = "42 MPa"
tbl = t._tbl; row = tbl.findall("{...}tr")[1]   # w:tr
sdt = parse_xml('<w:sdt ...><w:sdtPr/><w:sdtContent></w:sdtContent></w:sdt>')
tbl.replace(row, sdt); sdt.find("{...}sdtContent").append(row)
d.save("sdt_row.docx")
```

`parse_docx("sdt_row.docx")` 实测：

- 正常期望：`项目 ｜ 指标 ｜ 抗压 ｜ 42 MPa`
- 实际：`项目 ｜ 指标`（整行丢失），`rows=(("项目","指标"),)`，`unsupported.content_controls=1`
- 单元格包 `w:sdt` 时：`项目 ｜ 指标 ｜ 抗压`，`42 MPa` 格丢失（`content_controls=1`）。

`src/docqa/docx/parser.py:708,710` 的 `findall(w:tr)` / `findall(w:tc)` 只认直接子元素；`_iter_body_containers` 的容器下钻没有用在表格内部。

### 3.3 P0-03：XML 扫描绕过（`.bin` 主文档；无 BOM UTF-16 孤立 rels）

已在临时目录实测两条：

(a) **`.bin` 主文档 + decoy `word/document.xml`**

1. 取一份合法 DOCX；把真正的 `word/document.xml` 改名为 `word/document.bin`，root `_rels/.rels` 的 officeDocument target 指向 `word/document.bin`；保留原 `word/document.xml` 作为 decoy，并在 `[Content_Types].xml` 给 `.bin` 增加主文档 content-type。
2. 在 `.bin` 的 XML 声明后插入 `<!DOCTYPE ... [<!ENTITY secret "隐藏内容">]>`，把某段正文改成 `前&secret;后`。
3. 实测：`validate_docx(payload)` 返回 `OK`（`content_type=main+xml`）；`parse_docx(payload)` 返回 `blocks=['前']` —— `隐藏内容` 与 `后` 被静默丢弃，扫描从未看到 `.bin`。

(b) **无 BOM UTF-16LE 的孤立 `.rels` + DTD/ENTITY**

1. 在合法包中加入 `word/_rels/orphan.xml.rels`，内容用 UTF-16LE（**无 BOM**）编码，含 `<!DOCTYPE ... [<!ENTITY ...>]>` 与嵌套实体引用。
2. 实测：`levels=5`（37KB 载荷）`validate_docx` 通过，`tracemalloc` 峰值 5.2MB；`levels=6` 峰值 51.1MB 后抛 `InputError`（关系文件损坏）。并行安全审计用更优载荷测得约 560MB 峰值且完整 ingest 成功（`docs/reviews/2026-10-29-docx-security-audit.md` SEC-DOCX-01）。
3. 根因：`security.py:369-377` 只识别 BOM；`security.py:408-424` 用 stdlib ET 解析**所有** `.rels`（包括孤立文件），实体被展开；扫描与解析目标不一致。

修复优先级最高的是：**先确定实际会被 python-docx 解析的部件集合，再对它们按内容类型做 XML 安全扫描**，而不是按文件名扫描。

---

## 4. PDF 回归结论

结论：**PDF 主链路行为基本不变，但评测层有一处不应有的行为回归（P1-02）。**

已核对为不变/等价的部分：

- `Citation.render()`：PDF 有条号 `[p.N §X]`、无条号但有 `para_no` 的 `[p.N ¶n]`、裸页 `[p.N]` 均与旧实现一致；自定义 PDF 模板仍生效。
- `parse_citations()` 的 PDF 分支正则、条号精确/包含匹配、未知页跳过与旧实现一致；去重键改为 `chunk_id ｜ page ｜ normalize(clause)`，对 PDF 语义等价。
- `_CITATION_MARK_RE` 扩展后仍覆盖旧 PDF 三种形态，不会把 PDF 引用数字带入 `numeric_grounding`/`clause_consistency`；旧测试全绿。
- `check_extraction_health`、`policy.decide` 的失败页判定对 PDF 不变；`failed_pages` 在 `load_result`/`load_results`/API/报告处都过滤 `None`。
- 旧 PDF 产物回读：`profile.json` 缺 `source_type/unit_count`、`chunks.jsonl` 缺 locator 字段、`ocr.json` 缺失，均有 `.get()` 默认值与测试覆盖（`tests/docx/test_pipeline.py::TestLoadResult`）。
- 报告：PDF 的“PDF 解析结果”、页数、识别失败页、引用页码均保留；新增的 `source_type/docx` 字段不改变 PDF 渲染。

有意的提示词变化（非回归）：

1. PDF 无条号证据头由 `（第 N 页 §¶n）` 改为 `（第 N 页 ¶n）`（`src/docqa/qa/answer.py:118`）。这是冻结契约要求的修复，避免模型照抄 `§¶n`。
2. `SYSTEM_PROMPT` 第 2 条与修复提示改为“与证据头一致”，同时给出 PDF/DOCX 示例；PDF 示例仍是 `[p.3 §3.6]` / `[p.3 ¶12]`。
3. `Citation`/`Chunk`/`SourceProfile` 新增字段全部带默认值；PDF 构造与序列化向后兼容。

真正的回归：

- `src/docqa/eval/runner.py:196-199` 的零引用 PDF 题从 `page_ok=False` 变为 `None`，使 `passed=True`。复现见 P1-02。该路径没有测试覆盖（现有测试只覆盖“有引用但页码错”）。

---

## 5. 测试缺口

按重要性排序（均已通过只读检查确认未被覆盖）：

1. **切分块与评测/问答的联动**：`tests/docx/test_chunks.py` 只测了切分片本身（`test_long_paragraph_is_split_on_sentences` 等），没有任何“>max_chars 单段 → `eval-docx` 两条门禁 → 生成式问答自检”的测试。现有端到端测试的段落都很短。
2. **表格容器**：`test_parser.py::test_sdt_content_is_descended_into` 只覆盖 body 级 `w:sdt`；没有 `w:sdt` 包裹 `w:tr` / `w:tc`、`w:customXml` 包裹行/格的测试。
3. **无样式兜底 vs 生产配置**：测试用 `FALLBACK_PROFILE = r"^(?P<label>\d+(?:\.\d+)+)\s*(?P<title>.+)$"`（强制小数点），比 `config/domains/standard.yaml` 的裸整数模式窄，因而没有覆盖“42 MPa 是最小值”被误判为标题。
4. **PDF 评测零引用**：没有 `citation_pages` + 空 citations 的 `score_item` 测试；`tests/docx/test_eval.py::test_page_metric_is_na_when_no_citation_carries_a_page` 只覆盖“有 DOCX 引用但无页码”，不能保护 PDF 零引用语义。
5. **号码体系**：自动编号测试只覆盖 Heading 样式；没有“普通编号段落 → chunk label/section”的断言；没有样式级 `numPr`、中文数字条号与阿拉伯数字引用互认的测试。
6. **`eval-docx` 门禁失败路径**：没有 CLI 测试断言 `gates_passed=False` 时退出码为 `EXIT_VERIFICATION`（6）；现有 CLI 测试只跑 demo.docx（通过）。
7. **产物完整性**：没有 `unit_count` 与 `docx_blocks.jsonl` 行数不一致、块文件丢行、chunk `block_ids` 与 `docx_blocks.block_id` 不一致的测试。
8. **对抗变异**：`tests/test_adversarial*` 的样本都是 PDF 答案；DOCX 引用形态没有被变异/检出测试覆盖。
9. **安全回归**：`tests/docx/test_security.py` 覆盖了 BOM、DTD、路径、加密、压缩比、重复条目，但没有覆盖无 BOM UTF-16、`.bin` 主文档、损坏 DEFLATE（`zlib.error`）和 referenced 非 XML 部件的实际大小。
10. **前端混选状态**：`web/src` 只有 typecheck，没有 `mixed` 状态下 Overview/DocRail 的测试；`UNSUPPORTED_LABELS` 与后端键名的契约差异没有被发现。

测试质量整体是好的：没有发现为了通过而放宽断言、跳过或 `xfail` 的改动；`tests/docx` 收集干净；`docx_blocks.jsonl`、旧 PDF 产物回读、引用六形态、注入 locator、N/A 口径都有真实断言。上述缺口主要是“测了组件、没测组合”和“测试配置比生产配置窄”。

---

## 6. 与并行安全审计的关系

仓库同时存在 `docs/reviews/2026-10-29-docx-security-audit.md`（独立安全审计）。本报告与其重合/互相印证的部分：

- 本报告 P0-03 = 该报告 SEC-DOCX-01（无 BOM UTF-16 实体放大）与 SEC-DOCX-02（`.bin` 主文档绕过）；本次独立复现了“validate 通过 / parse 静默丢文本”的核心事实。
- 本报告 P1-05 = 该报告 SEC-DOCX-05（`zlib.error` 逃逸）。
- 该报告另列出的 P1/P2（`await request.body()` 先全量缓冲再检查大小、失败后 raw 文件残留/同名覆盖、Windows 文件名/路径泄露、`altChunk`、注入截断不可审计等）本报告未重复计数；即使不计这些，本报告 3 个 P0 已足以阻断放行。

安全侧做得对的部分：不落盘解压、OLE/空 ZIP/加密位/路径穿越/重复条目/宏与模板变体检查、外部关系只计数不请求、上传文件名 `Path.name` 归一、正文逐块注入扫描。

---

## 7. 声明

**仅审计，未修改代码。** 本次除创建本报告 `docs/reviews/2026-10-29-docx-audit.md` 外，没有编辑任何源码、测试、配置或数据；没有 commit/push；没有读取 `.env` 或密钥；没有调用真实模型端点。所有复现均在系统临时目录完成。

---

## 8. 建议的修复顺序

1. **P0-01**：统一“一个正文块 → 多个片段”的定位语义（解析到全部同定位片段或扩展引用语法），修 `eval-docx` roundtrip；补脚本化 provider 端到端测试。
2. **P0-02**：表格遍历改为容器感知（`w:sdt`/`w:customXml`），补行/单元格测试。
3. **P0-03**：安全扫描改为“按实际解析目标 + content-type”，拒绝非 UTF-8 XML；修 `.bin`/孤立 rels；统一 ZIP 异常为 `InputError`（含 P1-05）。
4. **P1-01/P1-02/P1-03/P1-04**：修覆盖率语义、PDF 零引用、普通编号段落传播、兜底标题误判。
5. 其余 P2 按发布范围处置；P2-05 前端混选问题应在 Web 工作台合并前修复。

---

## 复核（2026-10-30）

> 复核方式：只读代码 + 在系统临时目录重跑原 repro。复核期间 A/B/C/E 仍在并行落地修复（测试数从 798 增至 807），以下结论基于 **2026-10-30 01:50 左右**的当前快照；仅审计，未改代码/测试/配置/数据。

### 复核基线（命令与结果）

- `uv run --no-sync pytest tests -o addopts="" -q` → **807 passed, 4 skipped**（01:47 与 01:48 连跑两遍结果一致；4 个 skip 全是 `tests/test_live_endpoints.py` 的真实端点用例）。
- `uv run --no-sync pytest tests/docx -q --cov=docqa.docx` → **92.48%**（门槛 84%）。
- `uv run --no-sync pytest tests -q --cov`（按 `pyproject.toml` 的 `source=["src/docqa"]`）→ **90.62%**（门槛 84%；用 `--cov=src` 会覆盖 source/omit 配置，得到不同的 87.97%，不作为项目口径）。
- `uv run --no-sync pytest tests -o addopts="" -q --cov --cov-fail-under=0` → 807 passed / 4 skipped。
- `uv run --no-sync ruff check src tests tools` → All checks passed；`web` 下 `npm run typecheck` → 通过。
- PDF/旧产物/混合语料定向回归：`tests/test_verify.py tests/test_policy.py tests/test_report.py tests/test_eval_pipeline.py tests/docx/test_pipeline.py::TestLoadResult tests/docx/test_pipeline.py::TestMixedCorpus` → **120 passed**。
- 未复核项：独立安全审计的完整载荷矩阵（由 security-reviewer 复核）；浏览器 1440/768/390 E2E（由 e2e-runner/T8 复核，coordinator 报 22 项全过）。这些不改变下面的代码层结论。

### P0/P1 状态

| ID | 状态 | 复核证据（代码 + 实跑） |
|---|---|---|
| CODE-01（P0）长段落引用歧义 | **closed** | `Citation.evidence_chunk_ids`（`models.py:463-466`，序列化 `:647`）；`_resolve_docx_targets` 返回全部 in-range chunk（`qa/answer.py:257-326`）；`verify._cited_chunk_ids` 参与 `citation_validity`/`numeric_grounding`/`sentence_support`（`qa/verify.py:219-226,320-324`）；`eval/docx_eval.py:366-399` 按 evidence 集判回环。复跑：`eval-docx` 对“Heading 1 + 60 句 + 末尾 42 MPa”的文档 **exit 0，coverage=1.0，roundtrip=1.0**；脚本化 provider 回答 `42 MPa [§1 ¶2]` 时 **refused=False、attempts=1、citations=1（主锚点 b2，evidence=(b2,b3,b4)）**，无失败门禁。 |
| CODE-02（P0）sdt 表格丢行/格 | **closed** | `_linearize_table` 改用 `_iter_wrapped`（`docx/parser.py:751-792`），穿透 `w:sdt/w:sdtContent`、`w:customXml`、`mc:AlternateContent`。复跑：第二行包 `w:sdt` 与单元格包 `w:sdt` 两种载荷均得到 `项目 ｜ 指标 ｜ 抗压 ｜ 42 MPa`，`rows` 两行完整，`content_controls=1`。 |
| CODE-03 / SEC-DOCX-02（P0/P1）XML 目标/扫描 | **closed（代码/测试；安全独立复核仍在收尾）** | UTF-8-only 流式扫描（`security.py:430-533`）；`_reject_unscanned_xml_parts` 拒绝 XML content-type 但非 `.xml/.rels` 的 part（`:620-650`）；`_require_office_document_target` 要求 root rels 的 officeDocument = `word/document.xml`（`:688-720`）；`_parse_xml` 用 lxml 安全解析器（`:580-600`）；ZIP 异常统一 `_ZIP_ERRORS` → `InputError`。复跑：`.bin` 冒充主文档 → InputError（“声明为 XML 内容…扩展名不是 .xml/.rels”）；无 BOM UTF-16LE 孤立 rels → InputError（“看起来是 UTF-16/UTF-32（无 BOM）”）；翻转 DEFLATE 字节 → InputError（不再 `zlib.error` 逃逸）。 |
| CODE-04（P1）coverage 文本顶包 / unit_count | **closed** | `_coverage_index` 以 `chunk.block_ids` 为成员依据（`docx_eval.py:300-330`）；`_block_covered` 只在覆盖该块的片段文本里校验，长块退到逐句（`:330-355`）；`_unit_count_check` + `_failures` 加 `unit_count == 非空块行数` 门禁（`:490-530`）。复跑：重复文本删掉覆盖第二段的整个 chunk → **gates=False，coverage=3/4**；`unit_count=4` 而块文件 3 行 → **gates=False**，失败项明确写出 4 vs 3。 |
| CODE-05（P1）PDF 零引用 | **closed** | `eval/runner.py:193-199` 三态：零引用=False；有引用无页=None；有页=交集。复跑：PDF 题 `citation_pages=[1]` + 零引用 → `citation_page_ok=False, passed=False`；有 PDF 引用但页码不匹配 → False；DOCX 引用（有引用无页）→ None、passed=True。 |
| CODE-06（P1）正文条号 | **closed** | `emit_clause` + `_clause_start_display`（`docx/chunks.py:187-225,276-315`）把 `第一条`/`3.6.1` 作为条款起点；扁平的 `1`/`一、` 不提升。复跑：`numFmt=chineseCounting, lvlText=第%1条` 的普通段落 → chunk 文本 `第一条 …`/`第二条 …`，`label=一/二`、`section=第一条/第二条`；`1.` 与 `一、` 列表 → 一个普通 chunk、不产生条款标签。 |
| CODE-07（P1）标题兜底 | **closed** | `_fallback_heading` 加句末标点、`_MEASUREMENT_RE`（数值+单位）、单字母列项过滤（`docx/parser.py:665-710`）。复跑：`42 MPa 是最小抗压强度。` 与无句号的 `42 MPa 是最小抗压强度` 均为 paragraph；`3.6 斜度公差` 仍为 heading、level=2、label=3.6。 |
| 隐藏文字（P1） | **closed（直接 `w:vanish`）** | `_walk` 命中隐藏 run 时整棵子树返回 1、不递归（`docx/parser.py:489-503`）；`hidden_runs` 计数。复跑：隐藏 run 文本“隐藏的注入指令…”不进入 block/chunk，`hidden_runs=1`，ingest 的 `injection.hits=[]`。 |
| P1-05 `zlib.error` 逃逸 | **closed** | `_ZIP_ERRORS` 包含 `zlib.error/struct.error/EOFError/...`，`validate_docx` 外层兜底转 `InputError`（`security.py:120-135,195-205`）；API 400/CLI 退出 3 有测试。复跑：损坏 DEFLATE → InputError（不再 500/堆栈）。 |

**P0/P1 结论：全部 closed。** 未发现修复引入的新 P0/P1；PDF 原有渲染/解析/自检/失败页/旧产物回读与 DOCX 旧产物/混合语料定向回归均通过（120 passed）。**建议放行**，残余 P2 记录如下（不阻断）。

### 新发现（P2，不阻断）

| ID | 严重度 | 文件:行 | 问题 / 复现 | 建议 |
|---|---|---|---|---|
| R-01 | P2 | `src/docqa/qa/answer.py:287-327` | `[§1 ¶99]`（条号真实但段落号越界）在 `in_range` 为空时 `_restrict_to_range` 退回**全部**同条号候选，`evidence_chunk_ids` 变成整节；实测 `[§1 ¶99]` → evidence=(b1,b2,b3)。自检因此可在整节范围内寻找数值，弱化了“引用段落”的约束。 | `para_no` 明确给出却无 in-range chunk 时，只保留主锚点（或把该引用判为无效），不要把证据扩到整节。 |
| R-02 | P2 | `src/docqa/eval/docx_eval.py:330-355` | coverage 的表格单元格/逐句校验仍是子串匹配：表块 `rows=[["1"],["1.0"]]`、片段只含 `1.0` 时 `covered=1/1`（“1”被“1.0”顶替）；逐句同理。 | 单元格/句子匹配加数字边界或整值匹配；补 `1` vs `1.0` 回归。 |
| R-03 | P2 | `src/docqa/docx/chunks.py:320-340`、`src/docqa/pipeline.py:657-663` | 只有正文条款（无 Heading）的文档，chunk 已是 `[§第一条 ¶n]`，但 `structured=False`、`citation_mode="paragraph"`；UI/CLI 的“引用模式”与真实引用形态不一致（信息性问题）。 | `structured`/`citation_mode` 计入 body clause；或在报告里区分 “clause(body)” 。 |
| R-04 | P2 | `src/docqa/docx/parser.py:517-526` | 隐藏文字只识别 run 直接 `w:rPr/w:vanish`；通过**字符样式**（`w:rStyle` + 样式里的 vanish）隐藏的文本仍进入 text/chunk（实测 `可见隐藏`、`hidden_runs=0`，manifest 也不计数）。 | 沿 `rStyle` 查样式链的 vanish；或至少检测样式级 vanish 并计数/警告。 |

### 残余 P2（既有 finding，当前快照未关闭）

- **P2-01 样式级 `numPr`**：`_numbering_props` 仍只读段落 `w:pPr`；Heading 样式里的 `numPr` 仍 `clause_label=None`、`missing=0`、无警告（实测）。
- **P2-03 非 XML 部件声明大小**：被引用的 `word/media/*.bin` 把声明解压大小改成 1 字节后 `validate_docx` 仍通过；python-docx 读取时解压后才 CRC 失败（实测 20 MB payload）。仅 DoS 面，剩余风险有界，建议实际字节上限或 local/central 一致性校验。
- **P2-04 对抗变异对 DOCX 失明**：`adversarial.py` 仍未复用六形态引用正则；实测 DOCX 答案的 4/4 个 `clause` 变异都落在 `[§3.7 ¶4]` 等引用内部，`_drop_citations` 对 DOCX 返回 None，DOCX 的 `clause`/`citation` 检出率失真。
- **P2-05 前端混选/标签键**：`web/src/format.ts:76-86` 仍把 `mixed` 且 `unit_count>0` 判成 DOCX；`Overview.tsx:8-18` 的 `headers_footers/hidden_text/tracked_deletions` 与后端键名不一致；`UploadResult` 未声明 `source_type/unit_count/original_filename/stored_filename`。typecheck 通过，行为问题仍在（`web/` 未跟踪）。
- **P2-06 PDF 证据头 `page_no=None`**：`qa/answer.py:116-118` 仍直接插值，未加兜底（潜在路径）。
- **P2-07 条号提取/Roundtrip 窄**：中文数字条号与阿拉伯引用仍不能互认（`[§1 ¶1]` 对 `clause_label=一` 被判“不存在”）；Heading 样式 `A.1`、无空格 `3.6斜度`、`3.6：…` 仍不拆标签（实测/代码核对）。
- **P2-09 DOCX 注入命中 `page_no:0`**：`InjectionHit.to_dict()` 仍序列化 0（locator/para_no 已附加）。
- **RECHECK-02 残余**：上传路径已用 `sha256[:32]`，但**非上传的 CLI ingest** `_doc_id` 仍用 `sha256[:8]`（32 bit）；如需统一 128 bit，会改变旧产物 doc_id，需迁移说明。
- **SEC 已接受残余**：孤立 rels 仍会被安全解析（8 MiB/500 MiB 有界）；未做关系图可达性裁剪/多 EOCD 单独检测；`MAX_UPLOAD_BYTES=200MB` 与同步 ingest 仍在（RECHECK-04/03、RECHECK-01/02/06 当前代码/测试已修）。

### 测试质量复核

- 抽查断言均指向具体行为，而不是“跑通”：长段落 `evidence_chunk_ids == (b2,b3,b4)`、sdt 行/格 `rows == (...42 MPa...)`、隐藏文本 `block.text == "可见"`、`unit_count` 门禁 `gates_passed is False`、PDF 零引用 `citation_page_ok is False`、`.bin`/UTF-16 载荷在 `_parse_xml` 前被拒（monkeypatch 断言未调用）。这些断言在旧实现上会失败，不是同义反复。
- 4 个 skip 全是真实端点用例；没有为了过门禁新增 `xfail`/跳过。
- **仍缺的关键测试**：① 全链路 `ingest(long.docx) → QaAgent(脚本化 provider)` 的长段落问答（当前 parse/verify/eval 分层有测试，端到端靠手工复跑）；② `docqa eval-docx` CLI 对长段落退出 0（现有 CLI 测试只跑 demo）；③ 上面 R-01/R-02/R-03/R-04；④ 样式级 `numPr`、DOCX 对抗变异、前端 `mixed`、中文数字/附录标签；⑤ 安全侧 `orig_filename`/元数据 8 MiB 边界由并行安全审计覆盖，本轮未重复跑完整矩阵。

### 复核结论

- **P0/P1：全部 closed，未发现新 P0/P1。** 代码/测试层面可放行。
- 残余 P2 不阻断 Phase 1，但建议在发布说明/`docs/docx-support.md` 记入：R-01（越界段落证据扩到整节）、R-02（coverage 子串）、R-03（body clause 的 citation_mode）、R-04（样式级隐藏文字），以及既有 P2-01/03/04/05/06/07/09。
- 最终放行仍需安全审计的独立结论与 `docs/verification/2026-10-29-docx-phase1.md` 落盘；本报告只对 P0/P1 代码复核负责。
