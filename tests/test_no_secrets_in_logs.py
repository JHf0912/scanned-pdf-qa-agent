"""密钥绝不允许出现在日志输出里。

这条测试是"**日志层不做打码**"这个决定的前提
------------------------------------------------------------------
曾经 `logging.py` 里有一层"写进去之后再正则擦掉"的兜底。它被删掉了，理由：

1. **它是治标**——擦拭发生在错误已经犯下之后。当初之所以要加它，
   正是因为某处日志调用真的把密钥传进去了。该改的是那个调用。
2. **它制造虚假的安全感**——"反正有打码"会让人放松。
3. **它总有漏的格式**——正则只覆盖想得到的形态，当初那版就没匹配上。

删掉兜底之后，"密钥不进日志"就只剩纪律。**纪律必须有东西守，否则是裸奔。**
这条测试就是那个东西，而且它守的是**源头**：谁把密钥写进日志，测试立刻红。

做法：把密钥设成一个**独特哨兵**，跑一遍会大量打日志的路径
——**尤其是失败路径**（异常消息最容易夹带密钥）——断言输出里绝不含该哨兵。

注意断言的是**日志输出**（stderr），不是日志对象：一个密钥可能被塞进
`extra=` 的任意字段、或拼进消息字符串，只有看最终输出才拦得住。
"""

from __future__ import annotations

import json

import httpx
import pytest

from docqa import logging as dlog
from docqa.config import ProviderConfig, load_config
from docqa.errors import DocqaError
from docqa.llm import build_provider

# 独特、可断言。用 sk- 前缀是为了贴近真实密钥形态——
# 如果哪天有人重新加回"按形态打码"，这条会立刻暴露那件事。
SENTINEL = "sk-SENTINEL-MUST-NEVER-APPEAR-IN-LOGS-0123456789abcdef"


@pytest.fixture
def logging_to_stderr(monkeypatch):
    """把日志重新绑到当前的 sys.stderr，好让 capsys 抓得到。

    必须在用例内部重新 `setup()`：handler 是在 setup 时绑定 sys.stderr 的，
    早先绑定的那个对象 capsys 抓不到。
    """
    monkeypatch.setenv("DOCQA_LOG_LEVEL", "DEBUG")
    dlog.setup("DEBUG", json_output=True)
    yield


def _captured(capsys) -> str:
    out = capsys.readouterr()
    return out.out + out.err


# ---------------------------------------------------------------- 成功路径

def test_config_summary_does_not_leak_key(logging_to_stderr, capsys, monkeypatch):
    """配置摘要是最容易顺手打印整个配置对象的地方。"""
    monkeypatch.setenv("DOCQA_OCR_PROVIDER", "openai")
    monkeypatch.setenv("DOCQA_OCR_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("DOCQA_OCR_MODEL", "m")
    monkeypatch.setenv("DOCQA_OCR_API_KEY", SENTINEL)

    cfg = load_config()
    log = dlog.get_logger("test")
    log.info("配置就绪", extra={"detail": json.dumps(cfg.describe(), ensure_ascii=False)})

    assert SENTINEL not in _captured(capsys)


# ---------------------------------------------------------------- 失败路径
#
# 异常消息是最容易夹带密钥的地方——错误处理里常把请求上下文一并打出来。

def _failing_provider(status: int, *, body: str = "boom"):
    """构造一个总是返回指定错误码的 provider（不发真实请求）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        # 服务端「回显」请求头是现实中会发生的（有些网关把请求原样贴回错误页）。
        # 这里故意这么干，验证的是**我们的**代码不会把它带进日志。
        echoed = request.headers.get("authorization", "")
        return httpx.Response(status, text=f"{body} echoed_auth={echoed}")

    return build_provider(
        ProviderConfig(provider="openai", base_url="https://example.test/v1",
                       api_key=SENTINEL, model="m", supports_vision=True),
        max_retries=0,
    ), httpx.MockTransport(handler)


@pytest.mark.parametrize("status", [401, 429, 500, 400])
def test_provider_errors_do_not_leak_key(status, logging_to_stderr, capsys, monkeypatch):
    """各种失败码下，异常消息与日志都不得含密钥。"""
    provider, transport = _failing_provider(status)
    provider._client = httpx.Client(transport=transport)

    with pytest.raises(DocqaError):
        provider.complete("sys", "user")

    assert SENTINEL not in _captured(capsys)


def test_retry_logs_do_not_leak_key(logging_to_stderr, capsys, monkeypatch):
    """重试路径会**主动打日志**——这条路径必须单独验。

    它把异常消息原样写进日志，是密钥最容易漏出去的一处。
    """
    provider, transport = _failing_provider(429)
    provider._client = httpx.Client(transport=transport)
    provider._max_retries = 2

    with pytest.raises(DocqaError):
        provider.complete("sys", "user")

    assert SENTINEL not in _captured(capsys)


def test_vision_probe_logs_do_not_leak_key(logging_to_stderr, capsys, monkeypatch):
    """`docqa doctor` 的实测路径也要干净——它会打印模型回答。"""
    from docqa.cli import prove_vision

    monkeypatch.setenv("DOCQA_OCR_PROVIDER", "openai")
    monkeypatch.setenv("DOCQA_OCR_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("DOCQA_OCR_MODEL", "m")
    monkeypatch.setenv("DOCQA_OCR_API_KEY", SENTINEL)

    cfg = load_config()
    provider, transport = _failing_provider(500)

    import docqa.llm as llm_mod

    real_build = llm_mod.build_provider

    def patched(cfg_, **kw):
        p = real_build(cfg_, **kw)
        p._client = httpx.Client(transport=transport)
        return p

    monkeypatch.setattr("docqa.llm.build_provider", patched)
    ok, message = prove_vision(cfg)

    assert ok is False
    assert SENTINEL not in message, "doctor 的提示文案里夹带了密钥"
    assert SENTINEL not in _captured(capsys)


# ---------------------------------------------------------------- 结构性

def test_logging_module_has_no_masking_layer():
    """日志层**不得**重新引入打码。有它就会让人放松，反而更容易漏。

    这条断言防的是"以后有人觉得不安全又把正则加回来"——
    那样等于默认"写进去也没关系"，纪律就废了。
    """
    import inspect

    from docqa import logging as mod

    src = inspect.getsource(mod)
    for banned in ("_RedactingFilter", "scrub_secrets", "_SECRET_KEYS"):
        assert banned not in src, (
            f"logging.py 不应包含 {banned}——"
            f"密钥不该写进日志，而不是写进去再擦掉"
        )
