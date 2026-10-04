"""Walk-forward 滚动验证(因子评估的样本外滚动切分)。

对给定因子表达式做 N 窗 anchored 滚动切分:每窗样本外段(OOS)独立计算逐期 IC,
拼接全部 OOS 段 → 完整 OOS IC 序列 + 分窗汇总(均值/稳定性/正占比)。

范围裁定(T-03):因子表达式固定,不做每窗重新挖掘/参数估计(训练段仅作切分
边界,不参与计算);「训练 = 参数估计」超出本卡范围。IC 计算复用 evaluate 的
ic_series/compute_ic 路径(单一事实源),保证与单因子评估口径一致。
"""

from __future__ import annotations

import numpy as np

from .evaluate import forward_returns, ic_series
from .operators import evaluate_rpn


def walk_forward_split(T: int, n_windows: int) -> list[tuple[int, int]]:
    """把 [0, T) 切分为 n 个近似等长的连续 OOS 段(anchored walk-forward)。

    返回 [(oos_start, oos_end), ...]:第 i 窗的训练段 = [0, oos_start)(anchored,
    使用此前全部数据);OOS 段彼此连续不重叠。n_windows clamp 到 [2, 10]。
    """
    n = max(2, min(int(n_windows), 10))
    return [(T * i // n, T * (i + 1) // n) for i in range(n)]


def _window_stats(ics: np.ndarray) -> dict:
    """单窗 IC 段汇总:均值/稳定性(IC>0 占比)/有效期数。"""
    valid = ics[np.isfinite(ics)]
    if len(valid) == 0:
        return {"ic": None, "stability": None, "ic_positive_ratio": None, "n_days": 0}
    return {
        "ic": float(valid.mean()),
        "stability": float((valid > 0).mean()),
        "ic_positive_ratio": float((valid > 0).mean()),
        "n_days": int(len(valid)),
    }


def run_walk_forward(
    rpn: list[dict],
    panel: dict[str, np.ndarray],
    horizon: int = 5,
    n_windows: int = 3,
    dates: list[str] | None = None,
) -> dict:
    """滚动验证:每窗 OOS 段独立计算 IC 序列,拼接 OOS IC + 分窗汇总。

    - 因子在全面板求值一次(evaluate_rpn),各窗按时间轴切片(表达式固定,
      ts_* 滚动特征在各自段上语义一致);
    - 每窗 OOS fwd 由该段 close 独立 forward_returns(不跨窗,无泄露);
    - 输出 windows(分窗指标)、oos_ic_series(拼接序列)、oos_dates、
      summary(全 OOS 汇总)。
    """
    factor = evaluate_rpn(rpn, panel)
    close = panel["close"]
    T = close.shape[1]
    splits = walk_forward_split(T, n_windows)
    windows: list[dict] = []
    oos_ics: list[float] = []
    oos_dts: list[str] = []
    for i, (t0, t1) in enumerate(splits):
        oos_start = dates[t0] if dates is not None and t0 < len(dates) else None
        oos_end = (
            dates[t1 - 1] if dates is not None and 0 <= t1 - 1 < len(dates) else None
        )
        if t1 - t0 < 2:
            # 段过短(<2 列)无法算前向收益 → 空窗,不参与拼接与汇总
            windows.append(
                {
                    "window": i + 1,
                    "oos_start": oos_start,
                    "oos_end": oos_end,
                    "ic": None,
                    "stability": None,
                    "ic_positive_ratio": None,
                    "n_days": 0,
                }
            )
            continue
        seg_factor = factor[:, t0:t1]
        seg_close = close[:, t0:t1]
        seg_fwd = forward_returns(seg_close, horizon)
        seg_ics = ic_series(seg_factor, seg_fwd)
        stats = _window_stats(seg_ics)
        windows.append(
            {"window": i + 1, "oos_start": oos_start, "oos_end": oos_end, **stats}
        )
        valid_idx = np.isfinite(seg_ics)
        for j in range(len(seg_ics)):
            if valid_idx[j]:
                oos_ics.append(float(seg_ics[j]))
                if dates is not None and t0 + j < len(dates):
                    oos_dts.append(str(dates[t0 + j]))

    all_ics = np.asarray(oos_ics, dtype=np.float64)
    if len(all_ics) == 0:
        summary = {
            "mean_ic": None,
            "ic_std": None,
            "stability": None,
            "ic_positive_ratio": None,
            "n_days": 0,
        }
    else:
        summary = {
            "mean_ic": float(all_ics.mean()),
            "ic_std": float(all_ics.std()) if len(all_ics) > 1 else None,
            "stability": float((all_ics > 0).mean()),
            "ic_positive_ratio": float((all_ics > 0).mean()),
            "n_days": int(len(all_ics)),
        }
    return {
        "n_windows": len(splits),
        "horizon": int(horizon),
        "windows": windows,
        "oos_ic_series": [round(float(v), 6) for v in oos_ics],
        "oos_dates": oos_dts,
        "summary": summary,
    }
