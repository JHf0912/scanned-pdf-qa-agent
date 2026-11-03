# DOCX Phase 1 —— 审计发现分诊与修复派工

- 日期：2026-10-30
- 输入：
  - `docs/reviews/2026-10-29-docx-audit.md`（代码审计：P0=3、P1=5、P2=9）
  - `docs/reviews/2026-10-29-docx-security-audit.md`（安全审计：P0=1、P1=4、P2=8）
- 结论：**不可放行**；以下 P0/P1 修完并复测后才进入最终验证。
- 约束：审计 Agent 只审不改；修复按文件所有权分派；不提交、不推送。

## 1. 修复派工

| ID | 严重度 | 发现 | 归属 | 处置 |
|----|--------|------|------|------|
| SEC-DOCX-01 | P0 | 孤立 `.rels` + 无 BOM UTF-16LE 的 DTD/ENTITY，10,485,328 B 解压（988.5:1）绕过压缩比阈值，峰值约 560 MB | B | XML 只接受 UTF-8；解码后扫 DTD；lxml 安全解析器；扫描所有 XML part；压缩比 `>=` + 1 MiB 起算；加回归载荷 |
| CODE-01 | P0 | 长段落切成 b2/b3/b4 后都渲染 `[§1 ¶2]`，`parse_citations` 只落第一片，数值在后续片时被 `numeric_grounding` 误拒；`eval-docx` roundtrip 同源失败 | A/E | `Citation.evidence_chunk_ids` 收全部 in-range chunk；verify 用全部证据；`eval-docx` roundtrip/coverage 按 evidence/block_ids；长段落回归 |
| CODE-02 | P0 | 表格行/单元格被 `w:sdt` 包裹时线性化静默丢行/格（2×2 表丢 `抗压 ｜ 42 MPa`） | B | 表格内部容器感知遍历（`w:sdt/sdtContent`、`w:customXml`）；行/单元格级测试 |
| SEC-DOCX-02 / CODE-03 | P1/P0 | 根 rels 的 officeDocument target 指向 `word/document.bin`，扫描 `document.xml` 诱饵，解析对象不一致 | B | 解析根 rels，要求 target=`word/document.xml`；按 Content-Type/关系图扫描；拒绝 XML 类型但非 `.xml/.rels` 的 part；`.bin` 绕过测试 |
| SEC-DOCX-03 | P1 | `await request.body()` 先全量缓冲再查大小；无 Content-Length 预检 | C | Content-Length 预检；`request.stream()` 累计上限；超限立即 413；测试 |
| SEC-DOCX-04 | P1 | 深校验失败后 `raw/` 残留；同名覆盖破坏 manifest 证据链；Windows ADS 文件名；上传无限流 | C | 文件名净化 + 内容哈希后缀；失败 unlink；上传信号量 429；测试 |
| SEC-DOCX-05 | P1 | 损坏 DEFLATE 抛 `zlib.error`，API 500/CLI 堆栈；非 InputError 逃逸 | B/C | 校验入口统一转 `InputError`；API 500 不回显异常；CLI 退出 3；损坏包测试 |
| CODE-04 | P1 | `paragraph_coverage` 用文本子串，重复文本可掩盖丢块；`unit_count` 与 `docx_blocks` 行数不一致仍通过 | E | 以 `chunk.block_ids` 为成员依据 + 覆盖文本校验；`unit_count == 非空块数` 门禁 |
| CODE-05 | P1 | PDF 零引用答案的 `citation_page_ok` 从 False 变 None，错答被算对 | E | 三态：零引用=False；有引用无页=None；有页=交集 |
| CODE-06 | P1 | 正文段落自动编号（“第一条”/“3.6.1”）在 chunk 阶段被丢弃，证据文本无条号 | B | 至少把 block 自身 `clause_label` 前缀进文本；能作为条款起点则开新节；测试 |
| CODE-07 | P1 | 无样式兜底把 `42 MPa 是最小抗压强度。` 判成一级标题并重置章节栈 | B | 兜底要求短段落 + 条号模式 + 不以 `。！？；` 结尾；测试 |
| SEC-DOCX-06 | P2 | `altChunk`/sdt 包裹的表格/alternate content 不计数 | B | 计数或下钻；测试 |
| SEC-DOCX-07 | P2 | ZIP 多卷、符号链接、NUL 条目；`is_docx` 不查条目数 | B | 拒绝多卷/符号链接/NUL；条目上限 |
| SEC-DOCX-08 | P2 | 注入命中先扫描后截断，manifest 无 `truncated/hits_total` | B/leader | `InjectionReport` 暴露总数/截断；pipeline 落 manifest（若需 pipeline 改动由 leader 接） |
| SEC-DOCX-09 | P2 | 同步 ingest 阻塞事件循环；CLI 无文件大小预检 | C/leader | 文档记录；上传限流已覆盖部分；大文件预检 |
| SEC-DOCX-10 | P2 | Windows 文件名/ADS 导致 out_dir 失败/500 泄露绝对路径 | C | 文件名净化 + 内容哈希；错误不泄露路径 |
| SEC-DOCX-11 | P2 | 非 loopback 暴露无认证/限流 | leader/docs | 部署文档明确只绑 127.0.0.1；认证/限流不在 Phase 1 代码范围 |
| CODE-08 | P2 | 测试缺口：长段落问答/评测、sdt 表格、FALLBACK_PROFILE 过窄、PDF 零引用、样式级 numPr、eval-docx 失败退出码、unit_count 一致性、DOCX 对抗变异、mixed 前端状态 | A/B/C/E | 随修复补关键回归；未覆盖项写入 `docs/docx-support.md` |

