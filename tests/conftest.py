"""公共测试辅助：统一打印输入、期望与判定依据。"""

from __future__ import annotations

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("dqv-test")


def log_case(title: str, given: object, expected: object, verdict: object) -> None:
    """单测日志必须包含：输入(given)、期望(expected)、判定依据(verdict)。"""
    log.info("CASE %s | 输入=%s | 期望=%s | 判定依据=%s", title, given, expected, verdict)
