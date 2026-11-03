# DOCX 原生支持 Phase 1 —— 独立安全审计

- 日期：2026-10-30（任务标注 2026-10-29）
- 审计范围：DOCX 上传/解析新增攻击面（ZIP、XML、路径、解压炸弹、外部关系、注入文本）
- 审计方式：只读代码 + 在系统临时目录构造载荷实测；**未修改任何仓库代码/测试/配置/数据**
- 仓库：`D:\github\scanned-pdf-qa-agent`
- 关键版本：Python 3.12.14、python-docx 1.2.0、lxml 6.1.3、expat 2.8.3、Starlette 1.6.0
- 相关契约：`docs/reviews/2026-10-29-docx-gap-review.md`、`docs/docx-support.md`

---

## 1. 结论摘要

**结论：不可放行。** 当前实现有 **1 个 P0、4 个 P1、8 个 P2**。

| 严重度 | 数量 | 结论 |
|---|---:|---|
| P0 | 1 | 11.6 KB 的 DOCX 可让 `validate_docx` 分配约 560 MB 内存，且完整 ingest 返回成功（CLI 退出码 0）。若服务按部署文档绑定 `0.0.0.0`，这是未认证的远程内存耗尽攻击。 |
| P1 | 4 | XML 扫描与实际解析目标不一致（DTD 绕过 + 静默丢文本）；上传在大小检查前缓冲整个 body；拒绝/失败后 raw 文件残留且同名可覆盖；`zlib.error` 等非 `InputError` 逃逸，API 500、CLI 打印堆栈。 |
| P2 | 8 | ZIP 压缩比阈值边界/多卷/条目数预检、Windows 文件名与 500 路径泄露、`altChunk` 静默丢内容、注入扫描截断不可审计、解析后限流/CLI 无大小预检、NUL/符号链接条目、文档安全声明与实现不符、非 loopback 暴露无认证/限流。 |

P0 复现要点（已实测，非推测）：

- 构造 11,623 字节的 DOCX；其中 `word/_rels/orphan.xml.rels` 是**无 BOM 的 UTF-16LE** XML，含 `<!DOCTYPE>` 与嵌套 `<!ENTITY>`；该 rels 不被任何部件引用，因此 python-docx 根本不会读它。
- `validate_docx()` 通过；`tracemalloc` 峰值 **559.9 MB**，耗时 0.50 s；随后 `parse_docx()` 成功返回正文 `["正常正文"]`；`docqa ingest` **退出码 0**。
- 同一个炸弹放进 `[Content_Types].xml` 时，`validate_docx()` 同样先分配约 560 MB，之后才被 python-docx 的 lxml 放大保护拒绝（400）——内存已经花掉。
- 该构造可重复到总解压上限（500 MiB）附近；每次约 560 MB 峰值与约 0.5 s CPU。多个并发上传/多次 ingest 足以在 512 MB–1 GB 容器上 OOM。

---

## 2. Findings 表

> 所有 `file:line` 均为仓库内当前快照。P0 的载荷与复现步骤见 3.1；其余 finding 的载荷要点见对应小节。

| ID | 严重度 | 文件:行 | 载荷 / 触发 | 失败场景 | 建议 |
|---|---|---|---|---|---|
| SEC-DOCX-01 | **P0** | `src/docqa/docx/security.py:356,369-377,408-424,311,416,285-287` | 11.6 KB DOCX；无 BOM UTF-16LE 的孤立 `*.rels`（或 `[Content_Types].xml`）含 DOCTYPE + 嵌套 ENTITY | `_check_xml_parts` 只按扩展名扫描且只认 BOM，漏掉无 BOM UTF-16；`_count_external_relationships` 用 stdlib ET 解析**所有** `.rels`（含孤立文件），expat 展开实体 → 约 560 MB 峰值；孤立 rels 不被 python-docx 读取，完整 ingest 成功。远程/本地 DoS。 | XML 部件先做编码检测（UTF-8 之外一律拒绝，或解码后扫描）；用 defusedxml/lxml 安全解析器替换 ET；只解析可达 rels 或限制聚合大小；压缩比阈值改为 `>=` 并覆盖所有 XML 条目。 |
| SEC-DOCX-02 | **P1** | `src/docqa/docx/security.py:356,210-220,308-344`；`src/docqa/docx/parser.py:250-258` | 合法包 + `word/document.bin`（Content-Type 覆盖为主文档类型）+ 根 rels 指向 `.bin`；`.bin` 内含 DOCTYPE/ENTITY；另放一个无害 `word/document.xml` 作诱饵 | 扫描只覆盖 `.xml/.rels`，`.bin` 从未被扫；python-docx 按 content-type/relationship 把 `.bin` 当主文档解析。`parse_docx` 返回 `BEFORE[`（实体引用后的 `MID/AFTER` 被静默丢弃），CLI 退出 0。`word/styles.bin` 变体会在解析阶段报通用 XMLSyntaxError。当前 lxml `resolve_entities=False` 阻止了 XXE/放大，但“先扫描再解析”的不变量已被打破，内容可被篡改/隐藏。 | 通过根 rels 解析真正的 officeDocument target，并要求它就是 `word/document.xml`；按 content-type 找出所有 XML 部件并扫描（或拒绝 XML content-type 却非 `.xml/.rels` 的部件）；为 `.bin`/`.rels` 绕过加回归测试。 |
| SEC-DOCX-03 | **P1** | `src/docqa/api/app.py:382-388` | 分块/无 `Content-Length` 的请求体；或声明长度与实际不符；5 MB body + 把 `MAX_UPLOAD_BYTES` 调到 1 KB 时，服务端仍先读完 5 MB 才返回 413 | `await request.body()` 在大小检查前把全部 chunk 收进 `list` 再 `join`（约 2× 内存）；没有 `Content-Length` 预检；200 MB 上限本身很高，后续 `write_bytes` + `read_bytes` + python-docx 还会复制。未认证/并发时内存 DoS。 | 先检查 `Content-Length`；用 `request.stream()` 累计读取并在超限时立即 413；降低/配置上限；必要时落临时文件；加“读取字节数”测试。 |
| SEC-DOCX-04 | **P1** | `src/docqa/api/app.py:398-401,355-364,423-437`；`src/docqa/cli.py:1340`；`docs/deployment.md:123` | 通过 `is_docx()` 但 `validate_docx()` 失败的包（如缺 `[Content_Types].xml`）；同名二次上传 | 先 `write_bytes` 再校验/解析，且写在 try 之外；400/500 后 raw 文件保留。未认证请求可反复写 200 MB 文件填满磁盘；同名上传覆盖已有源文件，旧 manifest 的 `source.path` 与 `sha256` 不再对应（证据链断裂）。容器文档要求 `--host 0.0.0.0`，暴露后无认证/限流。 | 先写临时文件，校验/解析成功后再原子改名/入库；失败 `unlink`；raw 用内容哈希/随机名，原始文件名只进元数据；配额/清理/限流；非 loopback 加认证。 |
| SEC-DOCX-05 | **P1** | `src/docqa/docx/security.py:393-396,443-444`；`src/docqa/docx/parser.py:250`；`src/docqa/api/app.py:419-421`；`src/docqa/cli.py:504-506,1361-1367` | 合法 ZIP 中把 `word/document.xml` 的 DEFLATE 数据翻一个字节（中央目录大小/CRC 不变）；`is_docx()` 仍返回 True | `archive.open().read()` 抛 `zlib.error: Error -3 ...`，未被 `_check_xml_parts`/`_read_member` 的异常元组捕获；`validate_docx` 直接抛出。API 走到兜底 500 并把异常文本回给客户端；CLI 打印大段堆栈、退出码 1（契约应为 3）。`tests/docx/test_security.py:400` 的“zipfile errors never escape”只覆盖了错误 magic。 | 在校验入口统一包住所有 ZIP 读取异常（`zlib.error`、`struct.error`、`EOFError`、`ValueError`、`OverflowError`、`RuntimeError`、`NotImplementedError`、`BadZipFile`、`OSError`）→ `InputError`；日志留详情，响应只给可操作消息；补损坏 deflate/CRC/截断测试。 |
| SEC-DOCX-06 | **P2** | `src/docqa/api/app.py:358-362,398-401,419-421`；`src/docqa/pipeline.py:597-599` | `X-Filename: foo:bar.docx`（Windows）；`a\|b.docx`；`CON.docx`/`NUL.docx`；超长名 | 只过滤控制字符；Windows 上 `foo:bar.docx` 被写成基文件 `foo` 的备用数据流（ADS），随后 `doc_id` 含冒号导致 `out_dir.mkdir` 抛 WinError 267，API 500 的 detail 泄露绝对路径（`C:\Users\...\derived\foo:bar-...`）。`a\|b.docx` 在 `write_bytes` 处抛 OSError（try 之外）→ 纯文本 500；保留设备名/长名同类。无路径穿越（这点做对了）。 | 存储名不再使用用户文件名（sha256/UUID）；对 Windows 非法字符、保留设备名、尾随点/空格、长度做校验；`mkdir/write` 的 OSError 映射为 400/507，响应不含绝对路径；`doc_id` 同样清洗。 |
| SEC-DOCX-07 | **P2** | `src/docqa/docx/security.py:55-56,285-287,110-122,192-204` | 单条目解压 10,485,328 B（比 10 MiB 少 2 B）、压缩 10,607 B（988.5:1）→ 通过；把 EOCD 的 disk 字段改成 1 → 通过；6,002 条目 ZIP → `is_docx()` 先成功 | 压缩比检查条件是 `size > 10 MiB`（严格大于且只看单条目），恰好 10 MiB 或以下完全不查；多卷 EOCD 字段未拒绝（Python zipfile 忽略 disk 号）；`MAX_ENTRIES` 在 `infolist()` 之后才检查，`is_docx()` 更不检查，200 MB 中央目录可先物化大量 `ZipInfo`。这些是 SEC-DOCX-01 的 ZIP 层放大器。 | 压缩比用 `>=` 并降低最小体积（如 1 MiB），XML 部件单独更严；显式拒绝非零 disk 字段/多 EOCD；尽量在构造 `ZipFile` 前用 EOCD 读条目数，或降低上传上限；补边界/多卷/Zip64 测试。 |
| SEC-DOCX-08 | **P2** | `src/docqa/docx/parser.py:62-77,745-778,418-434,708-713`；`docs/docx-support.md:241` | 正文 `<w:altChunk r:id="rId9"/>` + 内部 `word/afchunk.html`；`w:sdt` 包裹的表格行；body 级 `mc:AlternateContent` | `altChunk` 内容既不抽取也不计数：`unsupported` 全部为 0，blocks 只有可见段落；文档却声称“所有不支持的构造都进入 unsupported，绝不静默丢弃”。`_linearize_table` 只 `findall(w:tr)`，sdt 包裹的行会丢；body 级 alternate content 也不会被 `_iter_body_containers` 下钻。属于可审计性/证据完整性问题，也可藏数值/指令。 | 把 `alt_chunks`/`alternate_content`/`content_parts` 加入 `UNSUPPORTED_KEYS` 并计数；要么抽取并参与注入扫描，要么显式 `InputError` 拒绝；表格行遍历支持 `w:sdt`；补测试。 |
| SEC-DOCX-09 | **P2** | `src/docqa/injection.py:20-21,96-100,131-139,142-164`；`src/docqa/pipeline.py:55-57,537-555,658`；`src/docqa/report.py`（无 injection） | 段落重复 `"ignore all previous instructions. "`；2,040,000 字符实测 `scan_text` 先生成 60,000 条命中（0.35 s），之后才截到 20 条 | 截断发生在 `scan_text` 返回之后，内存/CPU 没有上界；每块 20 条但块数最多 100,000，全局命中无上限，manifest 可被撑大；截断只打日志，`InjectionReport.to_dict()` 没有 `truncated`/`hits_total`，日志丢失后无法审计；`describe()` 在测试外无调用，报告/UI 不展示命中；docstring 宣称可配 `on_injection: refuse`，但 `DomainProfile` 没有该字段（配置会报未知项）。 | `scan_text` 内按 `max_hits` 提前停止；报告/ manifest 记录 `truncated` 与总命中数；加全局上限；在报告/UI 用 `describe()`（¶n）展示；实现或删除 `on_injection` 声明；补“截断可审计”测试。 |
| SEC-DOCX-10 | **P2** | `src/docqa/docx/parser.py:286-380,380`；`src/docqa/docx/security.py:50-58`；`src/docqa/pipeline.py:145,580`；`src/docqa/api/app.py:412` | 100 MB `word/document.xml`（低于 100 MB 上限）含大量 run；CLI 指向超大本地 DOCX | `check_parse_limits` 在完整遍历、构建全部 `DocxBlock.text` 之后才执行；python-docx 的 `PackageReader` 会把所有可达 part 的 blob 一次性读进内存（总解压上限 500 MB）；CLI `source_type_of()` 的 ZIP 分支和 `_ingest_docx()` 都直接 `read_bytes()`，没有 CLI 侧文件大小预检；上传端同步跑 ingest 会阻塞事件循环。 | 降低单条目/总量/正文 XML 上限；在遍历中增量检查段落/字符数；拒绝/延后加载大媒体 part；CLI `stat()` 预检大小；ingest 放线程池；补大文档/大媒体测试。 |
| SEC-DOCX-11 | **P2** | `src/docqa/docx/security.py:239-266,269-299` | 包内额外条目 `evil\x00.xml`；条目 `word/link` 的 external_attr 设为 Unix symlink（`0o120777<<16`）；多卷 EOCD（见 07） | 当前**不落盘解压**，所以 NUL/符号链接/设备文件暂时不可直接利用；但校验没有拒绝它们，安全完全依赖“永远不解压”这一实现细节。将来任何抽取/重打包、杀软/下游工具处理，或 zip confusion，都会把风险放大。 | 条目名校验增加 NUL/控制字符；拒绝 symlink/device/special external_attr；拒绝多卷/多 EOCD；把“永不抽取”写成注释/测试；补条目类型测试。 |
| SEC-DOCX-12 | **P2** | `docs/docx-support.md:241,245-262`；`README.md:251,307` | 文档声明与实现对照 | 文档称“任何 XML 部件都不得含 DOCTYPE/ENTITY”“校验失败一律 InputError”“压缩比 >200 且解压后 ≥10 MB”“所有不支持构造都计数”“报告用段而非页”，分别被 SEC-01/02、SEC-05、SEC-07、SEC-08、SEC-09 证伪。审计/用户会过度信任这些保证。 | 修复代码后同步修正文档；增加安全矩阵与残余风险（解析器版本、无认证部署、内存上限）；把文档声明纳入测试。 |
| SEC-DOCX-13 | **P2** | `src/docqa/api/app.py:340-437,441-489`；`src/docqa/cli.py:1338-1352`；`docs/deployment.md:123` | 非 loopback 暴露；未认证的 `POST /api/upload`、`POST /api/ask` | 默认 `127.0.0.1` 是正确默认；但部署文档直接要求容器用 `serve --host 0.0.0.0`，此时任何网络客户端都可上传/触发 ingest（叠加 SEC-01 的远程 OOM）并调用 `/api/ask` 消耗 LLM 配额。`MAX_CONCURRENT_ASKS` 只限 ask，上传无限流。没有 CORS 中间件 + 自定义 `X-Filename` 头使浏览器跨站 CSRF 基本不可行（这点做对了），但非浏览器客户端不受限。 | 默认仅 loopback；非 loopback 必须显式 token/认证并打印警告；上传/ask 加按 IP 限流；部署文档写清暴露风险；可加 Host 校验。 |

