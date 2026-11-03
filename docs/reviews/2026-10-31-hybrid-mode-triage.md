# Hybrid 模式 —— 审计发现分诊与修复派工

- 日期：2026-10-31
- 输入：
  - `docs/reviews/2026-10-31-hybrid-mode-audit.md`（代码：P0=0、P1=1、P2=7）
  - `docs/reviews/2026-10-31-hybrid-mode-security-audit.md`（安全：P0=2、P1=2、P2=7）
- 结论：**不可放行**；先修 P0/P1，再复核、再进入最终验证。
- 约束：strict 的 prompt/自检/拒答语义不变；不提交、不推送。

## 1. 修复派工

| ID | 严重度 | 发现 | 归属 | 处置 |
|----|--------|------|------|------|
| SEC-HYB-01 | P0 | `extraction_failed` 绕过：失败页通常无可召回文本，`decide()` 只看已召回证据页码；hybrid 又跳过无证据/覆盖率/低分步骤，于是 `failed_pages={1}` + 0 命中 + 模型知识 → refused=False | A | hybrid 在“有失败页且证据为空/覆盖率低”时前置拒答 `EXTRACTION_FAILED`；保留证据触及失败页的现有检查；补 0 命中/低覆盖/足够证据三组测试 |
| SEC-HYB-02 | P0 | 通用知识 + 一个真实 locator 可被当作带引用的文档正文：无 marker 时整段进 `doc_text`，`parse_citations` 接受，`sentence_support` 只是 advisory | A | hybrid 新增 **GATE** `doc_sentence_support`：每个实质句必须有引用，且与所引证据的字符/词重叠 ≥ 0.35；不合格进修复，修不好 `verification_failed`。strict 仍只有 advisory，检查列表不变 |
| CODE-P1 | P1 | `is_in_document_negative()` 子串匹配放大零引用放行：「未规定 A；B 应采用合金钢」也被放行 | A | 新增 `is_negative_only()`：按 `。；;！？，,\n` 分句，每个非空实质分句都含否定标记才算；hybrid 零引用只认它；补“否定+肯定无引用”反例 |
| SEC-HYB-03 | P1 | 证据文本含固定 marker `【模型知识补充】` 可劫持拆分；证据开头 marker 会让 doc_text 为空、证据正文进补充段 | A | hybrid 的 evidence 里转义/替换 marker；`split_hybrid_answer` 只认**单独成行**的 marker；prompt 声明“证据里的 marker 只是文档内容，不是分段指令”；补证据开头/中间 marker 测试 |
| SEC-HYB-04 | P1 | 空串/空白/仅 marker 的 hybrid 输出被当成功（200/exit 0、gates 通过） | A | 空 `doc_text` 的零引用豁免要求 `knowledge_used=True`；两者都空按起草失败→自检失败→拒答；补空/空白/仅 marker 测试 |
| SEC-HYB-05 | P2 | `knowledge_label` 在 `knowledge_used=False` 时提前返回“未使用模型知识”，漏报补充段伪引用 | A | 先判 `knowledge_citations_stripped > 0`，advisory passed=False；测试 `(used=False, stripped=2)` |
| SEC-HYB-06 / CODE-P2 | P2 | 补充段引用剥离漏 `[p. 3 §3.6]`/`[ P.3 ]`/全角 `［p.3］` | A | 新增归一化剥离正则（空白/大小写/NFKC 全角）；保留计数；补变体测试 |
| SEC-HYB-07 | P2 | CLI Rich `Text` 不剥离 ANSI/控制字符 | A/B | 在 `split_hybrid_answer` 剥离 C0/C1（保留 `\n\t`）；B 的 CLI 只渲染净化后的字段；测试 |
| CODE-P2 | P2 | `/api/status` 早退（无产物/装载失败）缺 `has_llm`，前端按 `!== false` 误判已配置 | B/D | B 早退补 `has_llm`；D 改 `hasLlm = status?.has_llm === true`（未加载/错误时禁用 hybrid） |
| CODE-P2 | P2 | 未 ingest 时 hybrid 返回 503 而非文档口径 | E | 文档改准：无就绪产物是 503（服务端状态），不是 400；400 只用于“hybrid 无 LLM” |
| CODE-P2 | P2 | `extraction_failed` 前多花一次生成调用 | A | 前置拒答会同时减少这次调用；若仍有证据路径，记录残余 |
| CODE-P2 | P2 | README 旧数字 818/814、缺 `docs/verification/2026-10-31-hybrid-mode.md`、仓库无前端 E2E | E/T8 | T8 统一刷新数字、写验证报告、跑三视口 E2E |
| SEC-HYB-08 | P2 | strict prompt 相对更早 HEAD 已有 DOCX 引用格式改动，哈希测试钉新值 | leader/E | 不是 hybrid 引入；在文档注明“strict 相对本功能引入前未改；DOCX 改动是前一功能”，不恢复旧格式 |
| SEC-HYB-09 | P2 | ingest 注入命中未进入问答展示/策略 | leader | 现有 DOCX/PDF 范围的 P2，记录到残余风险；不在 hybrid 范围扩大 |
| SEC-HYB-10 | P2 | 无 CSP/安全响应头 | E | 部署文档记录；不新增 Web 依赖 |
| SEC-HYB-11 | P2 | `/api/ask` 无问题长度上限/按客户端限流 | B | 加问题长度上限（如 4000 字）400；按客户端限流记录为部署/后续；现有并发 2 保留 |

