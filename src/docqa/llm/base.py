"""模型 provider 的公共接口与重试策略。

为什么要抽象
------------
本项目要同时支持：OpenAI 兼容端点（覆盖 DeepSeek / 通义 / 智谱 / vLLM / Ollama …）、
Anthropic Messages API、以及**没有模型时**的抽取式回退。三者的差异集中在
请求构造与响应解析上，其余（重试、计量、超时）应当完全共用。

把"没有模型"也做成一个 provider，而不是在调用处到处写 `if llm is None`，
好处是**降级路径本身也能被测试**——它是一条正常分支，不是异常处理。
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..config import ProviderConfig
from ..errors import AuthError, ProviderError
from ..logging import get_logger
from ..models import Usage

_log = get_logger("llm")

# 图片内联进请求前的大小上限。超过就应当走供应商的文件上传接口，
# 这里没有实现——而是明确报错，避免把超大请求发出去后拿到一个费解的响应。
MAX_INLINE_IMAGE_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True)
class ImagePart:
    """一张待识别的页图。已编码为 base64，可直接放进请求体。"""

    path: Path
    mime: str
    data_b64: str
    sha256: str = ""

    @classmethod
    def from_path(cls, path: Path) -> "ImagePart":
        raw = path.read_bytes()
        if len(raw) > MAX_INLINE_IMAGE_BYTES:
            raise ProviderError(
                f"页图 {path.name} 有 {len(raw) / 1e6:.1f} MB，超过内联上限 "
                f"{MAX_INLINE_IMAGE_BYTES / 1e6:.0f} MB。请降低渲染 DPI 或改用 JPEG。",
                retryable=False,
            )
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        return cls(path=path, mime=mime, data_b64=base64.b64encode(raw).decode("ascii"))

    @property
    def data_uri(self) -> str:
        return f"data:{self.mime};base64,{self.data_b64}"


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    usage: Usage = field(default_factory=Usage)
    # 结束原因。**必须暴露出来**：推理模型在 max_tokens 不足时会返回
    # `finish_reason="length"` 且 content 为空（token 全花在 reasoning 上），
    # 这跟"模型没读图"是两回事，但只看 content 是分不出来的。
    finish_reason: str | None = None
    # 推理模型的思维链。它可能包含答案（当 content 被截断时），
    # 但对生产用途而言**不能当作正式输出**——只用于诊断。
    reasoning: str = ""
    # 供应商返回的原始 JSON。留着是为了排查——当解析失败时，
    # 能直接看到对方到底回了什么，而不是只有一句"解析失败"。
    raw: dict[str, Any] | None = None

    @property
    def truncated(self) -> bool:
        """是否因 token 上限被截断。被截断的输出**不能当作完整结果**。"""
        return self.finish_reason == "length"

    def parse_json(self) -> Any:
        """把返回文本当 JSON 解析，容忍常见的包裹形式。

        模型即使被要求"只输出 JSON"，也常会包一层 ```json 代码块。
        这里做容错，但**不做"猜"**——解析不出来就抛出原始文本，
        由调用方决定是重试还是降级。

        **容错分两档，第二档是实测逼出来的**：
        `json.loads` 默认 `strict=True`，会拒绝字符串内部的裸控制字符
        （未转义的换行、制表符）。而模型把多行内容塞进一个 JSON 字符串时，
        很可能就写成裸换行——那不是"模型没按格式回话"，是**一个只需放宽
        一处、内容其实完好**的响应。真端点冒烟测试第一次跑就撞上了这个
        （`Invalid control character at: line 1 column 44`）。

        放宽只针对控制字符，其余语法错误照旧失败：
        把 `strict=False` 用在**第一次**解析上会让截断的 JSON、缺逗号之类
        的真错误也被吞掉，所以它只作兜底。
        """
        text = self.text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3:
                text = "\n".join(lines[1:-1])
        # 再退一步：截取第一个 { 到最后一个 }
        if not text.startswith("{") and "{" in text:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                text = text[start : end + 1]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 第二档：只放宽控制字符。仍然失败才报错。
            pass
        try:
            return json.loads(text, strict=False)
        except json.JSONDecodeError as exc:
            # **一个字都不回显**。OCR 场景下模型输出就是**文档正文的转录**，
            # 回显 500 字符等于把半页文档写进日志——而这个异常还会被
            # `vlm.py` / `pipeline.py` / `with_retry` 三处各截一段再打一次。
            #
            # HTTP 层早就做对了（`openai_compat.classify_status` 特意注明
            # "不回显响应体…不会把不可信内容带进日志"），这条路径漏了。
            #
            # 只给**形态 + 长度 + 摘要哈希**：形态足以判断"是 HTML 错误页、
            # 散文、还是截断的 JSON"，哈希足以在多次重试间对上"是不是同一个东西"，
            # 两者都不泄漏内容。
            # （试过"回显前 40 字符"——对短输出那就是全文，被测试当场抓出来了。）
            shape = "含 { 的片段" if "{" in self.text else "不含 { 的文本"
            digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:8]
            raise ProviderError(
                f"模型返回的不是合法 JSON（{exc}）。"
                f"返回 {len(self.text)} 字符，形态：{shape}，摘要：{digest}。"
                f"（内容不回显：OCR 场景下模型输出即文档正文）",
                retryable=True,
            ) from exc


class LLMProvider(Protocol):
    """所有 provider 的最小契约。"""

    name: str
    supports_vision: bool

    def complete(
        self,
        system: str,
        user: str,
        *,
        images: list[ImagePart] | None = None,
        max_tokens: int = 1200,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> LLMResponse: ...


# ---------------------------------------------------------------- 重试

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 522, 524})


def classify_status(status: int, body: str) -> ProviderError:
    """把 HTTP 状态码翻成带 `retryable` 的异常。

    分对这一点很实际：限流重试是对的，鉴权失败重试只是白烧配额，
    而且会把一个"密钥填错了"的明确问题伪装成一个"服务不稳定"的模糊问题。

    **为什么不把响应体拼进错误消息**（这里原先是拼的）
    ---------------------------------------------------
    响应体是**第三方返回的、不可信的内容**，而我们无法预知它里面有什么。
    一次实测就撞上了：模拟网关把请求的 `Authorization` 头**回显**在错误页里，
    于是密钥顺着"服务端暂时不可用（HTTP 500）。{body}"这条消息，
    经 `with_retry` 的日志、以及终端上的错误面板，一路漏了出去。

    有人会说"那就把密钥形态擦掉"——**那正是被删掉的那层正则**。
    擦拭是治标：它只能覆盖想得到的形态，而且会让人放松。
    这里改成**从源头不写**：错误消息只带状态码与我们自己的处置建议，
    不夹带任何外部返回的内容。

    代价是少了一点排查信息（比如"模型名拼错了"的原文）。
    但真正决定用户该做什么的是**状态码**——401 去查密钥、404 去查模型名——
    这些建议是我们自己给的，比对方的原文更可操作。
    """
    if status in (401, 403):
        return AuthError(
            f"鉴权失败（HTTP {status}）。请检查 .env 中的密钥是否正确、是否过期。"
        )
    if status == 404:
        return ProviderError("接口或模型不存在（HTTP 404）。请检查 base_url 与模型名。")
    if status == 400:
        return ProviderError("请求被拒绝（HTTP 400），通常是模型名错误或参数不支持。")
    if status in RETRYABLE_STATUS:
        return ProviderError(
            f"服务端暂时不可用（HTTP {status}）。", retryable=True, status=status
        )
    return ProviderError(
        f"未预期的响应（HTTP {status}）。", retryable=False, status=status
    )


def with_retry(fn, *, max_retries: int, what: str):
    """带抖动的指数退避。

    抖动不是可选项：多个页并发失败时，固定退避会让它们**同时**重试，
    把一次限流放大成二次限流。
    """
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except ProviderError as exc:
            last = exc
            if not exc.retryable or attempt == max_retries:
                raise
            delay = min(2**attempt, 20) * (0.5 + random.random())
            _log.warning(
                f"{what} 第 {attempt + 1} 次失败，{delay:.1f}s 后重试：{exc}",
                extra={"stage": "retry", "detail": str(exc)[:200]},
            )
            time.sleep(delay)
    assert last is not None
    raise last


def estimate_cost(cfg: ProviderConfig, usage: Usage) -> float:
    """按供应商自报单价估算成本。

    单价为 0（未配置）时返回 0，并在报告里如实标注"成本未计量"——
    而不是编一个看起来像模像样的数字。
    """
    if cfg.price_in_per_mtok <= 0 and cfg.price_out_per_mtok <= 0:
        return 0.0
    return (
        usage.tokens_in / 1_000_000 * cfg.price_in_per_mtok
        + usage.tokens_out / 1_000_000 * cfg.price_out_per_mtok
    )
