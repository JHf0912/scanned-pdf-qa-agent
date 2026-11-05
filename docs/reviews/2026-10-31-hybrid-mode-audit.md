# 回答范围模式（strict / hybrid）独立审计

- 审计日期：2026-10-31
- 审计对象：Web 工作台「回答范围」strict / hybrid、`POST /api/ask`、CLI `docqa ask --mode`
- 契约：[`docs/reviews/2026-10-31-hybrid-mode-contract.md`](2026-10-31-hybrid-mode-contract.md)（冻结版，20:25）
- 用户文档：[`docs/hybrid-mode.md`](../hybrid-mode.md)（审计快照 21:03:31，E 仍在校对）
- 代码快照：分支 `feat/web-workbench`，HEAD `8489bc2`；工作区含未提交的 DOCX 支持与 hybrid 改动
- 审计方式：只读代码审阅 + 只读测试/类型检查 + 系统临时目录中的复现脚本
- 约束遵守：未修改任何代码/测试/配置/数据；未 commit/push；未读取 `.env`；未调用真实模型端点。复现命令统一设置 `DOCQA_DISABLE_DOTENV=1`

## 结论摘要

**有条件放行（WARNING）**：未发现 P0（无安全漏洞、无数据丢失、无静默降级）；发现 **1 个 P1** 与 **7 个 P2**。

- **P1 建议合并前修复**：零引用放行被「答案里出现过否定词」这一子串判据放大，导致**非空、含肯定性规范断言的正文在没有引用时被放行**，并以 `answer`（文档正文）身份展示。这与 `docs/hybrid-mode.md` §6「有内容的文档正文仍必须引用」和 §7「不写 marker 时由 strict 自检兜底」的承诺冲突。
- P2 主要是空响应被当成功、补充段伪引用变体漏剥、advisory 信号丢失、`/api/status` 早退缺 `has_llm`、未 ingest 时 503 先于 400、extraction_failed 前多花一次模型调用，以及文档/验收证据漂移。
- strict 路径未因 hybrid 工作发生行为回归；详见下文「strict 回归结论」。

| 严重度 | 数量 | 状态 |
|---|---|---|
| P0 | 0 | pass |
| P1 | 1 | warn |
| P2 | 7 | info |

## Findings

| ID | 级别 | 标题 | 位置 |
|---|---|---|---|
| F1 | P1 | 零引用放行被否定词子串放大：肯定性正文无引用也可放行 | `src/docqa/qa/agent.py:264`、`src/docqa/qa/policy.py:452`、`src/docqa/qa/verify.py:265` |
| F2 | P2 | hybrid 空响应/空白响应被当作成功答案放行 | `src/docqa/qa/answer.py:158`、`src/docqa/qa/agent.py:264`、`src/docqa/qa/verify.py:483` |
| F3 | P2 | 补充段伪引用的变体形态漏剥，且 advisory 仍报「已显式标注」 | `src/docqa/qa/verify.py:51`、`src/docqa/qa/answer.py:165` |
| F4 | P2 | 只含伪引用的补充段被报成「未使用模型知识」，prompt 失效信号丢失 | `src/docqa/qa/verify.py:590`、`docs/hybrid-mode.md:167` |
| F5 | P2 | `/api/status` 早退分支缺 `has_llm`，前端在未就绪态把 hybrid 当可用 | `src/docqa/api/app.py:443`、`web/src/pages/Workbench.tsx:292` |
| F6 | P2 | 未 ingest 文档时 hybrid 请求返回 503，而非文档承诺的 400 | `src/docqa/api/app.py:689`、`docs/hybrid-mode.md:75`、`:92` |
| F7 | P2 | hybrid 在 extraction_failed 场景仍先花一次生成调用 | `src/docqa/qa/agent.py:128`、`src/docqa/qa/agent.py:296`、`src/docqa/qa/policy.py:332` |
| F8 | P2 | 文档与验收证据漂移：README 用例数过期；hybrid 验证报告缺失 | `README.md:39`、`README.md:453`、`docs/verification/` |

---

### F1 [P1] 零引用放行被否定词子串放大：肯定性正文无引用也可放行

**位置**

- `src/docqa/qa/agent.py:264-266`：`allow_zero_citations = mode == "hybrid" and (not draft.text.strip() or is_in_document_negative(draft.text))`
- `src/docqa/qa/policy.py:452-462`：`is_in_document_negative()` 只做子串匹配（`未规定`/`未涉及`/`不包含` 等）
- `src/docqa/qa/verify.py:265-272`：`allow_zero_citations=True` 时 `citation_validity` 直接 pass

**失败场景**

模型在 hybrid 下漏写 `【模型知识补充】` marker（文档已承认这是已知退化路径），或把补充内容写进 marker 之前，输出：

```text
文档中未规定 A 类材料的要求；B 类材料应采用合金钢。
```

`is_in_document_negative()` 因为出现「未规定」返回 True，于是整段 `doc_text`（包括第二句**无引用的肯定性规范断言**）获得零引用豁免，`numeric_grounding`/`clause_consistency` 没有数字或条号可查，全部 gate 通过；答案被作为**文档正文**返回，`model_knowledge=None`、`knowledge_used=False`，前端不会显示独立的模型知识块。

**可复现步骤**（离线，不读 `.env`，不调真实模型）

```bash
DOCQA_DISABLE_DOTENV=1 uv run --no-sync python - <<'PY'
import sys
sys.path.insert(0, "tests")
import test_hybrid_mode as th

for label, text in {
    "negative + positive claim": "文档中未规定 A 类材料的要求；B 类材料应采用合金钢。",
    "pure positive claim": "B 类材料应采用合金钢。",
    "pure negative": "文档中未规定 A 类材料的要求。",
}.items():
    p = th.FakeProvider(text)
    b = th.make_agent([th.pdf_chunk()], p).answer(th.STRENGTH_QUESTION, mode="hybrid")
    print(label, "| refused:", b.refused, "| gates:", b.verification.gates_passed,
          "| citations:", len(b.citations), "| answer:", repr(b.answer))
PY
```

实测输出：

```text
negative + positive claim | refused: False | gates: True | citations: 0 | answer: '文档中未规定 A 类材料的要求；B 类材料应采用合金钢。'
pure positive claim       | refused: True  | gates: False | citations: 0 | answer: None
pure negative             | refused: False | gates: True  | citations: 0 | answer: '文档中未规定 A 类材料的要求。'
```

**为什么是问题**

- 契约 §2.3 的意图是「doc_text 为空或为**明确否定**」时放行；当前实现把「整段里出现过否定词」等同于「整段是否定答案」。
- `docs/hybrid-mode.md` §6 明确写「**有内容的文档正文仍必须引用**」；§7 写「完全不写 marker 时退化为正文里混了无引用内容，由 strict 自检兜底」。F1 证伪了这两句：只要混入一个否定词，兜底失效。
- 补充段的内容因此可以以「文档正文」身份出现，违反「模型知识不是证据」的核心不变量。

**建议**

1. 收紧判据：仅当 `doc_text` 的**每个实质性分句**都是否定表述（或 `doc_text` 极短且整体是否定句）时才置 `allow_zero_citations=True`；任何无引用的肯定性断言都应继续触发 `citation_validity` 失败/修复。
2. 或者要求空/否定放行同时满足 `knowledge_used=True`（纯否定且无补充段时仍按零引用失败处理，或单独定义）。
3. 增加回归测试：`negative sentence + positive uncited sentence` 必须拒绝；`pure negative` 保持放行。
4. 若决定保留当前字面行为，必须同步修改契约与 `docs/hybrid-mode.md` §6/§7，不能再声称「有内容的正文仍必须引用」。

---

### F2 [P2] hybrid 空响应/空白响应被当作成功答案放行

**位置**

- `src/docqa/qa/answer.py:158-171`：无 marker 时 `doc_text=raw`（不 strip）；marker 后为空时 `knowledge=None`
- `src/docqa/qa/agent.py:264-266`：`not draft.text.strip()` 命中空响应
- `src/docqa/qa/verify.py:265-272`、`:483-492`：零引用与句级支撑度都按「不适用」通过
- 前端 `web/src/components/AnswerCard.tsx:216-225`：正文与补充段都为空时显示「模型没有返回正文」错误态
- CLI `src/docqa/cli.py:573-579`：正文为空且无补充段时打印一个空的「答案」面板

**失败场景**

生成端因内容过滤、截断、上游异常返回空串、纯空白或只有 marker：

```bash
DOCQA_DISABLE_DOTENV=1 uv run --no-sync python - <<'PY'
import sys
sys.path.insert(0, "tests")
import test_hybrid_mode as th

for label, text in {"empty": "", "whitespace": "   ", "marker only": "【模型知识补充】"}.items():
    b = th.make_agent([th.pdf_chunk()], th.FakeProvider(text)).answer(
        th.STRENGTH_QUESTION, mode="hybrid")
    print(label, "| refused:", b.refused, "| gates:", b.verification.gates_passed,
          "| answer:", repr(b.answer), "| knowledge:", b.model_knowledge,
          "| used:", b.knowledge_used)
PY
```

实测：

```text
empty        | refused: False | gates: True | answer: ''   | knowledge: None | used: False
whitespace   | refused: False | gates: True | answer: '   ' | knowledge: None | used: False
marker only  | refused: False | gates: True | answer: ''   | knowledge: None | used: False
```

**影响**

API 返回 200、`refused=false`、`gates_passed=true`，但答案为空；Web 卡片头部显示「门禁通过」，正文区却显示「模型没有返回正文」；CLI 打印空答案面板。这是自相矛盾的成功状态，且没有进入修复/拒答。

**建议**

- 空 `doc_text` 的零引用豁免应同时要求 `knowledge_used=True`；正文与补充段都为空时应视为起草失败（进入修复，最终 `verification_failed`），而不是成功。
- 增加空响应、空白响应、marker-only 的端到端测试（agent + API）。

---

### F3 [P2] 补充段伪引用的变体形态漏剥，且 advisory 仍报「已显式标注」

**位置**

- `src/docqa/qa/verify.py:51-56`：`_CITATION_MARK_RE` 只识别无空格/半角/小写 `p.` 的六种形态
- `src/docqa/qa/answer.py:165-167`：`split_hybrid_answer()` 用同一正则剥离补充段引用

**失败场景**

模型在补充段写出 `[p. 3 §3.6]`、`[ P.3 ]`、`［p.3］` 等变体（空格、大小写、全角括号）。正则不匹配，引用既不被剥离也不计数，advisory 仍返回 `passed=True`、detail「补充段已显式标注」。

**可复现步骤**

