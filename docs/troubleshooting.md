# 环境问题、排查过程与替代方案

作业要求"如果遇到依赖安装、OCR 环境、模型 API 等问题，请记录问题、排查过程和替代方案"。
本文按**真实遇到的时间顺序**记录，包括走错的路。

---

## 一、没有可用的 Python

**现象**：`python --version` 无输出，`python -c "print(1)"` 静默失败。

**排查**：`command -v python` 指向
`C:\Users\26775\AppData\Local\Microsoft\WindowsApps\python` —— 这是
**Windows Store 的占位符**，不是真的解释器。它会在被调用时打开应用商店，或直接静默退出。

它还有一个隐蔽得多的后果：**它会吞掉 `python - <<'PY'` 这类 heredoc 脚本**。
我在批量改配置文件时踩过一次——脚本没执行、没有任何报错，grep 一看文件根本没变。
排查这种"命令成功了但什么都没发生"的情况，比排查报错更费时间。

**替代方案**：本机已有 `uv 0.12.5`。

```powershell
uv python pin 3.12     # 自动下载 cpython-3.12.14
uv sync --all-extras
```

**教训**：任何脚本执行后都要**验证产物**，不能只看退出码。

---

## 二、PDF 工具链残缺

**现象**：需要判断 PDF 页数、抽取文本、渲染页图。

**排查**：本机只有 `pdftotext`（Glyph & Cog 4.00 版），
**没有** `pdfinfo` / `pdfimages` / `pdftoppm` / `mutool` / `gs`。

**两个坑**：

1. **`pdftotext` 对不存在的页也返回退出码 0**。
   `pdftotext -f 5 -l 5` 在一个 1 页的 PDF 上照样成功退出，输出为空。
   所以**不能用退出码判断页是否存在**，也不能用它数页数。
2. 页数只能用 `pymupdf` 的 `doc.page_count`。

**替代方案**：全程用 **PyMuPDF**（纯 wheel、无系统依赖、cp310-abi3 在 3.12 上可用），
完全绕开外部二进制。`pdftotext` 只保留一个用途：
**作为第二个独立抽取器做交叉验证**——两个引擎都给出 0 字符，比单一引擎的阈值判定硬得多。

---

## 三、OCR 引擎的选择反复

**最初想法**：用 PaddleOCR / PP-StructureV3（表格识别强，有 wired/wireless 之分）。

**放弃原因**：`paddlepaddle` 在 Windows 上的安装坑较多，
且完整 PP-StructureV3 要下载约 1.2–1.5 GB 模型——
与"三条命令可复现"的目标直接冲突。

**中间想法**：RapidOCR（ONNX、纯 wheel、无需 paddle）。

**发现的坑**：我最初查的是 `rapidocr-onnxruntime`，它的
`requires_python` 是 `<3.13,>=3.6`，所以我把 Python 钉在了 3.12。
后来发现**这个包已被官方弃用**，新的统一包 `rapidocr`（3.9.2）是
`py3-none-any`、支持到 3.13。

> **3.12 这个决定保留了，但理由换了**：MinerU 在 Windows 上只支持 3.10–3.12
> （被 `ray` 卡在 3.13），EasyOCR 也不推荐 3.12+。
> 钉 3.12 = 本地 OCR 的备选路线全部保留。
>
> **这条记下来是因为**：README 里原本会写一个"因为 rapidocr-onnxruntime 不支持 3.13"
> 的理由，而那是**错的**，评审 30 秒就能查出来。理由错了要改，而不是留着。

**最终路线**：VLM API + 可插拔适配层，本地引擎留注册位（`local`）。

---

## 四、扫描件本身的来源问题

**现象**：作业点名的 `GBT 1568-2008键 技术条件.pdf` 不在工作目录里。

**排查与解决**：从国家标准全文公开系统（openstd.samr.gov.cn）的**免费公开预览**取得。
该系统用**瓦片打乱**作为反抓取措施：一张 2320×5904 的 WebP 雪碧图里，
所有页面被切成 232×328 的小块并故意打乱顺序。

但查看器 HTML 里带着完整的还原映射
（每个 `<span class="pdfImg-列-行" style="background-position:-Xpx -Ypx">`
指出了该瓦片在雪碧图中的来源坐标），按映射可以拼回原页。
`tools/fetch_openstd.py` 就是这个还原脚本。

**限制**：还原结果**仅供本次作业本地技术验证，不再分发**，
README 与 `data/golden/transcription.md` 里都写明了这一点，
并且仓库不提交该 PDF 与全文转写（只保留评估所需的节选）。

---

## 五、`uv sync` 会卸载 extras（一个容易困惑的行为）

**现象**：跑完 `uv sync` 之后，`zhconv` 与 `urllib3` 被卸载了，评估脚本随即报
`ModuleNotFoundError`。

