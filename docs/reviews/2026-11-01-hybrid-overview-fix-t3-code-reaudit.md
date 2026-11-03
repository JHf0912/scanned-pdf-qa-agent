# T3 delta 复测（hybrid `doc_sentence_support`：Unicode 换行 / 闭合包裹 / 借用源）

- 日期：2026-11-01
- 输入契约：`docs/reviews/2026-11-01-hybrid-overview-fix-triage.md` §1-§3
- 审计对象（T3 五个文件，mtime 20:21-20:32）：
  - `src/docqa/qa/verify.py`（`_CLAUSE_SPLIT_RE` / `_SENTENCE_BOUNDARY_CHARS` / `_TRAILING_CLOSERS` / `_TRAILING_MARKS_RE` / `_split_doc_clauses` / `sentence_tail_marks`）
  - `tests/test_hybrid_mode.py`（新增 48 个用例：`TestDocSentenceSupportCitationAttachment` 32、`TestSplitDocClausesShape` 16）
  - `README.md`、`docs/design.md`、`docs/hybrid-mode.md`
- 结论：**有条件通过**（0 P0 / 0 P1 / 4 P2）
- 说明：本报告由 code-reviewer 审计 agent 产出，leader 按 hand-back 原文落盘；审计期间未改源码/测试。

## 1. 方法与命令

### 1.1 范围界定

hybrid 整功能相对 HEAD 仍未提交，无法用 `git diff` 隔离 T3；以 triage §2 的三处契约修订 + 文件 mtime（verify.py 20:26、tests 20:29、README 20:21、hybrid-mode 20:22、design 20:32；`answer.py`/`agent.py`/API/CLI/前端均未动）界定。

### 1.2 只读验证结果

| 命令 | 结果 |
|---|---|
| `uv run pytest tests/test_hybrid_mode.py -o addopts="" -q` | **216 passed** in 3.24s（T3 前 179 + 37） |
| `uv run pytest tests -o addopts="" -q` | **1052 passed, 4 skipped**, 9 warnings in 73.28s |
| `uv run ruff check .` | **All checks passed!** |
| `uv run pytest tests/test_hybrid_mode.py tests/test_verify.py -o addopts="" -q --cov=docqa.qa.verify` | 254 passed；`verify.py` **95.19%**（392→395 stmts，缺 19 行，均为既有 `_boundary_ok`/`_neighbor_substantive`/`_verbatim_match` 分支与 `check_extraction_health`） |
| T1 模拟 plugin（把三处改动还原为 T1）跑新用例 | **23 failed / 25 passed**（48 个新用例） |
| T1 模拟 plugin 跑整文件 | **23 failed / 193 passed**：25 个失败全部是 T3 新增；179 个 T3 前用例全部通过（模拟保真） |
| 边界探针（直接调用 checker / `_trailing_marks` / `_split_doc_clauses`） | 见 §3-§5，无崩溃 |

T1 模拟方式（job tmp，未改仓库）：`_split_doc_clauses`/`_SENTENCE_BOUNDARY_CHARS` 还原为不含 `\r\v\f\u2028\u2029\u0085`；`_TRAILING_MARKS_RE` 还原为无闭合包裹；`sentence_tail_marks` 还原为「最后一个子句无论有无标记都覆盖」。

## 2. 三项修复是否生效（T3 vs T1 模拟，均直接调用 checker）