---

## 3. Findings 详情

### 3.1 SEC-DOCX-01（P0）：无 BOM UTF-16 + 孤立 `.rels` 的实体扩展内存 DoS

**根因**

1. `security.py:356` 只扫描文件名以 `.xml` / `.rels` 结尾的部件；这不覆盖“实际会被解析的部件”的全集（见 SEC-02）。
2. `security.py:369-377` 只检查文件开头的 UTF-16 BOM（`FF FE` / `FE FF`）。libxml2 与 expat 都能从 `<\x00?\x00` / `\x00<\x00?` 自动识别**无 BOM** 的 UTF-16LE/BE；字节级 `<!doctype` 扫描完全看不到。
3. `security.py:311` 与 `security.py:416` 用 stdlib `xml.etree.ElementTree.fromstring` 解析不可信 XML。ET/expat **会展开内部实体**（实测 1,000,000 字符展开成功；外部实体会报 undefined，不会读文件）。
4. `security.py:408-424` 会解析**所有** `*.rels`，包括没有被任何 part 引用、python-docx 根本不会打开的孤儿 rels。这是把炸弹送进 ET、又让 python-docx 完全看不见它的关键。
5. `security.py:285-287` 的压缩比检查只在 `size > 10 MiB` 时生效，且是严格大于。攻击者把条目控制在 10 MiB 以下（本载荷 10,485,328 B，比 10 MiB 少 2 B），压缩比 988.5:1 也不会被拦。

**载荷（要点）**

ZIP 条目：

- `[Content_Types].xml`：正常 UTF-8，声明 `/word/document.xml` 为 `application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml`。
- `_rels/.rels`：正常 UTF-8，officeDocument → `word/document.xml`。
- `word/document.xml`：正常 UTF-8，正文 `正常正文`。
- `word/_rels/orphan.xml.rels`：**UTF-16LE，无 BOM**，约 10,485,328 B 解压、10,607 B 压缩：

```xml
<?xml version="1.0" encoding="UTF-16"?>
<!DOCTYPE Relationships [
  <!ENTITY a0 "AAAA...（约 5,242,000 个 A）">
  <!ENTITY a1 "&a0;&a0;...（100 个 &a0;）">
]>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <unused>&a1;</unused>
  <Relationship Id="rId1" Type=".../hyperlink"
                Target="https://example.invalid/x" TargetMode="External"/>
</Relationships>
```

该 rels 不被任何 part 引用；python-docx 的 `PackageReader._walk_phys_parts` 只沿关系图走，所以不会读它。

**实测结果**

- 整个 DOCX 11,623 字节；`is_docx()` 返回 True。
- `validate_docx()` **通过**；`tracemalloc` 峰值 **559.9 MB**，耗时 0.50 s。
- `parse_docx()` **成功**，blocks 为 `["正常正文"]`。
- `docqa ingest` **退出码 0**，正常写出产物。
- 换成 `[Content_Types].xml` 承载同一炸弹时，`validate_docx()` 同样先分配约 560 MB；之后 python-docx 的 lxml 放大保护报错，API 返回 400（内存已花掉）。

**为什么是 P0**

- 输入只有 11.6 KB，绕过了项目专门设计的 zip bomb/DTD 防线，且在**完整 ingest 成功**的情况下发生。
- 项目威胁模型明确把上传的 DOCX 当不可信输入；部署文档又要求容器 `--host 0.0.0.0`，而上传端点没有认证/限流。
- 单个请求约 560 MB 峰值；500 MiB 总解压预算内可放几十个类似 rels，CPU 也可被拉到数十秒。512 MB–1 GB 容器会 OOM；即使不 OOM，事件循环被同步 ingest 阻塞。
- 默认 `127.0.0.1` 降低了远程可利用性，但不改变“恶意文档可打爆本地进程”的事实；且这是对既有安全契约的直接绕过。

**修复建议**