```bash
DOCQA_DISABLE_DOTENV=1 uv run --no-sync python - <<'PY'
import sys
sys.path.insert(0, "tests")
import test_hybrid_mode as th

text = ("抗拉强度应大于等于 590 MPa。[p.1 §3.1]\n"
        "【模型知识补充】\n参见 [p. 3 §3.6]、[ P.3 ]、［p.3］。")
b = th.make_agent([th.pdf_chunk()], th.FakeProvider(text)).answer(
    th.STRENGTH_QUESTION, mode="hybrid")
chk = next(c for c in b.verification.checks if c.name == "knowledge_label")
print("knowledge:", repr(b.model_knowledge))
print("knowledge_label:", chk.passed, chk.detail)
PY
```

实测：

```text
knowledge: '参见 [p. 3 §3.6]、[ P.3 ]、［p.3］。'
knowledge_label: True 补充段已显式标注
```

**边界确认**

这些变体**不会**进入 `citations` / `evidence`（引用只从 `doc_text` 解析，已验证），所以不是证据污染；但展示层的「补充段禁止任何引用」安全网被绕过，用户会在虚线补充块里看到疑似文档引用，且 prompt 失效信号（advisory）没有触发。

**建议**

- 剥离前先做归一化（NFKC、去方括号内空白、`p`/`§`/`¶` 大小写与全半角统一），再计数与剥离；或在现有六种形态外补充变体正则。
- 增加测试覆盖空格、大小写、全角括号；确保 `knowledge_citations_stripped` 计数正确。

---

### F4 [P2] 只含伪引用的补充段被报成「未使用模型知识」，prompt 失效信号丢失

**位置**

- `src/docqa/qa/verify.py:590-606`：`check_knowledge_label()` 先判 `if not knowledge_used: pass "未使用模型知识"`，之后才检查 `knowledge_citations_stripped`
- `docs/hybrid-mode.md:167-168`：「若补充段里剥离过引用：**不通过**（advisory）」

**失败场景**

模型输出 `正文。[p.1 §3.1]\n【模型知识补充】\n[p.9 §9.9]`：补充段剥离后为空，`knowledge_used=False`（契约 §2.2 的规定），但 `knowledge_citations_stripped=1`。`check_knowledge_label(False, 1)` 返回 `passed=True`、detail「未使用模型知识」，与文档「剥离过引用 -> 不通过」不一致，也丢失了 advisory 设计目的（发现 prompt 失效）。

**可复现步骤**

```bash
DOCQA_DISABLE_DOTENV=1 uv run --no-sync python - <<'PY'
import sys
sys.path.insert(0, "tests")
import test_hybrid_mode as th
from docqa.qa.verify import check_knowledge_label

print(check_knowledge_label(False, 2).to_dict())
b = th.make_agent(
    [th.pdf_chunk()],
    th.FakeProvider("抗拉强度应大于等于 590 MPa。[p.1 §3.1]\n【模型知识补充】\n[p.9 §9.9]"),
).answer(th.STRENGTH_QUESTION, mode="hybrid")
chk = next(c for c in b.verification.checks if c.name == "knowledge_label")
print("bundle:", b.model_knowledge, b.knowledge_used, "| advisory:", chk.passed, chk.detail)
PY
```

实测：

```text
{'name': 'knowledge_label', 'passed': True, 'severity': 'advisory', 'detail': '未使用模型知识', 'offenders': []}
bundle: None False | advisory: True 未使用模型知识
```

**建议**

- 把 `knowledge_citations_stripped` 的判断放到 `knowledge_used` 之前；或在「未使用补充段」分支里也带上剥离计数并把 `passed=False`（仍为 advisory，不阻断）。
- 同步 `docs/hybrid-mode.md` §6 的措辞，并增加该组合的测试。

---

### F5 [P2] `/api/status` 早退分支缺 `has_llm`，前端在未就绪态把 hybrid 当可用

**位置**

- `src/docqa/api/app.py:443-447`：`load_error`（含「尚未 ingest 任何文档」）分支的 JSON 只有 `ready/error/docs/selected/domain`，没有 `has_llm`
- `web/src/pages/Workbench.tsx:292`：`const hasLlm = status?.has_llm !== false`（缺失按「已配置」处理）
- `web/src/components/ModeSwitch.tsx:38-43, 88-94`：hybrid 的 disabled 只取决于 `busy || !hasLlm`

**失败场景**

后端未配置生成模型且尚未 ingest 文档（或产物装载失败）时：

```bash
DOCQA_DISABLE_DOTENV=1 uv run --no-sync python - <<'PY'
import sys, tempfile, dataclasses
from pathlib import Path
from fastapi.testclient import TestClient
import docqa.api.app as api_module
from docqa.config import load_config

cfg = load_config()
with tempfile.TemporaryDirectory() as td:
    cfg = dataclasses.replace(cfg, cache_dir=Path(td) / "derived" / "cache")
    c = TestClient(api_module.create_app(cfg, domain="standard"))
    s = c.get("/api/status").json()
    print("keys:", sorted(s.keys()), "| has_llm present:", "has_llm" in s)
PY
```

实测：

```text
keys: ['docs', 'domain', 'error', 'ready', 'selected'] | has_llm present: False
```

前端因此把 `hasLlm` 当 True，hybrid 选项在「未就绪/错误」状态下可点击（输入框同时被 `disabled={!ready}` 禁用，所以实际发不出 hybrid 请求，不会造成静默降级）。这仍违反契约 §3.2/§4.1「`has_llm` 供前端决定是否禁用 hybrid」。

**建议**

- status 的早退分支（`load_error`/尚未 ingest）补上 `"has_llm": cfg.has_llm`（也建议带上 `max_concurrent`，避免前端在未就绪态读到不一致的能力信息）。
- 或前端区分 `undefined`（未知）与 `false`（未配置）：未知时 hybrid 保持 disabled，等 ready 状态到达再启用。
- 增加「无产物时 `/api/status` 必含 has_llm」的 API 测试。

---

### F6 [P2] 未 ingest 文档时 hybrid 请求返回 503，而非文档承诺的 400

**位置**

- `src/docqa/api/app.py:689-699`：`_load()` -> `load_error` 503 -> 空问题 400 -> hybrid/no-LLM 400
- `docs/hybrid-mode.md:75`、`:92`：`mode=hybrid` 且未配置生成模型 -> **400**
- 契约 §2.4 同句

**失败场景**

未 ingest 任何文档、且未配置生成模型时，`POST /api/ask {"mode":"hybrid"}` 返回 503（尚未 ingest），而不是 400（hybrid 需要生成模型）。实测：

```text
[ask hybrid no-doc/no-llm] status: 503 | body: {"detail":"尚未 ingest 任何文档。请 ..."}
```

**判断**

两个错误都可用、都可操作；但这是文档/契约层面的无条件承诺与实际优先级不一致。对只按文档写客户端逻辑的调用方，会看到未声明的 503。

**建议**

- 把 hybrid/no-LLM 检查移到 `_load()` 之前（契约字面行为），或
- 在 `docs/hybrid-mode.md` / 契约中明确「文档未就绪时 503 优先于 400」，并加一条无产物 API 测试锁定该顺序。

---

### F7 [P2] hybrid 在 extraction_failed 场景仍先花一次生成调用

**位置**

- `src/docqa/qa/agent.py:128-236`：每轮先检索、起草
- `src/docqa/qa/agent.py:296-309`：gates 通过后才调用 `decide()`
- `src/docqa/qa/policy.py:332-354`：`extraction_failed` 在 `decide()` 第 1 步

**失败场景**

证据触及未识别页（`failed_pages`）时，hybrid 通常仍会先调用一次模型（可能还有修复轮次），`decide()` 最后才以 `extraction_failed` 拒答。strict 至少有覆盖率预判这条零调用出口（覆盖率低时 `decide()` 会先判 `extraction_failed`）；hybrid 在没有歧义候选时没有任何前置 `decide()` 调用（覆盖率预判被跳过），因此 extraction_failed 要等到起草/自检之后才被判定，通常必然先付一次生成调用。（若同时检出歧义候选，前置 `decide()` 会先命中 extraction_failed，此时不会调用模型。）

**可复现步骤**

```bash
DOCQA_DISABLE_DOTENV=1 uv run --no-sync python - <<'PY'
import sys
sys.path.insert(0, "tests")
import test_hybrid_mode as th

p = th.FakeProvider("抗拉强度应大于等于 590 MPa。[p.1 §3.1]\n【模型知识补充】\n同类材料通常更高。")
b = th.make_agent([th.pdf_chunk()], p, failed_pages={1}).answer(
    th.STRENGTH_QUESTION, mode="hybrid")
print("refused:", b.refused, "| reason:", b.refusal_reason.value,
      "| provider calls:", len(p.calls), "| trace:", [t.step for t in b.trace])
PY
```

实测：

```text
refused: True | reason: extraction_failed | provider calls: 1 | trace: ['retrieve', 'draft', 'verify']
```

**建议**

- 在 hybrid 的检索完成后、起草之前，直接按 `failed_pages` 调 `decide()`（或复用同一个提取健康检查）拒答，避免无意义的生成调用；这一改动不改变拒答结果，只降低成本与延迟。
- 增加测试断言该场景 `provider.calls == []`。
- 这与 AI 生成代码的成本约束相关：不要在已知会拒答的路径上先升级到高成本模型调用。

---

### F8 [P2] 文档与验收证据漂移：README 用例数过期；hybrid 验证报告缺失

**位置**

- `README.md:39`：「818 个用例，覆盖率 91%」
- `README.md:453`：「818 个用例中 814 通过、4 个 live 用例跳过」
- `docs/verification/`：只有 2026-10-29/30 的 DOCX 文档，没有 `2026-10-31-hybrid-mode.md`

**实际**

本次只读全量测试：`882 passed, 4 skipped in 31.66s`（总 886）。README 的两处仍是 hybrid 之前的 818/814，差 64 个用例。契约 §5 的前端验收（三视口 E2E、无横向滚动、无 console error、reduced-motion、localStorage、切换只影响下一问）在仓库里没有对应测试或验证报告；`web/package.json` 只有 `typecheck/build`，没有 vitest/Playwright/E2E 脚本。

**建议**

- E 校对时更新 README 两处数字（并确认覆盖率数字与当前 `--cov` 实测一致）。
- 补 `docs/verification/2026-10-31-hybrid-mode.md`，记录三视口 E2E、reduced-motion、localStorage、无 console error 的实测结果；若 E2E 是临时脚本，至少把脚本与命令入库或在报告中给出可复现步骤。
- 若无自动化前端测试，明示这是人工验收证据，不要把「未验证」写成「已验证」。

---

## strict 回归结论

**结论：strict 未因 hybrid 工作发生行为回归。**