| 用例 | T3 | T1 模拟 |
|---|---|---|
| `A，B（[§1 ¶2]）。` / `A，B ([§1 ¶2])。` | **True** | False |
| `A，B [§1 ¶2]）。` / `A，B [§1 ¶2]”` / `A，B [§1 ¶2]”」` | **True** | False |
| `A，B [§1 ¶2]，文档中未规定。` | **True** | False |
| `A，B [§1 ¶2]，A。`（普通第三子句逐字） | **True** | False |
| `A，B [§1 ¶2]，通常采用调质处理。` | False（offender 点名「通常采用调质处理 不是逐字」） | False（不同原因） |
| `A [§1 ¶2]\r/…\u2028/…\u2029/…\u0085/…\v/…\fB。` | False（B「没有引用标记」） | **True**（T1 未把这些当句界） |
| `A [§1 ¶2]\r\nB。` / `\n\n` / `\r\r` | False | `\r\n`、`\n\n` False（既有），`\r\r` **True** |
| 全部 10 个闭合字符 × 55 个双字符组合 | `_trailing_marks` 全部返回 `[p.1]` | — |
| 3 个闭合字符 / 闭合后跟正文 / `]` / `］` / 零宽 / ASCII `.` | `[]`（不识别，符合文档） | — |

**结论：三项修复均生效，且新用例在 T1 上 RED**（23 个：6 个新换行参数 + `\r\r` + 3 个包裹正向 + 6 个包裹非逐字 + 1 个中间引用正向 + 2 个中间引用非逐字 + 边界集合 1 + `_trailing_marks` 形状 3 + 分隔符-only 1）。

## 3. 新增借用面的安全性质（全部未变）

| 性质 | 探针 | 结果 |
|---|---|---|
| 借用后仍逐字 | `A，B [§1 ¶2]，通常采用调质处理。`；包裹形态下前置/尾子句非逐字 | False，offender 点名非逐字子句 |
| 不跨句 | 句号、`\r\n`、`\u2028`、`\r` 后新句无标记 | False（「没有引用标记」） |
| own-mark 优先 | `test_clause_with_own_citation_never_borrows_another_clause` 保留 | 通过（A 用 p.2 判定，不借 p.1） |
| 伪引用 | `A，B [p.99 §9.9]），C。`：`doc_sentence_support` 全部「无法绑定」；完整 `verify()` 同时 `doc_citation_marks` 失败 | 双层拦截，未回退 |
| 通用否定 | 单独 `文档中未规定。`；通用否定尾 + 前文借用；通用否定自身带真实 citation | 行为与 T1 一致（skip / 借用 / 可作源） |
| 零引用 | `allow_zero_citations=True` 且无 citations → 跳过；有 citations 仍逐子句 | 与 T1 一致 |
| 完整 `verify()` | 包裹尾、中间引用、closer+下一子句三种新 pass 形态 | `gates_passed=True`，无失败项 |

补充说明：借用源改为「同句最后一个带尾部标记的子句」只是把**同句内**的可借用标记从「最后一个子句」放宽到「最后一个带标记子句」，覆盖扩大但每个被覆盖子句仍走原 `_verbatim_match`；探针未发现「无据放行」「跨句」「伪引用」三类绕过。

## 4. 闭合包裹正则的绕过/误匹配检查

- **没有发现绕过**：
  - 标记后跟正文（`[p.1]）后续`、`[p.1]） x`）→ `[]`；
  - 3 个及以上闭合字符 → `[]`（与文档「0–2」一致）；
  - `]`、全角 `］`、零宽空格、ASCII `.` → `[]`；
  - 两个标记夹一个闭合字符（`[p.1]）[p.2]`）→ 只返回末尾 `[p.2]`，不会把前面的标记一起借出；
  - 全角开引号 `“` 不在闭合集合（只收 `”`/`’`/ASCII `"`/`'`），不会把「引号开始」误当闭合。
- **没有误匹配**：10 个单闭合字符 + 55 个双字符组合全部精确返回 `[p.1]`；NBSP/全角空格/Tab 与闭合字符之间的空白均被 `\s*` 正确容忍。
- 即使正则误配，后果也只是「某个合法标记成为同句借用源」；借用方仍必须绑定 + 逐字，无法放宽内容门禁。

## 5. Unicode 换行与 `\r\n` / 连续分隔符