1. **先做编码白名单**：XML 部件只允许 UTF-8（可选 UTF-8 BOM）。用首 4 字节/BOM 检测；出现 NUL 或非 UTF-8 可解码（EBCDIC 等）一律 `InputError`。这同时覆盖 UTF-16LE/BE 无 BOM 和 UTF-32 BOM。
2. **不要用 stdlib ET 解析不可信 XML**：改用 `defusedxml.ElementTree`，或 lxml `XMLParser(resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False)`；并在解码后的文本上显式拒绝 `<!DOCTYPE`/`<!ENTITY`。
3. **只解析可达 rels**（沿根 rels/部件关系图），或对所有 rels 使用安全解析器并设置聚合解析上限；孤儿 rels 不应被解析。
4. **压缩比/大小检查**：`>=` 而不是 `>`；把最小体积降到 1 MiB 左右；对 XML 部件单独设更小上限；对总解压做更保守预算。
5. **回归测试**：构造 11 KB 级别的 UTF-16 无 BOM 孤立 rels 炸弹，断言：`validate_docx` 在解析前拒绝，或至少不展开实体；可用 monkeypatch 让 `ET.fromstring` 一旦被调用就失败，确保测试守住“先扫描/后解析”；同时覆盖 `[Content_Types].xml` 与 `*.rels` 两条 ET 调用路径；断言 `parse_docx`/`ingest` 不会成功。
6. 建议加一个受控的内存/CPU 上限（例如在 worker/子进程中解析并限制 RSS），作为纵深防御。

### 3.2 SEC-DOCX-02（P1）：扫描目标和解析目标不一致（`.bin` 绕过 + 静默丢文本）

**根因**

- `_check_xml_parts` 只按扩展名扫 `.xml/.rels`（`security.py:356`）。
- `_inspect_archive` 只要求存在 `word/document.xml`（`security.py:210-220`），`_main_content_type` 只检查 `/word/document.xml` 的 Override（`security.py:308-344`）。
- python-docx 不按固定文件名找主文档：`PackageReader` 从 `_rels/.rels` 的 officeDocument 关系取 target，`PartFactory` 按 **Content-Type** 选 part class。于是“校验的部件”和“解析的部件”可以是两个不同文件。
- 当前 python-docx 的 lxml 解析器 `resolve_entities=False`，所以没有 XXE/实体放大；但 DTD 被解析、实体引用后的文本被静默丢弃。

**载荷与实测**

- 包内：`[Content_Types].xml` 同时给 `/word/document.xml`（诱饵）和 `/word/document.bin` 声明主文档 Content-Type；`_rels/.rels` 的 officeDocument Target 指向 `word/document.bin`；`word/document.xml` 是无害诱饵；`word/document.bin` 含 DTD 与 `<w:t>BEFORE[&xxe;]MID[&b;]AFTER</w:t>`。
- `validate_docx()` **通过**（`.bin` 不在扫描集合里；诱饵满足必需部件与 Content-Type 检查）。
- `parse_docx()` 返回 block 文本 `BEFORE[`；`MID`、`AFTER` 及实体值全部消失。
- `docqa ingest` 对该包 **退出码 0**，即“通过安全校验并成功入库”的文档，服务端抽取文本与 Word 显示文本可以不同。
- `word/styles.bin` 变体：`validate_docx()` 通过；`parse_docx()` 抛 `XMLSyntaxError: Attribute references external entity 'xxe'`，最终 400，但拒绝理由不是清晰的 DTD/ENTITY。

**影响**

- 违反“先扫描所有 XML，再解析任何 XML”的设计不变量。
- 内容丢失可被用来隐藏/篡改数值、条号或限定语，而系统仍把它当作可信证据；对“答案必须可定位到原文”的产品是完整性问题。
- 一旦依赖升级/换用默认 lxml 解析器（`resolve_entities=True`），同一路径会直接变成 XXE/实体放大。因此不能因为“当前是安全的”而放行。

**修复建议**

- 解析根 `_rels/.rels`，得到 officeDocument target；要求 target 规范化后等于 `/word/document.xml`，并校验该 target 的 Content-Type 为主文档类型。
- 扫描集合改为“所有 XML content-type 的 part + 所有 `.xml/.rels`”，或直接拒绝“XML content-type 但名字不以 `.xml/.rels` 结尾”的 part。
- 对 styles/numbering/header/footer/footnotes/comments 等按关系/Content-Type 找到的 part 同样扫描。
- 增加测试：`word/document.bin`、`word/styles.bin`、`word/numbering.bin` 三种绕过；断言拒绝理由包含 DTD/ENTITY，且 `parse_docx` 不成功。

### 3.3 SEC-DOCX-03（P1）：上传在大小检查前缓冲整个 body

**根因**

`src/docqa/api/app.py:382` 先 `body = await request.body()`，`:385` 才比较 `MAX_UPLOAD_BYTES`。Starlette 1.6.0 的 `Request.body()` 实现是把所有 chunk 收进 `list[bytes]` 再 `b"".join(chunks)`，因此峰值约为 body 的 2 倍；没有 `Content-Length` 预检，也没有流式上限。

**实测**

- 把 `MAX_UPLOAD_BYTES` monkeypatch 成 1024，发送 5,000,000 字节 body（`PK\x03\x04` 开头）：返回 413，detail 为 `文件过大：5.0 MB，上限 0 MB`——证明大小检查发生在完整读取之后。
- 代码中没有任何 `content-length` 读取（全仓库 grep 只有 `MAX_UPLOAD_BYTES` 比较）。

**影响**

- 若服务暴露（见 SEC-DOCX-13），攻击者用 chunked body 或不匹配的 `Content-Length` 就能让进程在拒绝前分配任意内存；多个并发请求叠加。
- 200 MB 上限 × 2（body list+join）+ 200 MB 写入 + `_ingest_docx` 的 `read_bytes()` + python-docx 的 part blobs/DOM，单请求可到 GB 级。
- 与 SEC-DOCX-01 叠加：即使 body 很小，解析阶段也能分配数百 MB。

**修复建议**

- 读取 `Content-Length`，超过上限立即 413（注意 header 可缺失/可撒谎，仍需流式兜底）。
- 用 `async for chunk in request.stream()` 累计，超过 `MAX+1` 立即停止并 413；不要 `request.body()`。
- 降低默认上限（例如 50 MB，扫描件需求另配）；必要时写临时文件并限制磁盘。
- 上传写盘/ingest 放线程池，避免同步阻塞事件循环。
- 增加测试：chunked body 超限、Content-Length 超限、字节计数器断言“读取量 ≤ 上限+1”。

### 3.4 SEC-DOCX-04（P1）：raw 残留、同名覆盖、无清理/限流

**根因**

- `api/app.py:398-401` 在 `try` 之前就 `target.write_bytes(body)`；校验/解析失败不会删除。
- 文件名来自 `X-Filename`（只做控制字符/目录剥离），没有内容哈希/随机名；同名上传直接覆盖。
- 没有清理、配额、上传限流；`MAX_CONCURRENT_ASKS` 只作用于 `/api/ask`。

**实测**

- 缺 `[Content_Types].xml` 的包（`is_docx()` 为 True）→ 400，`data/raw/unsafe.docx` 仍存在。
- 损坏 DEFLATE → 500，raw 仍存在。
- 同名 `same.docx` 连续上传两份不同正文：两次都 200，第二次后 `raw/same.docx` 等于第二份；第一份的派生目录仍在，但其 manifest 的 `source.path` 现在指向第二份字节，`sha256` 对不上。
- `foo:bar.docx` 的 ADS 路径还会在 raw 留下基文件 `foo`（见 SEC-DOCX-06）。

**影响**

- 未认证/多用户部署下可反复写入最大 200 MB 的“合法 ZIP 但校验失败”文件填满磁盘。
- 同名覆盖破坏源文件与 manifest 的溯源一致性；对一个以“可审计证据链”为卖点的工具是实质问题。
- 与 SEC-DOCX-13 的 `0.0.0.0` 部署叠加时风险显著上升；默认 loopback 时是本地/共享机器风险。

**修复建议**

- 内容寻址或随机临时名：先写 `raw/.tmp-<uuid>`，`validate_docx`/`parse_docx` 成功后再原子 rename 到 `raw/<sha256>.docx`；失败 `unlink`。
- 原始文件名只作为元数据（清洗后）保存；不要用于路径。
- 增加配额/定期清理；非 loopback 加认证与限流；上传并发上限。
- 测试：400/500 后 raw 不存在；同名两次不覆盖；manifest source 与 sha 一致。

### 3.5 SEC-DOCX-05（P1）：非 `InputError` 逃逸导致 500/堆栈

**根因**

- `security.py:393-396` 与 `:443-444` 的 except 元组不包含 `zlib.error`、`struct.error`、`EOFError` 等。
- `parse_docx`（`parser.py:250`）在 `try` 之外调用 `validate_docx`，所以这些异常不会被 `Document` 的兜底捕获。
- API 的兜底 `except Exception`（`api/app.py:419-421`）把 `str(exc)` 回给客户端；CLI 只捕获 `DocqaError`（`cli.py:504-506`、`1361-1367`）。

**实测**

- 合法 ZIP，`word/document.xml` 的 DEFLATE 数据翻一个字节（中央目录大小/CRC 不变）：`is_docx()` 返回 True；`validate_docx()` 抛 `zlib.error: Error -3 while decompressing data: invalid code lengths set`。
- API：500，detail `解析失败：Error -3 while decompressing data: invalid code lengths set`；raw 残留。
- CLI：出现大段 Rich 堆栈，最后 `error: Error -3 ...`，**退出码 1**（文档契约是 `InputError` 退出码 3）。

**影响**

- 任意畸形/构造输入可触发 500/堆栈，违反“校验失败一律 InputError”的契约；日志噪音、错误响应不一致。
- 当前 payload 不含路径，但兜底逻辑会回显任意异常文本；与其他 OSError 组合会泄露绝对路径（见 SEC-DOCX-06）。
- 不直接造成数据泄露/执行，但属于明确的可用性与错误处理缺陷，且掩盖了“坏输入应可操作拒绝”的安全边界。

**修复建议**

- 在 `validate_docx` 入口包住 `_inspect_archive` 及所有 ZIP 读取；捕获 `Exception`（排除 `MemoryError`/`KeyboardInterrupt`/`SystemExit`）并转 `InputError`，保留内部日志。
- API 的 400 响应只给可操作消息；500 不返回异常文本；详细异常只进服务端日志。
- 补 `zlib.error`、坏 CRC、截断本地头、不支持的 compression method 测试；修正 `test_zipfile_errors_never_escape` 的名称/覆盖范围。

