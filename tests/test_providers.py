"""模型 provider 测试。

全程用假的 HTTP 客户端，**不发任何真实请求**。要验证的是：

- 重试策略对不对（限流重试、鉴权不重试）——分错这一点很实际：
  鉴权失败重试只会白烧配额，还会把一个明确的问题伪装成"服务不稳定"；
- 错误分类是否准确（404 说清是模型名/地址问题，而不是笼统报错）；
- 视觉能力探测能否识别出"端点其实没读图"这种静默失败。
"""

from __future__ import annotations

import json

import httpx
import pytest

from docqa.config import ProviderConfig
from docqa.errors import AuthError, ConfigError, ProviderError
from docqa.llm import build_provider
from docqa.llm.base import ImagePart, LLMResponse, classify_status, with_retry
from docqa.llm.openai_compat import OpenAICompatProvider

CFG = ProviderConfig(
    provider="openai", base_url="https://example.test/v1",
    api_key="sk-test", model="test-model", supports_vision=True,
)


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_body(text: str = "OK", model: str = "test-model") -> dict:
    return {
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


class TestStatusClassification:
    def test_auth_is_not_retryable(self):
        assert not classify_status(401, "bad key").retryable
        assert isinstance(classify_status(401, "bad key"), AuthError)

    def test_rate_limit_is_retryable(self):
        assert classify_status(429, "slow down").retryable

    def test_server_error_is_retryable(self):
        assert classify_status(503, "oops").retryable

    def test_bad_request_is_not_retryable(self):
        assert not classify_status(400, "bad model").retryable

    def test_404_mentions_model_or_url(self):
        assert "模型" in str(classify_status(404, "")) or "base_url" in str(classify_status(404, ""))


class TestRetry:
    def test_retries_then_succeeds(self):
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ProviderError("temp", retryable=True)
            return "ok"

        assert with_retry(flaky, max_retries=3, what="t") == "ok"
        assert attempts["n"] == 3

    def test_non_retryable_raises_immediately(self):
        attempts = {"n": 0}

        def fatal():
            attempts["n"] += 1
            raise ProviderError("fatal", retryable=False)

        with pytest.raises(ProviderError):
            with_retry(fatal, max_retries=3, what="t")
        assert attempts["n"] == 1  # 没有浪费重试

    def test_exhausts_retries(self):
        with pytest.raises(ProviderError):
            with_retry(lambda: (_ for _ in ()).throw(ProviderError("x", retryable=True)),
                       max_retries=1, what="t")


class TestOpenAICompat:
    def test_successful_completion(self):
        p = OpenAICompatProvider(CFG, client=make_client(lambda r: httpx.Response(200, json=ok_body("答案"))))
        resp = p.complete("sys", "user")
        assert resp.text == "答案"
        assert resp.usage.tokens_in == 100
        assert resp.usage.calls == 1

    def test_endpoint_appends_path(self):
        seen = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["url"] = str(req.url)
            return httpx.Response(200, json=ok_body())

        OpenAICompatProvider(CFG, client=make_client(handler)).complete("s", "u")
        assert seen["url"].endswith("/chat/completions")

    def test_base_url_already_ending_in_path(self):
        cfg = ProviderConfig(provider="openai", base_url="https://x.test/v1/chat/completions",
                             api_key="k", model="m")
        seen = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["url"] = str(req.url)
            return httpx.Response(200, json=ok_body())

        OpenAICompatProvider(cfg, client=make_client(handler)).complete("s", "u")
        assert seen["url"].count("/chat/completions") == 1

    def test_auth_header_sent(self):
        seen = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["auth"] = req.headers.get("authorization", "")
            return httpx.Response(200, json=ok_body())

        OpenAICompatProvider(CFG, client=make_client(handler)).complete("s", "u")
        assert seen["auth"].startswith("Bearer ")

    def test_image_is_sent_as_data_uri(self, tmp_path):
        img = tmp_path / "p.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
        seen = {}

        def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content)
            seen["content"] = body["messages"][-1]["content"]
            return httpx.Response(200, json=ok_body())

        OpenAICompatProvider(CFG, client=make_client(handler)).complete(
            "s", "u", images=[ImagePart.from_path(img)]
        )
        types = [c["type"] for c in seen["content"]]
        assert "image_url" in types
        assert seen["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_json_mode_adds_response_format(self):
        seen = {}

        def handler(req: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(req.content)
            return httpx.Response(200, json=ok_body())

        OpenAICompatProvider(CFG, client=make_client(handler)).complete("s", "u", json_mode=True)
        assert seen["body"]["response_format"]["type"] == "json_object"

    def test_auth_error_maps_to_autherror(self):
        p = OpenAICompatProvider(CFG, max_retries=0,
                                 client=make_client(lambda r: httpx.Response(401, text="nope")))
        with pytest.raises(AuthError):
            p.complete("s", "u")

    def test_malformed_response_raises(self):
        p = OpenAICompatProvider(CFG, client=make_client(lambda r: httpx.Response(200, json={"x": 1})))
        with pytest.raises(ProviderError, match="choices"):
            p.complete("s", "u")

    def test_timeout_maps_to_retryable(self):
        def handler(req):
            raise httpx.TimeoutException("too slow")

        p = OpenAICompatProvider(CFG, max_retries=0, client=make_client(handler))
        with pytest.raises(ProviderError) as e:
            p.complete("s", "u")
        assert e.value.retryable


class TestJsonParsing:
    def test_plain_json(self):
        assert LLMResponse("{\"a\":1}", "p", "m").parse_json() == {"a": 1}

    def test_strips_markdown_fence(self):
        assert LLMResponse("```json\n{\"a\":1}\n```", "p", "m").parse_json() == {"a": 1}

    def test_extracts_from_surrounding_prose(self):
        assert LLMResponse("好的，结果是 {\"a\":1} 完毕", "p", "m").parse_json() == {"a": 1}

    def test_invalid_json_raises_provider_error(self):
        with pytest.raises(ProviderError):
            LLMResponse("完全不是 JSON", "p", "m").parse_json()

    def test_unescaped_newline_in_string_is_tolerated(self):
        """字符串内的裸换行要能解析出来（`strict=False` 兜底）。

        这是**真端点冒烟测试第一次跑就撞上的**：模型把多行内容塞进
        一个 JSON 字符串时，常写成裸换行而不写 `\\n`，`json.loads`
        默认 `strict=True` 会直接拒绝：
        `Invalid control character at: line 1 column 44`。

        对 OCR 场景这不是小事——识别结果里**每一条正文块都含换行**，
        一旦整页 JSON 有处裸换行就被判"模型没按格式回话"并重试，
        等于把一次成功的识别当成失败，白花钱还拿不到结果。
        """
        raw = '{"blocks": [{"text": "3.1 抗拉强度\n应大于等于 590 MPa"}]}'
        got = LLMResponse(raw, "p", "m").parse_json()
        assert got["blocks"][0]["text"] == "3.1 抗拉强度\n应大于等于 590 MPa"

    def test_still_rejects_genuinely_broken_json(self):
        """放宽的只有控制字符，**真语法错误照旧失败**。

        这条是防止兜底变成"什么都收"：缺逗号、截断的 JSON 必须继续报错，
        否则下游会拿到一个结构不对的 dict，然后在几十行之外炸掉。
        """
        for bad in (
            '{"a": 1 "b": 2}',      # 缺逗号
            '{"a": [1, 2',          # 截断
            '{"a": 1} 后面还有话',   # 尾部多余内容（截取 {..} 后仍是坏的）
        ):
            with pytest.raises(ProviderError):
                LLMResponse(bad, "p", "m").parse_json()


class TestBuildProvider:
    def test_unknown_provider_rejected(self):
        with pytest.raises(ConfigError, match="不支持"):
            build_provider(ProviderConfig(provider="nope", api_key="k", model="m"))

    def test_missing_key_rejected(self):
        with pytest.raises(ConfigError, match="密钥"):
            build_provider(ProviderConfig(provider="openai", model="m"))

    def test_missing_model_rejected(self):
        with pytest.raises(ConfigError, match="模型名"):
            build_provider(ProviderConfig(provider="openai", api_key="k"))

    def test_error_names_the_purpose(self):
        with pytest.raises(ConfigError, match="OCR 识别"):
            build_provider(ProviderConfig(provider="openai", model="m"), what="OCR 识别")

    def test_anthropic_provider_constructed(self):
        p = build_provider(ProviderConfig(provider="anthropic", api_key="k", model="claude-opus-5"))
        assert "anthropic" in p.name


class TestImagePart:
    def test_data_uri_format(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 8)
        part = ImagePart.from_path(img)
        assert part.data_uri.startswith("data:image/png;base64,")
        assert part.mime == "image/png"

    def test_oversized_image_rejected(self, tmp_path, monkeypatch):
        from docqa.llm import base

        monkeypatch.setattr(base, "MAX_INLINE_IMAGE_BYTES", 4)
        img = tmp_path / "big.png"
        img.write_bytes(b"x" * 100)
        with pytest.raises(ProviderError, match="上限"):
            ImagePart.from_path(img)
