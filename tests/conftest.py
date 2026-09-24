"""共享测试夹具与判定日志。

每个用例通过 ``case`` 记录并打印：输入(input)、期望(expected)、
判定依据(basis)，满足可解释要求。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import pytest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("dq-tests")


@dataclass
class CaseLog:
    name: str
    parts: list[str] = field(default_factory=list)

    def input(self, label: str, value: Any) -> None:
        self.parts.append(f"INPUT[{label}]={value!r}")

    def expect(self, value: Any) -> None:
        self.parts.append(f"EXPECTED={value!r}")

    def basis(self, value: Any) -> None:
        self.parts.append(f"BASIS={value!r}")

    def actual(self, value: Any) -> None:
        self.parts.append(f"ACTUAL={value!r}")

    def flush(self) -> None:
        logger.info("CASE %s | %s", self.name, " || ".join(self.parts))


@pytest.fixture
def case(request):
    log = CaseLog(request.node.name)
    yield log
    log.flush()