### 3.6 SEC-DOCX-06（P2）：Windows 文件名/`doc_id` 未清洗 → 500 与绝对路径泄露

**根因**

- 只过滤控制字符（`app.py:358`）；`Path.name` 只剥离分隔符（`:362`）。
- `pipeline.py:597-599` 用 `path.stem` 拼 `doc_id` 和 `out_dir`，未清洗 Windows 非法字符。
- `write_bytes`/`mkdir` 的 `OSError` 分别在 try 之外和 ingest 内，最终以 500 回显。

**实测（Windows）**

- `foo:bar.docx`：`Path.name` 保留冒号；`write_bytes` 创建基文件 `foo` 的 ADS `bar.docx`（读取该路径能读回内容）；`doc_id=foo:bar-...`，`out_dir.mkdir` 抛 `WinError 267 目录名称无效`；API 500，detail 含 `C:\Users\...\derived\foo:bar-...` 绝对路径。
- `a|b.docx`：`write_bytes` 抛 OSError（try 之外），响应纯文本 `Internal Server Error`。
- `CON.docx`/`NUL.docx` 通过扩展名检查；保留设备名在不同 Windows 环境可能写控制台/空设备。
- `.docx`、`..docx` 被接受并生成隐藏/怪名文件（无穿越，但命名语义混乱）。

**修复建议**

- 存储名与用户文件名解耦（见 SEC-DOCX-04），从根上消除 ADS/设备名/长度问题。
- 若必须保留文件名：拒绝 `<>:"/\|?*`、尾随点/空格、保留设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9，含带扩展名形式）、超长名；Unicode 规范化。
- `mkdir`/`write` 的 OSError → 400/507，响应不含路径；`doc_id` 只允许 `[A-Za-z0-9._-]`。
- 补 Windows 专项测试（`foo:bar.docx`、`a|b.docx`、`CON.docx`、超长名）。

### 3.7 SEC-DOCX-07（P2）：ZIP 层阈值边界、多卷与条目数预检

**实测**

- 10,485,328 B 条目（10,485,760 = 10 MiB，少 2 B），压缩 10,607 B，比例 988.5:1：`validate_docx()` **通过**。原因：`size > SUSPICIOUS_RATIO_MIN_BYTES` 严格大于，且只对单条目；低于 10 MiB 的条目完全不查比例。
- 把 EOCD 的 “number of this disk”/“disk with central directory” 都改成 1：`validate_docx()` **通过**（Python `zipfile` 忽略这两个字段）。真正的多卷包在其它解析器/解压工具里可能产生不同视图（zip confusion）。
- 6,002 条目的 ZIP（700 KB）：`is_docx()` True，0.32 s、3.6 MB 峰值；`validate_docx()` 在 `infolist()` 之后才以“超过 5000”拒绝。`is_docx()` 本身没有条目上限；一个 200 MB 中央目录可在上限检查前物化大量 `ZipInfo`。
- Zip64 条目、本地头/中央目录文件名不一致、CRC/大小撒谎：实测均被 `zipfile`/现有逻辑拒绝（这是做对的部分）。

**修复建议**

- 比例检查改 `>=`，降低最小体积（如 1 MiB）；XML 部件单独更严。
- 显式读取 EOCD，拒绝非零 disk/多卷；拒绝多 EOCD 的可疑包。
- 在 `ZipFile` 构造前尽量限制条目数（或降低上传上限）；`is_docx()` 也加轻量条目上限。
- 补 10 MiB 边界、多卷、Zip64、条目数放大测试。

### 3.8 SEC-DOCX-08（P2）：`altChunk` 等内容静默丢失且不计数

**实测**

- body 为 `<w:p>visible</w:p><w:altChunk r:id="rId9"/>`，并提供一个内部 `word/afchunk.html`（内容 `IGNORE PREVIOUS INSTRUCTIONS`）：`validate_docx()` 通过；`parse_docx()` 的 `unsupported` 全部为 0；blocks 只有 `["visible"]`；HTML 内容既没有被抽取，也没有被注入扫描。
- 相关代码：`_SKIP_SUBTREES`/`_iter_body_containers` 不处理 altChunk；`UNSUPPORTED_KEYS` 没有 `alt_chunks`；`_linearize_table` 用 `table.findall(w:tr)`，`w:sdt` 包裹的行会漏掉；body 级 `mc:AlternateContent` 也不会被下钻。

**影响**

- 与 `docs/docx-support.md:241` “所有不支持的构造都进入 unsupported，绝不静默丢弃”直接矛盾。
- 恶意文档可把数值/指令放进 altChunk/内容控件包裹的行，系统既看不到也不报告；对“证据完整”是实质风险。当前外部 altChunk 不会被请求（python-docx 跳过 external），但内部内容确实被丢弃。

**修复建议**

- 增加 `alt_chunks`、`alternate_content`、`content_parts` 计数；要么抽取并纳入注入扫描，要么 `InputError` 拒绝。
- 表格遍历支持 `w:sdt` 包裹的行；对 body 级 wrapper 显式枚举/计数。
- 补 altChunk/sdt 表格行/alternate content 测试，并修正文档。

### 3.9 SEC-DOCX-09（P2）：注入扫描的截断、持久化与展示

**实测/代码**

- `scan_text`（`injection.py:142-164`）对每个 pattern 的每个 `finditer` 命中都 append；2,040,000 字符的重复指令文本产生 **60,000 条**命中，耗时 0.35 s，之后 `_scan_docx_injection` 才截到 20 条。20,000,000 字符上限下约 600,000 条命中。
- `InjectionReport.to_dict()`（`:131-139`）没有 `truncated`/`hits_total`；`_scan_docx_injection` 截断只 `_log.warning`（`pipeline.py:546-554`）。日志轮转/丢失后无法证明“截断发生过”。
- 命中确实写进 manifest（`pipeline.py:658`），且 locator 是 `paragraph`，`describe()` 用 `¶n`（`injection.py:96-100`）；但 `describe()` 在 `src/` 内没有调用点，`report.py` 完全没有 injection 渲染，前端只有 `format.ts` 的标签字符串。用户/审计报告看不到命中。
- docstring（`injection.py:20-21`）说“合规场景可以配成 `on_injection: refuse`”，但 `DomainProfile` 没有该字段；YAML 配置会因未知键报 `ConfigError`，即该策略从未实现。
- 页眉/页脚/文本框/脚注/批注的内容不抽取，因此也不扫描。按 Phase 1 契约它们不进入证据，风险有限，但文档应明确“扫描范围 = 已抽取正文块”，不要让人误以为整包都扫过。

**修复建议**

- `scan_text(text, ..., max_hits=N)` 命中达到 N 即停止；`InjectionReport` 增加 `truncated`、`hits_total`、`hits_omitted`，写进 manifest。
- 全局命中上限（如 1,000）并记录省略数；每块上限保留。
- 在报告/UI 用 `describe()` 展示命中（PDF 用页、DOCX 用 `¶n`）。
- 实现 `on_injection`（加入 `DomainProfile.qa`，用于 `decide()`）或删除该声明。
- 明确并测试“未抽取的构造不在注入扫描范围”。

### 3.10 SEC-DOCX-10（P2）：解析后限流、CLI 无大小预检、事件循环阻塞

**根因/实测**

- `check_parse_limits(paragraph_total, char_total)` 在 `_parse_document` 的完整遍历之后才调用（`parser.py:380`）；此时 `blocks`、每块文本、表格 rows 已全部构建。100 MB 的 `word/document.xml` 在 100 MB 上限内，可产生远大于 100 MB 的 lxml DOM 与 Python 文本。
- python-docx 的 `PackageReader` 会把关系图可达的所有 part 的 blob 一次性读入内存；`MAX_TOTAL_UNCOMPRESSED=500 MiB` 意味着合法包也可占数百 MB。
- `source_type_of()` 的 ZIP 分支（`pipeline.py:145`）与 `_ingest_docx()`（`:580`）都用 `read_bytes()`，CLI 没有文件大小预检；一个超大本地 DOCX 会在校验前 OOM。
- 上传端 `run_ingest` 是同步调用（`app.py:412`），DOCX 解析/切分/建索引期间阻塞事件循环，其它请求（包括 `/api/status`）全部停住。

**修复建议**

- 降低 `MAX_ENTRY_UNCOMPRESSED`/`MAX_TOTAL_UNCOMPRESSED`/`MAX_DOCUMENT_XML_BYTES`/`MAX_PARAGRAPHS`/`MAX_TOTAL_CHARS`，或在遍历中增量检查（每 N 个元素检查一次）。
- 拒绝/延后加载大媒体 part；对非 XML part 设置更严格上限。
- CLI 在 `read_bytes()` 前 `stat()` 预检；定义统一输入大小常量。
- 上传 ingest 放线程池；保留 /api/ask 的并发信号量。
- 补大 XML/多表/大媒体/CLI 大小测试。

### 3.11 SEC-DOCX-11（P2）：NUL、符号链接/设备条目与多卷

**实测**

- 在合法包中追加 `evil\x00.xml` 条目，并把 `word/link` 的 `external_attr` 设为 Unix symlink（`0o120777 << 16`，内容 `/etc/passwd`）：`validate_docx()` **通过**（5 个条目）。
- `check_entry_path` 不检查 NUL/控制字符；`_check_entries` 不检查条目类型/`external_attr`。
- 多卷 EOCD 字段未拒绝（见 SEC-DOCX-07）。

**为什么仍是问题**

- 当前代码只在内存中 `archive.open()`，从不 `extract`/`extractall`，所以这些条目暂时不可直接利用；安全性依赖“永不抽取”的实现细节。
- 任何后续的“另存/重打包/预览/杀软/下游工具”处理，或不同解析器对 ZIP 的解释差异，都可能把 NUL/符号链接/多卷变成路径穿越或 zip confusion。

**修复建议**