| 检查项 | 结论 | 证据 |
|---|---|---|
| `USER_TEMPLATE` 一字未改 | 通过 | 与 `git show HEAD:src/docqa/qa/answer.py` 逐字节相同 |
| `SYSTEM_PROMPT` 一字未改 | 通过（相对 hybrid 基线） | 与 HEAD 相比仅有 DOCX 引用格式改动（`[§3.6 ¶4]`、禁止 `[?]`），该改动记录在 `docs/reviews/2026-10-29-docx-audit.md:144`，不是 hybrid 引入；当前文本被 `tests/test_hybrid_mode.py:57` 的 SHA256 钉住 |
| strict 检查列表不含 `knowledge_label` | 通过 | `verify()` 仅在 `answer_mode == "hybrid"` 追加；`tests/test_hybrid_mode.py:374-396`、`tests/test_cli_api.py:1288-1294` |
| strict 零引用仍失败 | 通过 | `tests/test_hybrid_mode.py:398-401`；`allow_zero_citations` 默认 False |
| 六类拒答 / 覆盖率 / 低分 / 分差 | 通过 | `tests/test_hybrid_mode.py:556-579` 的 strict 基线；全量 882 passed |
| PDF/DOCX 引用、数值落地、条号一致性 | 通过 | 全量测试（含 `tests/docx/`、`tests/test_locator_contract.py`） |
| 旧产物回读 | 通过 | 全量测试；hybrid 未改 `pipeline.load_results` |
| API/CLI strict JSON | 通过（新增字段） | 新增 `answer_mode/model_knowledge/knowledge_used`，旧键不变；契约明确允许新增字段 |
| CLI strict 人读输出 | 通过（新增模式行） | 新增「模式：仅文档」一行，契约 §2.4 要求显示模式 |

两点说明：

1. **哈希钉住的是 DOCX 改动之后的 strict prompt**，不是 hybrid 之前的原始文本。它能防止今后再漂移，但不能证明「相对 DOCX 基线未变」；本次用 `git show HEAD` 人工比对确认：`USER_TEMPLATE` 完全相同，`SYSTEM_PROMPT` 的差异全部是 DOCX 引用格式块。
2. `_CITATION_MARK_RE` 为 DOCX 扩宽了 `[§...]`/`[¶...]` 形态。理论上会让 strict PDF 答案里出现的 `[§...]` 文本从「数值/条号断言」中被剔除；这是 DOCX 工作的一部分，不是 hybrid 引入，且全量测试未出现回归。

## hybrid 专项核对

| 契约项 | 结论 | 备注 |
|---|---|---|
| marker 缺失 | 通过 | 整段按 doc_text 自检；但见 F1 的兜底失效 |
| marker 为空 / 只有空白 | 通过 | `knowledge_used=False`、`model_knowledge=None` |
| marker 重复 | 通过 | 按第一个切分，后续 marker 剥离并计数 |
| marker 大小写/全半角/换行变体 | 未覆盖 | 中文 marker 无大小写；`［模型知识补充］`、跨行等变体会退化为 doc_text（安全失败，除非同时命中 F1） |
| doc_text / knowledge 边界 | 通过 | `partition()` 第一个 marker；head/tail strip |
| 六种引用形态剥离 | 通过 | `_CITATION_MARK_RE` 与 `_CITATION_RE` 形态一致 |
| 变体伪引用剥离 | 不通过 | 见 F3 |
| `[p.]`/`[§...]` 进入 citations/evidence | 通过 | 引用只从 doc_text 解析；API 测试断言 `evidence` 键集合 == citations chunk_id 集合 |
| 伪引用 advisory 不阻断 | 通过 | `severity=ADVISORY`，`gates_passed` 不受影响 |
| 伪引用 advisory 信号 | 不通过（部分） | 只含伪引用时被报「未使用模型知识」，见 F4 |
| 零引用放行条件 | 过宽 | 见 F1；空响应见 F2 |
| `numeric_grounding`/`clause_consistency` 只查 doc_text | 通过 | `verify()` 的 `answer` 参数只传 `draft.text` |
| `sentence_support` 只查 doc_text | 通过 | 同上；零引用时返回 advisory「不适用」 |
| extraction_failed 仍拒答 | 通过 | 但多花一次模型调用，见 F7 |
| 歧义仍拒答且不调模型 | 通过 | `detect_ambiguity()` 在起草前；`tests/test_hybrid_mode.py:756-770` |
| 正文自检失败仍拒答 | 通过 | `tests/test_hybrid_mode.py:727-741` |
| 拒答时清空 `model_knowledge/knowledge_used` | 通过 | `_finalize()` 中 `decision.refused` 时置空 |
| 修复只针对 doc_text | 通过（行为上） | 失败 gate 只来自 doc_text；修复提示会附带知识分离规则与剥离计数，补充段本身不触发修复 |
| API 非法 mode 422 | 通过 | Pydantic `Literal`；测试放宽为 400/422，见测试缺口 |
| API 无 LLM hybrid 400 | 通过（文档就绪时） | 未就绪时 503 优先，见 F6 |
| 无静默 strict 降级 | 通过 | Agent 抛 `ConfigError`；CLI 退出 2；API 400；前端强制 strict 发送且不覆写偏好 |
| 响应三字段 | 通过 | `AnswerBundle.to_dict()` 新增；前端 `normalizeAskResponse()` 容错 |
| CLI 人读 / JSON | 通过 | 模式行 + 独立「模型知识补充」面板；`--json` 含三字段 |
| 前端 radiogroup/方向键/roving/focus ring | 通过（静态审阅） | `ModeSwitch.tsx:62-96`；左右方向键对两选项等价于 toggling，正确 |
| reduced-motion | 通过 | `workbench.css:1625-1635` 关闭 indicator/hint 过渡与 sheen 动画 |
| localStorage 首屏同步读取 | 通过 | `Workbench.tsx:56-72,184`；读写 try/catch |
| 无 LLM 禁用且不覆写 | 通过（ready 态） | `effectiveMode` / `handleModeChange`；未就绪态见 F5 |
| 请求中禁用切换 | 通过 | `busy={asking}` -> 两个 radio `disabled` |
| 切换只影响下一问 | 通过 | `handleAsk` 在发请求时捕获 `effectiveMode`；已有 `AnswerCard` 用 `response.answer_mode` |
| 已有答案 chip 由 `answer_mode` 决定 | 通过 | `AnswerCard.tsx:96`、`ModeChip` |
| 知识块纯文本、无引用 chip、无 HTML 注入 | 通过 | `AnswerCard.tsx:248-257` 的 `<p>{modelKnowledge}</p>`；全仓无 `dangerouslySetInnerHTML` / `innerHTML` |
| 空正文 + 知识 | 通过 | 显示「文档中未找到可引用的依据」+ 知识块 |
| 拒答说明按模式 | 通过 | 拒答卡 + hybrid 说明 `AnswerCard.tsx:122-126` |
| 1440/768/390 无横向滚动、无 console error | 未验证 | 无浏览器/E2E 证据；静态 CSS 未见明显溢出风险（mode-switch 340px、header flex-wrap） |

**前端低影响备注（不计入 findings）**

- `ModeSwitch.tsx:85-89` 在「无 LLM」时给 hybrid 按钮同时设 `disabled` 与 `aria-describedby={noteId}`。禁用按钮不可聚焦，屏幕阅读器通过 radio 导航时可能读不到「未配置生成模型」的说明（提示文本本身在页面上可见）。建议把提示文本关联到 radiogroup/容器，或不要禁用而是用 `aria-disabled` 保留可聚焦性。
- 请求进行中（`busy`）把当前选中的 radio 也设为 `disabled`，radiogroup 短暂失去 Tab 停点；功能上可接受。
- 契约 §3.3 写「额外 `含模型知识` badge」，实现是「已用模型知识」（`AnswerCard.tsx:167`），当前 `docs/hybrid-mode.md` §5 已按实现更新。属契约措辞与实现/文档的轻微漂移，不影响行为。

---

## 测试缺口

已通过：

- `DOCQA_DISABLE_DOTENV=1 uv run --no-sync pytest tests -o addopts="" -p no:warnings -q` -> **882 passed, 4 skipped in 31.66s**
- 定向：`tests/test_hybrid_mode.py tests/test_cli_api.py` -> **135 passed in 4.70s**
- `cd web && npm run typecheck` -> exit 0
- `uv run --no-sync ruff check src tests` -> all checks passed

缺口（建议补）：

1. **F1**：没有「否定句 + 肯定性无引用句」必须拒答的测试；现有 `test_hybrid_negative_doc_text_with_zero_citations_passes_gates` 只覆盖纯否定。
2. **F2**：没有空响应、纯空白响应、marker-only 的 agent/API 测试；现有测试只覆盖「空正文 + 非空知识」。
3. **F3**：没有空格/大小写/全角括号伪引用变体的剥离与计数测试。
4. **F4**：没有 `knowledge_used=False + knowledge_citations_stripped>0` 的 advisory 测试。
5. **F5**：没有「无产物时 `/api/status` 含 `has_llm`」的 API 测试；前端没有任何单元测试。
6. **F6**：没有「无产物 + hybrid + 无 LLM」的 503/400 顺序测试。
7. **F7**：没有 assertion `provider.calls == []` 的 extraction_failed 零调用测试。
8. **前端**：无 vitest/Playwright/E2E；契约 §5 的三视口、reduced-motion、localStorage、切换只影响下一问、无 console error 均无仓库内自动化或验证报告。
9. **测试质量**：`tests/test_cli_api.py:1262-1265` 对非法 mode 接受 `(400, 422)`，而文档承诺 422，建议收紧断言；`tests/test_hybrid_mode.py:230-234` 的 hybrid prompt 测试只查关键词，建议像 strict 一样固定 hybrid prompt 文本/哈希；`tests/test_cli_api.py:1298-1304` 的类级 fixture 用实例方法定义，已触发 `PytestRemovedIn10Warning`，建议改为 `@staticmethod`/`@classmethod`。
10. **API 无 LLM 路径**：`test_hybrid_without_llm_is_400_not_silent_strict` 只断言错误响应里没有 `answer_mode`（对 `{"detail": ...}` 恒真），可再断言未构造 provider / 未发起生成调用。

## 文档一致性

