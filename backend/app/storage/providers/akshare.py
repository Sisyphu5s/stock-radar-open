"""akshare（东方财富 + 新浪多源）网络适配器（bt-scan,storage/providers）。

AkshareProvider：全市场快照（东财主源 + 腾讯备用）、多周期 K 线（分钟/日/周/月）、
新闻、业绩报表、财务摘要。纯网络 I/O，无业务状态；多源竞速/冷却/回退策略在
core/sources.py。

依赖注入约定：磁盘快照回退（_spot_disk_fallback/save_snapshot_disk）经
..snapshots 直连（storage 层内，H1a 下沉）；K 线成交量校验已上提 core/sources
门面（provider 不再校验，H1b）；交易时段 TTL 经 lib.session 直连（纯计算）；
_ak_retry 重试封装经 .base 直连。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from ...config import settings
from ...lib.codes import bare_code as _bare
from .base import (
    KLINE_TIMEOUT_S,
    SPOT_TIMEOUT_S,
    QuoteProvider,
    _ak_call,
    _ak_error,
    _ak_retry,
    _minute_window_minutes,
    _recent_quarters,
    _sina_symbol,
    _slice_kline,
)

logger = logging.getLogger("stockradar.quote")


class AkshareProvider(QuoteProvider):
    """akshare（东方财富 + 新浪多源）。"""

    name = "akshare"

    def __init__(self):
        self._spot_cache: pd.DataFrame | None = None
        self._spot_ts = 0.0
        self._fetch_lock = threading.Lock()
        self._fetching = False
        # T-73 指数快照：per-market 缓存（market -> 该市场行 DataFrame）与冷却
        self._indices_cache: dict[str, pd.DataFrame] = {}
        self._indices_ts: dict[str, float] = {}
        self._indices_blocked_until: dict[str, float] = {}
        self._indices_lock = threading.Lock()
        self._indices_fetching = False

    def get_spot(self, refresh: bool = False) -> pd.DataFrame:
        # 热缓存：交易时段用基础 TTL，非交易时段放大（数据不变化无需重复拉取）
        from .base import _ak_retry
        from ..snapshots import (
            _spot_disk_fallback,
            save_snapshot_disk,
        )
        from ...lib.session import session_spot_ttl

        ttl = session_spot_ttl(settings.quote_cache_ttl)
        now = time.time()
        if not refresh and self._spot_ts and now - self._spot_ts < ttl:
            return self._spot_cache if self._spot_cache is not None else pd.DataFrame()
        # 并发去重：已有抓取在进行中 → 直接返回最近成功快照（避免轮询触发重复全量）
        if not refresh and self._fetching:
            if self._spot_ts and now - self._spot_ts < 600:
                return (
                    self._spot_cache if self._spot_cache is not None else pd.DataFrame()
                )
        with self._fetch_lock:
            if self._fetching:
                return (
                    self._spot_cache if self._spot_cache is not None else pd.DataFrame()
                )
            self._fetching = True
        try:
            import akshare as ak

            t0 = time.time()
            try:
                raw = _ak_retry(
                    lambda: ak.stock_zh_a_spot_em(),
                    "get_spot",
                    attempts=2,
                    timeout=SPOT_TIMEOUT_S,
                )
                out = self._spot_from_em(raw)
            except Exception as e:
                # 主源（东财）失败 → 腾讯源备用（单次尝试，跨源兜底由调度层负责）
                logger.debug("东财快照失败，降级腾讯源: %s", str(e)[:120])
                try:
                    raw = _ak_retry(
                        lambda: ak.stock_zh_a_spot_tx(),
                        "get_spot_tx",
                        attempts=1,
                        timeout=SPOT_TIMEOUT_S,
                    )
                    out = self._spot_from_tx(raw)
                except Exception as e2:
                    # 全部失败：优先内存（10 分钟），其次磁盘快照（30 分钟内，离线兜底）
                    if self._spot_ts and now - self._spot_ts < 600:
                        logger.debug(
                            "akshare 快照刷新失败，使用 %ds 前缓存",
                            int(now - self._spot_ts),
                        )
                        return (
                            self._spot_cache
                            if self._spot_cache is not None
                            else pd.DataFrame()
                        )
                    disk = _spot_disk_fallback("akshare", now)
                    if disk is not None:
                        return disk
                    raise _ak_error(e2, "get_spot_tx") from e2
            if time.time() - t0 > 60:
                logger.warning(
                    "akshare get_spot 耗时异常(>60s): %.0fs", time.time() - t0
                )
            self._spot_cache = out
            self._spot_ts = now
            save_snapshot_disk("akshare", out)
            return out[
                [
                    "code",
                    "name",
                    "price",
                    "pct_change",
                    "volume",
                    "amount",
                    "turnover_rate",
                    "pe",
                    "pb",
                    "ps",
                    "market_cap",
                    "float_cap",
                    "industry",
                    "source",
                    "timestamp",
                ]
            ]
        finally:
            self._fetching = False

    # ===== 全球核心指数快照（T-73）=====
    # 市场级失败冷却（秒）：指数为展示通道，市场拉取失败后短冷却避免重复 8s 超时
    _INDICES_FAIL_COOLDOWN_S = 120

    def get_indices(self, refresh: bool = False) -> pd.DataFrame:
        """全球核心指数快照（8 只：A 股 4 / 美股 3 / 港股 1）。

        主源：东财 index_global_spot_em（一次全量 8 只全覆盖，含「最新行情时间」）；
        备源（东财失败或缺行）：新浪分市场——A 股 stock_zh_index_spot_sina、
        港股 stock_hk_index_spot_sina、美股 hq.sinajs.cn gb_* 实时通道（akshare
        美股指数接口仅日线，实时快照直连新浪 hq，实测 200 可用）。

        内部列：code/name/market/price/pct_change/currency/updated_at，
        固定 INDEXES 顺序 8 行；市场级失败保留旧缓存，无缓存该市场行 price 为 NaN、
        updated_at 为 None（API 层据此做 503 / 空降级）。per-market TTL 走
        lib.session.index_spot_ttl（交易时段 90s / 非交易时段 ×30）。
        """
        from ...lib.indices import INDEXES_BY_MARKET, MARKET_ORDER
        from ...lib.session import index_spot_ttl

        now = time.time()
        # 并发去重：已有拉取在进行中 → 直接返回最近成功快照（避免轮询触发重复全量）
        if not refresh and self._indices_fetching:
            return self._indices_merged()
        with self._indices_lock:
            if self._indices_fetching:
                return self._indices_merged()
            self._indices_fetching = True
        try:
            need: list[str] = []
            for m in MARKET_ORDER:
                cached = self._indices_cache.get(m)
                ttl = index_spot_ttl(settings.quote_cache_ttl, m)
                fresh = cached is not None and now - self._indices_ts.get(m, 0.0) < ttl
                cooling = now < self._indices_blocked_until.get(m, 0.0)
                if not refresh and (fresh or (cooling and cached is not None)):
                    continue
                need.append(m)
            if need:
                # 主源：东财全球指数一次全量（8 只全覆盖，含最新行情时间）
                em: dict[str, dict] | None = None
                try:
                    import akshare as ak

                    raw = _ak_retry(
                        lambda: ak.index_global_spot_em(),
                        "get_indices",
                        attempts=1,  # 指数为展示通道：失败立即降级新浪，不堆积重试
                        timeout=SPOT_TIMEOUT_S,
                    )
                    em = self._indices_from_em_global(raw)
                except Exception as e:
                    logger.warning(
                        "东财全球指数失败，降级新浪: %s",
                        str(_ak_error(e, "get_indices"))[:120],
                    )
                for m in need:
                    specs = INDEXES_BY_MARKET[m]
                    df_m: pd.DataFrame | None = None
                    if em is not None and all(s.code in em for s in specs):
                        df_m = self._indices_market_df(m, em)
                    if df_m is None:
                        # 东财失败/缺行 → 新浪分市场降级
                        try:
                            df_m = self._indices_fetch_sina_market(m)
                        except Exception as e:
                            logger.warning("新浪指数 %s 失败: %s", m, str(e)[:120])
                            self._indices_blocked_until[m] = (
                                now + self._INDICES_FAIL_COOLDOWN_S
                            )
                            df_m = self._indices_cache.get(m)
                        if df_m is None:
                            df_m = self._indices_market_df(m, None)  # 空行占位
                    self._indices_cache[m] = df_m
                    self._indices_ts[m] = now
            return self._indices_merged()
        finally:
            self._indices_fetching = False

    def _indices_merged(self) -> pd.DataFrame:
        """合并各市场缓存为固定 INDEXES 顺序 DataFrame（无缓存市场用空行占位）。"""
        from ...lib.indices import MARKET_ORDER

        parts = []
        for m in MARKET_ORDER:
            df = self._indices_cache.get(m)
            parts.append(df if df is not None else self._indices_market_df(m, None))
        return pd.concat(parts, ignore_index=True)

    @classmethod
    def _indices_market_df(
        cls, market: str, em_rows: dict[str, dict] | None
    ) -> pd.DataFrame:
        """把市场内指数行组装为内部列 DataFrame（固定该市场指数数）。

        em_rows: spec.code -> {"price", "pct", "updated_at"}；缺失指数行空值。
        """
        from ...lib.indices import INDEXES_BY_MARKET

        rows = []
        for spec in INDEXES_BY_MARKET[market]:
            row = em_rows.get(spec.code) if em_rows else None
            rows.append(
                {
                    "code": spec.code,
                    "name": spec.name,
                    "market": spec.market,
                    "price": row["price"] if row else np.nan,
                    "pct_change": row["pct"] if row else np.nan,
                    "currency": spec.currency,
                    "updated_at": row["updated_at"] if row else None,
                }
            )
        return pd.DataFrame(rows)

    @staticmethod
    def _indices_from_em_global(raw: pd.DataFrame) -> dict[str, dict]:
        """东财全球指数（index_global_spot_em）→ {spec.code: {price,pct,updated_at}}。

        代码精确匹配优先（em_global：000001/399001/399006/000300/HSI/DJIA/NDX/SPX），
        名称兜底（接口字段/代码格式变动时不因单只指数缺失而全链路失败）。
        涨跌幅为东财返回原值（该接口内部已 ÷100）。
        """
        from ...lib.indices import INDEXES

        out: dict[str, dict] = {}
        for spec in INDEXES:
            if not spec.em_global:
                continue
            sub = raw[raw["代码"].astype(str) == spec.em_global]
            if sub.empty:
                sub = raw[raw["名称"].astype(str) == spec.name]
            if sub.empty:
                continue
            r = sub.iloc[0]
            ts = r.get("最新行情时间")
            out[spec.code] = {
                "price": float(r["最新价"]),
                "pct": float(r["涨跌幅"]),
                "updated_at": (
                    str(ts)
                    if ts is not None and str(ts) not in ("", "NaT", "nan")
                    else None
                ),
            }
        return out

    def _indices_fetch_sina_market(self, market: str) -> pd.DataFrame:
        """新浪降级通道（按市场）：
        CN=stock_zh_index_spot_sina；HK=stock_hk_index_spot_sina；
        US=hq.sinajs.cn gb_* 实时通道（akshare 美股指数接口仅日线，实时走新浪 hq）。
        """
        from .base import SESSION

        import akshare as ak

        if market == "CN":
            raw = _ak_retry(
                lambda: ak.stock_zh_index_spot_sina(),
                "get_indices_sina_cn",
                attempts=1,
                timeout=SPOT_TIMEOUT_S,
            )
            return self._indices_from_sina_cn(raw)
        if market == "HK":
            raw = _ak_retry(
                lambda: ak.stock_hk_index_spot_sina(),
                "get_indices_sina_hk",
                attempts=1,
                timeout=SPOT_TIMEOUT_S,
            )
            return self._indices_from_sina_hk(raw)
        # US：新浪 hq 实时（gb_dji,gb_ixic,gb_inx），SESSION 已带新浪 Referer
        from ...lib.indices import INDEXES_BY_MARKET

        syms = ",".join(s.sina_us_hq for s in INDEXES_BY_MARKET["US"] if s.sina_us_hq)
        text = _ak_call(
            lambda: (
                SESSION.get(
                    f"https://hq.sinajs.cn/list={syms}", timeout=SPOT_TIMEOUT_S
                ).text
            ),
            timeout=SPOT_TIMEOUT_S,
        )
        return self._indices_from_sina_us_hq(text)

    @classmethod
    def _indices_from_sina_cn(cls, raw: pd.DataFrame) -> pd.DataFrame:
        """新浪 A 股指数（stock_zh_index_spot_sina，代码 sh000001 等）→ 内部列。

        新浪 A 股快照无时间列，updated_at 记拉取时刻（数据新鲜度口径）。
        """
        from ...lib.indices import INDEXES_BY_MARKET

        now_iso = datetime.now().isoformat(timespec="seconds")
        em_rows: dict[str, dict] = {}
        for spec in INDEXES_BY_MARKET["CN"]:
            sub = raw[raw["代码"].astype(str) == spec.sina_cn]
            if sub.empty:
                sub = raw[raw["名称"].astype(str) == spec.name]
            if sub.empty:
                continue
            r = sub.iloc[0]
            em_rows[spec.code] = {
                "price": float(r["最新价"]),
                "pct": float(r["涨跌幅"]),
                "updated_at": now_iso,
            }
        return cls._indices_market_df("CN", em_rows)

    @classmethod
    def _indices_from_sina_hk(cls, raw: pd.DataFrame) -> pd.DataFrame:
        """新浪港股指数（stock_hk_index_spot_sina，代码 HSI 等）→ 内部列。

        新浪港股快照无时间列，updated_at 记拉取时刻（数据新鲜度口径）。
        """
        from ...lib.indices import INDEXES_BY_MARKET

        now_iso = datetime.now().isoformat(timespec="seconds")
        em_rows: dict[str, dict] = {}
        for spec in INDEXES_BY_MARKET["HK"]:
            sub = raw[raw["代码"].astype(str) == spec.sina_hk]
            if sub.empty:
                sub = raw[raw["名称"].astype(str) == spec.name]
            if sub.empty:
                continue
            r = sub.iloc[0]
            em_rows[spec.code] = {
                "price": float(r["最新价"]),
                "pct": float(r["涨跌幅"]),
                "updated_at": now_iso,
            }
        return cls._indices_market_df("HK", em_rows)

    @classmethod
    def _indices_from_sina_us_hq(cls, text: str) -> pd.DataFrame:
        """新浪美股实时（hq.sinajs.cn gb_*）→ 内部列。

        实测格式：var hq_str_gb_dji="道琼斯,53772.8008,-0.12,2026-08-15 02:40:24,-67.19,...";
        分割字段：[0]=名称 [1]=现价 [2]=涨跌幅% [3]=北京时间戳（UTC+8）。
        """
        from ...lib.indices import INDEXES_BY_MARKET

        em_rows: dict[str, dict] = {}
        for spec in INDEXES_BY_MARKET["US"]:
            key = f'hq_str_{spec.sina_us_hq}="'
            if key not in text:
                continue
            seg = text.split(key, 1)[1].split('";', 1)[0]
            parts = seg.split(",")
            if len(parts) < 4 or not parts[1].strip():
                continue
            try:
                price = float(parts[1])
                pct = float(parts[2])
            except ValueError:
                continue
            em_rows[spec.code] = {
                "price": price,
                "pct": pct,
                "updated_at": parts[3].strip() or None,
            }
        return cls._indices_market_df("US", em_rows)

    @staticmethod
    def _spot_from_em(raw: pd.DataFrame) -> pd.DataFrame:
        """东财快照 → 内部列。"""
        try:
            out = pd.DataFrame()
            out["code"] = raw["代码"].astype(str) + np.where(
                raw["代码"].astype(str).str.startswith(("6", "9")),
                ".SH",
                np.where(
                    raw["代码"].astype(str).str.startswith(("4", "8")), ".BJ", ".SZ"
                ),
            )
            out["name"] = raw["名称"].astype(str)
            out["price"] = pd.to_numeric(raw["最新价"], errors="coerce").fillna(0.0)
            out["pct_change"] = pd.to_numeric(raw["涨跌幅"], errors="coerce").fillna(
                0.0
            )
            out["volume"] = pd.to_numeric(raw["成交量"], errors="coerce").fillna(0.0)
            out["amount"] = pd.to_numeric(raw["成交额"], errors="coerce").fillna(0.0)
            out["turnover_rate"] = pd.to_numeric(raw["换手率"], errors="coerce").fillna(
                0.0
            )
            # T-17: 量比（东财快照「量比」列；缺列防御——T-17 选股器消费该字段）
            if "量比" in raw.columns:
                out["volume_ratio"] = pd.to_numeric(
                    raw["量比"], errors="coerce"
                ).fillna(0.0)
            else:
                out["volume_ratio"] = 0.0
            out["pe"] = pd.to_numeric(
                raw.get("市盈率-动态", 0), errors="coerce"
            ).fillna(0.0)
            out["pb"] = pd.to_numeric(raw.get("市净率", 0), errors="coerce").fillna(0.0)
            # T-07: 市销率（东财快照无此列 → NaN，缺失由调用方面板构建记 NaN）
            out["ps"] = pd.to_numeric(raw.get("市销率", np.nan), errors="coerce")
            out["market_cap"] = (
                pd.to_numeric(raw.get("总市值", 0), errors="coerce").fillna(0.0) / 1e8
            )
            out["float_cap"] = (
                pd.to_numeric(raw.get("流通市值", 0), errors="coerce").fillna(0.0) / 1e8
            )
            out["industry"] = ""
            out["source"] = "akshare"
            out["timestamp"] = datetime.now().isoformat(timespec="seconds")
        except KeyError as e:
            raise _ak_error(e, "get_spot", "字段缺失") from e
        return out

    @staticmethod
    def _spot_from_tx(raw: pd.DataFrame) -> pd.DataFrame:
        """腾讯源快照（stock_zh_a_spot_tx，列: code/name/zxj/zdf/volume/turnover/
        hsl/pe_ttm/pn/zsz/ltsz）→ 内部列。单位: 成交量=手, 成交额=万元, 市值=亿。"""
        try:
            raw = raw.dropna(subset=["code"])
            out = pd.DataFrame()
            codes = raw["code"].astype(str)
            bare = codes.str.split(".", expand=False).map(
                lambda s: s[0] if isinstance(s, list) else s
            )
            bare = bare.str.replace(r"^(sh|sz|bj)", "", regex=True)
            out["code"] = bare + np.where(
                bare.str.startswith(("6", "9")),
                ".SH",
                np.where(bare.str.startswith(("4", "8")), ".BJ", ".SZ"),
            )
            out["name"] = raw.get("name", "").astype(str)
            out["price"] = pd.to_numeric(raw.get("zxj", 0), errors="coerce").fillna(0.0)
            out["pct_change"] = pd.to_numeric(
                raw.get("zdf", 0), errors="coerce"
            ).fillna(0.0)
            out["volume"] = pd.to_numeric(raw.get("volume", 0), errors="coerce").fillna(
                0.0
            )
            out["amount"] = (
                pd.to_numeric(raw.get("turnover", 0), errors="coerce").fillna(0.0) * 1e4
            )
            out["turnover_rate"] = pd.to_numeric(
                raw.get("hsl", 0), errors="coerce"
            ).fillna(0.0)
            out["pe"] = pd.to_numeric(raw.get("pe_ttm", 0), errors="coerce").fillna(0.0)
            out["pb"] = pd.to_numeric(raw.get("pn", 0), errors="coerce").fillna(0.0)
            # T-07: 腾讯源无市销率列 → NaN
            out["ps"] = np.nan
            out["market_cap"] = pd.to_numeric(
                raw.get("zsz", 0), errors="coerce"
            ).fillna(0.0)
            out["float_cap"] = pd.to_numeric(
                raw.get("ltsz", 0), errors="coerce"
            ).fillna(0.0)
            out["industry"] = ""
            out["source"] = "akshare"
            out["timestamp"] = datetime.now().isoformat(timespec="seconds")
        except KeyError as e:
            raise _ak_error(e, "get_spot_tx", "字段缺失") from e
        return out

    def get_kline(
        self,
        code: str,
        period: str = "daily",
        start_date: str | None = None,
        end_date: str | None = None,
        days: int | None = None,
        compose_intraday: bool = False,
    ) -> pd.DataFrame:
        from .base import _ak_retry

        import akshare as ak

        bare = _bare(code)
        if period in ("1", "5", "15", "30", "60"):
            # 分钟线：按需取数窗口（官方签名第 2/3 参数为起止时间），
            # 避免每次从 1979-09-01 全量拉取拖垮性能
            days = days or 400
            need_min = _minute_window_minutes(days, period)
            start_date_arg = (datetime.now() - timedelta(minutes=need_min)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            end_date_arg = (datetime.now() + timedelta(days=1)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            raw = _ak_retry(
                lambda: ak.stock_zh_a_hist_min_em(
                    symbol=bare,
                    start_date=start_date_arg,
                    end_date=end_date_arg,
                    period=period,
                    adjust="qfq",
                ),
                "get_kline",
                code,
                attempts=1,  # 分钟线仅自选扫描（低功耗）：失败立即回退，不堆积重试
            )
            if raw is None or raw.empty:
                logger.debug("akshare 分钟线空数据 %s/%s", code, period)
                return pd.DataFrame()
            try:
                df = pd.DataFrame(
                    {
                        "date": raw["时间"].astype(str),
                        "open": pd.to_numeric(raw["开盘"], errors="coerce"),
                        "high": pd.to_numeric(raw["最高"], errors="coerce"),
                        "low": pd.to_numeric(raw["最低"], errors="coerce"),
                        "close": pd.to_numeric(raw["收盘"], errors="coerce"),
                        "volume": pd.to_numeric(raw["成交量"], errors="coerce"),
                        "amount": pd.to_numeric(
                            raw.get("成交额", 0), errors="coerce"
                        ).fillna(0.0),
                    }
                )
            except KeyError as e:
                raise _ak_error(e, "get_kline", code) from e
        else:
            per = {"daily": "daily", "weekly": "weekly", "monthly": "monthly"}.get(
                period, "daily"
            )
            start = (start_date or "").replace("-", "") or None
            if start is None and days:
                # start_date 缺省时按 days 反推（约 1.6× 日历日覆盖交易日），
                # 避免 1970 全量拉取拖垮大盘扫描
                start = (datetime.now() - timedelta(days=int(days) * 1.6)).strftime(
                    "%Y%m%d"
                )
            end = (end_date or "").replace("-", "") or None
            try:
                raw = _ak_retry(
                    lambda: ak.stock_zh_a_hist(
                        symbol=bare,
                        period=per,
                        adjust="qfq",
                        start_date=start or "19700101",
                        end_date=end or "20500101",
                        timeout=KLINE_TIMEOUT_S,
                    ),
                    "get_kline",
                    code,
                    attempts=1,  # 快速失败：主源不可达时立即降级腾讯，避免全市场扫描堆积重试
                )
                if raw is None or raw.empty:
                    raise RuntimeError("东财日线空数据")
                df = pd.DataFrame(
                    {
                        "date": raw["日期"].astype(str),
                        "open": pd.to_numeric(raw["开盘"], errors="coerce"),
                        "high": pd.to_numeric(raw["最高"], errors="coerce"),
                        "low": pd.to_numeric(raw["最低"], errors="coerce"),
                        "close": pd.to_numeric(raw["收盘"], errors="coerce"),
                        "volume": pd.to_numeric(raw["成交量"], errors="coerce"),
                        "amount": pd.to_numeric(raw["成交额"], errors="coerce").fillna(
                            0.0
                        ),
                    }
                )
            except Exception as e:
                # 主源（东财）失败 → 腾讯源备用（symbol 需带 sh/sz 前缀）
                logger.warning("东财日线失败，降级腾讯源 %s: %s", code, str(e)[:120])
                symbol = _sina_symbol(bare)
                raw = _ak_retry(
                    lambda: ak.stock_zh_a_hist_tx(
                        symbol=symbol,
                        start_date=start or "19000101",
                        end_date=end or "20500101",
                        adjust="qfq",
                        timeout=KLINE_TIMEOUT_S,
                    ),
                    "get_kline_tx",
                    code,
                    attempts=1,  # 备用源同样快速失败：仍失败则回退缓存，不阻塞扫描
                )
                if raw is None or raw.empty:
                    logger.debug("腾讯日线空数据 %s/%s", code, period)
                    return pd.DataFrame()
                try:
                    df = pd.DataFrame(
                        {
                            "date": raw["date"].astype(str),
                            "open": pd.to_numeric(raw["open"], errors="coerce"),
                            "high": pd.to_numeric(raw["high"], errors="coerce"),
                            "low": pd.to_numeric(raw["low"], errors="coerce"),
                            "close": pd.to_numeric(raw["close"], errors="coerce"),
                            "volume": (
                                pd.to_numeric(raw["volume"], errors="coerce").fillna(0)
                                / 100
                            ),  # 股→手
                            "amount": pd.to_numeric(
                                raw["amount"], errors="coerce"
                            ).fillna(0.0),
                        }
                    )
                except KeyError as e2:
                    raise _ak_error(e2, "get_kline_tx", code) from e2
        df["pct_change"] = df["close"].pct_change().fillna(0) * 100
        return _slice_kline(df, start_date, end_date, days)

    def get_news(self, code: str, limit: int = 10) -> list[dict]:
        """东财个股新闻（stock_news_em，当日最近 100 条）。"""
        import akshare as ak

        try:
            raw = _ak_call(lambda: ak.stock_news_em(symbol=_bare(code)))
            if raw is None or raw.empty:
                return []
        except Exception as e:
            logger.warning(
                "东财新闻失败 %s: %s", code, str(_ak_error(e, "get_news", code))[:120]
            )
            return []
        out = []
        for _, r in raw.head(limit).iterrows():
            out.append(
                {
                    "title": str(r.get("新闻标题", "")),
                    "url": str(r.get("新闻链接", "")),
                    "time": str(r.get("发布时间", "")),
                    "source": str(r.get("文章来源", "东方财富")),
                }
            )
        return out

    def get_earnings(self, code: str) -> dict | None:
        """业绩报表最新一期（东财 stock_yjbb_em 全市场按报告期）。

        报告期动态取最近 4 个季度末，第一个非空即用（新季度未发布时自动回落）。
        """
        import akshare as ak

        raw = None
        for d in _recent_quarters():
            try:
                raw = _ak_call(lambda d=d: ak.stock_yjbb_em(date=d))
            except Exception as e:
                logger.warning(
                    "业绩报表失败 %s: %s",
                    code,
                    str(_ak_error(e, "get_earnings", code))[:120],
                )
                return None
            if raw is not None and not raw.empty:
                break
        if raw is None or raw.empty:
            return None
        row = raw[raw["股票代码"].astype(str) == _bare(code)]
        if row.empty:
            return None
        r = row.iloc[0]

        def g(col):
            v = r.get(col)
            return (
                float(v)
                if v is not None and str(v) not in ("", "nan", "None")
                else None
            )

        return {
            "period": str(r.get("报告期", r.get("日期", ""))),
            "eps": g("每股收益"),
            "revenue_yoy": g("营业总收入-同比增长"),
            "net_profit_yoy": g("净利润-同比增长"),
            "roe": g("净资产收益率"),
            "gross_margin": g("销售毛利率"),
        }

    def get_financials(self, code: str) -> dict | None:
        """财务摘要（新浪 stock_financial_abstract）：最近两期关键指标。"""
        import akshare as ak

        try:
            raw = _ak_call(lambda: ak.stock_financial_abstract(symbol=_bare(code)))
        except Exception as e:
            logger.warning(
                "财务摘要失败 %s: %s",
                code,
                str(_ak_error(e, "get_financials", code))[:120],
            )
            return None
        if raw is None or raw.empty:
            return None
        # 列: [选项, 指标, 报告期1, 报告期2, ...] → 取最近两期
        cols = list(raw.columns)
        if len(cols) < 4:
            return None
        report_cols = cols[2:]
        last, prev = report_cols[-1], report_cols[-2] if len(report_cols) > 1 else None
        wanted = {
            "归母净利润",
            "营业总收入",
            "营业成本",
            "净利润",
            "净资产收益率",
            "总资产周转率",
            "每股净资产",
            "销售毛利率",
            "营业利润率",
        }
        out: dict = {"period": str(last), "prev_period": str(prev) if prev else ""}
        for _, r in raw.iterrows():
            idx = str(r.get("指标", "")).strip()
            if idx in wanted:
                out[idx] = str(r.get(last, ""))
                if prev:
                    out[f"{idx}@prev"] = str(r.get(prev, ""))
        return out if len(out) > 2 else None