- 条目名拒绝 NUL/控制字符；拒绝 symlink/device/special external_attr（`stat.S_ISLNK/S_ISCHR/S_ISBLK/S_ISFIFO/S_ISSOCK`）。
- 拒绝非零 disk/多 EOCD；把“不抽取”写成显式不变量与测试。
- 补 NUL/symlink/multi-disk 测试。

### 3.12 SEC-DOCX-12（P2）：文档安全声明与实现不符

`docs/docx-support.md` 的“安全限制”与实测不符：

| 文档声明 | 实际 |
|---|---|
| `:255` “任何 XML 部件都不得含 `<!DOCTYPE>` / `<!ENTITY>`” | `.bin` 主文档/样式部件可绕过（SEC-02）；无 BOM UTF-16 可绕过（SEC-01） |
| `:261-262` “校验失败一律是 `InputError`（退出码 3）” | 损坏 DEFLATE 抛 `zlib.error`，API 500、CLI 退出 1（SEC-05） |
| `:251` “压缩比 > 200 且解压后 ≥ 10 MB 按 zip bomb 处理” | 代码是严格 `>` 10 MiB，且只看单条目；10 MiB 边界/以下不查（SEC-07） |
| `:241` “所有不支持的构造都进入 `unsupported` 计数” | `altChunk`、sdt 包裹的表格行、body 级 alternate content 不计数（SEC-08） |
| `:259` “报告文案用‘段’而非‘页’” | `report.py` 没有 injection 渲染，`describe()` 仅在测试中使用（SEC-09） |

**建议**：代码修复后同步修订文档，并在文档中写清残余风险（解析器版本、无认证部署、内存上限）；把上述声明变成可执行测试。

### 3.13 SEC-DOCX-13（P2）：非 loopback 暴露无认证/限流

- `docqa serve` 默认 `127.0.0.1`（`cli.py:1340`），这是正确默认。
- `docs/deployment.md:123` 明确要求容器使用 `serve --host 0.0.0.0`；此时 `POST /api/upload` 与 `POST /api/ask` 无任何认证/限流。上传可触发 SEC-DOCX-01 的远程 OOM；ask 可消耗 LLM 配额。
- 没有 CORS 中间件；上传要求自定义 `X-Filename` 头，浏览器跨站请求会触发 preflight 并被默认拒绝，因此 CSRF 风险较低（这点做对了）。但非浏览器客户端/同网段主机不受此限制。
- `MAX_CONCURRENT_ASKS=2` 只限制 ask；上传没有信号量/速率限制。

**建议**：默认仅 loopback；非 loopback 必须显式 token/认证并打印警告；上传/ask 加按 IP 限流；部署文档写清暴露风险；可加 Host 校验。


---

## 4. 已确认防线（做对的部分）

以下均通过代码阅读与实测确认，不应因上面的 findings 被忽略：

1. **不落盘解压**：`security.py` 只用 `zipfile` 在内存读；`src/docqa/docx/` 中没有 `extract`/`extractall`；路径穿越在当前实现中不可直接利用。
2. **容器层拒绝**：空文件、空 ZIP、非 ZIP、OLE（旧 `.doc`/加密包）均有明确 `InputError`（`security.py:175-189`）；`.doc/.docm/.dotm/.dotx` 后缀、宏/模板 Content-Type、`word/vbaProject.bin` 均拒绝（`:70-86,152-172,216-220,329-343`）；测试覆盖。
3. **加密条目**：`flag_bits & 0x1` 拒绝（`:272-277`）；OLE 加密容器由文件头识别。
4. **ZIP 一致性**：重复条目拒绝（`:239-249`）；本地头/中央目录文件名不一致、CRC/大小撒谎会被 `zipfile` 拒绝并转 `InputError`（实测，除 SEC-05 的 `zlib.error` 分支）；Zip64 条目实测通过。
5. **路径检查**：绝对路径、`..` 组件、反斜杠拒绝（`:252-266`）；上传端 `Path.name` + 反斜杠归一（`app.py:362`）；控制字符/NUL 文件名拒绝（`:358`）。
6. **扫描顺序**：`.xml/.rels` 的 DTD/ENTITY 字节扫描发生在 `[Content_Types].xml` 与 `.rels` 解析之前（`security.py:222-227`）；`[Content_Types].xml` 内的 UTF-8 DTD 有回归测试（`tests/docx/test_security.py:294-313`）。
7. **外部关系不请求**：`_count_external_relationships` 只计数；python-docx `PackageReader._walk_phys_parts` 跳过 `is_external`；DOCX 代码路径没有 `httpx`/`requests`/`socket`；stdlib ET 不加载外部 DTD（实测把外部 DTD 指向非法 XML 也不读取）。未发现由文档内容触发的 SSRF。
8. **python-docx 解析器配置**：1.2.0 的 `docx/oxml/parser.py` 与 `docx/opc/oxml.py` 都是 `resolve_entities=False`；lxml 默认 `no_network=True`；实测 `word/document.bin` 中的外部实体不会被读取，实体引用后的文本会丢但不会外泄。libxml2 的 256 层深度限制阻止了 `_walk`/`_container_children` 的深递归（2000 层嵌套实测报 `Excessive depth ... 256`，被包装为 `InputError`）。不执行宏/OLE，不解码图片。
9. **规模与覆盖不变量**：解析后段落/字符上限（`security.py:453-468`）、`chunks.py:257-277` 的每块覆盖断言、manifest 的 `unsupported` 计数（除 altChunk 缺口）、隐藏 run 计数且隐藏文字参与注入扫描。
10. **注入可观测性基础**：逐正文块 `scan_text`、`locator="paragraph"`、命中写 manifest、`describe()` 用 `¶n`；隐藏文字仍进入正文并参与扫描（这是正确的危险面选择）。
11. **上传基础防护**：扩展名 allowlist、PDF magic、DOCX `is_docx()` 便宜检查 + 深度校验、`InputError→400`/`DocqaError→502` 的意图正确；默认 loopback；无 CORS + 自定义头使浏览器 CSRF 难以成立。
12. **无硬编码密钥/无 shell/无 eval/pickle**：DOCX 代码路径未发现命令执行、反序列化或网络调用。

---

## 5. 测试缺口

### 5.1 现有安全测试的真实覆盖

- `tests/docx/test_security.py` 45 条全绿（本次运行）；大多数用例**真的构造了恶意载荷**（zip bomb 11 MB 零字节、DTD、路径穿越、加密位、宏/模板 Content-Type、重复条目），并断言了消息关键词，不是只断言异常类型——这点做得比一般项目好。
- 但以下用例只断言异常类型/过弱：
  - `:386-391 test_bad_zip_is_input_error_not_crash`、`:393-398 test_zip_with_only_directories_is_rejected`：只断言 `InputError`。
  - `:400-403 test_zipfile_errors_never_escape`：只测了 `PK\x03\x04broken`（坏 magic），名称却承诺“zipfile 错误绝不逃逸”；SEC-05 的 `zlib.error` 正好从该承诺下逃逸。
  - `:242-249 test_rejects_backslash_entry`：只调用 `check_entry_path`，没有端到端包；Windows 上 `zipfile` 会归一化反斜杠，端到端行为未被覆盖。
  - `:341-351 test_external_relationships_are_counted_not_fetched`：只断言计数，没有断言“没有网络调用”（虽然代码路径确实没有）。
- `pytest.skip`：`tests/docx/` 内没有 skip；`tests/test_cli_api.py:44` 在没有 ingest 产物时会跳过整个 PDF Web API 类（DOCX 的 `TestDocxWebApi` 用隔离 fixture，不会 skip）。这不会掩盖 DOCX 安全缺口，但会让部分 API 回归在干净 CI 上不执行。

### 5.2 缺失的关键测试

1. **编码绕过**：无 BOM UTF-16LE/BE、UTF-32BE BOM、EBCDIC/非 UTF-8；现有 `:316-328` 只测了带 BOM 的 UTF-16。
2. **解析目标不一致**：`word/document.bin`、`word/styles.bin`、`word/numbering.bin` 作为主/样式/编号部件；根 rels 指向非 `word/document.xml`。
3. **ET 实体扩展**：`[Content_Types].xml` 与 `.rels`（尤其孤立 rels）的 UTF-16 无 BOM 炸弹；断言“解析前拒绝/不展开”，最好 monkeypatch `ET.fromstring` 或限制 RSS。
4. **ZIP 读取异常**：损坏 DEFLATE、坏 CRC、截断本地数据、不支持 compression method、`struct.error`/`EOFError`；断言统一 `InputError` 且 API 400/CLI 3。
5. **上传边界**：无 `Content-Length`/chunked 超限、Content-Length 撒谎、413 前读取量、`MAX_UPLOAD_BYTES` 边界。
6. **raw 生命周期**：400/500 后无残留、同名不覆盖、manifest source 与 sha 一致、清理/配额。
7. **Windows 文件名**：`foo:bar.docx`（ADS）、`a|b.docx`、`CON/NUL/AUX/COM1`、尾随点/空格、超长名；断言 4xx 且响应无绝对路径。
8. **ZIP 结构**：10 MiB 比例边界、多卷 EOCD、多 EOCD、Zip64、6k+ 条目对 `is_docx`/`validate` 的耗时/内存上限。
9. **条目类型**：NUL/控制字符条目名、symlink/device external_attr。
10. **解析规模**：大 `document.xml`、多表/多单元格、大媒体 part、CLI 超大文件预检；断言在构建完全部 blocks 前中止。
11. **altChunk / sdt 表格行 / alternate content**：计入 `unsupported` 或显式拒绝。
12. **注入截断**：`scan_text` 提前停止、manifest `truncated/hits_total`、报告/UI 展示；现有 `test_pipeline.py:263-272` 只断言 `0 < len(hits) <= 20`，即使截断信息丢失也会通过。
13. **解析器配置回归**：用实体载荷断言 python-docx 的 lxml 不展开/不读文件；防止依赖升级悄悄改成 `resolve_entities=True`。
14. **认证/限流**：非 loopback 绑定时的拒绝/警告；上传/ask 速率限制。

### 5.3 建议的测试组织

