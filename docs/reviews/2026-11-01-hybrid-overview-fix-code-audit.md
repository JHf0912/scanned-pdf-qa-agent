# T1 hybrid `doc_sentence_support` 句子级引用覆盖 —— 代码审计

- 日期：2026-11-01
- 审计对象：T1「句末引用覆盖同句子句」修复
- 契约：`docs/reviews/2026-11-01-hybrid-overview-refusal-diagnosis.md` §3
- 结论：**有条件通过**（0 P0 / 0 P1 / 7 P2）
- 说明：本报告由 code-reviewer 审计 agent 产出，leader 按 hand-back 原文落盘；审计期间未改源码/测试。

## 1. 审计范围与方法

### 1.1 范围

- `src/docqa/qa/verify.py:677-735`（`_LEADING_CITATION_RE`、`_CLAUSE_SPLIT_RE`、`_SENTENCE_BOUNDARY_CHARS`、`_TRAILING_MARKS_RE`、`_split_doc_clauses`、`_trailing_marks`）
- `src/docqa/qa/verify.py:847-958`（`check_doc_sentence_support`：`sentence_tail_marks` 构建与借用逻辑）
- `tests/test_hybrid_mode.py:858-1000`（`TestDocSentenceSupportCitationAttachment`，6 个方法、参数化后 11 个用例）
- `docs/hybrid-mode.md` §6 规则说明

### 1.2 范围界定方法（重要）

整个 hybrid 功能相对 HEAD 都是未提交改动（`git show HEAD:src/docqa/qa/verify.py` 只有约 500 行、不含任何 hybrid 检查），因此 `git diff` 无法隔离 T1。本次以契约 §1.3/§3.1 的精确位置 + 文件 mtime 界定 T1：

- `src/docqa/qa/verify.py` 2026-11-01 19:52:10
- `tests/test_hybrid_mode.py` 2026-11-01 19:52:34
- `docs/hybrid-mode.md` 2026-11-01 19:52:54
- `src/docqa/qa/answer.py` 2026-10-31 22:14（未动，提示词未改）；`api/app.py`、`cli.py`、`web/` 均早于 19:40（未动）

### 1.3 跑过的命令与结果（只读）

| 命令 | 结果 |
|---|---|
| `uv run pytest tests/test_hybrid_mode.py -o addopts="" -q` | 179 passed in 1.08s（168 旧 + 11 新） |
| `uv run pytest tests -o addopts="" -q` | 1015 passed, 4 skipped, 9 warnings in 38.93s |
| `uv run ruff check .` | All checks passed! |
| `uv run pytest tests/test_hybrid_mode.py tests/test_verify.py -o addopts="" -q --cov=docqa.qa.verify` | 217 passed；`verify.py` 392 stmts / 20 miss / 95%；新借用分支已覆盖，仅 903（纯引用子句 continue）未覆盖 |
| 旧实现模拟（pytest plugin，job tmp） | 11 个新用例中 **5 failed / 6 passed**（168 deselected） |
| 边界探针（直接调用 `check_doc_sentence_support`，26 组） | 见 §3/§7，无崩溃，行为与代码阅读一致 |

旧实现模拟方式：临时 plugin 把 `_CLAUSE_SPLIT_RE` 还原为不含 `!?` 的旧字符类，并把 `_split_doc_clauses` 包装成「每个子句各占一个句子编号」，等价于「每个子句必须自带引用」；未改仓库文件。

## 2. 结论与契约核对

**结论：有条件通过。** 修复真实解决了上报场景，且未放宽逐字门禁。

契约 §3.1 逐条核对：

| 契约要求 | 实现 | 结论 |
|---|---|---|
| `_split_doc_clauses` 每个子句附带句子编号 | `verify.py:700-727` 返回 `list[tuple[str,int]]` | 符合 |
| 句子边界 `。！？!?` 与 `\n` | `_SENTENCE_BOUNDARY_CHARS` `verify.py:692` | 符合 |
| 逗号/顿号/冒号/分号仍切子句、不另起句 | `_CLAUSE_SPLIT_RE` `verify.py:687` + 循环仅按 `_SENTENCE_BOUNDARY_CHARS` 自增 | 符合 |
| own-mark 优先 | `verify.py:907-911` 先 `findall`，为空才借用 | 符合（探针 `A [p.2]...，B [p.1]` 用例通过） |
| 只借同一句最后一个子句的尾部标记 | `verify.py:894-896` 建 dict，`911` 借用 | 符合（但见 F4 的语义边角） |
| 不跨句借用 | 句子编号严格递增；6 个终止符参数化用例 + 探针 | 符合 |
| 借用后仍走 `_verbatim_match` | 借用只改 `marks`，后续同一代码路径 `verify.py:915-938` | 符合（探针：`通常采用调质处理，…… [m]` 借到标记后仍报「不是所引证据的逐字内容」） |
| `is_generic_negative` 跳过、`allow_zero_citations`、offender/detail 文案不变 | 代码路径与文案与诊断报告 §1.3 的旧输出逐字一致 | 符合 |
| 提示词不改、不新增端点、不改 API/CLI/前端 | `answer.py` mtime 未变；strict prompt 哈希测试通过；无其它 T1 文件 | 符合 |

