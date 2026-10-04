"""缓存路径单一事实源：全库缓存目录/文件路径的唯一出处。

所有模块（缓存磁盘持久化、面板/模型 checkpoint、行情快照、hs300/
行业/数据源状态落盘）统一从此导入路径常量，禁止在业务代码中再用
`Path(__file__).resolve().parents[N] / "cache"` 之类表达式自行推导。

约定：本文件位于 backend/app/storage/ 下，`parents[2]` 即 backend 根，
CACHE_DIR 一律指向 backend/cache。
"""

from __future__ import annotations

from pathlib import Path

# backend/cache 根目录（缓存磁盘持久化目录）
CACHE_DIR = Path(__file__).resolve().parents[2] / "cache"

# 面板磁盘冷缓存（dataset_{id}[_指纹].npz/.json）
PANEL_DIR = CACHE_DIR / "panels"

# MLP checkpoint 目录（lib/alpha/nn.py: nn_{int(time.time())}.npz）
MODEL_DIR = CACHE_DIR / "models"

# 行情磁盘快照（{source}.pkl，离线兜底）
SNAPSHOT_DIR = CACHE_DIR / "snapshots"

# 数据源手动锁定状态（source_state.json）
SOURCE_STATE_FILE = CACHE_DIR / "source_state.json"

# 沪深300 成分股缓存（hs300_codes.json）
HS300_FILE = CACHE_DIR / "hs300_codes.json"

# 行业分类映射缓存（industry_map.json）
INDUSTRY_FILE = CACHE_DIR / "industry_map.json"
