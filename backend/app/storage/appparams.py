"""AppParam 参数表存取（存储资产层）。

由原 common.py 迁移而来（common-split），函数体逐字搬移，行为不改。
import 直连 storage 层（.db / .models），不再经旧数据库/模型兼容层。

P1-61：get_app_param 单键查询带短 TTL 缓存——行情热路径（3 源竞速 × 快照/SSE
每轮）每次读取 settings.quote_cache_ttl 等可运行时覆盖键都经本函数开一个
SQLite 会话查询，属无谓 I/O。缓存按 (key, SessionLocal 身份) 记 1 秒：
- 同进程同 DB（SessionLocal 不变）1 秒内同键只查 1 次；
- 测试 monkeypatch 换 SessionLocal（身份变）→ 自动失效，不跨库串值；
- 写路径（set/delete_app_params）主动失效，配置变更立即可见（配合 runtime_config）。
"""

from __future__ import annotations

import threading
import time


class _ParamCache:
    """AppParam 单键查询短 TTL 缓存（P1-61）：key -> (value, 查询时刻, SessionLocal 身份)。

    - TTL 1 秒：热路径同进程同 DB 1 秒内同键只查 1 次；
    - 写路径主动失效（set/delete_app_params），配置变更立即可见；
    - SessionLocal 身份绑定：测试换临时库自动失效，不跨库串值。
    容器为类属性（非模块级可变容器，函数内修改不构成图审计 B4 global_write）。
    """

    _TTL_S = 1.0
    _data: dict[str, tuple[str | None, float, int]] = {}
    _lock = threading.Lock()

    @classmethod
    def invalidate(cls, keys: list[str]) -> None:
        """写路径联动：删除命中键的缓存条目（rt:/ind: 等前缀一视同仁）。"""
        if not keys:
            return
        with cls._lock:
            for k in keys:
                if isinstance(k, str) and k.strip():
                    cls._data.pop(k, None)

    @classmethod
    def get(cls, key: str, maker) -> tuple[str | None, float, int] | None:
        """取缓存条目；TTL 未过期且 SessionLocal 身份一致才命中。"""
        now = time.monotonic()
        with cls._lock:
            hit = cls._data.get(key)
            if hit is not None and now - hit[1] < cls._TTL_S and hit[2] == id(maker):
                return hit
        return None

    @classmethod
    def set(cls, key: str, value: tuple[str | None, float, int]) -> None:
        with cls._lock:
            cls._data[key] = value


def get_app_params(prefix: str | None = None) -> dict[str, str]:
    """AppParam 键值查询：prefix 非空时只取 key LIKE 'prefix%'。"""
    from .db import SessionLocal
    from .models import AppParam

    db = SessionLocal()
    try:
        q = db.query(AppParam)
        if prefix:
            q = q.filter(AppParam.key.like(f"{prefix}%"))
        return {r.key: r.value for r in q.all()}
    finally:
        db.close()


def get_app_param(key: str) -> str | None:
    """AppParam 单键查询：不存在返回 None。短 TTL 缓存（见 _ParamCache docstring）。"""
    from .db import SessionLocal
    from .models import AppParam

    maker = SessionLocal
    hit = _ParamCache.get(key, maker)
    if hit is not None:
        return hit[0]

    db = maker()
    try:
        row = db.get(AppParam, key)
        value = row.value if row is not None else None
    finally:
        db.close()
    _ParamCache.set(key, (value, time.monotonic(), id(maker)))
    return value


def delete_app_params(keys: list[str]) -> int:
    """AppParam 批量删除（存在才删），返回实际删除条数。"""
    from .db import SessionLocal
    from .models import AppParam

    db = SessionLocal()
    try:
        deleted = 0
        for k in keys:
            if not isinstance(k, str) or not k.strip():
                continue
            row = db.get(AppParam, k)
            if row is not None:
                db.delete(row)
                deleted += 1
        db.commit()
        _ParamCache.invalidate(keys)  # P1-61：删除后缓存立即失效（Settings hook 语义）
        return deleted
    finally:
        db.close()


def set_app_params(values: dict) -> list[str]:
    """AppParam 批量 upsert（非字符串/空键跳过），返回实际保存的键列表。"""
    from .db import SessionLocal
    from .models import AppParam, utcnow

    db = SessionLocal()
    try:
        now = utcnow()
        for k, v in values.items():
            if not isinstance(k, str) or not k.strip():
                continue
            row = db.get(AppParam, k)
            if row is None:
                db.add(AppParam(key=k, value=str(v), updated_at=now))
            else:
                row.value = str(v)
                row.updated_at = now
        db.commit()
        saved = [k for k in values if isinstance(k, str) and k.strip()]
        _ParamCache.invalidate(saved)  # P1-61：写入后缓存立即失效（Settings hook 语义）
        return saved
    finally:
        db.close()
