"""评估层：三层指标 + 对抗变异 + 噪声鲁棒性 + 回归基线。"""

from .adversarial import generate_mutations, run_adversarial
from .metrics import (
    cer,
    character_accuracy,
    ndcg_at_k,
    numeric_field_accuracy,
    recall_at_k,
    reciprocal_rank,
    table_cell_accuracy,
    wilson_interval,
)
from .noise import DEFAULT_LEVELS, NoiseConfig, inject_noise, noise_sweep_levels
from .runner import (
    EvalReport,
    GoldenItem,
    compare_baseline,
    context_utilization,
    load_golden,
    run_eval,
    score_item,
)

__all__ = [
    "cer", "character_accuracy", "wilson_interval", "recall_at_k",
    "reciprocal_rank", "ndcg_at_k", "table_cell_accuracy", "numeric_field_accuracy",
    "GoldenItem", "load_golden", "run_eval", "score_item", "compare_baseline",
    "context_utilization", "EvalReport",
    "generate_mutations", "run_adversarial",
    "NoiseConfig", "inject_noise", "noise_sweep_levels", "DEFAULT_LEVELS",
]