- `_split_doc_clauses` 形状验证：12 个句界字符（`。！？!?` + `\n\r\v\f\u2028\u2029\u0085`）各自自增句子号；`；;，,：、` 不另起句；`\r\n`/`\r\r\n`/`\n\n`/`\r\r` 连续分隔符不产生空子句；空串/纯分隔符串返回 `[]`。
- 新增边界字符在 `normalize()` 中会被 `\s+` 折叠为空格（`\s` 覆盖 U+2028/U+2029/U+0085），因此证据侧不会出现「答案按新行切、证据不按新行归一」的边界不一致；`\r\v\f` 本来就在 `_VERBATIM_BOUNDARY_CHARS` 里。
- **但存在新的可用性回归（R2）**：若证据原文本身含这些字符（尤其 lone CR、VT、FF、LS/PS/NEL），模型逐字引用整句时会被切成两句，只有末句带引用 → 前置子句判「没有引用标记」。实测同输入 T1=True / T3=False。DOCX 解析器把 `w:br`/`w:cr` 统一转 `\n`、PDF 文本层按行 `\n` 拼接，因此本管线命中概率低；属 SEC-T1-01 安全取舍的代价。

## 6. Findings

| ID | 严重度 | file:line | 问题 | 失败场景（实测） | 建议 |
|---|---|---|---|---|---|
| R1 | P2 | `src/docqa/qa/verify.py:679-681`（`_LEADING_CITATION_RE`）+ `699-706` | 闭合包裹只作用于**子句尾部**标记；语法层面写在**句号之后**的包裹引用不被 `_LEADING_CITATION_RE` 识别，且残留包裹会变成伪子句 | `B。（[§1 ¶2]）` → False，offenders：`用于验证…` 没有引用标记 + `（[§1 ¶2]）` 没有可校验的实质内容；`B。[§1 ¶2]）` → False（残留子句 `）`）；`B。“[§1 ¶2]”` → False | 扩展 `_LEADING_CITATION_RE` 容忍包裹（`（[mark]）`、`[mark]）`、`“[mark]”`）并消费掉包裹，或写入 §7 已知误杀并补测试。属 F3 同族的未覆盖形态 |
| R2 | P2 | `src/docqa/qa/verify.py:687-689`、`697`、`709-737` | 新增视觉换行作为句子边界后，**逐字引用本身含这些字符的证据原句**会被切成多句，前置子句失去引用 | 证据 `3.1 抗拉强度应大于等于 590 MPa，\r通常采用调质处理。`，答案同文 + 句末 `[p.1 §3.1]`：T1=True，T3=False（「抗拉强度…590 MPa」没有引用标记）；CR/VT/FF/LS/PS/NEL 六种均复现；TAB/NBSP 不受影响 | 契约取舍可接受，但应写入 `docs/hybrid-mode.md` §7（「证据含视觉换行时需逐段引用」）并补一条固定行为的测试；若判定不可接受，需改契约（仅把「行首/行尾换行」当边界，代价是 SEC-T1-01 回归） |
| R3 | P2 | `docs/hybrid-mode.md:221`；`docs/design.md:164` | 文档措辞与 T3 新语义不完全一致 | §6 首句仍写「被**自身或同一句的句末引用**覆盖」，而 222-223 行已说明借用源「不必是最后一个子句」；design 写「引用标记归前句」，§6 写「归前一个子句」 | 把 221 行改为「同一句内最后一个**带尾部标记**的引用」；design 统一为「前一个子句」 |
| R4 | P2 | `tests/test_hybrid_mode.py:1057-1104`、`1112-1149`、`1152-1222` | 测试缺口（不改变当前正确性，但不足以锁住语义） | 无「普通三子句中间引用」正向用例（只覆盖带通用否定尾的版本）；无 closer + CRLF/LS 跨句组合；闭合字符只测了 `）`/`】`/`”` 三个，其余 7 个与 55 个组合无用例；无 NBSP/U+3000 空白组合；无 R1 的「句号后包裹」用例（修复或文档化后都应补） | 按 §9 清单补，优先级：普通三子句中间引用 > closer+换行 > 其余闭合字符参数化 |

