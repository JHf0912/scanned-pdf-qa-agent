"""问答层：检索 → 有据生成 → 程序化自检 → 有界修复 → 拒答。"""

from .agent import QaAgent
from .answer import build_evidence_block, draft_answer, parse_citations
from .policy import Decision, decide, detect_ambiguity, is_in_document_negative
from .verify import Claims, extract_claims, normalize, verify

__all__ = [
    "QaAgent",
    "verify",
    "extract_claims",
    "normalize",
    "Claims",
    "decide",
    "detect_ambiguity",
    "is_in_document_negative",
    "Decision",
    "draft_answer",
    "parse_citations",
    "build_evidence_block",
]
