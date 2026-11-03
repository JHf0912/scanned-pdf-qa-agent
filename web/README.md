# docqa Web 工作台

React 18 + Vite + TypeScript 前端，一套代码同时适配桌面、平板与手机。
后端沿用现有 FastAPI 接口，**没有新增任何 API**。

## 两个路由

| 路由 | 内容 |
|---|---|
| `/` | 产品入口：价值主张、工作台预览、运行形态、进入工作台 |
| `/app` | 工作台：文档、对话、引用、自检、证据、文档概览 |

## 本地开发

```bash
# 终端 1：后端
uv run docqa serve --host 127.0.0.1 --port 8000

# 终端 2：前端（Vite 把 /api 代理到 8000）
cd web
npm install
npm run dev            # http://127.0.0.1:5173
```

Vite 开发服务器负责 `/`、`/app` 与 HMR；`/api/*` 转发给 FastAPI。

## 构建

```bash
cd web
npm run build          # tsc --noEmit && vite build
```

产物直接写入 `../src/docqa/web/`（`index.html` + `assets/`），随后：

```bash
uv run docqa serve     # http://127.0.0.1:8000
```

构建产物已入库，因此**运行镜像不需要 Node**。修改前端源码后必须重新
`npm run build`，否则 `src/docqa/web/` 仍是旧产物。

## API 契约（字段以 `src/docqa/web` 旧实现与后端为准）

| 接口 | 用途 | 前端使用到的字段 |
|---|---|---|
| `GET /api/status` | 当前产物概览 + 文档列表 | `ready`、`error`、`doc_id`、`kind`、`pages`、`chunks`、`tables`、`failed_pages`、`citation_mode`、`engine`、`has_llm`、`docs`、`selected` |
| `GET /api/docs` | 文档列表（备用） | `docs[]`、`selected` |
| `POST /api/select` | 切换 / 多选文档 | 请求 `{doc_ids: string[]}`；空数组 = 回到最新 |
| `GET /api/chunks` | 全部片段（证据文本） | `chunk_id`、`page_no`、`kind`、`clause_label`、`text` |
| `POST /api/upload` | 上传 PDF | 裸 body + `Content-Type: application/pdf` + `X-Filename` |
| `POST /api/ask` | 提问 | `AnswerBundle.to_dict()` + `evidence` + `server_elapsed_ms` |

`AnswerBundle` 的关键字段：`answer`、`refused`、`refusal_reason`、
`refusal_detail`、`candidates`、`citations[]`、`verification.checks[]`
（`name` / `passed` / `severity` / `detail` / `offenders`）、`hits[]`、`evidence`。

六类拒答：`not_in_document`、`extraction_failed`、`ambiguous_question`、
`verification_failed`、`out_of_scope`、`low_confidence`。

## 布局与状态

- **桌面 ≥1024px**：左侧文档栏 264px / 中间对话 / 右侧证据与自检 344px。
- **平板 768–1023px**：文档栏 + 对话；证据与自检为右侧抽屉。
- **手机 <768px**：单栏 + 底部“对话 / 证据 / 文档”标签；引用与自检用
  bottom sheet；提问框吸底并避开安全区。
- 覆盖：无文档、未就绪、加载骨架、请求错误、无引用、拒答、歧义候选。

## 可访问性与安全

- 键盘：`⌘K` / `Ctrl K` 聚焦提问框，`Esc` 关闭 sheet / 抽屉；可见 focus ring。
- `aria-live` 播报新答案；状态同时用图标 + 文字，不只靠颜色；触控目标 ≥44px。
- 尊重 `prefers-reduced-motion`：全局关闭动画与平滑滚动。
- OCR 文本一律按纯文本渲染（React 默认转义），不使用
  `dangerouslySetInnerHTML`；上传只在客户端做扩展名预检，服务端仍独立校验。

## 已知边界

- **上传进度**：`POST /api/upload` 是同步接口，不流式返回逐页状态；前端显示
  不确定进度条与“逐页识别中”，**不伪造页码**。要做逐页进度需要新增流式接口
  （本次没有改 API）。
- **删除文档**：后端没有删除接口；工作台提供“清除多选 / 重新扫描”，不提供删除。

## 设计 token

颜色、圆角、阴影与字体在 `src/styles/tokens.css`。布局断点在
`src/styles/workbench.css` 末尾；入口页在 `src/styles/landing.css`。