| 文档/断言 | 实际 | 结论 |
|---|---|---|
| `docs/hybrid-mode.md:3` 状态「已实现（2026-10-31）」 | A/B/D 已完成 | 一致（21:03 版本已更新；审计初读时的「A/B/D 实现中」已消失） |
| `docs/hybrid-mode.md:91` 非法 mode -> 422 | Pydantic 422 | 一致 |
| `docs/hybrid-mode.md:119-120` 非法 `--mode` -> 退出 2 | CLI `ConfigError` exit 2 | 一致 |
| `docs/hybrid-mode.md:75/92` 无 LLM hybrid -> 400 | 文档就绪时 400；未就绪时 503 | 不一致（F6） |
| `docs/hybrid-mode.md:110` `has_llm` 决定前端禁用 | 未就绪/装载失败时 status 缺该字段 | 不一致（F5） |
| `docs/hybrid-mode.md:158-159` 零引用条件「很窄」，有内容正文仍必须引用 | 否定词子串即可豁免整段 | 不一致（F1） |
| `docs/hybrid-mode.md:167-168` 剥离过引用 -> advisory 不通过 | 只含伪引用时 advisory 通过 | 不一致（F4） |
| `docs/hybrid-mode.md:169-171` 不写 marker 由 strict 自检兜底 | 否定词子串可绕过 | 不一致（F1） |
| `docs/hybrid-mode.md:123` 正文空 + 补充段 -> 「文档中未找到可引用的依据」 | CLI/前端均实现 | 一致 |
| `docs/hybrid-mode.md:137-139` 模式 chip / 已用模型知识 badge | 实现 | 一致 |
| `docs/hybrid-mode.md:140-142` 未配置模型强制 strict 发送、不覆写偏好 | `effectiveMode` / `handleModeChange` | 一致（ready 态） |
| `docs/design.md:156-173` 决策 8 | 与实现一致 | 一致（F1/F4 的承诺同样受影响） |
| `README.md:226-245` 回答范围说明 | 与 API/CLI/前端一致，除 F5/F6 的边界 | 基本一致 |
| `README.md:39/453` 818 用例 / 814 通过 / 4 跳过 | 882 passed / 4 skipped（共 886） | 不一致（F8） |
| 契约 §5 前端三视口验收 | 无仓库内 E2E/验证报告 | 证据缺失（F8） |

## 已执行命令与结果

```text
DOCQA_DISABLE_DOTENV=1 uv run --no-sync pytest tests -o addopts="" -p no:warnings -q
  -> 882 passed, 4 skipped in 31.66s

uv run --no-sync pytest tests/test_hybrid_mode.py tests/test_cli_api.py -o addopts="" -p no:warnings
  -> 135 passed in 4.70s

cd web && npm run typecheck
  -> exit 0

uv run --no-sync ruff check src tests
  -> All checks passed

复现脚本：系统临时目录，仅 import 仓库代码与 tests 辅助类，未写入仓库
```

## 审计声明

本次为**只读审计**：除本报告外，未创建或修改任何代码、测试、配置、数据、构建产物；未执行 commit/push；未读取 `.env` 内容；未调用真实模型端点。所有复现均使用 `FakeProvider` / `NullProvider` 与 `DOCQA_DISABLE_DOTENV=1`。

---

## 复核（2026-10-31）

- 复核对象：A/B/D 按 [`2026-10-31-hybrid-mode-triage.md`](2026-10-31-hybrid-mode-triage.md) 修复后的工作树（triage 21:23；docs 21:38；web assets 21:25）
- 复核方式：只读代码 + 全量/定向测试 + 系统临时目录中的假 provider 探针；未修改任何代码/测试/配置；未 commit/push；未读取 `.env`；未调用真实模型端点（探针均设 `DOCQA_DISABLE_DOTENV=1`）
- 复跑结果：**921 passed / 4 skipped**（复跑两次：32.94s / 34.87s）；定向 `tests/test_hybrid_mode.py tests/test_cli_api.py` **174 passed in 4.76s**；`npm run typecheck` exit 0；`ruff check src tests` all checks passed

### 结论

**原审计/安全审计的 P0、P1 均已按分诊修复；复核新增 1 个 P1（`doc_sentence_support` 的短句豁免）与 8 个 P2。**

- 若按分诊 §2「hybrid 文档正文必须句句可证：每句必须有引用」的验收口径，**暂不建议放行**：短于 6 字的无引用断言会被 gate 直接跳过，仍可带真实 locator 作为文档正文放行（见 NEW-P1）。
- 若团队正式接受「短句不逐句校验」并把它写入契约/验收（而不是只在 docs 里补一句），则 NEW-P1 可降级为已接受风险；其余 P2 不阻断。
- 原 P0/P1 的复核证据如下，未发现回退。

| 原 finding | 原级别 | 复核状态 | 证据 |
|---|---|---|---|
| SEC-HYB-01 失败页未召回时绕过 extraction_failed | P0 | 已修复 | 0 命中/低覆盖 + 失败页 -> `extraction_failed`、`provider.calls=0`；证据充足且失败页不相关 -> 正常回答；证据触及失败页 -> 仍拒答 |
| SEC-HYB-02 未标注模型知识 + 真实 locator | P0 | 已修复（句级），残余见 NEW-P1 | 通用知识 + 真实 locator 被 `doc_sentence_support` 拒；strict 仍 4 项检查 |
| SEC-HYB-03 证据内固定 marker 劫持拆分 | P1 | 已修复 | hybrid 证据中的 marker 被替换；marker 只认单独成行；模型照抄证据不会截断正文 |
| SEC-HYB-04 空输出被当成功 | P1 | 已修复 | 空串/纯空白/仅 marker -> `verification_failed`、`answer=None`、调用 max_attempts 次 |
| CODE-P1 否定词子串放大零引用 | P1 | 已修复（分句级），残余见 P2-2 | 「未规定 A；B 应采用合金钢」被拒；纯否定 + 补充段仍通过 |
| F2 空响应/空白响应成功 | P2 | 已修复 | 同 SEC-HYB-04 |
| F3 伪引用变体漏剥 | P2 | 已修复 | `[p. 3 §3.6]`、`[ P.3 ]`、全角/零宽变体被剥离并计数 |
| F4 仅伪引用补充段 advisory 漏报 | P2 | 已修复 | `knowledge_label(used=False, stripped=2)` -> passed=False |
| F5 status 早退缺 has_llm | P2 | 已修复 | 无产物 status 含 `has_llm`；前端改为 `=== true` |
| F6 未 ingest 时 503 vs 400 | P2 | 文档修正 | docs 明确 503 优先、400 只用于请求不合法 |
| F7 extraction_failed 前多花一次调用 | P2 | 部分修复 | 0 命中/低覆盖前置拒答不调模型；证据本身触及失败页时仍先起草 1 次（保留 decide 检查） |
| F8 README 数字/验证报告 | P2 | 待 T8 | README 已标注“数字待 T8 最终刷新”；验证报告仍缺 |
| SEC-HYB-05 仅引用补充段 advisory | P2 | 已修复 | 同 F4 |
| SEC-HYB-06 引用剥离变体 | P2 | 已修复 | 同 F3 |
| SEC-HYB-07 CLI 控制字符 | P2 | 部分修复 | hybrid 正文/补充段经 split 剥离 C0/C1；strict 答案与引用 quote 仍可把 ESC 送到终端（见 P2-3） |
| SEC-HYB-08 strict prompt 哈希基线 | P2 | 文档注明 | docs §7 已注明 DOCX 改动非本功能引入 |
| SEC-HYB-09 注入命中不进问答 | P2 | 未修，文档记录 | docs §7 列为已知限制 |
| SEC-HYB-10 CSP/安全头 | P2 | 未修，文档记录 | docs §7 列入部署注意 |
| SEC-HYB-11 问题长度/限流 | P2 | 部分修复 | 4000 字上限已加（400）；无 body 大小上限/按客户端限流（见 P2-8） |

### 逐项复核

**1) CODE-P1：`is_negative_only`**

- 实现位置：`src/docqa/qa/policy.py:475-493`，按 `[。；;！？，,\n]` 分句，每个非空分句都必须含否定标记；空文本返回 False。`src/docqa/qa/agent.py:309-312` 只在 hybrid 使用它，strict 的 `_finalize()` 仍用原 `is_in_document_negative()`（`agent.py:480`），strict 语义未动。
- 实测：
  - 「文档中未规定 A 类材料的要求；B 类材料应采用合金钢。」-> `refused=True`、`verification_failed`、2 次调用（先修复后拒答）。
  - 「文档中未规定 A，也未提及 B。」-> 放行，`knowledge_used=False`。
  - 「文档中未规定冲击韧性要求。\n【模型知识补充】\n……」-> 放行，正文为空引用、补充段独立，`knowledge_used=True`。
- 结论：分诊要求的三项均满足；残余 run-on 见 P2-2。

**2) SEC-HYB-02：`doc_sentence_support`**

- 只 hybrid：`verify()` 仅在 `answer_mode=="hybrid"` 追加（`src/docqa/qa/verify.py:877-886`）。strict 报告实测仍为 `citation_validity / numeric_grounding / clause_consistency / sentence_support` 四项，无 `doc_sentence_support`/`knowledge_label`。
- GATE + 0.35：`HYBRID_SENTENCE_SUPPORT_MIN = 0.35`（`verify.py:62`），`severity=GATE`（`verify.py:692-696`、`:729-738`）。
- 每句引用 + 重叠：通用知识 + 真实 locator「冲击韧性通常采用 [p.1 §3.1] 的方法测定。」-> gate 失败（重叠 0%），端到端最终 `verification_failed`。
- 误杀正常改写：**存在**。实测（证据「3.1 抗拉强度应大于等于 590 MPa。」）：
  - 「抗拉强度要求不低于 590 MPa。[p.1 §3.1]」-> 通过（测试用例，接近原文）。
  - 「材料应能承受至少 590 MPa 的拉应力。[p.1 §3.1]」-> 拒绝，重叠 31%。
  - 「该材料须能承受 590 兆帕的拉伸载荷。[p.1 §3.1]」-> 拒绝，重叠 13%；端到端 2 次调用后 `verification_failed`。
  - 「本产品需具备不低于 590 MPa 的抗拉强度。[p.1 §3.1]」-> 通过。
  阈值是分诊明确接受的工程折中，但「兆帕」等单位改写会被判低重叠；见 P2-1。
- 结论：按“句级、0.35、每句有引用且与所引证据重叠”的口径已落地；短句/句内粒度残余见 NEW-P1。

**3) SEC-HYB-01：失败页前置**

- 位置：`src/docqa/qa/agent.py:162-192`。实测：
  - 0 命中 + `failed_pages={1}` -> `extraction_failed`、`provider.calls=0`、trace `retrieve -> precheck`。
  - 低覆盖率 + 失败页 -> 同上，0 调用。
  - 证据充足（第 1 页）+ 不相关失败页（第 2 页）-> 正常回答，1 次调用。
  - 证据本身触及失败页（第 1 页）-> 仍 `extraction_failed`，1 次调用（保留 `decide()` 检查）。
  - 多文档：失败页在 A 文档、证据来自 B 文档同页码 -> 不被误伤。
- 结论：分诊要求的三组行为均满足；DOCX `unsupported` 仍未进入 QA（P2-5），多文档低覆盖时拒答理由未带 doc_id（P2-6）。

**4) SEC-HYB-03 / SEC-HYB-04：marker 与空输出**

