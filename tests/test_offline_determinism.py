"""默认测试通道必须**离线且确定**：不读开发机的 `.env`。

这条约束不是洁癖，是三件具体的事：

1. **钱**：本机 `.env` 配了真实端点。没钉住的话，几个没传 `--no-llm`
   的 CLI 用例会真的打端点，跑一次测试就花一次钱；
2. **可复现**：生成模型的输出有采样波动，同一条断言可能今天过明天不过。
   一个会自己变的测试不能当回归门禁；
3. **隔离**：CI 上没有 `.env`，本地有——两边跑的根本不是同一条路径，
   而"本地绿了"会被当成"CI 也会绿"。

实现见 `conftest.py::pytest_configure`（在收集用例前清掉 `DOCQA_*`
并置 `DOCQA_DISABLE_DOTENV=1`）与 `config.py::load_config`。

这里的用例守两件事：开关本身有效，以及它**没有把逃生口焊死**
（显式 `env_file=` 仍然要能读）。
"""

from __future__ import annotations

import os
from unittest import mock

import pytest

import docqa.config as config_module
from docqa.config import DOTENV_DISABLE_ENV, load_config

# 一份"看起来完全可用"的端点配置。值不重要，重要的是它**不该被读到**。
_FAKE_ENV = (
    "DOCQA_LLM_PROVIDER=openai\n"
    "DOCQA_LLM_BASE_URL=https://example.invalid/v1\n"
    "DOCQA_LLM_API_KEY=sk-offline-determinism\n"
    "DOCQA_LLM_MODEL=fake-model\n"
    "DOCQA_OCR_PROVIDER=openai\n"
    "DOCQA_OCR_API_KEY=sk-offline-determinism\n"
    "DOCQA_OCR_MODEL=fake-vision\n"
)


def _clear_docqa_env(keep_switch: bool = True) -> None:
    for key in list(os.environ):
        if key.startswith("DOCQA_") and not (keep_switch and key == DOTENV_DISABLE_ENV):
            del os.environ[key]


def _clear_docqa_env_via_monkeypatch(monkeypatch) -> None:
    """清掉 DOCQA_*（含开关），但让 monkeypatch 在用例结束后恢复。

    离线命令的测试要模拟**真实用户 shell**（没有 conftest 置上的开关）。
    这里不能直接 `del os.environ[...]`：那会把开关从整个测试会话里永久删掉，
    后续用例就会开始读开发机的 `.env`——测试之间互相污染，而且是静默的。
    """
    for key in [k for k in os.environ if k.startswith("DOCQA_")]:
        monkeypatch.delenv(key, raising=False)


class TestDefaultChannelIsOffline:
    def test_dotenv_loading_is_disabled(self, request):
        """默认通道里，conftest 必须已经把开关置上。"""
        if request.config.getoption("--live"):
            pytest.skip("--live 通道按设计会读 .env，这条只守默认通道")
        assert os.environ.get(DOTENV_DISABLE_ENV) == "1"

    def test_default_config_has_no_endpoints(self, request):
        """默认通道里读出来的配置不能有任何端点。

        这条直接检查**测试进程的实际状态**：如果将来有人把 conftest 的
        configure 钩子删掉、或改了开关名，它会立刻失败——而不是等人发现
        "怎么测试又开始花钱了"。
        """
        if request.config.getoption("--live"):
            pytest.skip("--live 通道按设计会读 .env")
        cfg = load_config()
        assert not cfg.has_llm, "默认测试读到了生成端点：.env 没有被隔离"
        assert not cfg.has_ocr, "默认测试读到了 OCR 端点：.env 没有被隔离"
        assert not cfg.has_ocr2


class TestDisableDotenv:
    def test_project_dotenv_is_not_read(self, tmp_path, monkeypatch):
        """开关打开时，项目根的 `.env`（哪怕内容完整）必须被忽略。"""
        (tmp_path / ".env").write_text(_FAKE_ENV, encoding="utf-8")
        monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)

        with mock.patch.dict(os.environ, {DOTENV_DISABLE_ENV: "1"}, clear=False):
            _clear_docqa_env()
            cfg = load_config()

        assert not cfg.has_llm
        assert not cfg.has_ocr

    def test_explicit_read_dotenv_false_does_not_depend_on_process_env(
        self, tmp_path, monkeypatch
    ):
        """`read_dotenv=False` 是命令级隔离：不靠环境变量，也不读项目根 .env。

        这条与上一条的区别很重要：上一条证明的是"测试进程里恰好把开关置上了"，
        离开 pytest 就不成立。离线命令（如 `eval-probes`）必须自己声明离线，
        否则用户在配了端点的机器上跑它，输入就与干净环境不是同一份。
        """
        (tmp_path / ".env").write_text(_FAKE_ENV, encoding="utf-8")
        monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
        _clear_docqa_env_via_monkeypatch(monkeypatch)

        cfg = load_config(read_dotenv=False)

        assert not cfg.has_llm, "read_dotenv=False 时仍然读到了 .env"
        assert not cfg.has_ocr

    def test_default_path_would_have_read_that_dotenv(self, tmp_path, monkeypatch):
        """反证：同一份 `.env` 在默认路径下确实会被读——否则上一条是空测试。"""
        (tmp_path / ".env").write_text(_FAKE_ENV, encoding="utf-8")
        monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)
        _clear_docqa_env_via_monkeypatch(monkeypatch)

        seen: list[tuple] = []
        monkeypatch.setattr(
            config_module, "load_dotenv", lambda *a, **k: seen.append((a, k))
        )

        load_config()

        assert seen, "默认路径没有调用 load_dotenv，这条反证不成立"

    def test_explicit_env_file_still_loads(self, tmp_path):
        """逃生口不能被焊死：显式 `env_file=` 时仍然读那个文件。

        显式指定文件与"自动去项目根捡 .env"是两回事——前者是调用方的意图，
        后者才是需要在测试里禁掉的环境状态。
        """
        env_file = tmp_path / "explicit.env"
        env_file.write_text(_FAKE_ENV, encoding="utf-8")

        with mock.patch.dict(os.environ, {DOTENV_DISABLE_ENV: "1"}, clear=False):
            _clear_docqa_env()
            cfg = load_config(env_file=env_file)

            assert cfg.has_llm
            assert cfg.has_ocr
