# Hybrid 回答范围模式 —— 最终验证报告

- 日期：2026-10-31
- 范围：Web 工作台（桌面/平板/手机）、`POST /api/ask`、CLI `ask --mode`
- 契约：`docs/reviews/2026-10-31-hybrid-mode-contract.md`
- 用户文档：`docs/hybrid-mode.md`
- 结论：**通过（条件放行）**。strict 路径未变；hybrid 的安全 P0/P1 全部关闭。发布说明必须列出第 8 节残余 P2（尤其“通用否定 free pass”与“完整子句截断可能断章取义”）。
- 约束：不提交、不推送；测试默认离线；手工真实模型冒烟使用项目 `.env`，未打印密钥。

## 1. 交付

| 层 | 内容 |
|----|------|
| 后端 | `AnswerMode` / `AnswerBundle` 三字段；strict prompt 不变；hybrid prompt + `【模型知识补充】` 行级 marker；补充段剥引用；`doc_sentence_support`（子句逐字+段边界）、`doc_citation_marks`（伪引用绑定）、`knowledge_label`（advisory）；失败页前置、空输出拒答、精确通用否定白名单 |
| API | `POST /api/ask {question, mode}`；非法 mode 422；hybrid 无 LLM 400；问题 >4000 字符 400；响应 `answer_mode/model_knowledge/knowledge_used`；`evidence` 只含文档 citations；status 早退也带 `has_llm` |
| CLI | `docqa ask --mode strict|hybrid`；无 LLM/非法模式退出 2；模式行 + 独立“模型知识补充”面板；JSON 透传三字段 |
| Web | `ModeSwitch`（radiogroup/方向键/滑动 220ms/reduced-motion/localStorage）；`AnswerCard` 模式 chip + 知识 badge + 独立纯文本知识块；空正文提示；拒答按模式说明；`has_llm === true` 才启用 hybrid |
| 文档 | `docs/hybrid-mode.md`、`docs/design.md` 决策 8、README |
| 审计 | `docs/reviews/2026-10-31-hybrid-mode-audit.md`、`...security-audit.md`、`...triage.md` |

## 2. strict 未回归

- `SYSTEM_PROMPT` 哈希 `22fc5621…`、`USER_TEMPLATE` 哈希 `7e02c7c0…` 未变（相对本功能引入前；DOCX 引用格式改动属于前一功能）。
- strict 报告仍 **4 项检查**：`citation_validity/numeric_grounding/clause_consistency/sentence_support`，无 `doc_sentence_support/doc_citation_marks/knowledge_label`。
- strict 零引用仍失败；六类拒答、PDF/DOCX 引用/数值/条号、旧产物回读不变。

## 3. 测试与静态检查

| 项 | 结果 |
|----|------|
| `uv run pytest tests -o addopts="" -q` 第 1 遍 | **1004 passed / 4 live skipped / 0 failed** |
| 第 2 遍 | **1004 passed / 4 skipped / 0 failed** |
| `--cov=src/docqa` | **92%**（6965 语句 / 578 未覆盖；门禁 84%） |
| `uv run ruff check .` | All checks passed |
| `tests/test_hybrid_mode.py` | **168 passed** |
| `npm run typecheck` / `npm run build` | 通过；JS 188,567 B / gzip 60,553 B，CSS 33,656 B / gzip 7,116 B |

## 4. 真实模型 hybrid 冒烟

```text
docqa ask "抗压强度 42 MPa 换算成 psi 是多少？" --mode hybrid --doc data/derived/demo-558392e0
```

- 第 1 轮模型改写证据，`doc_sentence_support` 拦截；
- 第 2 轮文档正文为 `“抗压强度 | 42 MPa | 合成数据” [§2.1 ¶8]`，补充段为 `1 MPa ≈ 145.0377 psi，因此 42 MPa ≈ 6091.58 psi`；
- 自检 7 项全过：基础 4 + `doc_sentence_support` + `doc_citation_marks` + `knowledge_label`；补充段无引用、独立面板。
- 结论：逐字 gate + 修复循环 + marker 拆分在真实模型上可用；改写会被拒，模型能按修复提示改成证据原句。

## 5. Web E2E（1440/768/390）

- 独立 8765 实例；Playwright-core + 系统 Chrome；strict/hybrid 的 `/api/ask` 与 `has_llm` 用 route mock，其余走真实后端。
- 全部检查通过，包括：
  - DOCX 上传后 `source_type=docx`、`unit_count`、`pages=0`、`failed_pages=[]`、Overview/来源/段数；
  - ModeSwitch 三视口可见、radiogroup+2 radio、点击与方向键切换、焦点跟随、390 点击目标 ≥40px；
  - 默认 strict、边界说明、strict 请求体 `mode=strict`、仅文档 chip、无补充块；
  - 切换只影响下一问：已有答案 chip/补充块不变；
  - hybrid 请求体 `mode=hybrid`、含模型知识 chip、knowledge badge、独立补充块、无引用 chip/token、知识标注中文名；
  - 请求中 ModeSwitch 禁用；空正文+补充段提示；localStorage 保留；无 LLM 禁用且不覆写；LLM 恢复后回到 hybrid；reduced-motion；无横向滚动/console error。
