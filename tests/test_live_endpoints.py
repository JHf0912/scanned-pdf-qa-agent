"""真实端点冒烟测试（`@pytest.mark.live`）。

这个文件补的是一个**已经写在文档里、但没有实现**的承诺：
`pyproject.toml` 注册了 `live` marker，`conftest.py` 写着"需要外部依赖的
测试标 live 并默认跳过"——但全仓**没有任何用例打这个标记**。
也就是说"真实端点验证"这件事从来只是说法。

## 这些用例回答的问题

单元测试全部用 mock，它们能证明"代码按我设想的方式处理了我设想的数据"，
但证明不了**真实端点是否按我设想的方式回话**。这一类问题只有打真端点才现形，
本项目已经踩过好几次（`get_image_info(xrefs=)`、`rect` 是点不是像素、
压缩质量影响识别率）。所以这里只放**最便宜、最能暴露形态差异**的断言。

## 怎么跑

```powershell
uv run pytest -m live --live -q
```

不加 `--live` 时全部 skip（见 conftest 的 `pytest_collection_modifyitems`）。
没有对应密钥时自动 skip，并说明缺哪一组变量——**不要把"没配"与"没测"混起来**。

## 成本

三个用例合计约 3 次调用（1 次文本、1 次图像、1 次 JSON 模式），
按常见价位不到一分钱。刻意压到这个量级：如果冒烟测试本身很贵，
它就会被跳过，然后等于没写。
"""

from __future__ import annotations

import pytest

from docqa.config import load_config
from docqa.errors import ProviderError
from docqa.llm import ImagePart, build_provider

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _require(cfg, attr: str, what: str) -> None:
    if not getattr(cfg, attr):
        pytest.skip(f"未配置{what}端点，跳过（这不是失败：缺的是密钥，不是能力）")


# ============================================================ 生成端点

class TestLlmEndpoint:
    def test_text_completion_returns_content(self, cfg):
        """最基础的一条：端点是通的，且 `content` 非空。

        为什么值得单测：`finish_reason` 与 `content` 是两件事。
        推理模型在 `max_tokens` 给得太小时会返回 `finish_reason="length"`
        且 `content` 为空——那看起来和"端点坏了"一模一样。
        这条断言把两者分开：要么有内容，要么报错信息里能看出是哪种。
        """
        _require(cfg, "has_llm", "生成")
        provider = build_provider(
            cfg.llm, timeout=cfg.http_timeout, max_retries=1, what="答案生成"
        )
        resp = provider.complete(
            "你是一个简洁的助手。只回答被问到的内容，不要解释。",
            "只回四个字：连接正常",
            max_tokens=256,
        )
        assert resp.text.strip(), (
            f"端点返回了空内容（finish_reason={resp.finish_reason}）。"
            f"若 finish_reason=length，说明 max_tokens 不够给推理模型思考用，"
            f"而不是端点故障。"
        )
        assert resp.model, "provider 必须报告实际使用的模型名（用于溯源）"

    def test_json_mode_parses(self, cfg):
        """JSON 模式：真实响应必须能被 `parse_json()` 解析。

        OCR 整条链路都建立在这个能力上（`vlm.py` 走
        `complete(json_mode=True)` → `response.parse_json()`）。
        单元测试用假响应覆盖了解析逻辑，但只有真端点能验证
        "它是否真的按 JSON 回话"——有些模型会把 JSON 包在 ```json 里，
        甚至前后加一段说明。
        """
        _require(cfg, "has_llm", "生成")
        provider = build_provider(
            cfg.llm, timeout=cfg.http_timeout, max_retries=1, what="答案生成"
        )
        resp = provider.complete(
            "你是一个只输出 JSON 的工具。不要输出 markdown 代码围栏，不要解释。",
            '返回一个 JSON 对象，恰好包含两个键：'
            '{"ok": true, "kind": "smoke"}（值照抄即可）',
            max_tokens=256,
            json_mode=True,
        )
        data = resp.parse_json()
        assert data.get("ok") is True, f"JSON 内容不符预期：{list(data)}"
        # 比对时去掉空白：模型可能把单词折行输出（实测把 "smoke" 写成
        # "sm\noke"）。这**不是**产品的 bug——`parse_json` 用 `strict=False`
        # 放宽控制字符，而 OCR 场景下的字符串本来就合法地含换行，
        # 抹掉它反而是错的。所以这里比的是"值有没有丢"，不是"排版是否一致"。
        kind = str(data.get("kind", "")).replace("\n", "").replace("\r", "").strip()
        assert kind == "smoke", f"kind 值不符：{data.get('kind')!r}"


# ============================================================ OCR 端点

class TestOcrEndpoint:
    def test_vision_endpoint_actually_reads_the_image(self, cfg, tmp_path):
        """**最关键的一条**：端点是否真的看得到图。

        只填了模型名不代表它支持图像输入——纯文本模型会**静默忽略**图片，
        然后返回一段看似合理的文字。这种失败在单元测试里永远发现不了，
        而它会让整条 OCR 链路的输出变成"模型在瞎猜"。

        做法：画一张只有我们知道答案的图（含一个四位数与一块颜色），
        要求模型同时答出两者。只说对一个是合理的部分失败，
        **一个都说不对**才是"它没看图"。
        """
        _require(cfg, "has_ocr", "OCR")
        from PIL import Image, ImageDraw

        img_path = tmp_path / "probe.png"
        img = Image.new("RGB", (320, 160), "white")
        draw = ImageDraw.Draw(img)
        draw.rectangle([10, 10, 120, 150], fill=(30, 90, 200))  # 蓝色块
        draw.text((180, 60), "7391", fill="black")
        img.save(img_path)

        provider = build_provider(
            cfg.ocr, timeout=cfg.http_timeout, max_retries=1, what="OCR 识别"
        )
        resp = provider.complete(
            "你是一个图像识别工具。只回答被问到的内容，不要解释。",
            "这张图里写了什么数字？左侧的大矩形是什么颜色？"
            "只回一行，格式：数字,颜色",
            images=[ImagePart.from_path(img_path)],
            max_tokens=2048,
        )
        got = resp.text.strip()
        hit_number = "7391" in got
        hit_color = any(c in got for c in ("蓝", "blue", "Blue"))
        assert hit_number or hit_color, (
            f"图像输入看起来被忽略了：返回 {got[:80]!r}，"
            f"既没有数字 7391 也没有颜色。"
            f"常见原因：该模型不支持视觉输入，或 endpoint 把图片丢掉了。"
        )

    def test_vision_failure_is_reported_not_silent(self, cfg):
        """没配 OCR 端点时，构造 provider 必须**明确报错**而不是返回 None。

        这一条是给"静默降级"上锁：如果将来有人把 `build_provider` 改成
        "缺密钥就返回空实现"，OCR 会因为每页都返回空块而**看起来成功**，
        然后全部答案都建立在空证据上。
        """
        if cfg.has_ocr:
            pytest.skip("本机已配 OCR 端点，这条只验证'缺配置时的行为'")
        with pytest.raises(ProviderError | Exception) as ei:
            build_provider(cfg.ocr, what="OCR 识别")
        assert "OCR" in str(ei.value) or "端点" in str(ei.value)
