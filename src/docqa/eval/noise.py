"""OCR 噪声注入：量化"证据被污染时，系统会怎样"。

这个实验要回答的问题
--------------------
前面的自检有个**已知盲区**：数值落地校验的是"答案与证据一致"，
不是"证据为真"。所以当 OCR 把 `15` 读成 `13`，答案照抄 `13`，
所有 gate 都会通过，系统自信地给出错误答案。

这个盲区无法靠"更聪明的校验"消除——**污染之下系统无从知道真相**。
能做的是把它**量化出来**，并据此给出工程上的应对
（高风险字段转人工、跨引擎交叉校验、低置信度标记）。

所以本模块产出的不是"我们做得很好"，而是
**"噪声到达什么强度时，系统开始不可靠"** 这条曲线。
在全套交付里，这张图比任何指标都更能说明设计者理解了问题的边界。

噪声类型刻意对应真实 OCR 的失败模式
-----------------------------------
- **形近字**：中文 OCR 最典型的错误（键/健、未/末、己/已）
- **数字混淆**：0/O、1/l、5/S、8/B —— 单位与公差最怕这个
- **丢字/多字**：低质量扫描或版面倾斜时的常见表现
- **空白错位**：分词边界被破坏，影响 BM25 词元臂
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from ..logging import get_logger

_log = get_logger("eval.noise")

# 形近字表。刻意选取在技术文档里**都合法**的字对——
# 替换后文本依然通顺，这正是这类错误难被发现的原因。
CONFUSABLE_PAIRS: tuple[tuple[str, str], ...] = (
    ("键", "健"), ("未", "末"), ("己", "已"), ("日", "曰"), ("土", "士"),
    ("千", "干"), ("拔", "拨"), ("候", "侯"), ("析", "折"), ("辨", "辩"),
    ("镀", "渡"), ("锈", "绣"), ("纹", "文"), ("裂", "列"), ("磨", "摩"),
    ("径", "经"), ("轴", "抽"), ("销", "消"), ("孔", "扎"), ("厚", "原"),
)

DIGIT_CONFUSIONS: tuple[tuple[str, str], ...] = (
    ("0", "O"), ("1", "l"), ("5", "S"), ("8", "B"), ("6", "G"), ("2", "Z"),
)

_WS_RE = re.compile(r"\s+")


@dataclass
class NoiseConfig:
    """噪声强度。各级独立开关，便于做单因素消融。"""

    level: float = 0.0  # 0.0 ~ 1.0，总体错误率
    confusable_ratio: float = 0.6  # 其中形近字占比
    digit_ratio: float = 0.25
    drop_ratio: float = 0.10
    insert_ratio: float = 0.05
    seed: int = 0

    def describe(self) -> str:
        return f"level={self.level:.2f} seed={self.seed}"


def inject_noise(text: str, cfg: NoiseConfig) -> str:
    """按配置注入噪声。

    注意：注入是**逐字符按概率**进行的，所以实际错误率会围绕 level 波动。
    报告里报的是配置值，同时给出实际 CER，二者都列出来——
    只报配置值会掩盖"实际注入得比预期少"这种情况。
    """
    if cfg.level <= 0 or not text:
        return text

    rng = random.Random(cfg.seed)
    chars = list(text)
    out: list[str] = []
    confusable_map = dict(CONFUSABLE_PAIRS)
    digit_map = dict(DIGIT_CONFUSIONS)

    for ch in chars:
        roll = rng.random()
        if roll >= cfg.level:
            out.append(ch)
            continue

        kind = rng.random()
        if kind < cfg.confusable_ratio and ch in confusable_map:
            out.append(confusable_map[ch])
        elif kind < cfg.confusable_ratio + cfg.digit_ratio and ch in digit_map:
            out.append(digit_map[ch])
        elif kind < cfg.confusable_ratio + cfg.digit_ratio + cfg.drop_ratio:
            continue  # 丢字
        elif ch.strip():
            # 多字：重复一个相邻字符
            out.append(ch)
            out.append(rng.choice([c for c in "的一是不了在人有我他这"]))
        else:
            out.append(ch)

    return "".join(out)


# 预设的噪声档位。用**固定 seed** 保证可复现——
# 噪声实验如果不可复现，那条退化曲线就没有意义。
DEFAULT_LEVELS: tuple[float, ...] = (0.0, 0.02, 0.05, 0.10, 0.20)


@dataclass
class NoisePoint:
    level: float
    seed: int
    actual_cer: float
    metrics: dict = field(default_factory=dict)


def noise_sweep_levels(levels: tuple[float, ...] = DEFAULT_LEVELS, seeds: int = 3) -> list[NoiseConfig]:
    """生成噪声档位 × 随机种子的组合。

    每个档位跑多个种子，是为了把"这一档的结果"与"这一次抽样的运气"分开——
    只跑一个种子的话，某档分数偏高可能只是碰巧改动的字符不重要。
    """
    out: list[NoiseConfig] = []
    for level in levels:
        for s in range(seeds):
            out.append(NoiseConfig(level=level, seed=s))
    return out


def perturb_pages(pages, cfg: NoiseConfig):
    """按噪声配置污染一批 `OcrPage` 的正文块，返回**新的**页面列表。

    为什么污染必须发生在切分之前
    ----------------------------
    这个实验要回答的是"证据被读错时，系统会怎样"。所以污染要落在**索引之前**：
    检索、生成、自检都面对这份被污染的文本。否则测的就不是"证据被污染"，
    而只是"答案被改写"——后者与 OCR 无关。

    表格块不污染：表格里全是数字与短单元格，随机注入形近字只会得到
    无法定位的乱码，测出来的退化没有解释力。这一点在报告里如实写明。

    返回新的 `OcrPage` 列表，**不修改原对象**——同一份产物要被多个档位复用。
    """
    from dataclasses import replace as _replace

    out = []
    for page in pages:
        new_blocks = []
        for b in getattr(page, "blocks", []) or []:
            if getattr(b, "is_table", False) or not getattr(b, "text", ""):
                new_blocks.append(b)
                continue
            polluted = inject_noise(b.text, cfg)
            new_blocks.append(_replace(b, text=polluted) if polluted != b.text else b)
        out.append(_replace(page, blocks=new_blocks) if new_blocks else page)
    return out


def actual_cer(reference: str, cfg: NoiseConfig) -> float:
    """实际注入后的字符错误率（Levenshtein / 参考长度）。

    报告里必须同时给"配置的 level"与"实际 CER"：只报配置值会掩盖
    "实际注入得比预期少"这种情况（形近字表覆盖不到的字会走别的分支，
    实际改动量与 level 并不相等）。
    """
    if not reference or cfg.level <= 0:
        return 0.0
    polluted = inject_noise(reference, cfg)
    if not polluted:
        return 1.0
    prev = list(range(len(polluted) + 1))
    for i, ch in enumerate(reference, start=1):
        cur = [i] + [0] * len(polluted)
        for j, ph in enumerate(polluted, start=1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (0 if ch == ph else 1))
        prev = cur
    return prev[-1] / len(reference)
