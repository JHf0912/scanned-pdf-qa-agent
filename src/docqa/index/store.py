"""混合检索：RRF 融合 + 表格路由。

为什么用 RRF（倒数排名融合）而不是加权求和
------------------------------------------
两条臂的分数量纲完全不同：BM25 的分数是无界的正数，余弦相似度在 [-1,1]。
要给它们加权就得先归一化，而归一化方式本身又是个需要调的参数——
调参就有过拟合金标集的风险，而且换一份文档参数就得重来。

RRF 只看**排名**：`score = Σ weight / (k + rank)`。它不需要任何跨臂的
分数可比性，因此在语料、查询分布变化时更稳。代价是丢掉了分数的绝对信息，
但对"取前 k 条给模型看"这个用途来说，排名已经够了。

表格路由
--------
"表 1 里半圆键的键宽 b 的 AQL 是多少"这类问题，答案只可能在表格里。
但因为表格单元格大多是短数字，BM25 对它们的打分天然偏低，
容易被长篇正文挤下去。所以当问题里出现表格线索词（场景配置里定义）时，
给表格 chunk 一个加权——这是**领域知识**，不是通用启发式，
所以它落在 domain profile 里而不是硬编码在这里。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import DomainProfile
from ..logging import get_logger
from ..models import Chunk, RetrievalHit
from .lexical import LexicalIndex

_log = get_logger("index.store")

# 数值/型号特征：出现这些时也倾向表格。"多少""几"等疑问词在场景配置里。
_NUMERIC_RE = re.compile(r"\d|[一二三四五六七八九十百千]+\s*(?:mm|MPa|级|号|个|条)")


@dataclass
class RetrievalResult:
    hits: list[RetrievalHit]
    # 每条臂各自的原始结果，便于排查"这条为什么没被召回"
    per_arm: dict[str, list[RetrievalHit]] = field(default_factory=dict)
    table_routed: bool = False
    # 融合后最高分与次高分。用于歧义检测（两个候选分差很小时提示消歧）。
    top_score: float = 0.0
    second_score: float = 0.0


class HybridIndex:
    """两条（可选三条）检索臂 + RRF 融合。"""

    def __init__(self, chunks: list[Chunk], profile: DomainProfile):
        self.chunks = list(chunks)
        self.profile = profile
        self._by_id = {c.chunk_id: c for c in self.chunks}
        weights = profile.retrieve.arm_weights or {"bm25_word": 1.0}

        self.arms: list[LexicalIndex] = []
        self.weights: dict[str, float] = {}
        for arm_name in ("bm25_word", "bm25_char"):
            w = float(weights.get(arm_name, 0.0))
            if w <= 0:
                continue
            self.arms.append(LexicalIndex(arm_name, self.chunks))
            self.weights[arm_name] = w

        # 可选的稠密臂：未配置 embedding 端点时**不启用**，
        # 并在评估报告里如实标注该项缺失，而不是静默当作 0 分。
        self.dense = None
        self.dense_available = False

    # ---------------------------------------------------------------- 查询

    def search(self, query: str, k: int | None = None) -> RetrievalResult:
        k = k or self.profile.retrieve.top_k

        per_arm: dict[str, list[RetrievalHit]] = {}
        for arm in self.arms:
            per_arm[arm.arm] = arm.search(query, k=max(k * 4, 20))

        fused: dict[str, float] = {}
        rrf_k = self.profile.retrieve.rrf_k
        for arm_name, hits in per_arm.items():
            w = self.weights.get(arm_name, 1.0)
            for hit in hits:
                fused[hit.chunk_id] = fused.get(hit.chunk_id, 0.0) + w / (rrf_k + hit.rank)

        # 表格路由
        routed = self._should_boost_tables(query)
        if routed:
            boost = self.profile.retrieve.table_boost
            for chunk_id in list(fused):
                chunk = self._by_id.get(chunk_id)
                if chunk is not None and chunk.kind == "table":
                    fused[chunk_id] *= boost

        # --- 归一化 ---
        # 原始 RRF 分数落在 1/(k+rank) 附近（k=60 时约 0.016 量级），
        # 这个量级下的阈值既不可读、也不能跨配置比较：一旦调整 rrf_k 或增加检索臂，
        # 所有阈值都得重调。所以除以"理论最大值"（每条臂都排第一时的分数），
        # 把分数映射到 (0, 1]，让 min_top_score 这类阈值有稳定的物理含义。
        max_possible = (sum(self.weights.values()) / (rrf_k + 1)) if self.weights else 1.0
        if max_possible > 0:
            fused = {cid: s / max_possible for cid, s in fused.items()}

        ordered = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
        hits = [
            RetrievalHit(chunk_id=cid, score=score, arm="rrf", rank=i + 1)
            for i, (cid, score) in enumerate(ordered)
        ]

        top = hits[0].score if hits else 0.0
        second = hits[1].score if len(hits) > 1 else 0.0
        return RetrievalResult(
            hits=hits,
            per_arm=per_arm,
            table_routed=routed,
            top_score=top,
            second_score=second,
        )

    def _should_boost_tables(self, query: str) -> bool:
        cues = self.profile.table_cues or []
        if any(cue and cue in query for cue in cues):
            return True
        return bool(_NUMERIC_RE.search(query))

    # ---------------------------------------------------------------- 工具

    def chunk(self, chunk_id: str) -> Chunk | None:
        return self._by_id.get(chunk_id)

    def get(self, chunk_id: str) -> Chunk | None:
        return self._by_id.get(chunk_id)

    def evidence_text(self, chunk_id: str) -> str:
        c = self._by_id.get(chunk_id)
        return c.text if c else ""

    def to_dict(self) -> dict:
        return {
            "chunks": len(self.chunks),
            "arms": [a.to_dict() for a in self.arms],
            "weights": self.weights,
            "dense_available": self.dense_available,
            "table_chunks": sum(1 for c in self.chunks if c.kind == "table"),
        }