- 新增 `tests/docx/test_security_bombs.py`：所有炸弹类用例，统一断言“拒绝原因 + 不展开/不 OOM”；必要时用 `tracemalloc` 或 `resource` 设上限。
- 新增 `tests/test_upload_security.py`：用隔离 `cfg.cache_dir` + `TestClient(raise_server_exceptions=False)` 覆盖 raw 生命周期、文件名、大小、错误映射。
- 给 `scan_text`/`InjectionReport` 增加截断字段测试；给 `report`/API 增加注入命中展示测试。
- 保留现有“构造真实载荷 + 断言可操作消息”的风格，不要退化成只断言异常类型。


---

## 6. 放行条件与修复优先级

**必须修复（P0，放行前）**

1. SEC-DOCX-01：XML 编码白名单 + 安全解析器 + 只解析可达 rels + 比例阈值修正；加 11 KB 级回归测试（含孤立 rels 与 `[Content_Types].xml` 两条路径）。
2. 验证方式：同一载荷下 `validate_docx` 在解析前拒绝（或至少不展开），`parse_docx`/`ingest` 不成功；内存峰值接近输入大小，而不是数百 MB。

**应在合并前修复（P1）**

3. SEC-DOCX-02：主文档解析目标与扫描集合统一；`.bin` 绕过测试。
4. SEC-DOCX-03：Content-Length 预检 + 流式上限；chunked 测试。
5. SEC-DOCX-04：临时文件/内容寻址 + 失败清理 + 配额/限流；raw 生命周期测试。
6. SEC-DOCX-05：统一 ZIP 读取异常 → `InputError`；API 不泄露异常文本；CLI 退出码 3 测试。

**建议同批处理（P2）**

7. SEC-DOCX-06/07/08/09/10/11/12/13：见上表；其中 06（路径泄露/500）、07（ZIP 层放大）、08（静默丢内容）、09（截断不可审计）优先。

**放行结论**：当前快照不满足 `docs/reviews/2026-10-29-docx-gap-review.md` 第 6 节“独立审计报告中的 P0 全部关闭；P1 有明确处置”的验收标准。P0 关闭且 P1 修复/有书面处置后，可进入复验。

---

## 7. 审计声明

- **仅审计，未修改代码/测试/配置/数据。** 除本报告文件外，仓库内没有任何文件被创建或修改；未 commit/push。
- 所有恶意载荷均在系统临时目录构造并运行（本次为 `%TEMP%\docx-audit-DWf8Zc`），未写入仓库 `data/raw`、未触碰 `data/derived`；上传探针使用隔离的 `cfg.cache_dir`。
- 未读取 `.env`：脚本使用 `load_config(read_dotenv=False)`；CLI 运行设置 `DOCQA_DISABLE_DOTENV=1`。
- 未请求任何真实模型/OCR 端点；DOCX 路径本身不触网（已用代码与实测确认）。
- 运行了 `pytest tests/docx/test_security.py -q`（45 passed，离线）；`npm audit` 因当前 npm 镜像未实现 `/-/npm/v1/security/*`（404）无法完成，依赖审计仅限版本核对（python-docx 1.2.0/lxml 6.1.3，未发现 DOCX 相关已知 CVE）。
- 本报告中的“实测”数字来自本机（Windows 11、Python 3.12.14）；不同平台/版本的内存峰值会有差异，但量级与根因不变。

---

## 8. 附：P0 最小复现脚本要点

> 以下仅描述构造方式，不包含任何真实密钥或外部地址；在临时目录运行，不要指向仓库数据目录。

```python
# 1) 合法骨架
parts = {
    "[Content_Types].xml": valid_content_types(),   # /word/document.xml -> MAIN
    "_rels/.rels": office_document_rels("word/document.xml"),
    "word/document.xml": benign_document_xml("正常正文"),
}

# 2) 孤立 rels 炸弹：UTF-16LE，无 BOM；不被任何 part 引用
literal = "A" * 5_242_000
bomb = (
    '<?xml version="1.0" encoding="UTF-16"?>'
    '<!DOCTYPE Relationships ['
    f'<!ENTITY a0 "{literal}">'
    f'<!ENTITY a1 "{("&a0;" * 100)}">'
    ']>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<unused>&a1;</unused>'
    '<Relationship Id="rId1" Type=".../hyperlink" '
    'Target="https://example.invalid/x" TargetMode="External"/>'
    '</Relationships>'
).encode("utf-16-le")  # 关键：无 BOM

parts["word/_rels/orphan.xml.rels"] = bomb

# 3) 条目解压后必须 < 10 MiB（10,485,760），才能跳过压缩比检查
#    本报告实测：10,485,328 B，压缩 10,607 B，比例 988.5:1；整个 DOCX 约 11.6 KB。

# 4) 断言：
#    is_docx(data) is True
#    validate_docx(data) 通过，tracemalloc 峰值约 560 MB
#    parse_docx(data) 成功，blocks == ["正常正文"]
#    docqa ingest 退出码 0
```

**修复后的预期**：上述包应在任何 XML 解析之前被 `InputError` 拒绝；`tracemalloc` 峰值应接近输入大小；`parse_docx`/`ingest` 不得成功。建议把该脚本（缩小字面量以控制测试耗时）固化为回归测试，并同时覆盖 `[Content_Types].xml` 变体。

---

## 复核（2026-10-30）

> 本节只追加，不修改上面的原始 finding。复核对象是 `docs/reviews/2026-10-29-docx-triage.md` 中 A/B/C 已修复的安全/上传/解析代码；E 仍在收尾评估/验证文档（复核期间已更新 `docs/docx-support.md` 7.1–7.4，但 `docs/verification` 尚不存在）。

### R1. 复核范围、方法与命令

- 代码快照：当前工作区（A/B/C 修复后）。只读代码；未改仓库代码/测试/配置/数据；未读 `.env`；未请求真实模型/OCR 端点；未 commit/push。
- 载荷：全部在系统临时目录 `%TEMP%\docx-recheck-3jCGCS` 构造；上传探针使用隔离 `cfg.cache_dir`，不写仓库 `data/raw`。
- 指定测试命令：`uv run --no-sync pytest tests/docx/test_security.py tests/test_cli_api.py -q`。
- 额外复现：独立 Python 探针（validate/parse/upload）、`docqa ingest` CLI、`tracemalloc` 峰值、direct `_read_upload_body` 流式计数、400 次确定性变异 fuzz。
- 关键结论：**SEC-DOCX-01/02/03/04/05 全部 closed；没有新的 P0/P1。** 但发现若干新的 P2/P3（见 R4），且指定测试命令存在顺序相关的测试隔离失败（见 R6），E 的用户文档尚未更新（见 R5）。

### R2. P0/P1 状态表

| ID | 原严重度 | 复核状态 | 证据（本机实测） |
|---|---|---|---|
| SEC-DOCX-01 | P0 | **CLOSED** | 原 11,619 B / 10,485,328 B 解压 / 10,607 B 压缩（989:1）孤立 rels 载荷：`validate_docx` **InputError**，`tracemalloc` 峰值 **0.0 MB**，`elapsed 0.0 s`；API 400、raw 为空；CLI 退出 **3**，无堆栈。另用 2,001,558 B、压缩比约 1 的无 BOM UTF-16LE 孤立 rels（绕过比例门限）验证编码层：**InputError**，峰值 **3.19 MB**，未进入 XML 解析。 |
| SEC-DOCX-02 | P1 | **CLOSED** | `word/document.bin` 冒充主文档（主 Content-Type + 根 rels 指向 `.bin` + 诱饵 `document.xml`）、`word/styles.bin`（XML 类型）、根 rels target `word/other.xml` 三类载荷全部 **InputError**；`parse_docx` 同样拒绝。`.bin` 由 `_reject_unscanned_xml_parts` 拒绝；root target 由 `_require_office_document_target` 拒绝。 |
| SEC-DOCX-03 | P1 | **CLOSED** | `Content-Length=5000`、上限 1024：**413 且 `stream_calls=0`**（一个字节未读）；无长度/撒谎长度（6+6+6，上限 10）：**413，`chunks_pulled=2`**（读到第二块即停）；限内返回完整字节。`_read_upload_body` 使用 `request.stream()`，代码中无 `await request.body()`。 |
| SEC-DOCX-04 | P1 | **CLOSED** | 深校验失败（缺 `[Content_Types].xml`）后 raw **无残留**；同名不同内容得到两个内容哈希名（`same-3502c99b.docx` / `same-d2c1cbc5.docx`）；同内容重复上传只有 1 份；`foo:bar.docx` → `foobar-<sha8>.docx`，无 `foo` ADS；`CON.docx`/`NUL.docx` 400；并发上限 0 → **429**；非 DocqaError 的 500 只返回通用文案（不含 `boom`/绝对路径/`RuntimeError`/`zlib.error`），raw 为空。 |
| SEC-DOCX-05 | P1 | **CLOSED** | 损坏 DEFLATE：`is_docx=True`，`validate_docx` **InputError**；API **400**，detail 无临时目录/`InputError`/`Traceback`/`zlib.error`，raw 为空；CLI **退出 3**，Rich 错误面板，无 traceback/堆栈。 |

### R3. P0/P1 复现细节

**SEC-DOCX-01（原 P0）**

- 重新构造的报告等价载荷：11,619 B DOCX；`word/_rels/orphan.xml.rels` 为 UTF-16LE **无 BOM**、10,485,328 B 解压、10,607 B 压缩（989:1）；含 `<!DOCTYPE>` + 嵌套 `<!ENTITY>`；不被任何 part 引用。
- `validate_docx` 现在在 `_check_entries` 的压缩比检查处拒绝（`security.py:377-387`；阈值已改为 `>= 1 MiB` 且 `>200`），**在任何 XML 读取/解析之前**；峰值 0.0 MB，CLI 退出 3。
- 为排除“只是比例检查挡住了”的误判，另构造压缩比约 1 的 2,001,558 B 无 BOM UTF-16LE 孤立 rels（含 `<?xml ... UTF-16` 与随机不可压缩内容）：`_scan_xml_part` 在首块 `_strip_or_reject_encoding` 处拒绝（`security.py:514-530`），峰值 3.19 MB，未进入 `_parse_xml`。content-types 变体（100k 字面量）同样在编码层拒绝，峰值 0.61 MB。
- 结论：原 P0 的“无 BOM UTF-16 + 实体链放大”路径已关闭；既没有 ~560 MB 峰值，也没有完整 ingest 成功。`_parse_xml` 已改为 lxml 安全解析器（`resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False`，`security.py:548-568`），且 `_count_external_relationships` 只用它解析（`:688-703`）。

