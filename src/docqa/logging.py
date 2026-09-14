"""结构化日志。

**本模块不做任何密钥打码。** 这一条是刻意的，理由值得写下来：

密钥**根本不应该出现在日志调用里**。曾经这里有一层"写进去之后再正则擦掉"
的兜底，它的坏处比好处多：

1. **它是治标。** 擦拭发生在错误已经犯下之后——那次之所以要加它，
   正是因为某处日志调用真的把密钥传进去了。该改的是那个调用，不是加一层网。
2. **它制造虚假的安全感。** "反正有打码"会让人放松，反而更容易漏出去。
3. **它总有漏的格式。** 正则只能覆盖想得到的密钥形态，换个供应商、
   换个前缀就漏了——当初那版就没匹配上。

所以正确做法只有一条：**不把密钥写进日志**。
这条纪律由 `tests/test_no_secrets_in_logs.py` 守住——它用一个哨兵密钥跑遍
会大量打日志的路径（含各种失败路径），断言日志输出里绝不含该哨兵。
**守的是源头：谁把密钥写进日志，测试立刻红。**

至于结构化日志本身（run_id、阶段耗时、用量），与打码无关，全部保留。
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

_RUN_ID = uuid.uuid4().hex[:12]
_configured = False


def run_id() -> str:
    """本次运行的标识。同一次 CLI 调用内所有日志共用。"""
    return _RUN_ID


class _JsonFormatter(logging.Formatter):
    """JSON Lines：便于 grep、便于喂给评估脚本，也便于贴进 issue。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "run": _RUN_ID,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # 附加的结构化字段（logger.info("...", extra={"stage": "ocr"})）
        for key in ("stage", "page", "doc", "usage", "elapsed_ms", "detail"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup(level: str = "INFO", *, json_output: bool = True) -> None:
    """初始化根 logger。重复调用是幂等的。"""
    global _configured
    root = logging.getLogger("docqa")
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _JsonFormatter()
        if json_output
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    if not _configured:
        setup()
    return logging.getLogger(f"docqa.{name}")


@contextmanager
def stage(name: str, logger: logging.Logger | None = None, **fields: Any) -> Iterator[dict]:
    """计量一个阶段的耗时，并在结束时打一条结构化日志。

    用法：
        with stage("ocr", log, page=3) as info:
            ...            # info["elapsed_ms"] 结束时可用
    """
    log = logger or get_logger("stage")
    info: dict[str, Any] = {"elapsed_ms": 0}
    started = time.perf_counter()
    log.info("阶段开始", extra={"stage": name, **fields})
    try:
        yield info
        status = "ok"
    except Exception:
        status = "error"
        raise
    finally:
        info["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
        log.info(
            f"阶段结束（{status}）",
            extra={"stage": name, "elapsed_ms": info["elapsed_ms"], **fields},
        )