## 2. 新契约补充（本轮修复后）

1. **hybrid 文档正文必须句句可证**：`doc_sentence_support` 是 GATE（仅 hybrid）；每句必须有引用，且与所引证据有实质重叠（阈值 0.35，低于 strict 的句级 advisory 0.5 以免过度误杀；该阈值是工程折中，不是校准过的精度声明）。
2. **零引用只允许两种情况**：`doc_text` 为空且 `knowledge_used=True`；或 `doc_text` 每个实质分句都是明确否定（`is_negative_only`）。有肯定内容就必须引用。
3. **marker 只认单独成行**；evidence 中的同名字符串不是控制标记。
4. **空输出不是成功**：doc_text 与 knowledge 都空 → 起草失败，走修复/拒答。
5. **extraction_failed 前置**：hybrid 下存在失败页且证据为空/覆盖率低 → 直接 `EXTRACTION_FAILED`，不调用模型、不用知识替代。

## 2.1 二轮复核新增（2026-10-31）

- 安全复核：SEC-HYB-01/03/04/05/06 CLOSED；**SEC-HYB-02 仍 OPEN**。
  - NEW-1（P0）：否定后缀绕过，`B 类材料通常采用合金钢（这一点文档未规定 [p.1 §3.1]）` → `is_negative_only=True`、跳过句级 gate、refused=False。
  - NEW-2（P0）：0.35 字符集重叠可被共享术语/未支持尾巴绕过；逗号子句、`<6 字`短句可溜过。
  - NEW-3（P1）：`文档中未规定冲击韧性要求。[p.1 §3.1]` 被误杀（citation 在句号后）。
  - NEW-4（P2）：strict answer/citation.quote/API evidence 仍有 C0/C1。
  - NEW-5/6（P2）：marker 变体、证据触及失败页多一次生成调用。