- marker 行级：`src/docqa/qa/answer.py:79-81` 只匹配单独成行的 marker；`split_hybrid_answer()` 用 `_HYBRID_MARKER_LINE_RE` 定位（`:205-219`）。行内 marker、冒号后缀均不分段（安全失败，后续 gate/引用检查会拦）。
- 证据改写：`build_evidence_block(chunks, mode="hybrid")`（`answer.py:222-249`）把证据里的 `【模型知识补充】` 替换成 `【模型知识补充（文档原文）】`；mapping/chunk 原文不变。实测证据开头/中间含 marker、模型照抄证据后再补知识，正文不被截断、补充段正确。
- 空输出：`agent.py:309-312` 要求空正文必须 `knowledge_used=True`；空串、纯空白、仅 marker、marker+仅引用都走修复并在 max_attempts 后 `verification_failed`，`answer=None`，`model_knowledge=None`。实测 2 次调用（`max_attempts=2`）。
- 控制字符：`answer.py:108-114,205` 在 split 时剥离 C0/C1（保留 `\n`/`\t`）；hybrid 正文/补充段已净化。CLI 其余字段见 P2-3。
- 结论：分诊验收项满足。

**5) API / CLI**

- status 早退：`src/docqa/api/app.py:446-453` 早退 JSON 含 `has_llm`；实测无产物 status keys 为 `docs/domain/error/has_llm/ready/selected`，`has_llm=False`。
- 问题长度：`MAX_QUESTION_CHARS=4000`（`app.py:82`），超限 400、恰好 4000 通过（测试 `TestAskInputLimits` 通过）；检查在 `_load()` 与 JSON 解析之后（P2-8 残余）。
- 无 LLM：API `mode=hybrid` -> 400（文档就绪时）；无产物时 503（docs 已明确该优先级）；CLI exit 2。
- 非法 mode：API 实测 422（Pydantic literal_error）；CLI exit 2。
- strict JSON：新增 `answer_mode/model_knowledge/knowledge_used`，旧键不变（测试通过）。
- CLI 控制字符：**部分**。hybrid `model_knowledge` 经 split 净化，现有测试 `test_hybrid_knowledge_control_chars_never_reach_the_terminal` 通过；但 CLI 层没有统一 sanitizer，strict 答案与引用 quote 仍可把 ESC 送到终端（P2-3）。

**6) 前端**

- `hasLlm = status?.has_llm === true`（`web/src/pages/Workbench.tsx:291-292`）；未加载/错误/旧响应缺字段时 hybrid disabled，符合 docs §5。
- 构建产物已刷新：`src/docqa/web/assets/index-C3NscO8E.js`（21:25，晚于 Workbench 21:24），bundle 内可见 `(n==null?void 0:n.has_llm)===!0`；`index.html` 引用新文件。
- ModeSwitch（radiogroup/方向键/roving tabindex/focus-visible/reduced-motion）、知识块纯文本、切换只影响下一问、已有答案 chip 由 `answer_mode` 决定：相关文件自首审后未改（ModeSwitch 20:34、AnswerCard 20:30、workbench.css 20:34），首审结论继续成立；`npm run typecheck` exit 0。
- 新缺口：`doc_sentence_support` 未加入前端 `CHECK_LABELS`（`web/src/format.ts:142-150`），hybrid 失败时卡片 chip/自检列表显示原始英文名（P2-4）。

**7) 文档**

- `docs/hybrid-mode.md`（21:38）已按收紧后的契约更新：行级 marker、证据替换、C0/C1、零引用两条件、extraction_failed 前置、空输出拒答、`doc_sentence_support` GATE/0.35、status `has_llm`、503/400 边界、已知限制（注入/CSP/客户端限流/strict prompt 基线）。
- 仍不精确：§6 末条「模型把知识混进正文且不标注……没有引用的断言会被拦截」在 NEW-P1/P2-2 的反例下过强；§7 未列句级粒度误杀与 CLI 控制字符残余（P2-7）。
- README：line 39 已标“数字待 T8”；line 457 仍是 818/814（T8 待刷新，不计入本次 finding）。

**8) 测试质量**

- 新增失败路径测试是真实断言，不是同义反复：`provider.calls == []`（失败页前置）、`chk.passed is False`（doc_sentence_support/negative-only）、`answer is None`/`max_attempts`（空输出）、`knowledge_label(used=False, stripped=2).passed is False`（SEC-HYB-05）。去掉对应实现后这些测试会失败。
- 没有把 gate 写成永远通过：hybrid 报告实测 6 项（4 基础 + doc_sentence_support + knowledge_label），strict 4 项。
- 缺口见「残余 P2」P2-7 与测试缺口小节。

### 新发现与残余 P2

| ID | 级别 | 标题 | 位置 |
|---|---|---|---|
| NEW-P1 | **P1** | `doc_sentence_support` 的「≥6 字实质句」豁免可被短句绕过；句内未支撑子句也随句通过 | `src/docqa/qa/verify.py:699-727`、`tests/test_hybrid_mode.py:612-660` |
| P2-1 | P2 | 0.35 阈值误杀正常改写（单位/同义改写被判低重叠） | `src/docqa/qa/verify.py:62,718-727`、`tests/test_hybrid_mode.py:625-633` |
| P2-2 | P2 | `is_negative_only` 的无标点 run-on 仍可零引用放行 | `src/docqa/qa/policy.py:460,486-493`、`src/docqa/qa/agent.py:309-312` |
| P2-3 | P2 | CLI 控制字符只净化 hybrid 字段；strict 答案与引用 quote 仍输出 ESC | `src/docqa/cli.py:550-599`、`src/docqa/qa/answer.py:110-114,205` |
| P2-4 | P2 | 前端缺 `doc_sentence_support` 中文标签，失败时显示原始英文名 | `web/src/format.ts:142-150`、`web/src/components/AnswerCard.tsx:259-267` |
| P2-5 | P2 | DOCX `unsupported` 仍未进入 QA/hybrid 判断 | `src/docqa/api/app.py:468`（仅 status/manifest） |
| P2-6 | P2 | 多文档下失败页前置拒答不带 doc_id，可能把 A 文档失败页报成当前问题原因 | `src/docqa/qa/agent.py:162-188` |
| P2-7 | P2 | 测试/文档缺口：无短句/句内子句/run-on/CLI quote 测试；改写测试接近原文；非法 mode 断言仍接受 400/422；类级 fixture 警告；docs §6 兜底表述过强 | `tests/test_hybrid_mode.py`、`tests/test_cli_api.py:1262-1265,1341-1347`、`docs/hybrid-mode.md:211-213` |
| P2-8 | P2 | `/api/ask` 无请求体大小上限（4000 字检查在 JSON 解析/`_load` 后）；无按客户端限流；status 注释仍写 `!== false` | `src/docqa/api/app.py:82,446-452,701-706`、`docs/hybrid-mode.md:232` |

#### NEW-P1 [P1] 短句绕过 `doc_sentence_support`，未支撑断言仍可带真实引用进入 `answer`

**复现（离线，假 provider）**

```bash
DOCQA_DISABLE_DOTENV=1 uv run --no-sync python - <<'PY'
import sys
sys.path.insert(0, "tests")
import test_hybrid_mode as th

for text in ["应使用钢。[p.1 §3.1]", "不得使用铝。[p.1 §3.1]"]:
    p = th.FakeProvider(text)
    b = th.make_agent([th.pdf_chunk()], p).answer(th.STRENGTH_QUESTION, mode="hybrid")
    print(repr(text), "| refused:", b.refused, "| gates:", b.verification.gates_passed,
          "| citations:", len(b.citations), "| answer:", repr(b.answer))
PY
```

实测：

```text
'应使用钢。[p.1 §3.1]'   | refused: False | gates: True | citations: 1 | answer: '应使用钢。[p.1 §3.1]'
'不得使用铝。[p.1 §3.1]' | refused: False | gates: True | citations: 1 | answer: '不得使用铝。[p.1 §3.1]'
```

**根因**：`check_doc_sentence_support()` 先过滤掉去引用后不足 6 字的句子（`verify.py:699-702`）；没有实质句时直接 `passed=True`（`:703-709`）。上面两句话分别只有 4/5 个汉字，gate 返回「答案中没有需要逐句校验的实质句」，`citation_validity` 因 locator 真实而通过，`numeric_grounding`/`clause_consistency` 无输入，`sentence_support` 只是 advisory，最终放行。这正是 SEC-HYB-02 的同类危害：**未支撑的断言以文档正文 + 可点击引用 chip 的形式出现**，只是句子短于阈值。

同一 gate 的句内粒度也可绕过（P2 级，同一根因）：

```text
'抗拉强度为 590 MPa，冲击韧性采用夏比试验 [p.1 §3.1]。'
-> gate=True, refused=False, citations=1；后半句「冲击韧性采用夏比试验」无证据支撑，
   但整句与证据重叠达标（前半句贡献），随句放行。
```

另外，`is_negative_only()` 对无标点 run-on 仍是单分句（`policy.py:486-493`），例如：

```text
'文档中未规定 A 类材料的要求 B 类材料应采用合金钢。'
-> allow_zero_citations=True, doc_sentence_support 整条跳过, refused=False, citations=0。
```

**建议**

1. 取消或显著降低「≥6 字」豁免：只要一句话在去掉引用后仍有非空内容（至少 2 个字符），就必须有引用且与所引证据重叠；`citation_validity` 已负责零引用拦截，短句不应被直接跳过。
2. 句内按 `，、：` 再分句做逐子句引用/重叠检查，或至少要求「每个含实词的子句」都能映射到所引证据。
3. 在 `is_negative_only()` 的分句集合中加入 `：、`，或对含否定词的分句再做「是否还含肯定性规范动词」的检查。
4. 增加测试：`应使用钢。[p.1 §3.1]`、`不得使用铝。[p.1 §3.1]`、`抗拉强度...，冲击韧性... [p.1 §3.1]`、无标点 run-on，均必须拒绝。
5. 若团队决定保留当前句级/6 字口径，必须把「短句与句内子句不逐句校验」写进 `docs/hybrid-mode.md` 已知限制，并停止使用「句句可证」的强表述。

#### 其余残余 P2 说明

