"""词法检索：两条**不需要模型**的检索臂。

为什么不用向量检索做主力
------------------------
向量检索需要一个 embedding 端点。一旦它成为必需项，就会带来两个后果：
评估结果在不同机器上不可复现（换个模型分数就变），以及没有密钥的人
根本跑不了评估。而"可复现"正是本次交付的硬要求之一。

所以主力是两条纯词法的臂，它们在中文上的效果被严重低估：

- **bm25_word**：jieba 分词后的词元级 BM25。对"抗拉强度""半圆键"这类术语准。
- **bm25_char**：**字符二元组** BM25。中文没有词边界，二元组天然覆盖了
  未登录词与生僻字；对"张三"这种人名、以及 OCR 把词切错的情况特别有效。

两条臂用 RRF 融合。神经稠密检索作为**可选第三臂**挂在同一个接口后面，
有端点时可以开启，没有也不影响主流程。

三条臂之所以能简单相加，是因为 RRF 只看排名、不看分数——
不同量纲的分数本来就没法直接加权，排名可以。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from ..logging import get_logger
from ..models import Chunk, RetrievalHit

_log = get_logger("index.lexical")

# 中文停用词的收益有限（BM25 的 IDF 已经在压制高频词），
# 这里只去掉纯粹的结构性符号，避免误伤"b""L"这类有意义的单字符术语。
_STRIP_RE = re.compile(r"[\s　]+")
_PUNCT_RE = re.compile(r"[，。、；：？！“”‘’（）《》〈〉【】…—\-·|/\\]+")


@dataclass
class _Arm:
    """一条检索臂的语料统计。"""

    docs: list[list[str]]
    df: Counter
    avg_len: float
    n_docs: int


def tokenize_words(text: str) -> list[str]:
    """jieba 分词。保留数字与拉丁字母词，它们常是型号与尺寸。"""
    import jieba

    cleaned = _PUNCT_RE.sub(" ", text)
    tokens = [t.strip() for t in jieba.lcut(cleaned)]
    return [t for t in tokens if t and not _STRIP_RE.fullmatch(t) and len(t) <= 30]


def tokenize_char_bigrams(text: str) -> list[str]:
    """字符二元组。

    只保留对检索有意义的字符（中日韩文字、字母、数字），跳过标点与空白——
    否则"的。""，、"这类组合会占据大量词表位置，稀释真正的信号。
    """
    chars = [c for c in text if c.isalnum() or "一" <= c <= "鿿"]
    if len(chars) < 2:
        return chars
    return ["".join(chars[i : i + 2]) for i in range(len(chars) - 1)]


class LexicalIndex:
    """一个 BM25 索引。不依赖任何外部模型。"""

    # BM25 自由参数。k1 控制词频饱和，b 控制长度归一化。
    # 1.5 / 0.75 是文献常用取值；中文短文本上对这两个值不敏感。
    K1 = 1.5
    B = 0.75

    def __init__(self, arm: str, chunks: list[Chunk]):
        if arm not in ("bm25_word", "bm25_char"):
            raise ValueError(f"未知的检索臂：{arm}")
        self.arm = arm
        self.chunk_ids = [c.chunk_id for c in chunks]
        tokenizer = tokenize_words if arm == "bm25_word" else tokenize_char_bigrams
        self._docs = [tokenizer(c.text) for c in chunks]
        self._df: Counter[str] = Counter()
        for doc in self._docs:
            self._df.update(set(doc))
        self._n = len(self._docs)
        self._avg_len = (sum(len(d) for d in self._docs) / self._n) if self._n else 0.0

    def _idf(self, term: str) -> float:
        # 带 +0.5 平滑的 BM25 IDF，并夹到非负：
        # 负 IDF 会让"包含该词"反而降低得分，在多文档语料里是已知的反常行为。
        df = self._df.get(term, 0)
        return max(0.0, math.log((self._n - df + 0.5) / (df + 0.5) + 1.0))

    def search(self, query: str, k: int = 10) -> list[RetrievalHit]:
        tokenizer = tokenize_words if self.arm == "bm25_word" else tokenize_char_bigrams
        q_terms = tokenizer(query)
        if not q_terms or not self._docs:
            return []

        scores: list[tuple[float, int]] = []
        q_counts = Counter(q_terms)
        for idx, doc in enumerate(self._docs):
            if not doc:
                continue
            dl = len(doc)
            tf = Counter(doc)
            score = 0.0
            for term, qn in q_counts.items():
                f = tf.get(term, 0)
                if f == 0:
                    continue
                denom = f + self.K1 * (1 - self.B + self.B * dl / (self._avg_len or 1))
                score += self._idf(term) * (f * (self.K1 + 1) / denom) * qn
            if score > 0:
                scores.append((score, idx))

        scores.sort(key=lambda x: (-x[0], x[1]))
        return [
            RetrievalHit(chunk_id=self.chunk_ids[idx], score=score, arm=self.arm, rank=r + 1)
            for r, (score, idx) in enumerate(scores[:k])
        ]

    def to_dict(self) -> dict:
        return {
            "arm": self.arm,
            "n_docs": self._n,
            "vocab": len(self._df),
            "avg_len": round(self._avg_len, 1),
        }