## 2. 已决策

1. **隐藏文字**：冻结契约要求“丢弃并计数”；B 当前只计数但仍拼进正文，修为**跳过 `w:vanish` 子树**，保留 `hidden_runs` 计数；E 更新文档。
2. **长段落引用**：保持用户可见格式 `[§x ¶n]`；解析时把同一 `¶n` 的**全部 in-range chunk** 放进 `Citation.evidence_chunk_ids`，自检/评估按证据集判断；UI 主锚点仍用 `chunk_id`，不新增引用语法。
3. **覆盖口径**：每个非空 block 必须被某个 chunk 的 `block_ids` 覆盖；文本校验用“覆盖它的 chunk 文本拼接”，不再用任意 chunk 子串。
4. **PDF 零引用**：`citation_page_ok=False`（错答），不是 N/A；N/A 只用于“有引用但无页定位”的 DOCX/混合。
5. **上传落盘**：净化文件名 + 内容哈希后缀，避免 ADS/覆盖；失败删除；上传并发上限 1，超限 429；Content-Length/流式上限。
6. **Phase 2/3 仍不做**：表格 `gridSpan/vMerge` 网格、单元格级引用、图片 OCR、脚注/文本框/公式结构、Word 页码模拟、`.doc/.docm/.dotx/加密 DOCX`。
7. **不提交/推送**；`web/` 源码仍未跟踪，是否纳入提交由用户决定。

## 3. 修复进展（滚动更新）

