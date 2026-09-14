# 合成演示件：评审不用密钥就能复现的那一份

本目录是一份**自造文档**走完整条管线后的产物。
它是这个仓库里**唯一可以被完整复现的实例**——因为那份真实文档不能公开：

| 文档 | 能公开吗 | 原因 |
|---|---|---|
| GB/T 1568-2008 | ✗ | 取自官方公开预览，已承诺**不再分发** |
| **本目录的合成件** | **✓** | 内容完全自撰，与任何真实标准或公文无关 |

## 复现步骤（无需任何模型密钥）

```powershell
uv run python tools/make_demo_artifact.py          # 重新生成文档与页图
uv run docqa detect data/raw/demo_scan.pdf         # 应判为 SCANNED、0 字体、100% 图像覆盖
uv run docqa ingest data/raw/demo_scan.pdf --engine fixture --fixtures-dir artifacts/demo/ocr
uv run docqa ask "示例件的额定载荷要求是多少？"
uv run docqa eval --golden data/golden/demo_qa.jsonl
uv run docqa serve
```

## 这里有什么，以及能验证什么

| 内容 | 能验证什么 |
|---|---|
| `demo_scan.pdf` | 输入本体（**自造内容，可公开**；`data/raw/` 整体不入库，所以副本放在这里） |
| `pages/p0001.png`、`pages/p0002.png` | 重跑 `render.py` 应得到**逐字节相同**的页图（位精确取图，不重采样） |
| `derived/pages.json` | 每页的 sha256、像素尺寸、原生 DPI、`native=True` |
| `derived/manifest.json` | 判定信号、渲染参数、`engine=fixture` 标注、切分统计 |
| `ocr/p000{1,2}.json` | 结构化识别结果（**构造的**，见下） |
| `prompt.ocr_page.v1.md` | 真实 OCR 会用到的那版提示词 |

`pages.json` 里记录的哈希：

```
p1  p0001_native.png  1240x1754  150dpi  native=True  sha256:dbac49ea454b5c81
p2  p0002_native.png  1240x1754  150dpi  native=True  sha256:fbf2fbeca018f8d7
```

## 两件必须说清楚的事

### 1. 这里的识别结果是**构造的**，不是识别出来的

`ocr/*.json` 由 `tools/make_demo_artifact.py` **从同一份源文本程序化生成**，
因此它**零 OCR 误差是构造的结果**。

所以本目录演示的是**管线与产物的格式**，**不是识别能力**。
不得据此声明任何 OCR 精度指标——`eval` 也会拒绝把 fixture 运行与实跑基线相比
（引擎名不同即拒绝，没有绕过开关）。

### 2. 这里**没有**溯源链，这是正常的

OCR 的溯源链（`Provenance`：模型、提示词哈希、请求参数、用量）只在**真实调用模型**时产生。
fixture 引擎不调用任何模型，所以**根本没有溯源记录可写**。

本目录**不含** `provenance/`——不是漏了，是没有。
真实文档跑 `docqa ingest` 后，溯源链会落在
`data/derived/cache/<cache_key>.provenance.json`（该目录不入库，因为文档不能公开）。

要看到真实的溯源链长什么样，配好密钥后跑一次真实 ingest 即可；
`src/docqa/ocr/cache.py` 里的 `cache_key` 计算方式说明了它为什么可被独立检验：

```
cache_key = sha256(image_sha256 ‖ provider ‖ model ‖ prompt_sha256 ‖ params)
```

提示词与参数一起进哈希，所以**改一个字提示词，键就变**，旧结果不会被误当作新提示词的产物复用。
