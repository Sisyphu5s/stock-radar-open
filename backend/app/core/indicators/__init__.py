"""指标调优与风险指标（bt-fin，core 层）。

纯计算实现由 lib/indicators.compute + lib.metrics 承担；本包承载调优编排
（tune.py 网格搜索/目标评分）与风险指标框架（risk.py），无数据通道依赖。
"""