| 时间 | 更新 |
|------|------|
| 2026-10-30 | A：CODE-01 证据集已修（`Citation.evidence_chunk_ids`；DOCX `[§x ¶n]` 收全部 in-range chunk；verify 按集合比对；158 passed）。全量 774 passed / 1 failed / 4 skipped，唯一失败是 E 的 `context_utilization` 旧口径，已分派 E。 |
| 2026-10-30 | B：SEC-DOCX-01/02/05 已关闭；隐藏文字改为丢弃+计数；sdt 表格行/单元格、正文条号、标题兜底、altChunk/AlternateContent 计数已修；`tests/docx` 280 passed、docx 覆盖率 92.41%、全量 793 passed/4 skipped。残余 P2：孤立 rels 仍会被安全解析（有 8 MiB/500 MiB 上限）、未做关系图可达性裁剪、多 EOCD 未单独检测。 |
| 2026-10-30 | C：SEC-DOCX-03/04/05 已关闭。`Content-Length` 预检 + `request.stream()` 边读边限；净化文件名 + 内容哈希存储名（无 ADS/覆盖）；失败 unlink；上传信号量 1（429）；500 不回显异常/路径；损坏 DEFLATE API 400/CLI 退出 3。全量 793 passed/4 skipped，覆盖率 90.69%。残留 P2：`MAX_UPLOAD_BYTES` 仍 200 MB；ingest 仍同步跑在事件循环里。 |
| 2026-10-30 | E：CODE-04/P0-1 的 coverage/roundtrip、CODE-05 PDF 零引用、`context_utilization` evidence 集、文档更新已完成；全量 798 passed/4 skipped，90.70%。 |
| 2026-10-30 | 安全复核：SEC-01/02/03/04/05 全部 CLOSED；新增 P2 已分派 B（RECHECK-03 ZIP NUL/反斜杠、RECHECK-04 元数据扫描上限）与 C（RECHECK-01 symlink、RECHECK-02 128-bit 哈希、RECHECK-06 测试隔离）。SEC-09 注入截断仍为 P2，写入残余风险。 |
| 2026-10-30 | E2E：1440/768/390 三视口 22 项全部 PASS（DOCX 上传/问答/`[§… ¶…]` 引用/证据/无横向滚动/无 console error）；期间发现并修复平板抽屉缺“文档概览”（D 补 `Overview`，bundle +113 B）。 |
| 2026-10-30 | B：RECHECK-03/04 已修（`orig_filename` NUL/反斜杠端到端拒绝；元数据 part 8 MiB 扫描前预检，9 MiB orphan rels 未扫描即拒；7 MiB 合法通过）；`tests/docx` 288 passed，全量 806 passed/4 skipped。 |
| 2026-10-30 | C：RECHECK-01/02/06 与 doc_id 重复哈希修复中；完成后全量需连跑两遍验证测试隔离。 |
| 2026-10-30 | 代码复核：CODE-01..07 全部 CLOSED，无新 P0/P1；PDF/旧产物/混合语料定向回归 120 passed；复核章节已追加到 `docs/reviews/2026-10-29-docx-audit.md`。新增 P2：R-01 `[§1 ¶99]` 越界段落扩证据、R-02 coverage 表格子串、R-03 无标题正文 structured=False、R-04 样式级 `w:vanish` 隐藏文字。R-01/R-04 已分派 A/B 修复；R-02/R-03 记录为残余 P2。 |
| 2026-10-30 | B：R-04 已修（直接 `w:vanish`、`w:rStyle` 字符样式及 basedOn 链、`w:pStyle` 段落样式链；run > 字符样式 > 段落样式；隐藏仍丢弃+计数）。`tests/docx` 291 passed，全量 814 passed/4 skipped。E 正在同步文档。A 的 R-01 修复中。 |
| 2026-10-30 | A：R-01 已修（`[§1 ¶99]` 越界段落不再扩到整节；空匹配让 `citation_validity` 拦截；`[§1 ¶2]` 仍收全部 in-range 证据）；A 范围 162 passed，全量 814 passed/4 skipped。 |
| 2026-10-30 | **最终验证完成**：`uv sync --all-extras`；全量 pytest 连跑两遍 **814 passed / 4 live skipped / 0 failed**；覆盖率 **91%**（6612/617）；`ruff check .` 全过；最终代码三视口 E2E **22/22 PASS**；DOCX ingest/ask/eval-docx 与 PDF fixture 回归通过。验证报告：`docs/verification/2026-10-29-docx-phase1.md`。R-02/R-03 与安全 P2 作为残余风险记录，不阻断 Phase 1。 |

## 4. 复测门槛

- `uv run --no-sync pytest tests -q` 全绿；`tests/docx` 覆盖率 ≥ 92%；全量覆盖率 ≥ 84%；ruff 干净。
- 安全回归载荷：11,623 B 实体链包在任何 XML 解析前 `InputError`；`.bin` 主文档绕过被拒；损坏 DEFLATE → 400/退出 3；上传超限不缓冲；失败无 `raw/` 残留；`foo:bar.docx` 无 ADS。
- 功能回归：长段落 `[§1 ¶2]` 数值在后续片时自检通过；`eval-docx` coverage/roundtrip 1.0；sdt 表格不丢行；隐藏文字不进正文；PDF 零引用仍判错；旧 PDF 产物回读不变。
- 最终报告：`docs/verification/2026-10-29-docx-phase1.md`（命令、结果、残余风险）。
