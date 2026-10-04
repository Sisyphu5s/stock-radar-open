"""快照磁盘后备（存储层），由 core/sources 门面 re-export。

H1a（快照 I/O 下沉）：把磁盘快照落盘/读取/离线兜底从 core/sources.py 下沉到
storage 层，消除 storage/providers 对 core.sources 的反向 import（恢复分层纪律：
storage 不得 import core）。core/sources.py 门面 re-export 这些符号——
tests/test_offline.py 经 `from app.core import sources` 引用 _SNAP_DIR /
SNAP_MAX_AGE_S / save_snapshot_disk / load_snapshot_disk，get_spot 门面的
离线兜底也复用 load_snapshot_disk。

测试隔离缝：`tests/test_offline.py` 经 `monkeypatch.setattr("app.storage.snapshots._SNAP_DIR", tmp_path)`
重定向快照目录（缝随实现下沉，不再挂 core/sources 门面）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd

from .paths import SNAPSHOT_DIR

_quote_logger = logging.getLogger("stockradar.snapshots")

# 默认快照目录（core/sources 门面 re-export 此值作为初始配置）。
_SNAP_DIR = SNAPSHOT_DIR
SNAP_MAX_AGE_S = 30 * 60  # 磁盘快照最长可用的离线窗口


def _snap_path(source: str) -> str:
    return str(_SNAP_DIR / f"{source}.pkl")


def save_snapshot_disk(source: str, df: pd.DataFrame) -> None:
    """成功拉取全量快照后落盘（离线兜底）。"""
    try:
        _SNAP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _snap_path(source) + ".tmp"
        df.to_pickle(tmp)
        Path(tmp).replace(_snap_path(source))
    except Exception as e:
        _quote_logger.debug("快照落盘失败 %s: %s", source, str(e)[:80])


def load_snapshot_disk(
    source: str, max_age_s: float = SNAP_MAX_AGE_S
) -> pd.DataFrame | None:
    """从磁盘加载最后一次成功快照（仅当未过期）。"""
    try:
        path = _snap_path(source)
        if not Path(path).exists():
            return None
        age = time.time() - Path(path).stat().st_mtime
        if age > max_age_s:
            return None
        df = pd.read_pickle(path)
        return df if df is not None and len(df) else None
    except Exception as e:
        _quote_logger.debug("快照磁盘读取失败 %s: %s", source, str(e)[:80])
        return None


def _spot_disk_fallback(source: str, now: float) -> pd.DataFrame | None:
    """内存缓存不可用（如重启后）时尝试磁盘快照。"""
    df = load_snapshot_disk(source)
    if df is not None:
        _quote_logger.warning(
            "数据源 %s 网络不可达，使用磁盘快照（%ds 前）",
            source,
            int(now - Path(_snap_path(source)).stat().st_mtime),
        )
    return df