**SEC-DOCX-02（原 P1）**

- `word/document.bin` 主文档：`[Content_Types].xml` 同时给 `/word/document.xml` 与 `/word/document.bin` 声明 MAIN，根 rels Target 指向 `.bin`，`.bin` 含 DTD；`validate_docx` → `InputError: DOCX 部件 word/document.bin 声明为 XML 内容...但扩展名不是 .xml/.rels`。
- `word/styles.bin`：Override 为 `application/xml`；同样被 `_reject_unscanned_xml_parts` 拒绝。
- 根 rels target `word/other.xml`：`InputError: DOCX 的主文档目标不是 word/document.xml，而是 'word/other.xml'`。
- 合法 `./word/document.xml`（normpath 后等于 `word/document.xml`）仍被接受，未见兼容性回归。
- 未发现新的 `.bin`/Content-Type 参数/Default Extension 绕过：`_is_xml_content_type` 覆盖 `+xml`、`application/xml`、`text/xml`、`*/xml`；`_require_office_document_target` 强制 target 规范化后为 `word/document.xml`。带参数或非 XML 的 `.bin` 不会被 python-docx 当作 XML part 解析（PartFactory 按精确 Content-Type 选类）。

**SEC-DOCX-03（原 P1）**

- `app.py:105-137` 的 `_read_upload_body` 先查 `Content-Length`，超限立即 413，不调用 `stream()`；否则 `async for chunk in request.stream()`，先判断 `len(buffer)+len(chunk) > MAX_UPLOAD_BYTES` 再扩展。
- 实测：声明 5000 / 上限 1024 → 413，`stream_calls=0`；无长度或声明撒谎（6+6+6 / 上限 10）→ 413，`chunks_pulled=2`；限内 3+3 → `b"abcdef"`；非法 `Content-Length` 字符串会退回流式读取。
- API 级 `MAX_UPLOAD_BYTES=1024` + 5000 字节 body → 413，`raw/` 不存在；测试固化了这两个行为。
- 注意：`bytearray` 累计后 `bytes(buffer)` 仍有一次复制，峰值约为上限的 2 倍；`MAX_UPLOAD_BYTES=200 MB` 仍是残余 P2（见 R5）。

**SEC-DOCX-04（原 P1）**

- 深校验失败：400，`raw/` 为空（`_discard_unverified` 只在本次新建时删除，`app.py:517-524`）。
- 同名不同内容：`same-<sha8>.docx` 两个文件，`doc_id` 不同；同内容重复上传只 1 份。
- `foo:bar.docx`：`stored_name=foobar-<sha8>.docx`，raw 只有该文件，无 `foo` 基文件/ADS；`a|b.docx` 也 200 并净化为 `ab-...`；`CON.docx`/`NUL.docx` 400。
- 并发：`MAX_CONCURRENT_UPLOADS=0` 时 429；生产为 1。
- 500：monkeypatch `pipeline.ingest` 抛 `RuntimeError("boom at C:\Users\secret\derived\x.docx zlib.error")`，响应 500 仅 `解析失败：服务端内部错误，详情见服务端日志`，不含 `boom`/路径/异常类；raw 为空。
- 损坏 DEFLATE：400；raw 为空；detail 无路径/类名/堆栈。
- **但** 新发现一个本地符号链接问题（R4-RECHECK-01），不属于原 P1 验收项，故 SEC-DOCX-04 仍判 closed、另记 P2。

**SEC-DOCX-05（原 P1）**

- `validate_docx` 在 `_inspect_archive` 外层统一捕获 `_ZIP_ERRORS`（含 `zlib.error`、`struct.error`、`EOFError`、`OverflowError`、`ValueError`、`RuntimeError`、`NotImplementedError`、`BadZipFile`、`OSError`），`security.py:185-203`；`_scan_xml_part`/`_read_member` 也各自捕获（`:505-510`、`:719-725`）。
- API：400，无 `zlib.error`/`Traceback`/路径；CLI：退出码 3，Rich 面板展示 `读取 DOCX 部件失败：[Content_Types].xml（error: Error -3 ...）`，无堆栈。
- 400 消息仍含底层 `Error -3 ...` 文本（不是类名/路径），属于可读性/信息最小化的小问题，非安全阻断。
- 400 次确定性变异 fuzz（content-types/rels/document.xml 随机字节/截断/非法 encoding 声明）：`validate_docx` 无任何非 `InputError` 逃逸，`is_docx` 无异常逃逸。

### R4. 新发现（复核新增；均为 P2/P3）

| ID | 严重度 | 文件:行 | 问题 | 证据 | 建议 |
|---|---|---|---|---|---|
| RECHECK-01 | **P2（本地）** | `src/docqa/api/app.py:496-503,517-524` | 上传仍直接写最终内容哈希路径；若该路径已被预置为符号链接，`Path.write_bytes` 会**跟随符号链接**，把上传字节写到 raw 目录之外的目标；`preexisting=True` 还会让失败清理跳过。 | Windows 实测：在 `raw/link-<sha8>.docx` 预置指向 `victim.txt` 的 symlink，上传同名内容后 200；`victim.txt` 从 `ORIGINAL` 变为 `PK\x03\x04...`（DOCX 字节），symlink 仍在。 | 写 `raw/.tmp-<random>` + `os.replace(temp, target)`（替换链接本身而非目标）；打开时 `O_CREAT\|O_EXCL\|O_NOFOLLOW`，`lstat` 校验普通文件；`preexisting` 若存在则校验完整 sha256 + 普通文件，否则拒绝/替换。 |
| RECHECK-02 | **P2** | `src/docqa/api/app.py:496-497`；`src/docqa/pipeline.py:79-80,597` | 内容哈希名/`doc_id` 只用 `sha256[:8]`（32 bit）。对 SHA-256 前 32 bit 的碰撞约 2^32 次计算，动机充分的攻击者可构造两份不同文档共享同一存储名/派生目录，覆盖源文件与证据链；`preexisting` 不校验完整哈希。 | 代码审查：`stored_name=f"{safe_stem}-{digest[:8]}{suffix}"`；`doc_id=f"{path.stem}-{sha[:8]}"`；`preexisting=target.exists()` 后直接写。 | 用完整 sha256（或至少 128 bit）作为存储名/`doc_id`；命中已存在文件时校验完整 sha256 且 `lstat` 为普通文件；加碰撞/复用测试。 |
| RECHECK-03 | **P3** | `src/docqa/docx/security.py:325-335,338-357` | `ZipInfo.filename` 会被 `zipfile._sanitize_filename` 在 NUL 处截断、在 Windows 把反斜杠替换成 `/`；`_check_names` 只看 `filename`，看不到原始 `orig_filename`。因此“NUL/反斜杠条目拒绝”在端到端包上并未真正生效（当前无解压，故无直接利用）。 | 生成 `evil\x00.xml` 条目：`info.filename='evil'`，`info.orig_filename='evil\x00.xml'`；`validate_docx` **通过**（4 entries），仅函数级 `check_entry_path` 测试通过。Windows 反斜杠条目同理会被 sanitize。 | `_check_names` 同时检查 `info.orig_filename`（或直接检查中央目录原始文件名字节）；补端到端 NUL/反斜杠包测试。 |
| RECHECK-04 | **P2（有界）** | `src/docqa/docx/security.py:424-443,470`；`:577,697` | 元数据 `.rels`/content-types 的读/解析上限是 8 MiB，但 `_scan_xml_part` 仍按 `MAX_ENTRY_UNCOMPRESSED`（100 MiB）流式扫描；一个 100 MiB `.rels`（压缩比 ≤200，压缩后约 0.5 MB）会先被完整读取/扫描，之后才在 `_read_member` 的 8 MiB 处拒绝。 | 7 MiB 随机 orphan `.rels`：通过，峰值 20.0 MB；9 MiB：`InputError: DOCX 部件解压后超过上限`，峰值 22.8 MB（扫描已完成）。100 MiB 情形按代码推算峰值约 100 MB。 | 对 `.rels`/content-types 在 `_scan_xml_part` 前检查 `info.file_size > MAX_METADATA_XML_BYTES` 并直接拒绝，或把扫描上限按元数据类别设为 8 MiB；补 9 MiB/边界测试。 |
| RECHECK-05 | **P2（文档，残余）** | `docs/docx-support.md:302-370`；`docs/verification/2026-10-29-docx-phase1.md`（不存在） | 复核期间 E 已更新安全文档：隐藏文字改为“跳过并计数”（`:26,:285,:336`）、压缩比 `≥1 MiB 且 >200`（`:317-319`）、XML 只接受 UTF-8（`:326-328`）、多卷/符号链接/NUL（`:309-316`）、8 MiB 元数据读取（`:320`）、200 MB/同步 ingest/孤立 rels/可达性/多 EOCD 残余（`:345,:362-367`）。**仍存在的文档缺口**：`docs/verification/...` 不存在；`:314` 声称 NUL/控制字符条目名拒绝，但端到端 `ZipFile.filename` 截断会绕过（RECHECK-03）；`:320` 声称元数据实际读取 8 MiB，但 `_scan_xml_part` 仍可读 100 MiB（RECHECK-04）；`:346` 只写“sha256 前 8 位”，未提示碰撞风险（RECHECK-02）；上传符号链接跟随（RECHECK-01）与注入截断字段（原 SEC-DOCX-09）未记录。 | 逐条对照代码与 `docs/docx-support.md` 7.1–7.4；验证文档不存在。 | E 生成验证文档；修正 NUL/8 MiB/hash8/symlink/injection 的文档表述，或先修对应代码；不要把未验证的保证写成已完成。 |
| RECHECK-06 | **P2（测试基础设施，非产品安全）** | `src/docqa/logging.py:61-75`；`tests/test_cli_api.py:853,875` | 指定测试命令存在顺序相关的测试隔离失败：logging 的 `StreamHandler(sys.stderr)` 在 setup 时绑定当时的捕获流，后续捕获流关闭后 handler 仍写入，触发 `ValueError: I/O operation on closed file`；logging 错误输出被 `CliRunner` 收进 `r.output`，污染 eval 的 JSON，导致 `json.loads` 失败。 | 首轮 `pytest tests/docx/test_security.py tests/test_cli_api.py -q` 通过；复跑出现 2 个 `TestDocxEvalCli` 失败，traceback 明确是 `ValueError: I/O operation on closed file`；两用例单独运行通过；`-s` 运行 exit 0（但仍有 logging error 文本）。 | 测试侧每个用例重置 `docqa.logging` handlers；或 `logging.setup` 使用动态 `sys.stderr` 代理/每次 emit 解析当前流；或 eval CLI 测试用独立 logger/capsys；这不影响生产 stderr 稳定场景。 |

