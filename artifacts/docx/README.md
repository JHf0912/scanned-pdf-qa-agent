# DOCX 演示样例

`demo.docx` 是**合成**样例：内容由脚本生成，没有版权材料，也没有真实姓名、
单位、金额或任何个人信息。可以用它做人工检查、演示和固定输入，不必担心
把客户文件带进仓库。

## 生成

```bash
uv run python tools/make_demo_docx.py
# 输出默认写到 artifacts/docx/demo.docx；可用 --out 指定别处
```

如果本机 `docqa` Web 服务正在运行、`uv run` 报 `docqa.exe` 被占用，
加 `--no-sync` 跳过环境同步即可（与生成本身无关）：

```bash
uv run --no-sync python tools/make_demo_docx.py
```

重新生成的内容结构一致，但 ZIP 条目的时间戳由打包时刻决定，**字节不保证相同**。
修改生成脚本后请重新生成并一并提交。

## 覆盖的结构

| 结构 | 在样例里的位置 | Phase 1 的行为 |
|------|----------------|----------------|
| `Heading 1` / `Heading 2` | `1 总则` … `2.1 外观` | 标题栈；`clause_path` 记录层级 |
| 字面条号 | `1.1 适用范围` 等 | `clause_label` 取裸条号 `1.1`，`title` 去掉条号 |
| Word 自动编号（两级） | `试验方法` / `步骤` | 文本里没有条号，解析器从 `numbering.xml` 算出 `3` / `3.1` |
| 普通正文段落 | 各标题下的说明文字 | 同节段落合并到 `max_chars`，超长按句子切 |
| 简单表格 | `项目 / 指标 / 备注` | 线性化为一个原子 table chunk，`structure_parsed=false` |

解析结果（`docqa ingest` 链路接入后可直接核对）：

- 非空正文块 `¶1..¶12`（标题 6、段落 5、表格 1）；
- `docx.numbering = {literal: 4, computed: 2, missing: 0}`；
- 1 张表，线性化 1 张；
- 每个非空块都被至少一个 chunk 覆盖。

## Phase 1 明确不覆盖

- 表格合并单元格（`gridSpan` / `vMerge`）只做跳过式线性化，不建矩形网格；
- 内嵌图片、文本框、脚注/尾注、批注、页眉页脚不解析，只在 manifest 计数；
- 不使用 Word 排版出来的页码；DOCX 引用一律 `[§条号 ¶n]`。

这些限制和冻结契约见 `docs/reviews/2026-10-29-docx-gap-review.md`。
