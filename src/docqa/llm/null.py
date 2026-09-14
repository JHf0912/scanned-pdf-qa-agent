"""无模型时的降级路径。

设计立场
--------
"没有配置生成模型"不是异常，而是**一条正常的运行分支**。把它写成
`if llm is None:` 散落在调用处，会出现两个后果：这条路径没人测，
而且它到底降级成什么样说不清楚。

所以这里把它做成一个显式的、可测试的策略：**抽取式回答**。
系统仍然完成检索、仍然给出引用、仍然跑自检与拒答判定——
只是答案不再由模型组织语言，而是把证据原文按相关度拼装出来。

这样做的价值：
- 没有密钥的评审方也能完整验证**检索 / 引用 / 自检 / 拒答 / 评估**这半边；
- 它是"答案必须来自证据"这一约束的**极端情形**——答案字面上就是证据，
  于是数值落地校验必然通过，正好反衬出真正需要防的是证据本身被 OCR 污染。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ProviderConfig
from ..errors import ProviderError
from ..models import Usage
from .base import LLMResponse


@dataclass(frozen=True)
class Evidence:
    """一段用于抽取式拼装的证据。"""

    text: str
    citation: str


class NullProvider:
    """不调用任何模型的 provider。

    `complete()` 会明确报错而不是返回一段假答案——**静默编造一个答案
    是最糟的降级**。真正的降级由 `assemble_extractive()` 完成，
    它不会假装自己在"生成"。
    """

    name = "null"
    supports_vision = False

    def __init__(self, cfg: ProviderConfig | None = None):
        self.cfg = cfg or ProviderConfig()

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
        raise ProviderError(
            "未配置生成模型，无法进行语言生成。\n"
            "这是**预期行为**而非故障：系统会自动改用抽取式回答"
            "（直接引用证据原文），检索、引用、自检与拒答判定均正常执行。\n"
            "若要启用语言组织，请在 .env 中配置 DOCQA_LLM_PROVIDER / "
            "DOCQA_LLM_API_KEY / DOCQA_LLM_MODEL。",
            retryable=False,
        )

    def close(self) -> None:
        return None


def assemble_extractive(question: str, evidence: list[Evidence], *, max_chars: int = 400) -> str:
    """把证据原文拼装成答案。

    不做任何改写或推断——只标注每段来自哪里。这是"答案完全来自证据"的
    最保守实现，也因此**它永远不会产生数值幻觉**：它根本没有产生新数字的能力。
    """
    if not evidence:
        return ""
    parts: list[str] = []
    used = 0
    for item in evidence:
        text = item.text.strip().replace("\n", " ")
        if not text:
            continue
        if used + len(text) > max_chars and parts:
            break
        parts.append(f"{text} {item.citation}")
        used += len(text)
    if not parts:
        return ""
    return "（未配置生成模型，以下为检索到的证据原文）\n" + "\n".join(parts)