- **P2-1 改写误杀**：实测「材料应能承受至少 590 MPa 的拉应力」（31%）与「该材料须能承受 590 兆帕的拉伸载荷」（13%）均被拒，2 次调用后 `verification_failed`。现有正向测试只用了接近原文的改写。建议用一组同义改写做校准；至少记录「重度改写可能被拒」的已知限制。
- **P2-2 run-on**：见 NEW-P1 的第三种载荷；分诊的「未规定 A；B 应……」已修复，但无标点版本仍放行。
- **P2-3 CLI 控制字符**：实测 strict 答案 `...\x1b[mRED` 与引用 quote `...\x1b[mGREEN` 均被 `_print_bundle` 原样输出（`Text` 不剥离 ESC）；hybrid 正文/补充段因 `split_hybrid_answer()` 已净化。建议在 `cli._print_bundle()` 入口对所有不可信字段（answer/model_knowledge/quote/check detail/refusal detail）统一 `sanitize_console_text()`，并补 strict + quote 测试。
- **P2-4 前端标签**：`CHECK_LABELS` 缺 `doc_sentence_support`，hybrid 自检失败时 Web 显示英文标识；建议加「逐句支撑」。
- **P2-5 DOCX unsupported**：status/manifest 有 `unsupported`，但 `QaAgent`/`policy` 不读取；DOCX 脚注/文本框/公式等未解析构造仍可能被 hybrid 以「未规定 + 知识」补位。安全审计 SEC-HYB-01 的建议未落地；建议至少 warning/advisory，必要时 hybrid 拒答。
- **P2-6 多文档失败页**：`failed_page_nos` 丢掉了 doc_id；实测 A 文档失败页 + B 文档低覆盖问题 -> `extraction_failed`，detail 只写 `[1]`，用户不知道该重扫哪份。建议 detail 带 doc_id，或按失败页所属文档与召回证据文档做交集。
- **P2-7 测试/文档**：无 NEW-P1/P2-2/P2-3 的回归测试；`test_doc_sentence_support_passes_evidence_supported_rephrase` 过弱；`test_invalid_mode_is_rejected_by_validation` 仍 `(400, 422)`；类级 fixture 仍触发 `PytestRemovedIn10Warning`；docs §6「没有引用的断言会被拦截」在 NEW-P1 下过强。
- **P2-8 输入/注释**：4000 字检查在 Pydantic 解析与 `_load()` 之后，超大 JSON body 仍会先被解析；未按客户端限流（docs §7 已列后者）；`app.py:450` 注释仍写前端用 `!== false`，实际已是 `=== true`。

### 复核后的放行判断

- **P0：0**（原 2 个 P0 均修复）。
- **P1：1 个新发现**（NEW-P1 短句/句内粒度绕过；原 3 个 P1：CODE-P1、SEC-HYB-03、SEC-HYB-04 均修复）。
- **P2：8 个残余**（上表）。

**建议**：若按分诊 §2「每句必须有引用」验收，先修 NEW-P1（成本很低：去掉/降低 6 字豁免 + 补 3 个测试）；修完后可放行，P2 记录/排期。若团队选择保留句级口径，则必须把短句与句内子句的豁免写入契约和 `docs/hybrid-mode.md` 已知限制，并把“句句可证”改为“实质句（≥6 字）可证”，同时补 CLI quote 净化与前端标签这两个低成本 P2。

### 复核声明

本次复核仍为**只读**：除向本报告追加本节外，未修改任何代码、测试、配置、数据或构建产物；未 commit/push；未读取 `.env`；未调用真实模型端点。复现均在系统临时目录、使用 `FakeProvider` 与 `DOCQA_DISABLE_DOTENV=1`。

### 复测 3（最终收紧）

- 最终快照：`verify.py` 23:00:50、`agent.py` 22:50:19、`policy.py` 22:46:28、`tests/test_hybrid_mode.py` 23:01:21、`web/src/format.ts` 23:05:53、构建产物 `index-BK8PULSW.js` 23:06:13；复核结束时工作树已稳定
- 说明：复核期间工作树短暂处于 `is_negative_only` → `is_generic_negative` 迁移中（旧测试 import 失败/4 个用例失败）；测试更新后最终快照全绿，以下结论以**最终快照**为准
- 复跑结果：**989 passed / 4 skipped**（32.17s）；`tests/test_hybrid_mode.py` **154 passed**；`tests/test_cli_api.py` **88 passed**；`npm run typecheck` exit 0；`ruff check src tests` all checks passed
- 只读：未修改代码/测试/配置；未 commit/push；未读取 `.env`；未调用真实模型端点

#### 结论

**P0 = 0；P1 = 1 个残余（逐字归一化按“字符”删除 的/为/和/按，允许在内容词内部插入这四个字符并通过 gate）；P2 = 5 类残余。** 其余前两轮的 P0/P1 均已修复。

- 若要求 hybrid 文档部分严格逐字，应先处理 R3-1（P1）或明确接受并在 docs/契约中写清“这四个字符可被任意插入/删除”；否则可放行（WARNING），R3-1 记为已知残余。
- 其余 P2 为 sanitizer 覆盖、`doc_citation_marks` 误杀、具体否定/prompt 与白名单不一致、文档精度，以及 T8/E2E/低风险项；不阻断核心可追溯性。

#### 1) strict

- `verify()` 在 strict 下仍只产出 `citation_validity / numeric_grounding / clause_consistency / sentence_support` 四项；`doc_sentence_support`、`doc_citation_marks`、`knowledge_label` 仅在 `answer_mode=="hybrid"` 追加（`verify.py:1028-1042`）。实测 strict 4 项、hybrid 7 项。
- `SYSTEM_PROMPT` / `USER_TEMPLATE` 哈希仍为 `22fc5621...` / `7e02c7c0...`，与测试常量一致；本轮未改 strict prompt。
- strict 零引用仍失败（`test_strict_zero_citations_still_fails` 与全量通过）；strict 无 hybrid 检查。

#### 2) hybrid 7 项与逐字 gate

- 检查列表实测：基础 4 项 + `doc_sentence_support` + `doc_citation_marks` + `knowledge_label`（共 7 项）。
- `doc_sentence_support`：仅 hybrid、GATE、子句级（`。！？；;，,：、\n`，引用归前句）、**短句不豁免**（无长度过滤）、字符级逐字（去引用/NFKC/空白/表格 `|`/四个连接词）；`±`、`%`、`℃`、`μ`、`该`、`被`、`以上` 等保留；边界检查阻止截断数值/单位（`0.5 mm` vs `±0.5 mm`、`80` vs `80 ℃`）；首尾包裹引号/书名号/括号剥离（`_VERBATIM_WRAP_CHARS`）。
- 否定子句豁免已收窄为**通用否定白名单** `is_generic_negative`（`policy.py:466-503`）：`文档中未规定。` / `本标准未提及相关内容。` 通过；具体否定、run-on、含具体对象/谓词均不通过。
- `doc_citation_marks`：扫描原始 `doc_text` 的 citation-like token，逐个绑定已解析 citations；无法绑定即 GATE 失败，不因 allow_zero 跳过；`[p.99 §9.9]`、`[p. 3]` 变体均被拒。`_evidence_text_for_marks` 改为“绑定失败即失败”，不再回退到全部 citations。
- 实测其他 chunk 路径：cite A quote B -> 失败；cite A+B quote B -> 通过（B 本身被合法引用）；bogus mark + B cited elsewhere -> `doc_sentence_support` + `doc_citation_marks` 双失败；不存在用别的 chunk 文本绕过而不引用它的路径。

#### 3) NEW-7 / NEW-8 / NEW-9 回归测试真实性（抽看断言）

- **NEW-7（通用否定白名单）**：`TestGenericNegative::test_non_generic_forms_are_rejected` 断言具体否定/run-on 不是 `is_generic_negative`；`test_positive_predicates_cannot_hide_behind_generic_prefix` 参数化使用/要求/含有/达到/保持/提高/降低/包括/属于/设置/适用于/进行/具有/分为/规定等谓词，全部断言 False。旧 `is_negative_only`（含情态词黑名单）对这些具体否定/无标点 run-on 会返回 True、旧 agent 会零引用放行；这些断言在旧实现上会失败（或旧函数不存在直接 collection 失败）。`test_allow_zero_with_citations_still_runs_gate` 断言 allow_zero=True 时 gate 仍失败；旧 gate 在 allow_zero 时直接 pass，测试会失败。
- **NEW-8（字符级/边界）**：`TestVerbatimNormalization::test_missing_pm_sign_fails`（0.5 mm vs ±0.5 mm）、`test_missing_temperature_unit_fails`（80 vs 80 ℃）、`test_missing_micron_unit_fails`（1 vs 1 μm）、`test_missing_percent_fails`（5 vs 5%）、`test_qualifier_insertion_fails`（插入“该”）、`test_passive_voice_insertion_fails`（插入“被”）均断言 `chk.passed is False`。旧 token 归一化会丢弃 ±/℃/μ/该/被（我们前一轮实测旧 gate 会放行这些载荷），这些测试在旧实现上会失败。
- **NEW-9（伪 mark 绑定）**：`TestDocCitationMarks` 用 `next(c for c in report.checks if c.name == "doc_citation_marks")` 取检查；旧实现没有该检查 -> `StopIteration`，测试直接失败；agent 级 `test_generic_negative_with_fake_citation_is_refused` 断言 `[p.99 §9.9]` 被拒，旧实现会放行。
- 结论：回归测试不是同义反复，均以具体布尔/offenders 断言旧行为会失败；但**未覆盖** R3-1 的内容词字符插入、R3-3 的 `[p.3]` 文档内容误杀、R3-4 的具体否定 + 真实引用。

#### 4) API / CLI

- status 早退带 `has_llm`（测试 `test_status_without_artifacts_still_reports_has_llm` 通过）；`MAX_QUESTION_CHARS=4000`，超限 400、恰好 4000 通过；无 LLM hybrid 400、CLI exit 2；非法 mode 422、CLI exit 2；`evidence` 键集合仍只含 citations。
- **上传成功**：`state["selected"] = [result.doc_id]`，并对产物目录 `os.utime(out_dir, None)`；`test_reupload_selects_the_uploaded_docx_over_a_newer_pdf` 覆盖幂等二次上传与重启后仍选中刚上传文档，通过。
- **控制字符**：`_sanitize_payload` / `_sanitize_bundle_dict` **已经覆盖** `verification.checks[].detail` 与 `offenders`（A 的担心不成立）；实际遗漏是 `citations[].rendered`、`citations[].clause_label`、`candidates[].citation`，以及 CLI `_print_bundle` 的 rendered citation / candidate citation（见 R3-2）。证据 `text` 已净化；`AnswerBundle.to_dict` 覆盖 answer/model_knowledge/refusal_detail/quote/candidate interpretation/value/trace detail。
- **CLI 自检表英文名**：CLI 历来对所有检查项显示英文名（citation_validity 等），`doc_citation_marks` 显示英文名与现有风格一致，可接受；Web 已加中文标签（见第 5 项）。

#### 5) 前端

- `format.ts` 已加 `doc_sentence_support: "句级引用支撑"` 与 `doc_citation_marks: "引用标记绑定"`；构建产物 `index-BK8PULSW.js` 23:06 已包含两者及 `has_llm === true`；`index.html` 引用新 bundle。
- `hasLlm = status?.has_llm === true`（Workbench 21:24 未变）；ModeSwitch/知识块/切换时序/a11y 文件自首审后未改，原结论继续成立（radiogroup、方向键、roving tabindex、focus-visible、reduced-motion、切换只影响下一问、知识块纯文本/无 chip）。既有低影响 a11y 备注仍在：无 LLM 时 `aria-describedby` 关联到不可聚焦的 disabled radio；请求中选中 radio 也被 disabled，radiogroup 短暂失去 Tab 停点。
- `npm run typecheck` exit 0；T8 已补 `docs/verification/2026-10-31-hybrid-mode.md`（23:38），报告 1440/768/390 E2E 全过（含 ModeSwitch/焦点/请求中禁用/切换只影响下一问/无横向滚动/无 console error）。本次只读未独立复跑浏览器 E2E，以该报告为证据。