**原因**：`uv sync` 会让环境**精确匹配**本次请求的依赖集合，
所以不带 `--extra` 的调用会移除上次装的 extras。这是设计行为，不是 bug。

**解决**：统一用

```powershell
uv sync --all-extras
```

---

## 六、镜像源

本机 `pip.ini` 已配 USTC 镜像：

```ini
[global]
index-url = https://mirrors.ustc.edu.cn/pypi/web/simple
```

但 **`uv` 不读 `pip.ini`**。如果 `uv sync` 慢或超时：

```powershell
uv sync --all-extras --index-url https://mirrors.ustc.edu.cn/pypi/web/simple
```

---

## 七、模型端点的三个坑

### 1. "配置填了"不等于"功能可用"

有些纯文本模型**会静默忽略图像输入**，返回一段看似合理的文字。
只检查配置项是否填写，会在密钥正确但模型不支持视觉时给出虚假的通过。

**解决**：`docqa doctor` 用一张**合成测试图**实测——
图上画了特定颜色的矩形和一个数字，问模型"数字是多少、矩形什么颜色"。
纯文本模型无法从提示词推断出答案，答对才说明它真的读了图。

### 2. 环境变量劫持

官方 SDK 会按 `ANTHROPIC_API_KEY` → `ANTHROPIC_AUTH_TOKEN` → 本地 profile
的顺序自己找凭证。而本机环境里恰好设置了 `ANTHROPIC_BASE_URL` /
`ANTHROPIC_AUTH_TOKEN`（指向另一个服务）。

**后果**：如果依赖 SDK 的隐式解析，会拿 A 服务的密钥去请求 B 服务，
报错信息还很难懂。

**解决**：Anthropic 适配器**一律显式传入** `api_key` 与 `base_url`，
主动屏蔽环境变量的隐式解析。代码里有注释说明原因。

### 3. 采样参数已被移除

当前一代模型对 `temperature` / `top_p` / `top_k` 直接返回 400。
识别任务天然需要确定性，而确定性现在由能力本身保证，不再靠采样参数。

**解决**：Anthropic 适配器默认**不发送** `temperature`。

---

## 八、没有视觉模型密钥时怎么办

这是**用户最可能遇到的情况**，所以说明清楚。

**症状**：`docqa ingest` 报"缺少 OCR 视觉模型配置"。

这不是故障，而是扫描件场景的固有约束：**没有可读文本层，没有视觉模型就一步也走不了。**
`docqa doctor` 与报错信息都会把替代路径讲清楚。

**可选方案**：

| 方案 | 适用 | 说明 |
|---|---|---|
| 配一个 OpenAI 兼容端点 | 有任意一家密钥 | DeepSeek / 通义 / 智谱 / Kimi / vLLM / Ollama 都行 |
| 本地起一个 VLM | 数据不能出内网 | Ollama / LM Studio 提供 OpenAI 兼容接口，填 `base_url` 即可 |
| 用 fixture 引擎 | **仅测试与开发** | 读预置 JSON，**不是真实识别**；产物会标注 `engine=fixture`，评估时机械隔离 |
| 只验证下游 | 只想看检索/问答/自检 | `docqa ask --doc <已 ingest 的产物目录>`，不需要任何密钥 |

**关于 fixture 的边界**：一份别人递进来的 JSON 与"手工录入全文"在性质上无法区分。
所以它不是"绕过解析的旁路"，而是**显式的测试通道**：

- 必须显式传 `--engine fixture` 才会启用；
- 引擎名会写进 manifest 与溯源链，无法伪装成真实运行；
- `eval` 会拒绝把 fixture 运行与实跑基线相比（引擎名不同即拒绝），**没有绕过开关**——
  少一个开关就少一个被误用的地方；
- 报告里机械标注，且**不得据此声明任何 OCR 精度指标**。

---

## 九、性能：测试跑得慢

**现象**：`uv run pytest` 需要数分钟。

**原因**：jieba 首次加载要构建前缀词典（约 1.5 秒），
加上 PyMuPDF 渲染与多次完整 ingest。

**缓解**：jieba 会缓存到 `%TEMP%\jieba.cache`，第二次起明显变快。
CI 上可以把缓存目录持久化。

---

## 十、Windows 上的路径与编码

- 中文文件名在 Git Bash 下正常，但**跨工具传递时容易被转码**。
  代码里统一用 `pathlib.Path`，不做字符串拼接；
- 所有文件读写显式指定 `encoding="utf-8"`，
  否则在中文 Windows 上会落到 GBK，产出乱码；
- `uv` 的硬链接警告（"Failed to hardlink files"）是因为缓存与项目在不同磁盘，
  不影响正确性，加 `--link-mode=copy` 可消除。
