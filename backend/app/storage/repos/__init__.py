"""仓储查询层（storage/repos/，bt-api）：api 层收敛入口。

模块级查询函数 + 显式 session 参数（api 用 Depends(get_db) 注入的 db；
直连点由本层自建会话——函数内 `from ..db import SessionLocal`
（storage 层直连，运行时取当前值，测试 patch app.storage.db.SessionLocal 生效），
与 storage/appparams.py 既有模式一致）。
"""

from . import indicators as indicators
from . import models as models
from . import params as params
from . import signals as signals
from . import stocks as stocks

__all__ = ["indicators", "models", "params", "signals", "stocks"]