#### 6) 文档

- `docs/hybrid-mode.md`（22:55）已覆盖通用否定白名单、字符级逐字、边界、首尾引号（代码有，文档未明确提）、`doc_citation_marks`、具体否定拒绝、已知误杀；`docs/design.md` 同步。
- 仍不一致：§6「任何差异都会被拒」与 R3-1 的四字符插入冲突；§7「具体否定……必须引用真实证据」与“具体否定 + 真实 citation 仍被 verbatim gate 拒”冲突；§3.2 sanitizer 覆盖描述未提 rendered/candidate citation；§6 未写首尾包裹引号/括号剥离。
- README 数字仍标“待 T8”（line 39/460）；`docs/verification/2026-10-31-hybrid-mode.md` 仍缺；T8 统一刷新数字与三视口 E2E。

#### 新发现与残余 P2

| ID | 级别 | 标题 | 位置 |
|---|---|---|---|
| R3-1 | **P1** | `_verbatim_text` 按字符删除 的/为/和/按，允许在内容词内部插入这四个字符后通过 gate | `src/docqa/qa/verify.py:67,699-711` |
| R3-2 | P2 | sanitizer 未覆盖 `citations[].rendered` / `clause_label` / `candidates[].citation`；CLI 打印 rendered/candidate citation；bidi 未剥离 | `src/docqa/api/app.py:291-308`、`src/docqa/cli.py:553-567,591,612-614`、`src/docqa/models.py:692-701` |
| R3-3 | P2 | `doc_citation_marks` 误杀：文档原文含 `[p.3]` 时，逐字引用也会被当伪 mark | `src/docqa/qa/verify.py:81-85,851-888` |
| R3-4 | P2 | 具体否定 + 真实 citation 仍拒答，与 docs「必须引用真实证据」不一致；hybrid prompt rule 3 未要求通用否定白名单；带逗号/第二「也未…」分句的通用否定被拒 | `src/docqa/qa/policy.py:466-503`、`src/docqa/qa/answer.py:131`、`docs/hybrid-mode.md:99-101,268-270` |
| R3-5 | P2 | docs 精度：首尾包裹引号/括号未写；「任何差异都会被拒/任何内容词插入都会失败」过强；sanitizer 覆盖过述；README/T8/E2E/验证报告待补 | `docs/hybrid-mode.md:209-218`、`README.md:39,460`、`docs/verification/` |
| R3-6 | P2 | 低风险残余：DOCX `unsupported` 未进 QA；无 body 大小/按客户端限流；非法 mode 测试仍接受 400/422；类级 fixture 警告 | `src/docqa/api/app.py`、`tests/test_cli_api.py:1307-1310,1341-1347` |

##### R3-1 [P1] 四字符按字符删除，内容词插入可通过

`_VERBATIM_REMOVE_CHARS = ("的","为","和","按")`，`_verbatim_text` 用 `str.replace` **删除任意位置**的这四个字符（不是只删独立连接词 token）。因此把这四个字符插入内容词内部，归一化后仍与证据子串一致。端到端实测（证据为 chunk 原文，问题可召回）：

```text
证据 "3.1 操作钮应清晰。"    + 答案 "操作按钮应清晰。[p.1 §3.1]"  -> refused=False, gates=True, citations=1
证据 "3.1 合同平利用。"      + 答案 "合同和平利用。[p.1 §3.1]"    -> refused=False, gates=True, citations=1
证据 "3.1 安全行要求。"      + 答案 "安全行为要求。[p.1 §3.1]"    -> refused=False, gates=True, citations=1
```

`加 按/和/为` 后进入了内容词（按钮/和平/行为），不是纯连接词；docs §6「任何内容词、解释或推断插入都会让子串关系断裂」「任何差异都会被拒」与实际不符。**建议**：只删独立成词的连接词（jieba token 或前后边界判断），或明确把「这四个字符可任意插入/删除」写入契约/docs 已知限制；补 `操作钮→操作按钮`、`平利用→和平利用` 回归测试。若严格逐字是产品承诺，应在放行前修复。

##### R3-2 [P2] sanitizer 覆盖遗漏

实测（构造 clause_label/candidate citation 含 `\x1b[31m`）：

```text
to_dict: citations.rendered=True(含ESC), citations.clause_label=True, candidates.citation=True, checks.detail=True
api _sanitize_payload: checks.detail=False(已净化), checks.offenders=False, rendered=True, candidate citation=True
cli _print_bundle: answer 净化(quote/detail 均无 ESC), 但 rendered citation / candidate citation 仍含 ESC
sanitize_display_text: 只剥离 C0/C1，bidi(U+202E) 保留
```

`_sanitize_payload` 并不漏 `verification.checks[].detail/offenders`；遗漏的是 rendered locator 与 candidate citation。CLI 终端会解释 ESC（文档 heading/OCR 文本可含控制字符）。建议对所有展示字段（包括 rendered/clause_label/section/candidate citation）统一净化，并补测试；bidi 可按安全审计建议另行处理。

##### R3-3 [P2] `doc_citation_marks` 误杀文档内 `[p.3]` 文本

文档 chunk "3.1 见 [p.3] 的说明。"，模型逐字引用 "见 [p.3] 的说明。[p.1 §3.1]"：

```text
doc_sentence_support: False（引用标记 [p.3] 无法绑定）
doc_citation_marks:   False（1/2 个引用标记无法绑定）
```

这是**正确的逐字引用**，但被当伪引用拒绝。建议：只把“答案自带的引用标记”与“引用证据原文中的方括号内容”区分开，或至少写入已知限制/提供 strict 出口；补文档含 `[p.3]` 的测试。

##### R3-4 [P2] 具体否定 / prompt / 白名单不一致

- `"文档中未规定冲击韧性要求。[p.1 §3.1]"`（真实 citation）-> `refused=True`、`verification_failed`；`"文档中未规定。[p.1 §3.1]"`（通用否定）-> 放行。docs §7「具体否定……必须引用真实证据」会让人以为带真实 citation 即可，实际还需要该否定句逐字出现在证据里；建议改为“具体否定仍会被逐字 gate 拒绝，应改写为通用否定 + 补充段，或切 strict”。
- `HYBRID_SYSTEM_PROMPT` rule 3 只说“先在正文写'文档中未规定'”，没有说必须是**通用否定白名单短句**、不得带具体对象；真实模型很可能输出具体否定而被拒。建议 prompt 明确给出 `文档中未规定相关内容。` 这类模板，并要求具体说明放补充段。
- `is_generic_negative("文档中未规定相关内容，也未提及相关要求。")` -> False（只按 `；;。` 切分且第二分句缺主语）；`…；也未提及…` 同样 False。安全方向，但属已知误杀，建议文档说明或允许省略主语。

##### R3-5 / R3-6 其余

- docs §6 未写 `_VERBATIM_WRAP_CHARS` 的首尾引号/书名号/括号剥离；“任何差异都会被拒”在 R3-1 下过强；§3.2 sanitizer 覆盖过述；README 数字/T8、验证报告/三视口 E2E 仍待 T8。
- DOCX `unsupported` 仍未进入 `QaAgent`/`policy`（安全审计 SEC-HYB-01 的并发建议未落地）；`/api/ask` 无 body 大小上限/按客户端限流（docs §7 已列后者）；API 非法 mode 测试仍接受 `(400,422)`（实际 422）；类级 fixture 仍有 `PytestRemovedIn10Warning`。

#### 放行判断（复测 3）

- **P0：0**；**P1：1**（R3-1 四字符内容插入；若严格逐字是承诺，建议放行前修复，成本可控；若接受，需写入契约/docs 已知限制并修正“任何内容词插入都会失败”的表述）。
- **P2：5 类残余**（R3-2…R3-6，含清单内低风险项）。
- strict 4 项 / prompt 哈希 / 零引用均未回归；hybrid 7 项、154 用例、全量 989/4、typecheck/ruff 全绿。
- **结论**：若团队接受 R3-1 为已知归一化取舍并修正文档，则**可放行（WARNING）**；否则先修 R3-1 再放行。R3-2/R3-3/R3-4 建议在 T8 前一并处理或明确记录。

#### 复测 3 声明

本次仍为**只读复核**：除向本报告追加本节外，未修改任何代码、测试、配置、数据或构建产物；未 commit/push；未读取 `.env`；未调用真实模型端点。复现均在系统临时目录、使用 `FakeProvider` 与 `DOCQA_DISABLE_DOTENV=1`。复核期间工作树曾短暂处于迁移中间态，报告以最终稳定快照（989 passed / 4 skipped）为准。

### 复测 4（边界/连接词）

- 最终快照：`verify.py` 23:22:18、`tests/test_hybrid_mode.py` 23:28:45、`docs/hybrid-mode.md` 23:30:09、`README.md` 23:28:58、`models.py` 23:28:23；复核期间代码稳定
- 复跑结果：**1004 passed / 4 skipped**（30.60s；比发布的 1003 多 1 个用例，以最终快照为准）；`tests/test_hybrid_mode.py` **168 passed**；`tests/test_cli_api.py` **89 passed**；`npm run typecheck` exit 0；`ruff check src tests` all checks passed
- 结论：**P0 = 0；P1 = 0**（NEW-11/NEW-12 指定载荷与前两轮 P0/P1 全部关闭）。残余 P2 见下；其中 R4-1（空白/换行/表格分隔的否定前缀）是已记录的“截取完整片段断章取义”的一个实例——若团队把它视为必须阻断的 P1，则这是唯一 P1；按分诊的残余口径记为 P2 并修正 README 的绝对措辞后，**可放行（WARNING）**。

#### 1) NEW-11 / R3-1 载荷与完整正例