用户原句（`本文件规定了演示样件的技术要求与试验方法，用于验证 DOCX 原生文本的解析链路 [§1 ¶2]。`）实测 `passed=True`，detail「2 个肯定子句均为所引证据的逐字内容」；句号后引用（`……。[§1 ¶2]`）同样通过。

## 3. Findings

| ID | 严重度 | file:line | 问题 | 失败场景（均实测） | 建议 |
|---|---|---|---|---|---|
| F1 | P2 | `README.md:233` | 仍写「GATE `doc_sentence_support` 要求每个子句独立引用」，与新语义相反 | 读者按 README 理解，会以为 `A，B [§1 ¶2]。` 仍应被拒 | 改为「按句覆盖：句末引用覆盖同句子句；每个被覆盖子句仍须逐字可证」 |
| F2 | P2 | `docs/design.md:163-166` | 仍写「按子句粒度……每个肯定子句都必须独立引用」，且分隔符列表缺 `!?` | 设计文档与代码/`hybrid-mode.md` 不一致；后续维护者按旧语义改代码 | 同步 `docs/hybrid-mode.md` §6 的表述与字符集 |
| F3 | P2 | `verify.py:695-697`、`730-735` | `_TRAILING_MARKS_RE` 只认「标记恰好是子句最后 token（允许空白）」。括号/引号包裹后即失配 | `A，B（[§1 ¶2]）。` → A 报「没有引用标记」，整条 False；`A，B [§1 ¶2]）。`、`……[§1 ¶2]”` 同类；`A，B [§1 ¶2].`（ASCII 句点）同样失败 | 允许 1-2 个闭合包裹字符（`）) ”」』】`）出现在尾部标记之后；或明确写入 §7 已知误杀并补测试。注意 ASCII `.` 同时不在 `_VERBATIM_BOUNDARY_CHARS`，属既有归一化缺口，与本条叠加 |
| F4 | P2 | `verify.py:894-896`、`908-912` | 借用源固定是「同句最后一个子句」；若该子句是被跳过的通用否定且无尾部标记，整句失去借用源 | `A，B [§1 ¶2]，文档中未规定。` → A、B 均报「没有引用标记」（False），尽管同句存在可绑定引用、两条子句都逐字 | 借用源改为「同句最后一个**带尾部标记**的子句」；若坚持契约字面语义，至少在 §6/§7 写明并补测试 |
| F5 | P2 | `verify.py:908-912` | 仅句末尾部引用可被借用；句首/句中引用不覆盖后续逗号子句（属契约冻结范围，非违规） | `A [§1 ¶2]，B。`、`[§1 ¶2]A，B。`、`A，[§1 ¶2]，B。` 均仍拒 B（False） | 记为已知限制（与原始误杀同族）；后续若要覆盖，可把「同句唯一的标记」也作为候选，或写入 §7 |
| F6 | P2 | `verify.py:687`、`692` | `_CLAUSE_SPLIT_RE` 加入 ASCII `!?` 后，句中 ASCII `!`/`?`（阶乘 `n!`、混排「注意!」）被当作句子边界，原本通过的单子句被切后判「没有引用标记」 | `计算 n! 的值应符合规定 [p.1 §3.1]。`：旧实现（模拟）True，新实现 False，offender「子句「计算 n」没有引用标记」 | 契约明确要求 `!?` 为句边界，属预期内的严格化；建议只在「`!?` 后随空白/换行/结束」时切句（需改契约），或在 §7 写明该误杀并补测试 |
| F7 | P2 | `tests/test_hybrid_mode.py:858-1000` | 边界用例缺口（不影响当前正确性，但不足以锁住语义） | 连续分隔符、句号后引用+多逗号子句、括号包裹尾引用、多标记尾、通用否定尾、句首/句中引用、`_split_doc_clauses` 形状均无测试 | 见 §7 清单，建议优先补 1/3/4/5/7 |

非阻塞观察（P3）：`check_doc_sentence_support` 约 111 行（T1 只新增约 10 行）；可把 `sentence_tail_marks` 的构建与借用抽成 `_sentence_tail_marks()`，把逐字校验抽成 `_check_clause_verbatim()`，便于后续加测试。不影响本次结论。

## 4. strict 回归结论

**无回归，零暴露。**

1. `check_sentence_support`（strict advisory）在 `verify.py:544` 用自己的 `re.split(r"[。！？；\n]", answer)`，不读 `_CLAUSE_SPLIT_RE`/`_SENTENCE_BOUNDARY_CHARS`/`_split_doc_clauses`。
2. `_split_doc_clauses` 全仓库唯一调用点是 `verify.py:892`，且 `check_doc_sentence_support` 只在 `verify()` 的 `answer_mode == "hybrid"` 分支追加（`verify.py:1143-1149`）。grep 全仓库无其它调用点，签名改动无遗漏。
3. strict 4 项检查（citation_validity / numeric_grounding / clause_consistency / sentence_support）代码路径未动；`SYSTEM_PROMPT`/`USER_TEMPLATE` 哈希钉在 `tests/test_hybrid_mode.py:61-62`，`TestStrictPromptFrozen`、`TestStrictVerifyUnchanged` 全过，全量 1015 passed。
4. 旧实现模拟中 6 个新用例在旧代码上即通过，说明它们起到回归护栏作用而非改写旧行为。

