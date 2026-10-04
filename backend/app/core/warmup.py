"""启动预热：后台线程拉取 HS300 近 5 年日线并构建数据集。

- 若已存在 universe='hs300' 且 row_count>0 且 end_date>=昨天 的数据集 → 跳过。
- 否则并发（8 workers）增量写入 klines 表，随后 build_dataset。
- 全程不抛异常，daemon 线程，失败不阻塞服务启动。

（bt-fin 迁移：原 warmup.py 全文迁入本模块；
hs300/klines 走 storage 直连，数据集构建走 core.datasets 直连。）
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta

from ..lib.session import now_cn

logger = logging.getLogger("stockradar.warmup")

_state: dict = {"status": "idle", "progress": 0.0, "message": "", "dataset_id": None}
_lock = threading.Lock()
_thread: threading.Thread | None = None

DATASET_NAME = "沪深300 · 近5年日线"
WORKERS = 8


def get_warmup_status() -> dict:
    with _lock:
        return dict(_state)


def start_warmup(background: bool = True):
    """启动预热；已在运行则忽略。background=False 时同步执行（测试用）。"""
    global _thread
    with _lock:
        if _state["status"] == "running":
            logger.info("预热已在运行中，忽略本次调用")
            return
        # 置 running 与启动线程必须处于同一临界区：若先置 idle 再在锁外启线程，
        # 两次快速调用会在「idle 且线程未 running」窗口内双双通过检查 → 双线程并发拉取。
        _state.update(
            {
                "status": "running",
                "progress": 0.0,
                "message": "准备中",
                "dataset_id": None,
            }
        )
        if background:
            try:
                t = threading.Thread(
                    target=_run_warmup, name="hs300-warmup", daemon=True
                )
                t.start()
            except Exception:
                _state["status"] = "idle"  # 启动失败回滚，避免状态卡死在 running
                raise
            _thread = t
    if not background:
        _run_warmup()


def _set(**kw) -> None:
    with _lock:
        _state.update(kw)


def _run_warmup() -> None:
    from ..storage.db import SessionLocal
    from ..storage.models import Dataset

    _set(status="running", progress=0.0, message="检查现有数据集")
    try:
        # 1. 已有合格数据集 → 跳过（但需校验 klines 表确实有该数据集成分的缓存数据，
        #    否则视为缓存被清空，继续重建，避免"数据集合格但 K 线为空"）
        # 日期口径用上海时区（A股自然日）：date.today() 取服务器本地时区，非 +08:00
        # 的部署环境会偏一天，导致"end_date >= 昨天"判定与数据实际时点错位
        end = now_cn().date()
        start = end - timedelta(days=365 * 5)
        yesterday = (end - timedelta(days=1)).isoformat()
        db = SessionLocal()
        try:
            existing = db.query(Dataset).filter(Dataset.universe == "hs300").all()
            if existing:
                from sqlalchemy import func
                from ..storage.klines_db import Kline, klines_session
                from ..storage.hs300 import get_hs300_codes

                try:
                    codes_ok = get_hs300_codes()
                except Exception as e:
                    logger.warning(
                        "获取 HS300 成分失败，跳过缓存校验: %s", str(e)[:200]
                    )
                    codes_ok = []
                cnt = 0
                if codes_ok:
                    # Kline 已迁独立 K 线库（T-103），主库不再有 klines 表：
                    # 计数必须走 klines_session，否则迁移后 no such table。
                    # klines 库无表（迁移未执行/被清空）时 graceful 视为无缓存，触发重建。
                    kdb = klines_session()
                    try:
                        cnt = (
                            kdb.query(func.count(Kline.id))
                            .filter(
                                Kline.code.in_([c.split(".")[0] for c in codes_ok]),
                                Kline.period == "daily",
                            )
                            .scalar()
                            or 0
                        )
                    except Exception as e:
                        logger.warning(
                            "K 线库缓存校验失败，按无缓存重建处理: %s",
                            str(e)[:200],
                        )
                        cnt = 0
                    finally:
                        kdb.close()
                if cnt > 0:
                    for ds in existing:
                        if ds.row_count > 0 and ds.end_date >= yesterday:
                            _set(
                                status="done",
                                progress=100.0,
                                message="已存在合格数据集，跳过预热",
                                dataset_id=ds.id,
                            )
                            logger.info(
                                "预热跳过: 数据集 ds=%s 已覆盖至 %s", ds.id, ds.end_date
                            )
                            return
                logger.warning("klines 表无成分缓存（%d 行），重建预热", cnt)
        finally:
            db.close()

        # 2. HS300 成分 → 并发增量写入 klines
        from ..storage.hs300 import get_hs300_codes
        from ..storage.klines import fetch_many

        _set(message="获取沪深300成分股")
        codes = get_hs300_codes()
        if not codes:
            raise RuntimeError("HS300 成分股为空")
        _set(message=f"拉取 {len(codes)} 只近5年日线 (workers={WORKERS})")

        last_pct = -1

        def cb(done: int, total: int) -> None:
            nonlocal last_pct
            pct = round(done / total * 100, 1)
            _set(progress=pct, message=f"K线缓存 {done}/{total}")
            if int(pct // 10) != last_pct:  # 每 10% 打日志
                last_pct = int(pct // 10)
                logger.info("预热进度: %.0f%% (%d/%d)", pct, done, total)

        fetch_many(
            codes,
            "daily",
            workers=WORKERS,
            progress_cb=cb,
            start_date=start.isoformat(),
        )

        # 3. 构建数据集（core.datasets 直连）
        from .datasets import build_dataset

        _set(message="构建数据集")
        res = build_dataset(DATASET_NAME, "hs300", start.isoformat(), end.isoformat())
        _set(
            status="done",
            progress=100.0,
            message=f"完成: {res['stock_count']} 只 × {res['row_count']} 行",
            dataset_id=res["id"],
        )
        logger.info(
            "预热完成: 数据集 ds=%s (%d 只, %d 行)",
            res["id"],
            res["stock_count"],
            res["row_count"],
        )
    except Exception as e:
        logger.warning("预热失败: %s", str(e)[:200])
        _set(status="failed", message=f"失败: {str(e)[:120]}")
