"""换手率必须基于全局股票身份而非局部下标（evaluate.factor_to_returns 回归测试）。

背景：factor_to_returns 对每期做 mask 过滤后取 Top 集合，旧实现用过滤后的
局部下标构建 top_set，而不同期缺失股票不同（上市/退市/停牌）时局部下标不指向
同一只股票——两期局部下标 0 可能分别是股票 A 和股票 B，相交结果无身份语义，
导致换手率虚高/虚低。

本测试构造 12 只股票 × 2 期：第一期 12 只全部有效、第二期股票 0 缺失。
- 第一期 Top（20% × 12 = 2 只）全局身份 {0, 1}
- 第二期 Top（20% × 11 = 2 只）全局身份 {1, 2}
- 按全局身份：交集 {1} → churn = 1 - 1/2 = 0.5
- 按局部下标：两期 top 局部下标都是 {0, 1} → churn = 0（错误）
"""

import numpy as np
import pytest

from app.lib.alpha.evaluate import factor_to_returns


def test_turnover_uses_global_identity_not_local_index():
    n = 12
    factor = np.full((n, 2), np.nan)
    fwd_ret = np.full((n, 2), np.nan)
    # 第一期：12 只全部有效，因子从高到低（股票 i 的因子 = n - i，Top = 股票 0/1）
    factor[:n, 0] = np.arange(n, 0, -1, dtype=float)
    fwd_ret[:n, 0] = 1.0
    # 第二期：股票 0 缺失（仅股票 1..11 有效），因子从高到低（Top = 股票 1/2）
    factor[1:n, 1] = np.arange(n - 1, 0, -1, dtype=float)
    fwd_ret[1:n, 1] = 1.0

    _, _, turnover = factor_to_returns(factor, fwd_ret, top_pct=0.2)

    # 第一期 Top = {0,1}、第二期 Top = {1,2}（全局身份），交集 1/2 → churn = 0.5；
    # 若按局部下标 {0,1}∩{0,1} 会误算为 0.0（虚低）。
    assert turnover == pytest.approx(0.5)