## 5. 测试真实性评估

**真实性合格，非「跟着实现写」。**

- 旧实现模拟：11 个新用例中 **5 failed / 6 passed、168 deselected**。失败的是契约要求的三条核心用例（用户原句、多逗号共享尾引用、非逐字子句被点名）+ `!`/`?` 两个句子边界参数；这正是 T1 的净新增行为。6 个在旧实现上通过的用例是回归护栏（跨句不借用、own-mark 优先等），符合契约用例 4/5/6 的定位。
- 断言对象是行为（`chk.passed`、offender 内容、gate 汇总），没有断言私有函数的内部形状；`"2 个肯定子句"` 之类的 detail 断言依赖的是契约冻结的文案（与诊断报告 §1.3 的旧输出逐字一致），可接受。
- 覆盖：`verify.py` 95%，借用分支（`908-912`）与 `_trailing_marks` 均被执行；新增代码块仅 `903`（纯引用子句 continue）未覆盖。
- 缺口：见 F7/§7。特别是没有 `_split_doc_clauses` 的句子编号直接用例——该函数是整个修复的地基，值得一组形状测试（尾引用、句号后引用、连续分隔符、跨段落、纯引用子句）。

## 6. 文档一致性

- `docs/hybrid-mode.md` §6（217-236 行）：与实现一致——按 `。！？!?；;，,：、\n` 切子句、引用归前子句、自身或同句末引用覆盖、不跨句、借用后仍须段边界逐字；句子边界的 `!?` 也已写明。§1/§3.1 的「逐句引用」与新语义相容。
- `README.md:233`：**旧表述**（每条子句独立引用）——见 F1。
- `docs/design.md:163-166`：**旧表述**且分隔符列表缺 `!?`——见 F2。
- `docs/verification/2026-10-31-hybrid-mode.md:14` 的「子句逐字+段边界」属历史验证报告，契约要求不改写历史审计（2026-10-31-*），保持原样合理。
- 契约 §3.4 要求的 `docs/verification/2026-11-01-hybrid-overview-fix.md` 尚未创建——按编排属 T4，T2 阶段不记为 finding。
- `docs/hybrid-mode.md` §7 已知误杀清单未包含 F3/F5/F6 三类残形，建议 T3/T4 一并补。

## 7. 未覆盖 / 建议补充的用例（当前实测行为）

| # | 用例 | 当前结果 | 建议 |
|---|---|---|---|
| 1 | `A，B [§1 ¶2]。[§1 ¶2]`（句号后引用 + 同句多逗号子句） | 通过 | 补测试（尾引用与「句号后引用」组合未覆盖） |
| 2 | `A，，B [§1 ¶2]。`；`。A [§1 ¶2]。`；`A。！B [§1 ¶2]。` | 通过（前导/连续分隔符安全） | 补 1-2 条 |
| 3 | `A，B（[§1 ¶2]）。` / `A，B [§1 ¶2]）。` | 失败（误杀，F3） | 决定语义后补测试 |
| 4 | `A，B [p.1 §3.1][p.2 §3.2]。`（多标记尾被同句借用） | 通过，按两 chunk 证据并集判定 | 补测试并明确这是预期（拼接片段符合 prompt「按原文顺序拼接相邻片段」） |
| 5 | `A，B [§1 ¶2]，文档中未规定。` | 失败（F4） | 决定「借用源」语义后补测试 |
| 6 | `[§1 ¶2]A，B。` / `A [§1 ¶2]，B。` | 失败（F5，契约冻结内） | 写入已知限制或后续扩展 |
| 7 | `计算 n! 的值应符合规定 [p.1 §3.1]。` | 失败（F6，旧实现通过） | 契约确认后补测试/文档 |
| 8 | `A，B [§1 ¶2].`（ASCII 句点） | 失败：A「没有引用标记」+ B「不是逐字」；`. ` 不在 `_VERBATIM_BOUNDARY_CHARS`，属既有归一化缺口 | 记为已知误杀或统一句点处理 |
| 9 | `_trailing_marks` / `_split_doc_clauses` 直接单测（句号后引用、连续分隔符、纯引用子句、CRLF、空/全分隔符答案） | 无直接测试（探针行为正常：空答案/全分隔符返回 passed=True、evaluated=0） | 补一组形状测试 |

## 8. 备注

- 审计未修改任何源码、测试、README、`.env`；未提交/推送/联网；未停止 8000/5173 服务；未触碰 `data/derived`。
- 探针与旧实现 plugin 位于 `C:/Users/26775/AppData/Local/Temp/docqa-t1-audit/`（仓库外，job tmp）。
- 本次 coverage 运行在仓库根生成的 `.coverage`（`.gitignore:43` 已忽略）已删除。
