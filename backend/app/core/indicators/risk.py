"""风险指标框架（core 层 re-export）。

（bt-rm 收官：risk_metrics 为纯 numpy/pandas 计算、无 I/O，唯一实现已迁
app/lib/indicators/risk.py；本模块仅为旧路径 re-export，保证
`from app.core.indicators.risk import ...` 调用方零改动。新代码应直接导入
app.lib.indicators.risk。）
"""

from ...lib.indicators.risk import RISK_EXPLANATIONS, risk_metrics  # noqa: F401
