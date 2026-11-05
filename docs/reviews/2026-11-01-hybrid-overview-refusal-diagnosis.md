# Hybrid「这份文档是干什么的？」拒答 —— 诊断与修复契约

- 日期：2026-11-01
- 触发问题：Web 工作台、`mode=hybrid`、选中 DOCX 演示件，提问「这份文档是干什么的？」
- 现象：`refused=true`，`refusal_reason=verification_failed`，提示「生成的答案未通过自检：doc_sentence_support。证据已附在下方，请人工核对。」
- 结论：**模型实际上已经逐字引用了文档原句，是自检的「引用附着粒度」与提示词的「逐句引用」不一致导致的误杀**。修 checker，不放宽逐字门禁。

## 1. 复现证据

### 1.1 真实 API 轨迹（2026-11-01，`POST /api/ask`）

```json
{
  "question": "这份文档是干什么的？",
  "answer_mode": "hybrid",
  "refused": true,
  "refusal_reason": "verification_failed",
  "refusal_detail": "生成的答案未通过自检：doc_sentence_support。证据已附在下方，请人工核对。",
  "attempts": 2,
  "verification": {
    "checks": [
      {"name": "citation_validity", "passed": true},
      {"name": "numeric_grounding", "passed": true},
      {"name": "clause_consistency", "passed": true},
      {"name": "sentence_support", "passed": true, "detail": "4/4 句与证据有实质重叠"},
      {"name": "doc_sentence_support", "passed": false,
       "detail": "1/3 个子句缺少引用或不含逐字证据",
       "offenders": ["子句「本文件规定了演示样件的技术要求与试验方法」没有引用标记"]},
      {"name": "doc_citation_marks", "passed": true},
      {"name": "knowledge_label", "passed": true}
    ]
  }
}
```

两轮起草：

| 轮次 | 正文 | 引用 | 补充段 | 结果 |
|------|------|------|--------|------|
| 1 | 80 字 | 2 条 | 94 字 | `doc_sentence_support` 失败 |
| 2 | 79 字 | 2 条 | 无 | 同一 offender，修复后仍失败 → give_up |

### 1.2 证据原文（`chunks.jsonl`，`b2`）

```text
1 总则
本文件规定了演示样件的技术要求与试验方法，用于验证 DOCX 原生文本的解析链路。
```

offender 命中的子句 **逐字出现在证据里**，只差一个引用标记的位置。

### 1.3 离线探针（不调用模型）

用同一句正文、同一引用、同一证据直接调用 `check_doc_sentence_support`：

```text
answer: 本文件规定了演示样件的技术要求与试验方法，用于验证 DOCX 原生文本的解析链路 [§1 ¶2]。
passed: False
detail: 1/2 个子句缺少引用或不含逐字证据
offenders: ['子句「本文件规定了演示样件的技术要求与试验方法」没有引用标记']
```

## 2. 根因

1. `HYBRID_SYSTEM_PROMPT` 第 1 条要求 **「逐句引用」**（每一句结论后面附引用），模型按自然中文写法把引用放在整句末尾。
2. `qa/verify.py::_split_doc_clauses()` 按 `。！？；;，,：、\n` 切子句，逗号也算分隔符。
3. `check_doc_sentence_support()` 要求 **每个子句自己携带引用标记**；引用只附着到它前面那个子句。
4. 于是「A，B [§1 ¶2]」中的 A 被判「没有引用标记」，尽管 A、B 都是同一句、同一引用、同一证据的逐字内容。
5. 修复提示只说「改为逐字引用证据原句」，没有要求「每个逗号分句重复一遍引用」；模型两轮都保持自然写法，最终 give_up。

这是 **checker 与 prompt 的契约不一致**，不是模型改写了证据，也不是 citation 伪造。

### 2.1 影响面

中文标准/合同里「本文件规定了……，适用于……。」「A，B，C。」这类逗号长句非常常见。只要模型按「一句一引」的自然写法输出，hybrid 就会误杀正确回答。范围包括 `，`、`、`、`：`、`；` 分隔的逐字引用。

### 2.2 与 strict 的关系

