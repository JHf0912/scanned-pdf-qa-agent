# Hybrid「文档概述类问题」误杀修复 —— 最终验证报告

- 日期：2026-11-01
- 触发问题：Web 工作台 hybrid 模式，选中 DOCX 演示件，提问「这份文档是干什么的？」被拒，`refusal_reason=verification_failed`，`doc_sentence_support` offender 为「子句「本文件规定了演示样件的技术要求与试验方法」没有引用标记」。
- 结论：**通过（条件放行）**。根因是自检的引用附着粒度与提示词「逐句引用」不一致；模型实际上逐字引用了证据。修复后同一问题在真实模型上正常回答，7 项自检全过；strict 路径未变。
- 约束：不提交、不推送；测试默认离线；真实模型冒烟使用项目 `.env`，未打印密钥。

## 1. 根因

1. `HYBRID_SYSTEM_PROMPT` 要求「文档能支持的内容必须**逐句**引用」，模型按中文自然写法把引用放在整句末尾（`A，B [§1 ¶2]。`）。
2. `qa/verify.py::_split_doc_clauses()` 按逗号切子句，`check_doc_sentence_support()` 要求**每个子句自己携带引用标记**。
3. 于是同一句的逗号前半句被判「没有引用标记」，尽管它是证据的逐字内容；两轮修复模型都保持自然写法，最终 give_up。
4. 严格说，模型没有改写证据、也没有伪造引用；这是 **checker 与 prompt 的契约不一致**。

诊断全文：`docs/reviews/2026-11-01-hybrid-overview-refusal-diagnosis.md`。

## 2. 交付

| 层 | 内容 |
|----|------|
| 引用覆盖 | 句末引用覆盖**同一句**的逗号/分号/顿号子句；own-mark 优先；每个被覆盖子句仍必须走原 `_verbatim_match()`（段边界逐字）；不跨句 |
| 句子边界 | `。！？!?` 与所有视觉换行：`\n`、`\r`、`\v`、`\f`、U+2028、U+2029、U+0085（SEC-T1-01） |
| 借用源 | 同句最后一个**带尾部标记**的子句（F4；不必是最后一个子句） |
| 尾标记形态 | 标记后允许 0–2 个闭合包裹字符 `）)】」』》”’`；ASCII `.`/`"`/`'` 不支持（见 §7） |
| 确定性 | `_evidence_text_for_marks()` 用有序去重（`dict` 插入顺序），修复 `set` 受 `PYTHONHASHSEED` 影响的问题（SEC-T3-04） |
| 不变 | `HYBRID_SYSTEM_PROMPT`、strict 的 4 项检查、API/CLI/前端、`is_generic_negative`、`allow_zero_citations`、offender/detail 文案 |

## 3. 测试与静态检查

| 项 | 结果 |
|----|------|
| `uv run pytest tests -o addopts="" -q` | **1078 passed, 4 skipped**（9 warnings） |
| `--cov=src/docqa` | **91.74%**（6985 语句 / 577 未覆盖；门禁 84%） |
| `uv run ruff check .` | **All checks passed!** |
| `tests/test_hybrid_mode.py` | **242 passed**（原 168 + T1 11 + T3 37 + T3.2 26） |
| `PYTHONHASHSEED=0..5` 跑 hybrid 文件 | 每次 **242 passed**（确定性回归） |
| T1 新增用例在旧实现上 | 5 个 RED；T3 新增用例在 T1 实现上 23 个 RED；T3.2 顺序/闭合用例在改前 8 个 RED |

## 4. 真实模型冒烟

服务：`uv run docqa serve --host 127.0.0.1 --port 8000`（最终代码重启）；选中文档 DOCX 演示件；`has_llm=true`。

### 4.1 原问题（hybrid）

```text
问：这份文档是干什么的？
refused=false，attempts=1
答：本文件规定了演示样件的技术要求与试验方法，用于验证 DOCX 原生文本的解析链路 [§1 ¶2]。
    适用于 Phase 1 的演示与人工检查 [§1.1 ¶4]。
```

- `citations`：`[§1 ¶2]`、`[§1.1 ¶4]`；`knowledge_used=false`。
- 自检 7 项全过：`citation_validity / numeric_grounding / clause_consistency / sentence_support / doc_sentence_support / doc_citation_marks / knowledge_label`。
- `doc_sentence_support` 详情：「3 个肯定子句均为所引证据的逐字内容」。
- 第一轮即通过（修复前两轮都失败）。

### 4.2 第二种问法（hybrid）

```text
问：这份文档的主要内容是什么？
refused=false，attempts=2
答：同一段逐字引用 + 两条 citation；7 项自检全过。
```

### 4.3 strict 对照

```text
问：这份文档是干什么的？（strict）
refused=true，refusal_reason=not_in_document
detail：问题中的关键词只有 0% 出现在文档里（阈值 60%）
```

strict 的保守策略未变；本次不修 strict 的概述类检索/覆盖策略，记为后续独立议题。

## 5. Web E2E

- 用系统 Chrome + Playwright-core 驱动 `http://127.0.0.1:8000/app`：
  - 选择「文档 + 模型知识」→ 输入问题 → 发送 → 等待答案卡；
  - 答案卡显示 `含模型知识`、`门禁通过`、逐字引用 + `[¶2]`/`[¶4]` chip、`[§1 ¶2]`/`[§1.1 ¶4]` 引用；
  - 右侧自检 7 项全绿（含「句级引用支撑：3 个肯定子句均为所引证据的逐字内容」）；
  - 390px 视口无横向滚动、无 console error；
  - 截图：`C:/Users/26775/.claude/jobs/fb455bb7/tmp/web-shots/answer-hybrid-1440.png`、`answer-hybrid-390.png`（job tmp，不提交）。

