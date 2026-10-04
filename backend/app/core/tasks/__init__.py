"""任务执行器核心(core/tasks):任务队列的 registry / runner / errors。

T-104:由原 experiments/queue.py 迁移而来;队列执行职责并入本包(runner),
全部调用方零改动。
本卡建 registry(任务类型注册表,数据驱动分派) + runner(状态机/锁/worker/
处理器) + errors(JobCancelled 等共享异常与错误文案);后续 C2 卡再拆独立文件。
"""