> 说明：RECHECK-06 不是安全回归，也不影响 P0/P1 判定；但它使“指定测试命令全绿”这一放行门槛在当前快照无法稳定满足，必须记录。

### R5. 残余 P2 的处置状态（逐项回答）

| 残余项 | 当前实际行为 | 可利用性/严重度 | 文档状态 | 结论/修法 |
|---|---|---|---|---|
| 孤立 rels 仍会被“安全解析”（8 MiB/500 MiB 上限） | `_scan_xml_part` 扫描所有 `.rels`（含孤儿，UTF-8 编码/DTD 检查）；`_count_external_relationships` 用 lxml 安全解析器读每个 `.rels`，`_read_member` 限 8 MiB；总解压限 500 MiB。实测 7 MiB 随机 orphan `.rels` 通过（峰值 20.0 MB），9 MiB 在 8 MiB 处拒绝（峰值 22.8 MB）。**注意**：扫描阶段仍按 100 MiB/条目读取（RECHECK-04），不是 8 MiB。 | 无实体展开/无网络/无代码执行；有界内存（当前实际最大约 100 MB/条目），P2 可用性。 | triage `:48` 已记录；`docs/docx-support.md` 7.4 `:364` 已记录；8 MiB 与 100 MiB 扫描不一致仍未记录（RECHECK-04）。 | 安全上可接受 Phase 1；建议把元数据扫描上限改为 8 MiB 并在用户文档写明。 |
| 未做关系图可达性裁剪 | `_count_external_relationships` 遍历 **所有** `*.rels`，不判断该 rels 是否被任何 part 引用；孤儿 rels 也会被计数/解析。 | 无网络（只计数）；有界 CPU/内存；攻击者可让 manifest 的 `external_relationships` 包含孤儿文件的关系（信息计数偏差），P3。 | triage `:48` 已记录；`docs/docx-support.md` 7.4 `:365` 已记录。 | Phase 1 可接受；若要严格，按 PackageReader 的关系图只解析可达 rels，或明确“计数包含孤儿 rels”。 |
| 多 EOCD 未单独检测 | `_find_eocd` 找最后一个 comment length 与文件末尾吻合的 EOCD；校验器与 Python `zipfile` 都取最后一个；未统计 EOCD 数量。 | 未发现 in-app differential（两者取同一个）；跨工具 zip confusion 风险，P3。 | triage `:48` 已记录；`docs/docx-support.md` 7.4 `:366` 已记录。 | 记录为跨工具残余风险；如需严格，统计/拒绝多 EOCD，或只接受 EOCD 紧邻中央目录且无多余尾随。 |
| `MAX_UPLOAD_BYTES` 仍 200 MB | `_read_upload_body` 限制累计读取；`bytearray` 后 `bytes()` 复制，峰值约 2×；随后 raw 写入、`_ingest_docx` `read_bytes`、python-docx blobs/DOM 还有复制；上传并发槽 1。 | 单请求可到数百 MB；多进程/多 worker 时每进程一份；默认 loopback 时本地 DoS，非 loopback 时远程 DoS，P2（可用性）。 | triage `:49` 已记录“仍 200 MB”；`docs/docx-support.md` 7.2 `:345` / 7.4 `:367` 已记录。 | 降到可配置的 20–50 MB（扫描件另配）；或流式落临时文件后校验；docs 写明。 |
| ingest 仍同步跑在事件循环里 | `api/app.py:531-535` 在 async `_upload_impl` 中直接调用同步 `run_ingest`；大文档解析期间 `/api/status` 等请求停住。 | 单次/大文档可用性；叠加 200 MB 时更明显，P2。 | triage `:49` 已记录“同步”；`docs/docx-support.md` 7.4 `:367` 已记录。 | `await run_in_threadpool(run_ingest, ...)`/`asyncio.to_thread`；或独立 worker；加并发/超时测试。 |
| `is_docx` 仍先构造 `ZipFile`/`infolist` | `is_docx` 在 `MAX_ENTRIES` 判断前构造 `ZipFile` 并物化中央目录；200 MB 上传仍可能先分配大量 `ZipInfo`。 | 有界 200 MB；P2/P3 可用性。 | triage 未单列；用户文档未记录。 | 降低上传上限；或用 EOCD 条目数字段做预检；补多条目测试。 |
| 注入扫描截断（原 SEC-DOCX-09） | `InjectionReport` 仍无 `truncated/hits_total`；`_scan_docx_injection` 仍在 `scan_text` 返回后才截到 20；`describe()` 无生产调用；`on_injection` 仍只在 docstring。 | 恶意长文档可放大内存/manifest；截断不可审计；P2（非 P0/P1）。 | 原始报告已列；`docs/docx-support.md:336` 只写“逐块 scan_text/命中写 manifest”，未记录截断字段；triage `:27` 曾分派 B/leader，但当前代码未改。 | 若本轮不修，必须写入“已知残余 P2”；修法见原 SEC-DOCX-09。 |

### R6. 测试结果（含指定命令的真实结果）

- `uv run --no-sync pytest tests/docx/test_security.py tests/test_cli_api.py -q`：
  - **首轮**：全部通过（安全 + 上传 + eval 用例）。
  - **复跑同一命令**：2 个失败，均为 `TestDocxEvalCli`（`test_eval_docx_custom_parseable_template_roundtrips`、`test_eval_docx_noncanonical_template_fails_the_gate`）；traceback 是 logging handler 向已关闭的 `sys.stderr` 写入：`ValueError: I/O operation on closed file`，错误文本被 `CliRunner` 收进 `r.output`，随后 `json.loads` 失败。**不是安全逻辑失败**；两个用例单独运行通过；`-s` 运行 exit 0（仍有 logging error 文本）。详见 RECHECK-06。
- 安全/上传相关用例全部通过；未发现 P0/P1 相关失败。
- 400 次确定性变异 fuzz：`validate_docx` 无非 `InputError` 逃逸，`is_docx` 无异常逃逸。
- CLI：原 P0 载荷退出 3（比例拒绝）；损坏 DEFLATE 退出 3（无 traceback）；API：原 P0 400、raw 空；损坏 DEFLATE 400、raw 空。
- 未运行全量 `tests`（超出本次指定范围）；E 的 eval/文档仍在收尾，失败的两个 eval 用例属于该区域。

### R7. 放行结论

- **P0/P1：5/5 closed，没有新的 P0/P1。** 原始 P0 的 560 MB 实体扩展、`.bin` 解析目标不一致、上传全量缓冲、raw 残留/覆盖/ADS、`zlib.error` 逃逸均已通过独立载荷复测关闭。
- **但不是无条件放行**：当前快照仍有以下放行前事项：
  1. E 完成验证文档；`docs/docx-support.md` 7.1–7.4 已在复核期间更新，但 `docs/verification/2026-10-29-docx-phase1.md` 仍不存在，且 RECHECK-01/02/03/04 对应的文档缺口（symlink、hash8、NUL 端到端、8 MiB/100 MiB 扫描）需修正或修代码（RECHECK-05）。
  2. 指定测试命令存在顺序相关的 logging/测试隔离失败；必须修复或稳定化（RECHECK-06），否则“全绿”门槛不成立。
  3. 新 P2/P3 需处置或明确风险接受：RECHECK-01（symlink 跟随，建议修）、RECHECK-02（8 位哈希，建议修）、RECHECK-04（元数据扫描 100 MiB，建议修/写清）、RECHECK-03（NUL/反斜杠 orig_filename，建议修或记录）；原 SEC-DOCX-09 注入截断 P2 仍在（若本轮不修，写清残余）。
  4. `docs/docx-support.md` 7.4 已记录 200 MB/同步 ingest/孤立 rels/多 EOCD/可达性；但 NUL 端到端、8 MiB/100 MiB 扫描、hash8、symlink 跟随、注入截断字段仍需写入或修复，否则读者会误以为这些已被完全消除。
- **判断**：安全代码层面 P0/P1 已关闭，可以进入最终验证/条件放行；在上述文档、测试稳定性、P2 处置完成前，**不建议宣布最终放行**。

### R8. 复核声明

- **仅审计，未修改代码/测试/配置/数据。** 本次只在报告末尾追加本节；没有改原有 finding；没有 commit/push。
- 所有载荷在 `%TEMP%\docx-recheck-3jCGCS` 构造；上传探针使用隔离 `cfg.cache_dir`，未写仓库 `data/raw`；未读 `.env`（`load_config(read_dotenv=False)` / `DOCQA_DISABLE_DOTENV=1`）；未请求真实模型/OCR 端点。
- 复核环境：Windows 11、Python 3.12.14、python-docx 1.2.0、lxml 6.1.3、Starlette 1.6.0；实测数字为该环境下的 `tracemalloc`/状态码/退出码，跨平台量级可能有差异。
