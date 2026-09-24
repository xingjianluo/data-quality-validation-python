"""本地数据质量契约校验与监控服务入口。

启动方式：fastapi dev main.py  或  python -m uvicorn main:app
"""

from dqv.service import app

__all__ = ["app"]