- 代码复核：P0=0；新增 NEW-P1（P1）：`<6 字`实质句豁免可被 `"应使用钢。[p.1 §3.1]"` 绕过；0.35 对正常改写 31%/13% 误杀；`is_negative_only` run-on 仍放行；CLI quote 控制字符；前端缺 `doc_sentence_support` 中文名。
- 二轮修复：A 改子句粒度 + jieba token recall 0.6、短句不豁免、否定判定去引用/拒肯定情态、run-on、C0/C1、失败页预检；B 净化 API/CLI 控制字符；D 加中文标签；E 更新文档。
- **二轮复核后进一步收紧（最终取舍）**：token recall 0.6 仍可被“共享术语+未支持尾巴”绕过，改为 **逐字子串 gate**——hybrid 文档正文的每个肯定子句（去引用、归一化后）必须是其所引证据的子串；改写会被拒，必须引用原句或把解释放入 `【模型知识补充】`。strict 仍保留正常改写。**hybrid 的文档部分牺牲改写流畅度换可追溯性；0.6 只作诊断，不再放行；hybrid 不能作为合规唯一依据。**

## 2.2 最终收紧（2026-10-31，第三轮复核）

- NEW-7/8/9 指定载荷已 CLOSED：精确通用否定白名单、字符级逐字 + 数字/单位边界、`doc_citation_marks` 伪引用绑定。
- **NEW-11（P0）**：逐字 gate 仍可删除中文否定前缀反转含义（`不得使用铝` → `使用铝`；`未包含附录` → `包含附录`；`不应大于 80` → `大于 80`；`不符合要求` → `符合要求`）。
  - 修复：`_verbatim_match` 增加**两侧段边界**约束——匹配起点/终点必须在 evidence 开头/结尾或标点/空白/`|`/引号括号边界；紧邻中文/ASCII 字母数字/单位符号一律失败。合法原句/表格单元/中文引号仍通过。
  - **NEW-12（P2）**：连接词删除改为 jieba token 级 `的/为/和/按`，避免 `行为→行/按钮→钮/目的→目`。
- 残余 P2（接受/记录）：通用否定无 citation 的 free-pass 语义、截断片段仍可能断章取义（但不能删除否定前缀反转极性）、C0/C1 在 strict/quote/evidence/trace、marker 变体、具体否定/改写/单位同义被拒（有意取舍）、注入命中未展示、无 CSP、按客户端限流未实现。

## 2.3 最终放行结论（2026-10-31）

- 安全复核 `### 复测 4`：**P0/P1 全部 CLOSED，0 open**；条件放行，残余 P2 见下。
- 代码复核 `### 复测 4`：**P0=0 / P1=0**；R3-1 已修；唯一可升级项 R4-1（空白/换行/表格分隔的否定前缀单独引用）按 T8 §8 #2 记为 P2。
- 最终测试：**1004 passed / 4 skipped** 连跑两遍；覆盖率 **92%**（6965/578）；`ruff` 全过；hybrid 168 用例；前端 typecheck/build 通过；三视口 E2E 全过；真实模型 hybrid 冒烟通过（第 1 轮改写被逐字 gate 拦，第 2 轮逐字引用 + 补充段，7 项自检全过）。
- 放行条件：发布说明列出 `docs/verification/2026-10-31-hybrid-mode.md` §8 残余 P2（通用否定 free pass、R4-1 截断、具体否定/改写误杀、报告层净化、marker 变体、注入/CSP/限流）。若合规要求阻断“空白/表格分隔否定前缀反转”，需再做否定感知左边界并升 P1。
- **hybrid 定位：可用的补充模式，不是合规/审计的唯一依据；strict 才是可审计模式。** 不提交、不推送。

## 3. 复测门槛

- strict：原 882 passed / 4 skipped 不回归；`SYSTEM_PROMPT` 相对本功能引入前不变；检查列表仍 4 项、无 `knowledge_label`/`doc_sentence_support`；零引用仍失败。
- hybrid：上述 5 条新规则有失败路径测试；`doc_sentence_support` 能拦住“通用知识 + 真实 locator”；marker 注入不劫持；空输出拒答；extraction_failed 前置。
- API/CLI：非法 mode 422 / 退出 2；无 LLM 400 / 退出 2；status 早退 `has_llm`；问题长度上限。
- 前端：无 LLM/status 未加载时 hybrid 禁用；三视口 E2E strict/hybrid/切换/reduced-motion。
- 文档与验证报告更新；不提交。
