"""free（自由回答）模式契约测试。

free 是 strict / hybrid 之外的第三种回答模式：**不检索、不引用、不拒答**，
直接由生成模型自由作答。它没有任何文档依据，因此：

- 有模型时：返回 answer，`citations` 为空、`answer_mode == "free"`、
  `refused is False`，并且**只调用一次模型**（不进入检索/自检/修复循环）；
- 无模型时：Agent 抛 `ConfigError`，API 返回 400（与 hybrid 同样处理）；
- `validate_answer_mode("free")` 通过、非法值仍被拒绝。

strict / hybrid 的行为完全不变（本文件不重复它们的用例，见 test_hybrid_mode.py）。
全部离线、用假 provider，不发任何真实请求。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import docqa.api.app as api_module
from docqa.config import ProviderConfig, load_config, load_domain
from docqa.errors import ConfigError
from docqa.index import HybridIndex
from docqa.llm import LLMResponse, NullProvider
from docqa.models import Chunk, Usage
from docqa.qa import QaAgent
from docqa.qa.answer import (
    ANSWER_MODES,
    FREE_REQUIRES_LLM_MESSAGE,
    FREE_SYSTEM_PROMPT,
    validate_answer_mode,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_DOCX = PROJECT_ROOT / "artifacts" / "docx" / "demo.docx"


# ---------------------------------------------------------------- 构造工具


class FakeProvider:
    """返回预置文本的假 provider。记录每次调用的 system / user。"""

    name = "fake"
    supports_vision = False

    def __init__(self, text: str = "自由回答示例。"):
        self.text = text
        self.calls: list[tuple[str, str]] = []

    def complete(
        self,
        system: str,
        user: str,
        *,
        images=None,
        max_tokens: int = 1200,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> LLMResponse:
        self.calls.append((system, user))
        return LLMResponse(
            text=self.text,
            provider="fake",
            model="fake-model",
            usage=Usage(tokens_in=10, tokens_out=5, calls=1),
        )


def _chunk() -> Chunk:
    return Chunk(
        chunk_id="d:p1:c1",
        doc_id="d",
        page_no=1,
        ordinal=1,
        kind="clause",
        text="3.1 抗拉强度应大于等于 590 MPa。",
        clause_path=("3.1",),
        clause_label="3.1",
    )


def _agent(provider, chunks: list[Chunk] | None = None) -> QaAgent:
    cfg = load_config()
    profile = load_domain("standard", cfg)
    return QaAgent(
        HybridIndex(chunks if chunks is not None else [_chunk()], profile),
        provider,
        cfg,
        profile,
        doc_id="d",
    )


# ---------------------------------------------------------------- 1. 模式校验


class TestValidateAnswerMode:
    def test_free_is_accepted(self):
        validate_answer_mode("free")  # 不抛异常即通过

    @pytest.mark.parametrize("mode", ["strict", "hybrid", "free"])
    def test_every_answer_mode_is_accepted(self, mode):
        validate_answer_mode(mode)
        assert mode in ANSWER_MODES

    def test_bogus_is_rejected(self):
        with pytest.raises(ValueError, match="free"):
            validate_answer_mode("bogus")


# ---------------------------------------------------------------- 2. Agent 行为


class TestFreeAgent:
    def test_free_with_model_returns_answer_without_citations(self):
        provider = FakeProvider("这是一个自由回答。")
        bundle = _agent(provider).answer("解释一下什么是淬火", mode="free")

        assert bundle.refused is False
        assert bundle.answer_mode == "free"
        assert bundle.answer == "这是一个自由回答。"
        assert bundle.citations == []
        assert bundle.hits == []
        assert bundle.attempts == 1
        assert bundle.verification.checks == []
        # 不检索：只调用一次模型，且使用 free 的系统提示
        assert len(provider.calls) == 1
        assert provider.calls[0][0] == FREE_SYSTEM_PROMPT
        assert provider.calls[0][1] == "解释一下什么是淬火"
        # 轨迹只记录一步 free（没有 retrieve / draft / verify / repair）
        assert [t.step for t in bundle.trace] == ["free"]

    def test_free_bundle_serializes_free_mode(self):
        provider = FakeProvider("自由回答。")
        d = _agent(provider).answer("任意问题", mode="free").to_dict()
        assert d["answer_mode"] == "free"
        assert d["refused"] is False
        assert d["citations"] == []

    def test_free_without_model_raises_config_error(self):
        with pytest.raises(ConfigError, match="free"):
            _agent(NullProvider()).answer("任意问题", mode="free")

    def test_free_does_not_refuse_even_without_relevant_evidence(self):
        """空索引下 strict 会拒答，free 仍应正常作答（不检索、不拒答）。"""
        cfg = load_config()
        profile = load_domain("standard", cfg)
        provider = FakeProvider("自由作答。")
        agent = QaAgent(HybridIndex([], profile), provider, cfg, profile, doc_id="d")
        bundle = agent.answer("天上为什么是蓝的", mode="free")
        assert bundle.refused is False
        assert bundle.answer == "自由作答。"
        assert bundle.citations == []
        assert provider.calls


# ---------------------------------------------------------------- 3. API 行为


def _isolated_cfg(tmp_path: Path):
    """把 `cfg.cache_dir` 指到临时目录：不读也不写仓库里的 `data/derived`。"""
    return dataclasses.replace(load_config(), cache_dir=tmp_path / "derived" / "cache")


def _llm_configured(base):
    """把配置改成"已配置生成模型"，模型本身由测试替身提供。"""
    return dataclasses.replace(
        base,
        llm=ProviderConfig(provider="openai", api_key="x", model="fake-model"),
    )


class TestFreeApi:
    @staticmethod
    def _client(tmp_path: Path, monkeypatch, *, with_llm: bool) -> TestClient:
        cfg = _isolated_cfg(tmp_path)
        if with_llm:
            cfg = _llm_configured(cfg)
            monkeypatch.setattr(
                api_module, "build_llm_provider", lambda _cfg: FakeProvider("自由回答。")
            )
        client = TestClient(api_module.create_app(cfg, domain="standard"))
        r = client.post(
            "/api/upload",
            content=DEMO_DOCX.read_bytes(),
            headers={"X-Filename": "demo.docx"},
        )
        assert r.status_code == 200, r.text
        return client

    def test_free_without_llm_is_400_not_silent_fallback(self, tmp_path, monkeypatch):
        client = self._client(tmp_path, monkeypatch, with_llm=False)
        r = client.post("/api/ask", json={"question": "任意问题", "mode": "free"})
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "free" in detail and "生成模型" in detail
        assert FREE_REQUIRES_LLM_MESSAGE == detail
        # 不能静默降级：错误响应里根本不该出现一份答案。
        assert "answer_mode" not in r.json()

    def test_free_with_llm_returns_free_mode_without_citations(
        self, tmp_path, monkeypatch
    ):
        client = self._client(tmp_path, monkeypatch, with_llm=True)
        r = client.post("/api/ask", json={"question": "任意问题", "mode": "free"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["answer_mode"] == "free"
        assert d["refused"] is False
        assert d["citations"] == []
        assert d["answer"] == "自由回答。"
