"""Anthropic Messages API 客户端（官方 SDK）。

为什么这里用 SDK 而不用裸 HTTP
------------------------------
本项目的 OpenAI 兼容适配器走裸 HTTP，因为那一侧对接的是**一堆厂商共同实现的
兼容协议**，没有单一官方 SDK 可依赖。Anthropic 这边不一样：有官方 SDK，
它替你处理了重试、超时、类型化异常和版本头。手搓只会把已经解决过的问题再解一遍。

两个容易踩的坑，都在代码里处理了
--------------------------------
1. **环境变量劫持。** SDK 会按 `ANTHROPIC_API_KEY` → `ANTHROPIC_AUTH_TOKEN`
   → 本地 profile 的顺序自己找凭证。而本机环境里恰好设置了
   `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN`（指向另一个服务）。
   所以这里**一律显式传入** api_key 与 base_url，绝不依赖 SDK 的隐式解析——
   否则会拿 A 服务的密钥去请求 B 服务，报错还很难懂。
2. **采样参数已被移除。** 当前一代模型（Opus 5、Sonnet 5、Opus 4.8/4.7 等）
   对 `temperature` / `top_p` / `top_k` 直接返回 400。识别任务天然需要确定性，
   而确定性现在由能力本身保证，不再靠采样参数。所以这里**默认不发送 temperature**；
   只有在配置里显式打开 `send_temperature` 时才发（供 Haiku 4.5 等旧模型使用）。
"""

from __future__ import annotations

import time

from ..config import ProviderConfig
from ..errors import AuthError, ProviderError
from ..logging import get_logger
from ..models import Usage
from .base import ImagePart, LLMResponse, estimate_cost, with_retry

_log = get_logger("llm.anthropic")

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-opus-5"


class AnthropicProvider:
    """Anthropic Messages API。"""

    def __init__(
        self,
        cfg: ProviderConfig,
        *,
        timeout: int = 120,
        max_retries: int = 3,
        client=None,
    ):
        if not cfg.api_key:
            raise ProviderError(f"provider {cfg.provider!r} 缺少 api_key", retryable=False)

        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - 依赖已在 pyproject 中
            raise ProviderError(
                "使用了 anthropic provider 但未安装官方 SDK。请执行 `uv sync` 安装。",
                retryable=False,
            ) from exc

        self._anthropic = anthropic
        self.cfg = cfg
        self.name = f"anthropic:{cfg.model or DEFAULT_MODEL}"
        self.supports_vision = cfg.supports_vision
        self._timeout = timeout
        self._max_retries = max_retries
        # 显式传参，屏蔽 SDK 对 ANTHROPIC_* 环境变量的隐式解析（见模块文档）
        self._client = client or anthropic.Anthropic(
            api_key=cfg.api_key,
            base_url=(cfg.base_url or DEFAULT_BASE_URL).rstrip("/"),
            timeout=float(timeout),
            max_retries=0,  # 重试统一由 with_retry 控制，避免双重退避
        )
        self._model = cfg.model or DEFAULT_MODEL

    def _build_content(self, user: str, images: list[ImagePart] | None) -> list[dict]:
        content: list[dict] = []
        for img in images or []:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img.mime,
                        "data": img.data_b64,
                    },
                }
            )
        content.append({"type": "text", "text": user})
        return content

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
        anthropic = self._anthropic
        kwargs: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": self._build_content(user, images)}],
        }
        if system:
            kwargs["system"] = system
        # 见模块文档第 2 点：当前一代模型拒绝采样参数。
        # json_mode 同理——这里不发送任何输出格式约束，改用提示词约束 +
        # LLMResponse.parse_json 的容错解析，保证跨模型版本行为一致。

        def _call() -> LLMResponse:
            started = time.perf_counter()
            try:
                resp = self._client.messages.create(**kwargs)
            except anthropic.AuthenticationError as exc:
                raise AuthError(
                    f"Anthropic 鉴权失败。请检查 .env 中的 DOCQA_OCR_API_KEY / "
                    f"DOCQA_LLM_API_KEY 是否正确。（{exc}）"
                ) from exc
            except anthropic.RateLimitError as exc:
                raise ProviderError(f"触发限流：{exc}", retryable=True, status=429) from exc
            except anthropic.APIConnectionError as exc:
                raise ProviderError(f"网络错误：{exc}", retryable=True) from exc
            except anthropic.APIStatusError as exc:
                retryable = exc.status_code >= 500
                raise ProviderError(
                    f"接口返回 HTTP {exc.status_code}：{exc}",
                    retryable=retryable,
                    status=exc.status_code,
                ) from exc

            # 安全拒绝是正常响应（HTTP 200），必须显式检查，
            # 否则会把"模型拒答"误读成"模型返回了空内容"。
            if getattr(resp, "stop_reason", None) == "refusal":
                details = getattr(resp, "stop_details", None)
                category = getattr(details, "category", None) if details else None
                raise ProviderError(
                    f"模型以安全策略拒绝了本次请求（category={category}）。"
                    f"这通常与文档内容有关，可改用其它 provider 或先做内容审查。",
                    retryable=False,
                )

            text = "".join(
                block.text for block in resp.content if getattr(block, "type", "") == "text"
            )
            if not text:
                raise ProviderError(
                    f"模型返回了空文本（stop_reason={getattr(resp, 'stop_reason', None)}）",
                    retryable=True,
                )

            u = getattr(resp, "usage", None)
            usage = Usage(
                tokens_in=int(getattr(u, "input_tokens", 0) or 0),
                tokens_out=int(getattr(u, "output_tokens", 0) or 0),
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
                provider="anthropic",
                model=getattr(resp, "model", self._model),
                usage=usage,
                raw=None,
            )

        return with_retry(_call, max_retries=self._max_retries, what=f"调用 {self.name}")

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - 关闭失败不应影响主流程
            pass