同一问题在 strict 下会以 `not_in_document` 拒答：问题关键词在文档中覆盖率 0%（阈值 60%），因为「文档/干什么」不是文档正文词。这是 strict 的关键词覆盖策略，**不是本次要修的 hybrid 问题**；记为后续独立议题（元问题/概述类问题的检索与覆盖策略），本次不改 strict。

## 3. 修复契约（冻结）

### 3.1 唯一改动

`check_doc_sentence_support()` 的引用覆盖粒度从「子句」改为「句子」：

- `_split_doc_clauses()` 为每个子句附带**句子编号**；句子边界为 `。！？!?` 与换行 `\n`，**逗号/顿号/冒号/分号仍是子句分隔符，不另起一句**。
- 子句自带引用标记时，仍只用**自己的**标记绑定证据（保持精确性）。
- 子句没有自己的标记时，可以借用**同一句最后一个子句上的尾部引用标记**；找不到则仍判「没有引用标记」。
- 借用后仍走原有 `_verbatim_match()`：子句必须是所引证据的**段边界逐字子串**；不是逐字内容照样失败。
- **不跨句借用**：`。！？!?`/换行之后的新句子不能借用上一句的引用。
- `is_generic_negative()` 跳过、`allow_zero_citations`、offender 文案、detail 文案、strict 的 advisory `check_sentence_support` 全部不变。
- 提示词不改；不新增端点；不改 API/CLI/前端。

### 3.2 为什么不是放宽安全门禁

安全属性是「每个肯定子句必须是所引证据的逐字内容」，不是「每个逗号分句都要重复引用」。本次只改变**引用覆盖范围**，每条被覆盖的子句仍必须逐字可证：

- `抗拉强度应大于等于 590 MPa，通常采用调质处理。[p.1 §3.1]`：后半句不是证据逐字内容 → 仍失败（NEW-2 回归）。
- `冲击韧性通常采用 [p.1 §3.1] 的方法测定。`：去掉引用后仍不是逐字内容 → 仍失败（SEC-HYB-02 回归）。
- `文档中未规定。[p.1 §3.1]`：通用否定白名单 → 行为不变。
- 伪引用 `[p.99 §9.9]`：`doc_citation_marks` 仍失败。
- 跨句不借用；新句子没有引用仍失败。

### 3.3 验收测试

在 `tests/test_hybrid_mode.py` 新增（先 RED 后 GREEN）：

1. 用户原句：`本文件规定了演示样件的技术要求与试验方法，用于验证 DOCX 原生文本的解析链路 [§1 ¶2]。` → 通过（同句两个逗号子句 + 一个尾部引用）。
2. 多逗号子句全部逐字 + 一个尾部引用 → 通过。
3. 其中一个逗号子句非逐字 → 失败，offender 点名该子句。
4. 新句子没有引用 → 仍失败（现有 `test_doc_sentence_support_rejects_sentence_without_citation` 保持）。
5. 不跨句借用：上一句尾部引用不能让下一句无引用子句通过。
6. 子句自带引用优先：`A [p.2]，B [p.1]。` 中 A 只用 p.2 的证据判定，不能借 p.1。
7. 现有 168 个 hybrid 用例全部保持通过；strict 4 项检查与 prompt 哈希不变。

### 3.4 文档

- 更新 `docs/hybrid-mode.md` 的规则说明：引用可以落在句末，检查按「句」覆盖；但每个被覆盖子句仍必须逐字可证。
- 新建验证报告 `docs/verification/2026-11-01-hybrid-overview-fix.md`（实现完成后）。
- 不改写历史审计报告（`2026-10-31-*` 保持原样）。

## 4. 编排

| 阶段 | 角色 | 交付 |
|------|------|------|
| T1 实现 | tdd-guide agent | `verify.py` + `tests/test_hybrid_mode.py` + `docs/hybrid-mode.md`；先写失败用例 |
| T2 审计 | code-reviewer + security-reviewer（并行、只审不改） | `docs/reviews/2026-11-01-hybrid-overview-fix-audit.md` |
| T3 修复 | 按审计 P0/P1 回派 | 修复 + 回归 |
| T4 验证 | leader | 全量 pytest/覆盖率/ruff；真实模型同一问题冒烟；验证报告 |

约束：不提交、不推送；不读/打印 `.env`；测试默认离线；不动 `data/derived` 产物；临时文件只放 job tmp。