非阻塞观察（P3）：`_CLAUSE_SPLIT_RE` 上方注释（683-686）只解释了 ASCII `!?`，未提新增视觉换行（换行集合在 691-697 有说明，可合并）；`test_every_sentence_boundary_char_starts_a_new_sentence` 把常量与字面集合做等值断言，属契约钉住（需两处同步维护），可接受。

## 7. 测试真实性

- **RED 真实**：T1 模拟下 23/48 新用例失败，覆盖三处改动的全部方向；整文件 179 个 T3 前用例在模拟下全部通过，说明模拟没有把无关行为改坏。
- **不是同义反复**：行为用例断言 `passed`/offender 指向「被借用的非逐字子句」；包裹类同时有正向（可借）与两组负向（借用后仍逐字、带标记子句本身仍逐字），能区分「只放宽识别」与「放宽门禁」。
- **守卫用例**：`\r\n`/`\n\n` 连续换行、own-mark、跨句、三闭合字符、ASCII `.` 在 T1 上已通过，属回归护栏，不是新语义。
- 缺口见 R4；`_split_doc_clauses` 形状测试已补（句子号、连续分隔符、空输入、纯引用子句），是本次质量提升的主要部分。

## 8. 文档一致性

已同步：
- `docs/hybrid-mode.md` §6（217-226）：切分字符含全部视觉换行；借用源「最后一个带尾部标记的子句」；闭合包裹集合与 `_TRAILING_CLOSERS` 逐字一致（10 个）；ASCII `.` 指向 §7。
- `docs/hybrid-mode.md` §7：SEC-T1-04（引用语义：不保证相邻/同序）、F5（句首/句中引用不覆盖后续）、F6（ASCII `!?` 误杀 `n!`）、SEC-T1-05（ASCII `.`）、SEC-T1-02（无条号页级回退）、SEC-T1-03（多标记并集）均已写入。
- `README.md:233-235`、`docs/design.md:163-181` 已改为新语义；全仓已无「每个子句独立引用」旧表述。

待修：
- R3（§6 首句「句末引用」措辞与借用源规则冲突；design「前句」/「前一个子句」不一致）。

历史文档未改动（`2026-10-31-*`、`2026-11-01-hybrid-overview-fix-*-audit.md` 保持原样），符合约束；`docs/verification/2026-11-01-hybrid-overview-fix.md` 尚未创建，属 T4。

## 9. 未覆盖 / 建议补充的用例

| # | 用例 | 当前 T3 行为 | 建议 |
|---|---|---|---|
| 1 | `A，B [§1 ¶2]，A。`（普通三子句，最后一个子句无标记但逐字） | 通过 | 补正向用例（现有只测通用否定尾） |
| 2 | `A，B [§1 ¶2]，通常采用调质处理。` | 失败（非逐字） | 补负向用例 |
| 3 | `A [§1 ¶2]）\r\nB。` / `\u2028` | 失败（不跨句） | 补组合用例，钉住「包裹不放宽跨句」 |
| 4 | 10 个闭合字符逐个 + 双字符组合 | 全部支持（探针已验证） | 参数化补测试 |
| 5 | `B。（[§1 ¶2]）` / `B。[§1 ¶2]）` | 失败（R1） | 先决定修复或文档化，再补测试 |
| 6 | 证据含 CR/VT/FF/LS/PS/NEL 的逐字引用 | 失败（R2） | 写入 §7 并用测试固定行为 |
| 7 | `_trailing_marks` + NBSP/全角空格组合 | 支持 | 低优先补充 |
| 8 | 伪引用 + 闭合包裹源的完整 `verify()` | 双层失败（探针已验证） | 可选补 `verify()` 级用例 |

## 10. 备注

- 未改源码/测试/文档；`.coverage`（gitignored）已删除；探针与 plugin 位于 `C:/Users/26775/AppData/Local/Temp/docqa-t1-audit/`，仓库外。
- 未读/打印 `.env`，未联网，未提交/推送，未停止后台服务，未触碰 `data/derived`。
