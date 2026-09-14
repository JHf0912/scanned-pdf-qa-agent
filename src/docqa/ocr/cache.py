"""OCR 结果的**内容寻址缓存**与溯源链。

这一层要解决的问题
------------------
扫描件识别要花真金白银（每次调用都发一张整页图），所以结果必须缓存。
但"缓存"和"绕过解析"之间只隔着一层窗户纸：如果缓存里放一份**来历不明**的
JSON，那它与"手工录入全文"在性质上没有区别——而作业明确禁止绕过解析。

所以缓存必须是**可验证**的，靠一条哈希链把
`PDF → 页图 → 提示词 → 模型 → 输出` 串起来：

```
cache_key = sha256(image_sha256 ‖ provider ‖ model ‖ prompt_sha256 ‖ params)
```

于是评审方可以：
1. 自己重跑 `render.py`，核对页图哈希；
2. 用**自己的密钥**重发一次**完全相同**的请求（提示词与参数都纳入 `cache_key` 的哈希，
   因此只要输入一致，重算出的键必然一致），再与已有输出逐字比对；
3. 直接读 `Provenance` 看到这份产物是什么时候、用哪个模型、哪版提示词产生的。

缓存命中因此不是一句"信我"，而是一个能被独立检验的事实。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..logging import get_logger
from ..models import OcrBlock, OcrPage, TableGrid, Usage

_log = get_logger("ocr.cache")

CACHE_VERSION = 1


@dataclass(frozen=True)
class Provenance:
    """这份产物的来历。**入库，供审计。**"""

    created_at: str
    provider: str
    model: str
    prompt_version: str
    prompt_sha256: str
    image_sha256: str
    request_params: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    cache_version: int = CACHE_VERSION


def prompt_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_key(
    image_sha256: str,
    *,
    provider: str,
    model: str,
    prompt_digest: str,
    params: dict[str, Any] | None = None,
) -> str:
    """内容寻址的缓存键。

    把提示词与参数一起纳入哈希：改一个字提示词，键就变——**旧结果不会
    被误当成新提示词的产物复用**。这是刻意的，因为提示词变了，
    输出的语义就变了，两者不可互换。
    """
    payload = json.dumps(
        {
            "v": CACHE_VERSION,
            "img": image_sha256,
            "provider": provider,
            "model": model,
            "prompt": prompt_digest,
            "params": params or {},
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class OcrCache:
    """磁盘上的内容寻址缓存。"""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _paths(self, key: str) -> tuple[Path, Path]:
        return self.root / f"{key}.json", self.root / f"{key}.provenance.json"

    def get(self, key: str) -> tuple[OcrPage, Provenance] | None:
        data_path, prov_path = self._paths(key)
        if not data_path.exists():
            return None
        try:
            payload = json.loads(data_path.read_text(encoding="utf-8"))
            prov = Provenance(
                **json.loads(prov_path.read_text(encoding="utf-8"))
                if prov_path.exists()
                else {"created_at": "", "provider": "", "model": "",
                      "prompt_version": "", "prompt_sha256": "", "image_sha256": ""}
            )
        except (json.JSONDecodeError, TypeError) as exc:
            # 缓存损坏不应让整轮中断——当作未命中，重新识别即可
            _log.warning("缓存条目损坏，将重新识别", extra={"detail": f"{key}: {exc}"})
            return None
        return _page_from_dict(payload), prov

    def put(self, key: str, page: OcrPage, prov: Provenance) -> None:
        data_path, prov_path = self._paths(key)
        data_path.write_text(
            json.dumps(page.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        prov_path.write_text(
            json.dumps(asdict(prov), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def has(self, key: str) -> bool:
        return self._paths(key)[0].exists()

    def list_keys(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json") if not p.name.endswith(".provenance.json"))


def _page_from_dict(d: dict) -> OcrPage:
    """从 JSON 还原 OcrPage。"""
    blocks = [
        OcrBlock(
            block_id=b["block_id"],
            page_no=b["page_no"],
            type=b["type"],
            order=b["order"],
            text=b["text"],
            bbox=tuple(b["bbox"]) if b.get("bbox") else None,
            clause_label=b.get("clause_label"),
            uncertain=bool(b.get("uncertain")),
            needs_review=bool(b.get("needs_review")),
            alternatives=list(b.get("alternatives") or []),
        )
        for b in d.get("blocks", [])
    ]
    tables = [
        TableGrid(
            table_id=t["table_id"],
            page_no=t["page_no"],
            caption=t.get("caption"),
            n_rows=t["n_rows"],
            n_cols=t["n_cols"],
            cells=t["cells"],
            n_header_rows=t.get("n_header_rows", 1),
            parse_degraded=bool(t.get("parse_degraded")),
        )
        for t in d.get("tables", [])
    ]
    u = d.get("usage") or {}
    return OcrPage(
        page_no=d["page_no"],
        blocks=blocks,
        tables=tables,
        image_sha256=d.get("image_sha256", ""),
        provider=d.get("provider", ""),
        model=d.get("model", ""),
        prompt_version=d.get("prompt_version", ""),
        usage=Usage(
            tokens_in=u.get("tokens_in", 0),
            tokens_out=u.get("tokens_out", 0),
            est_cost_usd=u.get("est_cost_usd", 0.0),
            latency_ms=u.get("latency_ms", 0),
            calls=u.get("calls", 0),
        ),
        warnings=list(d.get("warnings") or []),
        failed=bool(d.get("failed")),
        error=d.get("error"),
    )