- **否定前缀删除（无边距）**：`使用铝。` vs `3.1 不得使用铝。` -> gate False；`规定执行。` vs `3.1 未按规定执行。` -> False；完整否定句 `不得使用铝。` vs 同文 -> True。NEW-11 测试断言 `passed is False`，实测符合。
- **内容词插入（R3-1）**：`操作按钮。` vs `3.1 操作钮。` -> False；`安全行为要求。` vs `3.1 安全行要求。` -> False。NEW-12 的 jieba token 级删除（只删恰好等于 的/为/和/按 的 token）修复了 R3-1。
- **完整句/表格/中文引号**：完整句 `抗拉强度应大于等于 590 MPa。` -> True；表格单元 `抗压强度为 42 MPa。` vs `... | 抗压强度 | 42 MPa | ...` -> True；表格行中文引号 `“抗压强度 | 42 MPa | 合成数据”[§2.1 ¶8]` -> True（NEW-12/包裹引号测试通过）。
- **残余 1（空白/换行/表格分隔的否定前缀）**：`使用铝。` vs `3.1 不得 使用铝。`、`3.1 不得\n使用铝。`、`3.1 | 不得 | 使用铝 |` 均 **gate True**；端到端 `refused=False`、`gates=True`、`citations=1`。原因是 `_verbatim_match` 把空白/`|`/换行当作段边界，否定词在“前一段”被丢弃。docs §7 已用“截取完整片段仍可能断章取义”记录该一般限制，但 README 的“删除‘不得/未/不’前缀会失败”是无条件表述，过强；建议改为“紧邻前缀”，或对左边界向前找最近的实质 token，若为 不/未/无/禁/勿/不得/不应/禁止 则失败，并补 space/newline/table 回归测试。
- **残余 2（中文引号 + 句号在引号内）**：`“抗拉强度应大于等于 590 MPa。”[p.1 §3.1]` vs `3.1 抗拉强度应大于等于 590 MPa。` -> gate False（句号在引号内导致切分；闭引号后的 citation 不以引用开头，未归入前句）。现有 `TestWrappedQuotes` 只覆盖无句号的表格行；建议允许 `_LEADING_CITATION_RE` 前出现首尾包裹字符，或不在引号内切句；补测试。属误杀（安全方向），P2。
- **残余 3（词中间截取）**：`大于等于 590 MPa。` vs 完整句 -> False（段边界要求）；docs 的“可以省略/拼接相邻片段”容易被读成任意子串，建议明确“只能从段/词边界截取”。P2 文档精度。

#### 2) NEW-12 连接词 token

- 完整词 `行为/按钮/目的` vs 同文 -> **True**；片段 `行/按/目` vs `行为/按钮/目的` -> **False**；插入 `操作按钮` vs `操作钮`、`安全行为要求` vs `安全行要求` -> **False**。NEW-12 目标达成。
- 全量 168 个 hybrid 用例通过；测试断言为具体布尔/offenders，不是同义反复。

#### 3) strict / hybrid 检查与 prompt

- strict：`['citation_validity','numeric_grounding','clause_consistency','sentence_support']` 4 项、gates True；strict 零引用 `gates=False`（citation_validity + numeric_grounding 失败）。
- hybrid：基础 4 + `doc_sentence_support` + `doc_citation_marks` + `knowledge_label` = 7 项。
- strict prompt 哈希：`SYSTEM_PROMPT=22fc5621...`、`USER_TEMPLATE=7e02c7c0...`，与本功能前一致，未回归。

#### 4) API / CLI

- status 早退带 `has_llm`；`MAX_QUESTION_CHARS=4000`（超限 400、恰好 4000 通过）；无 LLM hybrid 400 / CLI exit 2；非法 mode 422 / exit 2；`evidence` 键集合仍只含 citations。
- 上传成功：`state["selected"] = [result.doc_id]` + `os.utime(out_dir, None)`；`test_reupload_selects_the_uploaded_docx_over_a_newer_pdf`（幂等二次上传 + 重启后仍选中）通过。
- **净化已补齐（出口）**：`models.sanitize_display_text`（23:28）已包含 C0/C1 **与 bidi**（U+202A–202E/U+2066–2069）；API `_sanitize_payload` / CLI `_sanitize_bundle_dict` 复用并覆盖 `citations[].quote/rendered/clause_label`、`candidates[].citation`、`verification.checks[].detail/offenders`、`evidence.text`。实测 API/CLI 出口的 rendered/clause_label/candidate citation/check detail 均无 ESC/bidi。**仍可能保留控制符的路径**：`AnswerBundle.to_dict()` 自身未净化 `citation.clause_label/rendered`、`checks.detail/offenders`、`question`；CLI `trace.detail` 未走 `_sanitize_outlet`。docs §3.2/§7 仍写「models 只覆盖 C0/C1、bidi 只在出口」，与当前实现不符，需更新（R4-7）。
- CLI 自检表显示 `doc_citation_marks` 英文名与既有检查一致，可接受；Web 已加中文标签。

#### 5) 前端

- `format.ts`：`doc_sentence_support: "句级引用支撑"`、`doc_citation_marks: "引用标记绑定"`；构建产物 `index-BK8PULSW.js`（23:06）已包含两者与 `has_llm === true`，`index.html` 引用新 bundle。
- `hasLlm = status?.has_llm === true`；ModeSwitch/知识块/切换时序/a11y 文件未改，原结论继续成立（radiogroup、方向键、roving tabindex、focus-visible、reduced-motion、切换只影响下一问、知识块纯文本/无 chip）。既有低影响 a11y 备注仍在：无 LLM 时 `aria-describedby` 挂在不可聚焦的 disabled radio；请求中选中 radio 也被 disabled，radiogroup 短暂失去 Tab 停点。
- `npm run typecheck` exit 0；T8 已补 `docs/verification/2026-10-31-hybrid-mode.md`（23:38），报告 1440/768/390 E2E 全过（含 ModeSwitch/焦点/请求中禁用/切换只影响下一问/无横向滚动/无 console error）。本次只读未独立复跑浏览器 E2E，以该报告为证据。

#### 6) 文档

- `docs/hybrid-mode.md`（23:30）已写 NEW-11/12（段边界、jieba token、`行为/按钮/目的` 不拆、删除否定前缀、截断/断章取义），并记录：具体否定 + 真实 citation 仍拒、原文含 `[p.3]` 可能误判、bidi 只在出口、注入/CSP/限流等已知限制；README hybrid 段同步。
- T8 已补：README 现为 1008 用例 / 1004 通过 / 4 skipped / 覆盖率 92% / hybrid 168，并新增 `docs/verification/2026-10-31-hybrid-mode.md`（含三视口 E2E、真实模型冒烟与 §8 残余 P2 清单）。仍存在的文档/实现张力：README 的“删除‘不得/未/不’前缀会失败”未限定“紧邻”（R4-1）；docs 未明确“通用否定是结论/例外，带真实 citation 不代表被该 citation 支持”（R4-4）；中文引号 + 句号在引号内的误杀、词中间截取未写入已知误杀（R4-2/R4-3）；docs §3.2/§7 的 bidi 描述已过时（R4-7）。

#### 新发现与残余 P2

| ID | 级别 | 标题 | 位置 |
|---|---|---|---|
| R4-1 | P2（若按严格极性安全可升 P1） | 空白/换行/表格分隔的否定前缀删除仍放行（`不得 使用铝`/`\| 不得 \| 使用铝 \|` → `使用铝`） | `src/docqa/qa/verify.py:711-817`、`README.md` hybrid 段、`docs/hybrid-mode.md:283-300` |
| R4-2 | P2 | 中文引号包裹完整句且句号在引号内时，闭引号后的 citation 不归前句，正确引用被误杀 | `src/docqa/qa/verify.py:677-696`、`tests/test_hybrid_mode.py:978-1018` |
| R4-3 | P2 | 词中间截取（`大于等于 590 MPa`）被段边界拒绝；docs「可以省略/拼接相邻片段」易被读成任意子串 | `src/docqa/qa/verify.py:774-817`、`docs/hybrid-mode.md:209-237` |
| R4-4 | P2 | 通用否定 free pass：通用否定 + 真实 citation 可放行，但该 citation 不证明“文档未规定”；docs 未把它写成“结论/例外” | `src/docqa/qa/policy.py:466-503`、`docs/hybrid-mode.md:84-105` |
| R4-5 | P2 | T8 已补 README 1008/1004/168/92% 与验证报告（含三视口 E2E）；README「删除不得/未/不前缀会失败」的绝对措辞仍待限定 | `README.md` hybrid 段、`docs/verification/2026-10-31-hybrid-mode.md` |
| R4-6 | P2 | 具体否定/改写误杀（docs 已记录）；hybrid prompt rule 3 仍只说“写文档中未规定”，未要求通用否定白名单措辞 | `src/docqa/qa/answer.py:131`、`docs/hybrid-mode.md:283-288` |
| R4-7 | P2 低 | DOCX `unsupported` 未进 QA；无 body 大小/按客户端限流；API 非法 mode 测试仍接受 400/422；类级 fixture 警告；`to_dict` 未净化 `rendered/clause_label/checks/question`、CLI `trace.detail` 仍可能带控制符；docs 的 bidi 描述过时 | `src/docqa/api/app.py`、`src/docqa/models.py`、`src/docqa/cli.py`、`tests/test_cli_api.py:1307-1310,1341-1347` |

**R3-1（四字符内容插入）已关闭**：NEW-12 的 jieba token 级删除后，`操作按钮` vs `操作钮`、`安全行为要求` vs `安全行要求` 均失败；完整词 `行为/按钮/目的` 通过。
**R3-2（sanitizer）已关闭（API/CLI 出口 + models 已含 bidi）**：rendered/clause_label/candidate citation/checks/evidence text 均净化；`to_dict` 未净化字段（rendered/clause_label/checks/question）与 CLI `trace.detail` 仍可能带控制符；docs §3.2/§7 的“models 只 C0/C1”已过时，需更新（R4-7）。
**R3-3（`[p.3]` 误杀）已记录**：docs §7 明示原文含引用字样可能被当作伪 mark。
**R3-4（具体否定 + 真实 citation）已记录**：docs §3.3/§7 已改为“仍会被拒，需逐字证据/通用否定/strict”，与实现一致；prompt 仍待同步（R4-6）。

#### 最终放行判断

- **P0：0**。
- **P1：0**（指定 NEW-11/NEW-12 载荷、R3-1 与前两轮 P0/P1 全部关闭）。**唯一可升级项是 R4-1**：若团队认为“空白/表格分隔的否定前缀反转”必须阻断，则它是 P1；若按分诊把“截取完整片段断章取义”作为已知残余 P2，则 P1=0。
- **P2：7 类**（R4-1…R4-7，含 T8）。
- **结论**：在“接受 R4-1 为已记录的截断/断章取义限制（T8 验证报告 §8 #2 已用 `不得 | 使用铝` 记录该例），并把 README 的绝对措辞改为‘紧邻前缀’”的前提下，**可放行（WARNING / 条件放行）**；否则先修 R4-1。T8 已完成 README 数字、验证报告与三视口 E2E；R4-2/R4-4/R4-6 建议随发布说明或后续修正。
- strict 4 项、prompt 哈希、零引用均未回归；hybrid 7 项；全量 1004/4、hybrid 168、CLI/API 89、typecheck/ruff 全绿。

#### 复测 4 声明

本次仍为**只读复核**：除向本报告追加本节外，未修改任何代码、测试、配置、数据或构建产物；未 commit/push；未读取 `.env`；未调用真实模型端点。复现均在系统临时目录、使用 `FakeProvider` 与 `DOCQA_DISABLE_DOTENV=1`。
