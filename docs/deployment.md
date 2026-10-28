# 部署与运行

这份文档回答四件事：**怎么装、怎么配、怎么跑、装在别人机器上要注意什么**。
前三件在 README 的「快速开始」里也有，这里补充容器化、健康检查、数据挂载
与**许可证边界**——最后一条在分发场景里不是脚注，是前置条件。

---

## 一、两条路：本机直跑 / 容器

### 路 A：本机直跑（开发与调试首选）

```powershell
uv python pin 3.12
uv sync --all-extras          # 不带 --all-extras 会卸载 eval 依赖
Copy-Item .env.example .env   # 填密钥
uv run docqa doctor           # 会实测端点是否真支持图像输入
uv run docqa serve
```

适合：改代码、跑评估、看中间产物。每一步都能单独跑、单独看。

### 路 B：容器（交付与部署）

```bash
docker build -t docqa:0.1.0 .

# 密钥走 env-file，**不要**用 -e 直接写（会留在 shell history 与进程列表里）
docker run --rm -p 8000:8000 \
  --env-file .env \
  -v "$PWD/data/raw:/app/data/raw:ro" \
  -v docqa-derived:/app/data/derived \
  docqa:0.1.0
```

> `data/raw` **只读挂载**是有意的：容器只负责"读文档、出产物"，
> 没有理由让它能改输入。产物目录给一个具名 volume，重启不丢。

`Dockerfile` 里做了三件容易被忽略的事：

1. **非 root 运行**（uid 10001）。本项目处理的是**外来 PDF**——不可信输入；
2. **健康检查看 `/api/status` 的 `ready` 字段，不看 HTTP 状态码**。
   该端点在装载失败时仍返回 200，只看状态码会把"一份产物都装不上"判成健康；
3. **`.dockerignore` 排除 `.env`**。镜像是分层且不可变的，密钥一旦进过某一层，
   后面删掉也仍能从层里捞出来。

---

## 二、配置从哪来

| 来源 | 放什么 | 是否入库 |
|---|---|---|
| `config/default.yaml` | 管线参数（渲染、切分、检索、问答阈值） | ✅ |
| `config/domains/*.yaml` | 业务场景 profile（改场景不改代码） | ✅ |
| `.env` | **模型端点与密钥** | ❌ 已 gitignore |
| 环境变量 | 覆盖 `.env` 里的同名项（容器部署用这条） | — |

配置层只按**变量名**引用密钥，YAML 里不出现真值。
拼错的配置键会**直接报错**而不是静默忽略——静默忽略是运维事故的常见来源。

---

## 三、运行期要挂哪些目录

| 路径 | 内容 | 建议 |
|---|---|---|
| `data/raw/` | 输入 PDF | 只读挂载 |
| `data/derived/` | 产物、OCR 缓存、溯源链 | 可写卷，**要备份** |
| `out/` | 报告、导出 | 可写卷 |
| `.env` | 密钥 | 用 `--env-file`，别挂进镜像 |

**`data/derived/` 值得单独说**：它不是纯粹的临时文件。里面有
`cache/<key>.provenance.json` —— 记录每页是谁识别的、用的哪版提示词、
花了多少 token。删掉它，**审计链就断了**，而"这个数字是从哪来的"
恰恰是这类系统最需要回答的问题。`data/derived/` 在 `.gitignore` 里，
所以**它只存在于你的机器上，备份责任在使用者**。

---

## 四、并发与限额

- Web 端 `/api/ask` 有**并发上限**（默认 2），超限立刻返回 **429** 并说明原因，
  **不排队**：让用户等 30 秒再返回，他只会以为页面卡住了；
- 单题修复轮次上限 `qa.max_attempts`（默认 2），防止自检失败后无限重试；
- **没有**总预算护栏。`usage_total` 与 `est_cost_usd` 是**记账**，不是**限流**。
  跑大文档或整轮 eval 之前，请自己先估算：页数 × 每页 token × 单价。

> 这是已知缺口，不是"已经做好了"。见 README「未完成 / 不完美」第 7 条。

---

## 五、许可证边界（**分发前必须读**）

本仓库自己的代码是 **MIT**（见 [`LICENSE`](../LICENSE)）。
但它依赖 **PyMuPDF**，而 PyMuPDF 是 **AGPL-3.0**（双许可）。这两件事必须分开看：

| 你的用法 | AGPL 是否触发 | 你要做的事 |
|---|---|---|
| 自己机器上跑、不发给别人 | 通常不触发 | 无 |
| 公司内部部署、仅内网使用 | 通常不触发（"内部使用"一般不构成分发） | 建议留档说明 |
| **把镜像推到 registry / 发给客户** | **触发** | 整个组合作品按 AGPL 开源，或买 Artifex 商业许可，或换 PDF 库 |
| **作为公开网络服务提供** | **触发**（AGPL 第 13 条） | 向使用者提供对应源码 |

**MIT 不能覆盖依赖的许可。** 你在 `LICENSE` 里写 MIT，不会让 PyMuPDF 变成 MIT。

三条出路，按改造成本从低到高：

1. **买商业许可**：Artifex 提供 PyMuPDF 商业授权，成本可预期；
2. **整包开源**：既然 MIT 已经开源，把服务端也按 AGPL 放出去，通常可行；
3. **换库**：`pdfminer.six`（MIT）+ `pypdfium2`（Apache-2.0/BSD）能覆盖
   文本层抽取；但**位精确取图**这一路（本项目对扫描件的核心优化）需要重写，
   因为 PyMuPDF 的 `get_image_info(xrefs=True)` 取原始位图没有直接替代。

> 本文件**不是法律意见**。上表是工程视角的提醒，具体义务请读 AGPL 原文或咨询法务。

---

## 六、排查入口

| 现象 | 先看哪 |
|---|---|
| 起不来 / 缺变量 | `docqa doctor`（会说清缺哪个、影响哪一步、有哪些替代路径） |
| 装了容器但连不上 | 容器里必须 `serve --host 0.0.0.0`；默认 127.0.0.1 时外部连不上 |
| 健康检查一直 unhealthy | 看 `/api/status` 返回的 `ready` 与 `error` 字段 |
| 识别结果为空 | 该端点可能**静默忽略图片**；`docqa doctor` 用合成图实测 |
| 数字对不上 | `data/derived/<doc>/manifest.json` 记了引擎、模型、提示词版本、渲染参数 |
| 报告里三层指标缺一层 | 缺 `--transcription`（索引层）或标了 `--no-llm`（生成层行为不同） |

更多环境问题见 [`troubleshooting.md`](troubleshooting.md)。

---

## 七、CI 与回归门禁

`.github/workflows/test.yml` 有两段：

1. **test**：`ruff check` → `pytest`（覆盖率低于 84% 直接失败）；
2. **regression**：用**合成演示件**（fixture 引擎，不需要密钥）跑一遍
   `docqa eval --baseline data/baselines/demo_fixture.json --no-llm --noise-sweep 3`，
   任何指标下滑都以**退出码 5** 失败。

真实文档（GB/T）**不进 CI**：它不入库，那份评估只在本地跑，结论写在 README 里。

> 基线必须用 `--no-llm` 录。配了生成模型后同一份代码每次跑出的数字都不同，
> 那样的"回归"只是在测模型采样，CI 会自己翻红。

需要真实端点的验证有独立通道（默认跳过）：

```bash
uv run pytest -m live --live -q     # 真的打一次端点，会产生少量费用
```
