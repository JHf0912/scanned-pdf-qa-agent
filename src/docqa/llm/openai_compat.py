"""OpenAI 兼容的 /chat/completions 客户端。

覆盖面
------
这一条适配器同时服务 DeepSeek、通义千问、智谱、Kimi、vLLM、Ollama、LM Studio
等一大批服务——它们都实现了 OpenAI 的请求/响应格式。所以整个项目只需要
两个真实 provider 实现，就能接上绝大多数端点。

图像输入用的是 OpenAI 的 `image_url` + data URI 形式。**注意**：
能接受这个字段不等于模型真的看得见图——有些纯文本模型会静默忽略图像部分，
返回一段看似合理的文字。所以 `docqa doctor` 会用一张测试图实测，
不靠"字段发出去了"来推断能力。
"""

from __future__ import annotations

import time

import httpx

from ..config import ProviderConfig
from ..errors import ProviderError
from ..logging import get_logger
from ..models import Usage
from .base import (
    ImagePart,
    LLMResponse,
    classify_status,
    estimate_cost,
    with_retry,
)

_log = get_logger("llm.openai")

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAICompatProvider:
    """任何实现 OpenAI Chat Completions 协议的端点。"""

    def __init__(
        self,
        cfg: ProviderConfig,
        *,
        timeout: int = 120,
        max_retries: int = 3,
        client: httpx.Client | None = None,
    ):
        if not cfg.api_key:
            raise ProviderError(f"provider {cfg.provider!r} 缺少 api_key", retryable=False)
        self.cfg = cfg
        self.name = f"openai-compat:{cfg.model}"
        self.supports_vision = cfg.supports_vision
        self._timeout = timeout
        self._max_retries = max_retries
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    # ------------------------------------------------------------ internals

    @property
    def _endpoint(self) -> str:
        base = (self.cfg.base_url or DEFAULT_BASE_URL).rstrip("/")
        # 用户可能填到 /v1，也可能填到根；两种都接受
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }

    def _build_messages(
        self, system: str, user: str, images: list[ImagePart] | None
    ) -> list[dict]:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        if images:
            content: list[dict] = [{"type": "text", "text": user}]
            for img in images:
                content.append({"type": "image_url", "image_url": {"url": img.data_uri}})
            messages.append({"role": "user", "content": content})
        else:
            messages.append({"role": "user", "content": user})
        return messages

    # ------------------------------------------------------------ public

    def complete(
        self,
        system: str,
        user: str,
        *,
        images: list[ImagePart] | None = None,
        max_tokens: int = 1200,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> LLMResponse:
        payload: dict = {
            "model": self.cfg.model,
            "messages": self._build_messages(system, user, images),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            # 并非所有兼容端点都支持 response_format；不支持时由调用方
            # 依赖提示词约束 + parse_json 的容错，所以这里失败不致命。
            payload["response_format"] = {"type": "json_object"}

        def _call() -> LLMResponse:
            started = time.perf_counter()
            try:
                resp = self._client.post(
                    self._endpoint, headers=self._headers(), json=payload, timeout=self._timeout
                )
            except httpx.TimeoutException as exc:
                raise ProviderError(f"请求超时（{self._timeout}s）：{exc}", retryable=True) from exc
            except httpx.HTTPError as exc:
                raise ProviderError(f"网络错误：{exc}", retryable=True) from exc

            if resp.status_code >= 400:
                raise classify_status(resp.status_code, resp.text)

            try:
                data = resp.json()
            except ValueError as exc:
                # **不回显响应体**：这里最常见的场景就是网关返回了一个 HTML 错误页，
                # 而错误页恰恰是最可能把请求头（含 Authorization）原样贴回来的地方。
                # 只报"是什么类型、多大"，足以判断"这不是我们的接口"，
                # 又不会把不可信内容带进日志。
                ctype = resp.headers.get("content-type", "未知")
                raise ProviderError(
                    f"响应不是 JSON（HTTP {resp.status_code}，"
                    f"Content-Type: {ctype}，{len(resp.content)} 字节）。"
                    f"若 Content-Type 是 text/html，多半是网关或代理返回的错误页，"
                    f"请检查 base_url 是否指向了正确的接口。",
                    retryable=True,
                ) from exc

            try:
                choice = data["choices"][0]
                msg = choice["message"]
                text = msg.get("content") or ""
                finish = choice.get("finish_reason")
                # 推理模型把思考放在 reasoning_content，正式答案在 content。
                # 取出来是为了在 content 为空时能说清"是被截断了"而不是"没读图"。
                reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
            except (KeyError, IndexError, TypeError) as exc:
                # 只说**结构**（顶层有哪些键），不回显内容——同样是不可信数据。
                shape = sorted(data)[:12] if isinstance(data, dict) else type(data).__name__
                raise ProviderError(
                    f"响应结构不符合预期，缺少 choices[0].message.content。"
                    f"顶层实际包含：{shape}。请确认该端点是否实现了 OpenAI 兼容协议。",
                    retryable=True,
                ) from exc

            usage_raw = data.get("usage") or {}
            usage = Usage(
                tokens_in=int(usage_raw.get("prompt_tokens") or 0),
                tokens_out=int(usage_raw.get("completion_tokens") or 0),
                latency_ms=int((time.perf_counter() - started) * 1000),
                calls=1,
            )
            usage = Usage(
                tokens_in=usage.tokens_in,
                tokens_out=usage.tokens_out,
                est_cost_usd=estimate_cost(self.cfg, usage),
                latency_ms=usage.latency_ms,
                calls=1,
            )
            return LLMResponse(
                text=text,
                provider=self.cfg.provider,
                model=data.get("model") or self.cfg.model,
                usage=usage,
                finish_reason=finish,
                reasoning=reasoning,
                raw=data,
            )

        return with_retry(_call, max_retries=self._max_retries, what=f"调用 {self.name}")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
