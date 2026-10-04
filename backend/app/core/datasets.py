"""Alpha 数据集构建（core 层，bt-fac）：从 K 线缓存构建 (S, T) 面板数据。

由原 alpha/dataset.py 模块合流而来:
纯计算(universe 配置/日期范围/特征面板对齐构造)已提取至 app.lib.alpha.features,
本模块保留数据通道（DB 读写/网络拉取/面板 npz 落盘/内存热缓存）。

stock_count 语义：build_dataset 的 stock_count = 原始合格成分股数（K 线≥60 根）；
load_panel 构建面板时会按日期对齐进一步筛选（新上市/停牌股被剔除），
实际面板股票数以 load_panel 返回的 stock_count 为准。

缓存分层：
- panel_cache（内存 TTL 1h）：热缓存，key 含特征子集 `panel:{ds_id}:{features}`，
  不同特征子集请求各自独立缓存。
- backend/cache/panels/dataset_{id}[_{特征指纹}].npz（磁盘冷缓存，TTL 7 天）：
  meta json 记录 row_count 与 klines 表行数一致且未过期时直接复用，否则重建。
  特征子集请求写带指纹的子集文件（互不覆盖）；全量请求写无后缀文件，
  子集文件 miss 时回退全量文件（want ⊆ 全量特征则命中）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from ..lib.alpha.features import (
    ESTIMATE_FEATURES,
    INDUSTRY_FEATURE,
    UNIVERSE_DEFAULT_LIMITS,
    UNIVERSES,
    align_panel,
    default_range,
    industry_codes,
    list_universes,
)
from ..lib.codes import bare_code, in_chunks
from ..storage.db import SessionLocal
from ..storage.hs300 import get_hs300_codes
from ..storage.klines import cached_kline
from ..storage.models import Dataset, DatasetStock
from ..storage.paths import PANEL_DIR
from .sources import _MOCK_STOCKS, get_provider, get_spot

logger = logging.getLogger("stockradar.alpha.dataset")


def _build_workers() -> int:
    """数据集 K 线拉取并行度：min(os.cpu_count(), 12)；≤1 时纯串行。"""
    return max(1, min(os.cpu_count() or 1, 12))


def _fetch_kline(code: str, start_date: str, end_date: str):
    """单只 K 线拉取（历史缓存优先，start_date 兜底深历史）；失败返回 None。

    SQLite 写入由 kcache 内部在途去重/单事务保证，调用方（build_dataset /
    load_panel 重建路径）不自行并发写 DB，只读回 DataFrame。
    """
    try:
        k = cached_kline(code, "daily", max_rows=2000, start_date=start_date)
        if k is not None and len(k):
            k = k[(k["date"] >= start_date) & (k["date"] <= end_date)]
    except Exception:
        return None
    if k is None or len(k) < 60:
        return None
    return code, k


PANEL_TTL = 7 * 24 * 3600  # 7 天


def _amount_top_codes(limit: int) -> list[str]:
    """成交额 Top N（akshare 快照按成交额降序；离线回退内置列表）。"""
    try:
        spot = get_spot()
        if spot is not None and not spot.empty and get_provider().name == "akshare":
            return (
                spot.sort_values("amount", ascending=False)["code"].head(limit).tolist()
            )
    except Exception as e:
        logger.warning(
            "成交额 Top%d 快照获取失败，回退内置列表: %s", limit, str(e)[:100]
        )
    codes = [c for c, _, _ in _MOCK_STOCKS[:limit]]
    if not codes:
        raise ValueError("成交额排名不可用（快照获取失败且无离线回退）")
    return codes


def _all_market_codes(db, limit: int) -> list[str]:
    """全市场股票：优先 akshare 快照全量；回退数据库全部股票；再回退内置列表。"""
    try:
        spot = get_spot()
        if spot is not None and not spot.empty and get_provider().name == "akshare":
            codes = spot["code"].tolist()
            return codes[:limit] if limit else codes
    except Exception as e:
        logger.warning("全市场快照获取失败，回退数据库股票列表: %s", str(e)[:100])
    from ..storage.models import Stock

    codes = [s.code for s in db.query(Stock).all()]
    if codes:
        return codes[:limit] if limit else codes
    codes = [c for c, _, _ in _MOCK_STOCKS]
    if not codes:
        raise ValueError("全市场股票列表不可用（快照/数据库均无数据）")
    return codes[:limit] if limit else codes


def _resolve_codes(
    db, universe: str, limit: int, custom_codes: list[str] | None
) -> list[str]:
    """按 universe 语义确定股票池（不静默回退：未知/不可用均抛 ValueError）。

    limit 语义：>0 为硬上限；0 表示不截断（universe 全量 / 默认规模）。
    """
    if universe not in UNIVERSES:
        raise ValueError(f"未知 universe: {universe}（可选: {', '.join(UNIVERSES)}）")
    if universe == "custom":
        if custom_codes:
            codes = [c.strip() for c in custom_codes if c.strip()]
            if not codes:
                raise ValueError("custom_codes 为空，未提供任何股票代码")
            return codes[:limit] if limit else codes
        from ..storage.models import Stock

        watchlist = [
            s.code for s in db.query(Stock).filter(Stock.is_watchlist.is_(True)).all()
        ]
        if watchlist:
            return watchlist[:limit] if limit else watchlist
        raise ValueError("universe 'custom' 需要 custom_codes 或自选股")
    if custom_codes:
        raise ValueError(
            f"universe '{universe}' 不支持 custom_codes，请使用 universe='custom'"
        )
    if universe == "hs300":
        codes = get_hs300_codes()
        if not codes:
            raise ValueError("universe 'hs300' 成分获取失败")
        return codes[:limit] if limit else codes
    if universe == "top500":
        return _amount_top_codes(limit or 500)
    return _all_market_codes(db, limit)  # universe == "all"


def build_dataset(
    name: str,
    universe: str,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 300,
    custom_codes: list[str] | None = None,
    progress_cb=None,
) -> dict:
    """从行情服务拉取 K 线，构建面板数据。custom_codes 支持自定义股票池。

    start_date/end_date 默认近 5 年；写入 dataset_stocks 记录实际成分股；
    progress_cb(done, total) 可选进度回调。
    limit 契约：>0 为上限，0 表示不截断（universe 全量/默认规模）；负数抛 ValueError。
    """
    if not start_date or not end_date:
        start_date, end_date = default_range()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise ValueError(f"limit 必须是整数: {limit!r}")
    if limit < 0:
        raise ValueError(f"limit 不能为负: {limit}")
    if start_date > end_date:
        raise ValueError(
            f"日期范围非法: start_date({start_date}) 晚于 end_date({end_date})"
        )
    db = SessionLocal()
    try:
        codes = _resolve_codes(db, universe, limit, custom_codes)
        if not codes:
            raise ValueError(f"universe '{universe}' 无可用股票代码")

        logger.info(
            "数据集 %s: 拉取 %d 只股票 K 线 (%s ~ %s)",
            name,
            len(codes),
            start_date,
            end_date,
        )
        rows = 0
        good = 0
        good_codes: list[str] = []

        n_workers = _build_workers()
        if n_workers <= 1:
            # 纯串行回退：保持原行为
            for idx, code in enumerate(codes):
                if progress_cb:
                    progress_cb(idx + 1, len(codes))
                r = _fetch_kline(code, start_date, end_date)
                if r is None:
                    continue
                good += 1
                good_codes.append(code)
                rows += len(r[1])
        else:
            # 并行拉取（I/O bound），进度回调保持 (done, total) 且节流；结果按 codes 顺序归并
            picked: dict[str, pd.DataFrame] = {}
            done = 0
            last_t = time.time()
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = {
                    pool.submit(_fetch_kline, c, start_date, end_date): c for c in codes
                }
                try:
                    for fut in as_completed(futures):
                        r = fut.result()
                        if r is not None:
                            picked[r[0]] = r[1]
                        done += 1
                        if progress_cb:
                            now = time.time()
                            if (
                                done == len(codes)
                                or done % 25 == 0
                                or now - last_t >= 0.2
                            ):
                                last_t = now
                                progress_cb(done, len(codes))
                except BaseException:
                    for f in futures:
                        f.cancel()
                    raise
            for code in codes:  # 恢复顺序：DatasetStock.seq 与 codes 顺序一致
                k = picked.get(code)
                if k is None:
                    continue
                good += 1
                good_codes.append(code)
                rows += len(k)

        ds = Dataset(
            name=name,
            universe=universe,
            start_date=start_date,
            end_date=end_date,
            stock_count=good,
            row_count=rows,
        )
        db.add(ds)
        db.commit()
        db.refresh(ds)
        if good_codes:
            # 分批提交（每批 ≤400 行），避免 limit=0 全量时 add_all 一次性写入数千行
            # 触发 SQLite too many SQL variables（backend 批量写铁律）
            seq = 0
            for chunk in in_chunks(good_codes, 400):
                db.add_all(
                    [
                        DatasetStock(dataset_id=ds.id, code=c, seq=seq + i)
                        for i, c in enumerate(chunk)
                    ]
                )
                db.commit()
                seq += len(chunk)
        return {
            "id": ds.id,
            "name": ds.name,
            "universe": ds.universe,
            "start_date": ds.start_date,
            "end_date": ds.end_date,
            "stock_count": ds.stock_count,
            "row_count": ds.row_count,
        }
    finally:
        db.close()


# ---------- 面板磁盘冷缓存（numpy） ----------


def _panel_fingerprint(features: list[str] | None) -> str | None:
    """特征子集指纹：排序后取 md5 前 8 位；空/全量请求返回 None（复用全量文件）。"""
    if not features:
        return None
    return hashlib.md5(",".join(sorted(features)).encode("utf-8")).hexdigest()[:8]


def _panel_paths(dataset_id: int, features: list[str] | None = None) -> tuple[str, str]:
    """npz/meta 路径。features 非空（特征子集请求）时文件名带指纹，
    不同特征子集的冷缓存相互独立，互不覆盖；features 为空（全量）时
    用无后缀文件（兼容旧版缓存与 delete_panel_files）。"""
    tag = f"_{_panel_fingerprint(features)}" if features else ""
    return (
        str(PANEL_DIR / f"dataset_{dataset_id}{tag}.npz"),
        str(PANEL_DIR / f"dataset_{dataset_id}{tag}.json"),
    )


def _klines_row_count() -> int:
    """K 线独立库总行数（T-103 后 klines 已迁独立库，主库不再有该表）。

    面板冷缓存 row_count 校验的单一事实源：meta 记录行数与 K 线库当前行数
    一致才复用 npz，否则触发重建。与 warmup 缓存校验同口径（klines_session
    + func.count(Kline.id)）；库缺失/查询失败 graceful 返 0（视为缓存失效
    触发重建，不阻塞面板加载）。
    """
    from sqlalchemy import func

    from ..storage.klines_db import Kline, klines_session

    try:
        db = klines_session()
    except Exception as e:
        logger.warning("K 线库会话创建失败，按 0 处理: %s", str(e)[:200])
        return 0
    try:
        return int(db.query(func.count(Kline.id)).scalar() or 0)
    except Exception as e:
        logger.warning("K 线库行数统计失败，按 0 处理: %s", str(e)[:200])
        return 0
    finally:
        db.close()


def _save_panel_disk(
    dataset_id: int,
    panel: dict,
    dates: list[str],
    codes: list[str],
    features: list[str] | None = None,
) -> None:
    """npz 存 {dates, codes, feature...} + meta json 记录 ts/row_count。

    features 非空（特征子集重建）时写带指纹的子集文件；meta 记录 features
    便于诊断。全量请求（features=None）写无后缀文件。
    """
    try:
        os.makedirs(PANEL_DIR, exist_ok=True)
        npz_path, meta_path = _panel_paths(dataset_id, features)
        np.savez_compressed(
            npz_path,
            dates=np.array(dates, dtype=str),
            codes=np.array(codes, dtype=str),
            **panel,
        )
        tmp = meta_path + ".tmp"
        meta = {
            "ts": time.time(),
            "row_count": _klines_row_count(),
            "n_stocks": len(codes),
            "n_dates": len(dates),
        }
        if features:
            meta["features"] = sorted(features)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp, meta_path)
    except Exception as e:
        logger.warning("面板磁盘缓存写入失败 ds=%s: %s", dataset_id, str(e)[:100])


def _read_panel_npz(
    npz_path: str, meta_path: str, allow_stale: bool, want: list[str] | None
) -> dict | None:
    """单个 npz 文件的 TTL/row_count/特征校验读取；任一条件不满足返回 None。"""
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        return None
    if time.time() - meta.get("ts", 0) > PANEL_TTL:
        return None
    if meta.get("row_count") != _klines_row_count() and not allow_stale:
        return None
    try:
        with np.load(npz_path, allow_pickle=False) as data:
            panel = {k: data[k] for k in data.files if k not in ("dates", "codes")}
            dates = [str(x) for x in data["dates"]]
            codes = [str(x) for x in data["codes"]]
            if want:
                missing = [f for f in want if f not in panel]
                if missing:
                    logger.info(
                        "面板磁盘缓存缺新特征 %s (ds=%s)，触发重建",
                        ",".join(missing),
                        _dataset_id_from_path(npz_path),
                    )
                    return None
    except Exception as e:
        logger.warning("面板磁盘缓存读取失败: %s", str(e)[:100])
        return None
    logger.info(
        "面板 ds=%s 从磁盘冷缓存加载 (%d 只 × %d 天)",
        _dataset_id_from_path(npz_path),
        len(codes),
        len(dates),
    )
    return {
        "panel": panel,
        "dates": dates,
        "codes": codes,
        "stock_count": len(codes),
    }


def _dataset_id_from_path(npz_path: str) -> str:
    """从 npz 文件名提取 dataset_id（含特征指纹后缀时截取到指纹前）。"""
    return Path(npz_path).name.removeprefix("dataset_").split("_")[0]


def _load_panel_disk(
    dataset_id: int,
    allow_stale: bool = False,
    want: list[str] | None = None,
) -> dict | None:
    """meta 未过期且 row_count 与 klines 表一致 → 读 npz，否则重建。

    allow_stale=True（离线场景）：跳过 row_count 校验，用现有 npz 数据。
    want 非空时校验特征齐全：旧版 npz 缺新特征视为缓存 miss（触发重建），
    避免 load_panel 在 `{f: result["panel"][f] for f in want}` 处 KeyError 500。

    读取顺序：① 特征子集文件（want 指纹）→ ② 无后缀全量文件（兼容旧版缓存，
    want ⊆ 全量特征时直接命中截取），两者都 miss 才触发重建。
    """
    loaded = _read_panel_npz(
        *_panel_paths(dataset_id, want), allow_stale=allow_stale, want=want
    )
    if loaded is not None:
        return loaded
    if want:
        loaded = _read_panel_npz(
            *_panel_paths(dataset_id), allow_stale=allow_stale, want=want
        )
        if loaded is not None:
            return loaded
    return None


def delete_panel_files(dataset_id: int) -> None:
    """删除数据集时清理面板缓存：磁盘 npz/meta（全量 + 各特征子集）+ 内存热缓存。

    热缓存不清理时，删除后 ~1h（TTL）内任务仍会读到已删数据的旧面板；
    子集文件按 dataset_{id}_* 前缀扫描删除（_load_panel_disk 会读子集文件）。
    """
    from ..storage.cache import panel_cache

    panel_cache.delete_prefix(f"panel:{dataset_id}:")
    for p in _panel_paths(dataset_id):
        try:
            os.remove(p)
        except OSError:
            pass
    prefix = f"dataset_{dataset_id}_"
    try:
        for fn in os.listdir(PANEL_DIR):
            if fn.startswith(prefix) and fn.endswith((".npz", ".json")):
                try:
                    os.remove(str(PANEL_DIR / fn))
                except OSError:
                    pass
    except OSError:
        pass


# ---------- 面板加载 ----------

# T-07: 估值特征 ← 快照列名映射（turnover 特征对应快照换手率 turnover_rate）
_ESTIMATE_SPOT_COLS = {
    "pe": "pe",
    "pb": "pb",
    "ps": "ps",
    "market_cap": "market_cap",
    "float_cap": "float_cap",
    "turnover": "turnover_rate",
}


def _industry_code_table(spot: pd.DataFrame) -> dict[str, int]:
    """行业名 → 整数编码表（sorted 保证确定性；0 保留给未知/缺失）。

    单一事实源：快照 industry 列（经 storage.industry.apply_industry 由板块映射
    填充）。面板内全部股票共用同一编码表，同行业同名同码。
    """
    names = sorted(
        {
            str(n)
            for n in spot["industry"].dropna().unique()
            if str(n).strip() and str(n) != "nan"
        }
    )
    return {n: i + 1 for i, n in enumerate(names)}


def _inject_estimate_features(
    candidates: list[tuple[str, pd.DataFrame]],
    est_cols: list[str],
    spot: pd.DataFrame | None,
) -> list[tuple[str, pd.DataFrame]]:
    """把每日快照估值字段注入候选 K 线（数据通道，纯 I/O + 列操作）。

    对齐策略：估值序列与 K 线日期按交易日 merge——只在该股 K 线最后一个交易日
    （快照时点对应日）写入快照值，其余日期缺失为 NaN。绝不把当前快照值回填
    历史日期（回填即前视偏差，历史估值不可知）。快照缺失（网络失败/停牌/新股
    不在快照）→ 该股整列 NaN，由 align_panel 缺失列容错兜底。

    industry 为分类特征：注入行业名整数编码（0=未知），数值面板可参与 RPN。
    spot 列名与 _ESTIMATE_SPOT_COLS 映射；快照缺列（如数据源无 ps）记 NaN。
    """
    if spot is None or spot.empty:
        return candidates
    ind_table = _industry_code_table(spot)
    # 索引用 bare code（快照 code 形如 600519.SH；成分可能无后缀，如测试桩）
    spot_map: dict[str, dict] = {}
    for _, row in spot.iterrows():
        spot_map[bare_code(str(row.get("code", "")))] = row.to_dict()
    out: list[tuple[str, pd.DataFrame]] = []
    for code, k in candidates:
        k = k.copy()
        for f in est_cols:
            k[f] = np.nan
        row = spot_map.get(bare_code(code))
        if row is not None and len(k):
            last_date = k["date"].iloc[-1]
            mask = k["date"] == last_date
            for f in est_cols:
                if f == INDUSTRY_FEATURE:
                    raw_ind = row.get("industry", "")
                    code_v = (
                        ind_table.get(str(raw_ind), 0) if raw_ind is not None else 0
                    )
                    k.loc[mask, f] = float(code_v)
                else:
                    v = row.get(_ESTIMATE_SPOT_COLS[f])
                    k.loc[mask, f] = (
                        float(v)
                        if v is not None and not (isinstance(v, float) and np.isnan(v))
                        else np.nan
                    )
        out.append((code, k))
    return out


def panel_ready(dataset_id: int, features: list[str] | None = None) -> bool:
    """只读探测：面板是否可直接同步服务（内存热缓存 / 磁盘冷缓存命中）。

    与 load_panel 的缓存命中判定逐条对齐（热缓存须含 codes、磁盘走
    TTL/row_count/特征齐全校验 + 特征子集回退全量文件），仅探测不触发任何
    重建。冷缓存 miss 时调用方应转 202 后台 panel_build 任务，避免同步重建
    （拉全量成分 K 线 + spot 注入 + 对齐，可达数十秒）阻塞请求线程。
    """
    from ..lib.alpha.operators import FEATURES as ALL_FEATURES
    from ..storage.cache import panel_cache

    want = features or ALL_FEATURES
    key = f"panel:{dataset_id}:{','.join(want)}"
    cached = panel_cache.get(key)
    if cached is not None:
        # 旧版热缓存（无 codes）与 load_panel 同语义失效，回退磁盘判定
        return "codes" in cached
    from .sources import network_down

    return (
        _load_panel_disk(dataset_id, allow_stale=network_down(), want=want) is not None
    )


def load_panel(dataset_id: int, features: list[str] | None = None) -> dict | None:
    """从已构建数据集重建面板（内存热缓存 → 磁盘冷缓存 → 重建）。

    返回 {panel, dates, stock_count, codes}:codes 为与面板行序严格对齐的股票代码
    (T-02 涨跌停按板块阈值必需);旧版热缓存(无 codes)直接失效重读磁盘。
    """
    from ..lib.alpha.operators import FEATURES as ALL_FEATURES
    from ..storage.cache import panel_cache

    want = features or ALL_FEATURES
    key = f"panel:{dataset_id}:{','.join(want)}"
    cached = panel_cache.get(key)
    if cached is not None:
        if "codes" not in cached:
            # 旧版热缓存(无 codes)→ 失效重走磁盘/重建,保证约束路径拿到代码
            panel_cache.delete(key)
        else:
            return cached

    from .sources import network_down

    result = _load_panel_disk(dataset_id, allow_stale=network_down(), want=want)
    if result is not None:
        out = {
            "panel": {f: result["panel"][f] for f in want},
            "dates": result["dates"],
            "codes": result["codes"],
            "stock_count": result["stock_count"],
        }
        panel_cache.set(key, out)
        return out

    db = SessionLocal()
    try:
        ds = db.get(Dataset, dataset_id)
        if ds is None:
            return None
        # 真实成分股优先；成分缺失时按 Dataset.universe 语义重新解析（_resolve_codes），
        # 解析失败/不可用 → 返回 None 报错。禁止静默回退成交额 top300 猜测池——
        # 否则 hs300 数据集成分丢失后会悄悄变成 top300 池，与 universe 语义脱钩。
        rows = (
            db.query(DatasetStock)
            .filter(DatasetStock.dataset_id == dataset_id)
            .order_by(DatasetStock.seq.asc())
            .all()
        )
        if rows:
            codes = [r.code for r in rows]
        else:
            try:
                codes = _resolve_codes(db, ds.universe, 0, None)
            except ValueError as e:
                logger.warning(
                    "数据集 %s 无成分记录，按 universe=%s 重建失败: %s",
                    dataset_id,
                    ds.universe,
                    str(e)[:100],
                )
                return None
            if not codes:
                logger.warning(
                    "数据集 %s 无成分记录且 universe=%s 无可用股票",
                    dataset_id,
                    ds.universe,
                )
                return None
        # 候选池：先取全部合格 K 线（含日期序列），再做日期对齐；
        # 并行拉取（与 build_dataset 同款线程池），结果按 codes 顺序归并
        n_workers = _build_workers()
        picked: dict[str, pd.DataFrame] = {}
        if n_workers <= 1:
            for code in codes:
                r = _fetch_kline(code, ds.start_date, ds.end_date)
                if r is not None:
                    picked[r[0]] = r[1]
        else:
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = {
                    pool.submit(_fetch_kline, c, ds.start_date, ds.end_date): c
                    for c in codes
                }
                try:
                    for fut in as_completed(futures):
                        r = fut.result()
                        if r is not None:
                            picked[r[0]] = r[1]
                except BaseException:
                    for f in futures:
                        f.cancel()
                    raise
        candidates: list[tuple[str, pd.DataFrame]] = []
        for code in codes:
            k = picked.get(code)
            if k is None:
                continue
            candidates.append((code, k.reset_index(drop=True)))
        if not candidates:
            return None
        # T-07: 估值特征（pe/pb/ps/market_cap/float_cap/turnover/industry）由每日快照
        # 注入（按交易日对齐、非快照日 NaN）；快照不可用时不注入，align_panel 缺失
        # 列容错为整列 NaN。industry 注入整数编码（单一编码表，0=未知）。
        est_cols = [f for f in want if f in ESTIMATE_FEATURES or f == INDUSTRY_FEATURE]
        if est_cols:
            try:
                spot = get_spot()
            except Exception as e:
                logger.warning(
                    "面板 ds=%s 估值快照获取失败，估值特征记缺失 NaN: %s",
                    dataset_id,
                    str(e)[:100],
                )
                spot = None
            candidates = _inject_estimate_features(candidates, est_cols, spot)
        # 日期对齐 + 特征矩阵构造（纯计算，见 lib/alpha/features.py align_panel）
        aligned_result = align_panel(candidates, want)
        if aligned_result is None:
            return None
        out_panel = aligned_result["panel"]
        dates = aligned_result["dates"]
        good_codes = aligned_result["good_codes"]
        result = {
            "panel": out_panel,
            "dates": dates,
            "stock_count": aligned_result["stock_count"],
        }
        # 全量请求写无后缀文件（覆盖旧缓存，兼容历史）；特征子集请求写指纹文件
        save_features = None if set(want) == set(ALL_FEATURES) else want
        _save_panel_disk(
            dataset_id, out_panel, dates, good_codes, features=save_features
        )
        out = {
            "panel": {f: result["panel"][f] for f in want},
            "dates": result["dates"],
            "codes": aligned_result["good_codes"],
            "stock_count": result["stock_count"],
        }
        panel_cache.set(key, out)
        return out
    finally:
        db.close()
