"""OCR 引擎接口。

为什么要有这层抽象
------------------
当前实现走 VLM，但"用哪个引擎"不该硬编码进流水线：

- 换供应商（专用 OCR 模型 / 通用多模态 / 本地 ONNX）只应换一个注册项；
- 跨引擎一致性校验需要**同时挂两个引擎**，接口不统一就做不了；
- 未来接入本地引擎时不应改动 ingest 的任何一行。

所以 ingest 只认 `OcrEngine` 协议，不认具体实现。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..models import OcrPage, PageImage, PageProfile


class OcrEngine(Protocol):
    """把一页图像识别成结构化文本。"""

    name: str
    model: str
    supports_vision: bool

    def ocr_page(self, image: PageImage, *, profile: PageProfile | None = None) -> OcrPage:
        """识别一页。

        **失败语义**：单页失败应当抛出 `PageOcrError`，由调用方隔离处理
        （记录该页失败、继续处理后续页）。不要返回一个空的 OcrPage 来表示失败——
        空页面与"这一页本来就是空白"无法区分，会让下游把解析失败
        误当成"文档里没有这项内容"。
        """
        ...

    def close(self) -> None: ...


class OcrEngineInfo:
    """引擎的元信息，写进 manifest 与溯源链。"""

    def __init__(self, name: str, model: str, prompt_version: str, vision: bool):
        self.name = name
        self.model = model
        self.prompt_version = prompt_version
        self.vision = vision

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "vision": self.vision,
        }


def load_prompt(path: Path) -> tuple[str, str]:
    """读取提示词文件，返回 (正文, 版本号)。

    版本号从 YAML front matter 的 `version:` 取；取不到就用文件名。
    版本号会进溯源链——评审方要能看出这份产物是哪版提示词产生的。
    """
    text = path.read_text(encoding="utf-8")
    version = path.stem
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end > 0:
            for line in text[3:end].splitlines():
                if line.strip().startswith("version:"):
                    version = line.split(":", 1)[1].strip()
                    break
            text = text[end + 4 :].lstrip("\n")
    return text, version