## 6. 审计链

| 阶段 | 报告 | 结论 |
|------|------|------|
| 诊断 | `docs/reviews/2026-11-01-hybrid-overview-refusal-diagnosis.md` | 根因 + 修复契约 |
| 代码审计（T1） | `docs/reviews/2026-11-01-hybrid-overview-fix-code-audit.md` | 0 P0 / 0 P1 / 7 P2 |
| 安全审计（T1） | `docs/reviews/2026-11-01-hybrid-overview-fix-security-audit.md` | 0 P0 / 0 P1 / 6 P2；既有 NEW-2/7/8/9/11 保持 CLOSED |
| 分诊 | `docs/reviews/2026-11-01-hybrid-overview-fix-triage.md` | 修 F1/F2/F3/F4/SEC-T1-01；其余文档化 |
| T3 代码复测 | `docs/reviews/2026-11-01-hybrid-overview-fix-t3-code-reaudit.md` | 0 P0 / 0 P1 / 4 P2；三项修复生效 |
| T3 安全复测 | `docs/reviews/2026-11-01-hybrid-overview-fix-t3-security-reaudit.md` | 0 P0 / 0 P1；SEC-T1-01 CLOSED；SEC-T3-04（预存 P2）建议修 |
| T3.2 | 本次收尾 | SEC-T3-04 已修（有序去重）；SEC-T3-01 收窄支持面；R3/SEC-T3-03 文档修正；R1/R2 固定为已知误杀 |

## 7. 残余 P2（发布说明必须列出）

1. **引用只保证子句级逐字，不保证片段相邻/同序（SEC-T1-04，重要）**：单句末引用下的多个子句可省略、拼接、重排；引用不是「整句原文连读」的证据。核对上下文/顺序/完整性时请打开原文；合规/审计以原文为准。
2. **多标记并集可跨 chunk 拼接（SEC-T1-03）**：`[p.1] [p.2]` 会把两个 chunk 拼接后匹配；顺序现已确定（SEC-T3-04 已修），但拼接本身仍是既有语义。
3. **句号后带包裹引用仍误杀（R1/SEC-T3-02）**：`B。[p.1]）`、`B。（[p.1]）` 仍拒；已写入 `docs/hybrid-mode.md` §7 并用固定失败测试钉住。
4. **证据原文含视觉换行时需逐段引用（R2）**：`\r`/VT/FF/LS/PS/NEL 现在是句子边界；逐字引用含这些字符的整句会被切成多句，前句失引用。这是 SEC-T1-01 安全修复的代价。
5. **无条号文档上的伪条号可借页级回退绑定（SEC-T1-02，预存）**：`[p.1 §9.9]` 在 `known_clause_labels` 为空时可按页回退；有条号集合的文档仍被 `citation_validity` 拦下。
6. **ASCII `.` 阻止句末引用借用（SEC-T1-05）**：`A，B [p.1].` 仍误杀；`.` 与数字小数点归一化冲突，不能全局加入边界字符。
7. **句首/句中引用不覆盖后续子句（F5）**：只有位于某子句尾部的标记可被同句借用；`[p.1] A，B` 仍拒。
8. **ASCII `!?` 句子边界可能误杀阶乘/混排写法（F6）**：`计算 n! ...` 会被切成两句。
9. **空白/换行/表格/逗号分隔的否定前缀（SEC-T1-06，预存）**：`不得 | 使用铝`、`不得，使用铝` 等仍可能被单独引用；紧邻无空格前缀已阻止。
10. **展示层净化边界、注入命中、CSP、限流**等既有 P2 见 `docs/verification/2026-10-31-hybrid-mode.md` §8，本次未变。

## 8. 发布说明措辞（建议）

> hybrid 模式现在按「句」覆盖引用：句末引用可覆盖同一句的逗号/分号子句（`A，B [p.1 §3.1]。`），但每个被覆盖子句仍必须是被引证据的逐字内容；引用不跨句。引用只保证「每个子句逐字有据」，不保证所覆盖的多个片段在原文中相邻或同序（可省略、可拼接）。列举型、ASCII 句点、句号后包裹引用、句首/句中引用等自然写法仍可能被保守拒绝。strict 路径未变。

## 9. 复现命令

```bash
uv run pytest tests -o addopts="" -q
uv run pytest tests -o addopts="" -q --cov=src/docqa --cov-report=term
uv run ruff check .

uv run docqa serve --host 127.0.0.1 --port 8000
# Web: http://127.0.0.1:8000/app，选 DOCX 演示件 → 文档 + 模型知识 → 问「这份文档是干什么的？」
# CLI:
uv run docqa ask "这份文档是干什么的？" --mode hybrid --doc data/derived/demo-558392e0-558392e0
uv run docqa ask "这份文档是干什么的？" --mode strict --doc data/derived/demo-558392e0-558392e0
```

## 10. 运行状态

| 入口 | 地址 | 状态 |
|------|------|------|
| 后端 + 内置前端 | `http://127.0.0.1:8000/`（`/app` 工作台） | 运行中；`has_llm=true`；选中 DOCX 演示件 |
| Vite 开发前端 | `http://127.0.0.1:5173/` | 运行中；`/api` 代理到 8000 |

未提交、未推送；`web/` 源码仍为未跟踪状态。
