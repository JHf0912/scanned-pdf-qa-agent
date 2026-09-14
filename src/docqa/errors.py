"""异常体系与进程退出码。

设计意图
--------
命令行工具的错误处理要能被脚本和 CI 消费，所以**不同性质的失败要有不同的退出码**，
而不是一律 1。同时，ODR（单页失败）不应该拖垮整轮 ingest——逐页错误单独建模，
由调用方决定是记录后继续还是中止。

退出码约定
----------
    0  成功
    1  未预期的内部错误
    2  配置错误（缺密钥、配置项非法）
    3  输入错误（PDF 读不了、页数异常）
    4  模型/服务方错误（鉴权失败、限流、超时）
    5  评估回归（指标相对基线下滑）
    6  校验失败（自检不过且不可修复）
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_CONFIG = 2
EXIT_INPUT = 3
EXIT_PROVIDER = 4
EXIT_REGRESSION = 5
EXIT_VERIFICATION = 6


class DocqaError(Exception):
    """本项目所有可预期错误的基类。"""

    exit_code: int = EXIT_INTERNAL


class ConfigError(DocqaError):
    """配置缺失或非法。消息必须说清：缺什么、去哪配、不配会影响哪一步。"""

    exit_code = EXIT_CONFIG


class InputError(DocqaError):
    """输入文件层面的问题。"""

    exit_code = EXIT_INPUT


class PdfUnreadableError(InputError):
    """PDF 打不开、加密、或结构损坏。"""


class ProviderError(DocqaError):
    """模型服务方的错误。

    `retryable` 决定调用方是否退避重试：限流与 5xx 可重试，
    鉴权失败与请求非法不可重试（重试只会浪费配额）。
    """

    exit_code = EXIT_PROVIDER

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class AuthError(ProviderError):
    """密钥缺失或无效。"""

    def __init__(self, message: str):
        super().__init__(message, retryable=False, status=401)


class PageOcrError(DocqaError):
    """单页 OCR 失败。

    这条错误是**被隔离的**：ingest 会把它记在页上并继续处理后续页，
    最终凡是证据触及该页的问题一律以 EXTRACTION_FAILED 拒答，
    而不是拿不完整的证据去回答。
    """

    exit_code = EXIT_INTERNAL

    def __init__(self, page_no: int, message: str):
        super().__init__(f"第 {page_no} 页识别失败：{message}")
        self.page_no = page_no


class TableParseError(DocqaError):
    """表格结构解析失败。

    降级策略：保留原始 HTML 作为单个单元格，并标记 `parse_degraded=True`，
    绝不静默丢弃或猜一个结构。
    """


class VerificationFailed(DocqaError):
    """自检未通过且修复轮次已用尽。"""

    exit_code = EXIT_VERIFICATION


class RegressionDetected(DocqaError):
    """评估指标相对基线下滑。"""

    exit_code = EXIT_REGRESSION
