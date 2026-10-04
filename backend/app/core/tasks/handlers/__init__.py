"""core/tasks/handlers:10 个任务处理器独立文件(归属 bt-handlers, T-105)。

按依赖聚类拆自 core/tasks/runner.py(registry 注册项):
- gp_run.py        _run_gp(GP 符号回归)
- evaluate.py      _run_evaluate(单因子完整评估)
- neural_train.py  _run_neural_train + _persist_nn_model(MLP 训练)
- backtest.py      _run_backtest(因子回测)
- alpha101.py      _run_alpha101_score(Alpha101 全库评分)
- factor_tune.py   _run_factor_tune(因子参数调优)
- dataset.py       _run_dataset_build(数据集构建)
- market_scan.py   _run_market_scan(全市场扫描)
- paper.py         _run_paper + _prepare_kline + _writeback_paper_result(模拟盘实验)
- walk_forward.py  _run_walk_forward(Walk-forward 滚动验证, T-03)

runner 只留执行框架(状态机/锁/超时/worker/分派),末尾经模块属性转发绑定
本包各处理器(runner._run_gp = gp_run._run_gp 等),保证测试 monkeypatch
(runner 模块属性)穿透;注册表 core/tasks/registry 驱动分派,runner 不
import 本包任何函数体。
"""

from __future__ import annotations