- 期间修复：平板抽屉缺“文档概览”、同内容重复上传后仍选旧 PDF（`selected=[doc_id]` + `os.utime`）。

## 6. 安全/代码审计

- 安全终审 `### 复测 4`：**P0/P1 全部 CLOSED，0 open**。
  - 失败页前置（0 命中/低覆盖/证据触及）不调用模型；
  - 通用否定精确白名单；`不得使用铝`→`使用铝` 等否定前缀删除失败；单位 `±/%/℃/μm` 边界；中文引号表格原句通过；
  - `doc_citation_marks` 伪 `[p.99 §9.9]` 失败、不回退、不因零引用跳过；
  - 空串/空白/仅 marker 拒答；marker 行级+证据替换；伪引用变体剥离；C0/C1+bidi 展示净化。
- 代码终审：strict 4 项/哈希未变；hybrid 7 项；NEW-7/8/9/11/12 回归测试真实；R3-1（字符级连接词删除）已改 token 级。**P0=0 / P1=0**。
- 代码终审唯一可升级项 **R4-1（P2）**：空白/换行/表格单元分隔的否定前缀仍可被单独引用（`不得 | 使用铝` → `使用铝` 通过）；这是第 8 节 #2“完整子句/表格单元截断”的实例，按分诊记为 P2；README 已改为“紧邻无空格前缀会失败”，不再宣称绝对安全。若团队要求连该形态也阻断，需要再做“否定感知左边界”并升 P1。
- 结论：**可放行（条件：接受以下 P2 残余并写入发布说明）**。

## 7. 运行中的服务

| 入口 | 地址 | 状态 |
|------|------|------|
| 后端 + 内置前端 | `http://127.0.0.1:8000/`（`/app` 工作台） | 运行中；`has_llm=true`；当前选中文档 `demo-558392e04979dac68c45dc2907d0de42`（DOCX） |
| Vite 开发前端 | `http://127.0.0.1:5173/` | 运行中；`/api` 代理到 8000 |

## 8. 残余 P2（发布说明必须列出）

1. **通用否定 free pass**：`文档中未规定。` 无 citation/带任意有效 citation 仍可放行；这是模型判断而非证据。UI/文档需说明“通用否定只表示未找到，具体结论需引用或补充段”。
2. **完整子句/表格单元截断（R4-1）**：边界允许按空白/`|`/逗号引用完整片段，但如 `不得 | 使用铝`、`不得\n使用铝`、`不得 使用铝` 或只引用“除不锈钢外”的后半句，仍可能断章取义；紧邻无空格的否定前缀已阻止，但空白/换行/表格分隔的否定前缀仍可能被单独引用。若合规要求阻断该形态，需否定感知左边界（P1 级修复）。
3. **具体否定/改写被拒**：`文档中未规定冲击韧性要求。` 无 citation 会拒；正常改写、`℃`→`摄氏度`、语序/符号差异会被逐字 gate 拒。hybrid 文档正文应逐字引用；需要改写用 strict。
4. **误杀补充**：句号在中文引号内的完整句（`“抗拉强度应大于等于 590 MPa。”[p.1 §3.1]`）可能被切分误杀；词中间截取（`大于等于 590 MPa`）会被段边界拒绝；只能从段/表格单元/词边界截取。
5. **展示净化边界**：`models.sanitize_display_text` 已含 C0/C1+bidi；API/CLI 出口覆盖主要字段；`to_dict` 的 `citation.clause_label/rendered`、`checks.detail/offenders`、`question`、CLI `trace.detail` 等报告层路径仍可能有控制符（P2）。
6. **marker 变体/原文自带 `[p.3]`**：zero-width/全角 marker 不识别；文档原文含 `[p.3]` 可能被 `doc_citation_marks` 误判为伪引用（P2）。
7. **跨 workstream P2**：ingest 注入命中未进入问答展示/策略；无 CSP/安全响应头；按客户端限流未实现（仅全局并发 ask 2 / upload 1）；DOCX `unsupported` 未进入 QA。
8. **strict prompt 相对更早 HEAD 的 DOCX 引用格式改动**不是本功能引入；本功能未改 strict prompt。
9. **表格/图片/脚注等 Phase 2/3 限制**见 `docs/docx-support.md`。

## 9. 复现命令

```bash
uv sync --all-extras
uv run pytest tests -o addopts="" -q
uv run pytest tests -o addopts="" -q --cov=src/docqa --cov-report=term
uv run ruff check .

DOCQA_DISABLE_DOTENV=1 uv run docqa ingest artifacts/docx/demo.docx --domain standard
uv run docqa ask "抗压强度 42 MPa 换算成 psi 是多少？" --mode hybrid --doc data/derived/demo-558392e0
DOCQA_DISABLE_DOTENV=1 uv run docqa ask "抗压强度是多少？" --doc data/derived/demo-558392e0

cd web && npm run typecheck && npm run build
uv run docqa serve --host 127.0.0.1 --port 8000
```
