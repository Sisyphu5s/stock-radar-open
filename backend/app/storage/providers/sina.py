"""新浪财经网络适配器（bt-scan,storage/providers）。

SinaFinancialsMixin + SinaProvider：全市场快照（含 PE/PB/市值）、多周期 K 线、
新闻、财务摘要。纯网络 I/O，无业务状态；多源竞速/冷却/回退策略在 core/sources.py。

依赖注入约定：磁盘快照回退（_spot_disk_fallback/save_snapshot_disk）经
..snapshots 直连（storage 层内，H1a 下沉）；周/月重采样 _resample_period 经
.base 直连；K 线成交量校验已上提 core/sources 门面（provider 不再校验，H1b）；
交易时段 TTL / now_cn 经 lib.session 直连（纯计算）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd

from ...config import settings
from ...lib.codes import bare_code as _bare
from .base import (
    PERIOD_SCALE,
    SESSION,
    SPOT_TIMEOUT_S,
    QuoteProvider,
    _resample_period,
    _sina_symbol,
    _slice_kline,
)

logger = logging.getLogger("stockradar.quote")


class SinaFinancialsMixin:
    """新浪财务摘要（供 SinaProvider 复用，同一数据源）。"""

    def get_financials(self, code: str) -> dict | None:
        from .akshare import AkshareProvider

        return AkshareProvider().get_financials(code)


class SinaProvider(SinaFinancialsMixin, QuoteProvider):
    """新浪财经：全市场快照（含 PE/PB/市值）、多周期 K 线、新闻、财务摘要。"""

    name = "sina"

    def __init__(self):
        self._list_url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData"
        )
        self._kline_url = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
        self._news_url = "https://feed.mix.sina.com.cn/api/roll/get"
        self._spot_cache: dict[str, pd.DataFrame] = {}
        self._spot_ts = 0.0
        self._codes_cache: list[str] | None = None
        self._fetch_lock = threading.Lock()
        self._fetching = False

    # ---------- 全市场快照 ----------
    def _fetch_page(self, page: int, num: int = 100, retries: int = 2) -> list[dict]:
        params = {"page": page, "num": num, "sort": "symbol", "asc": 1, "node": "hs_a"}
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                r = SESSION.get(self._list_url, params=params, timeout=SPOT_TIMEOUT_S)
                if r.status_code == 456:  # 限流：快速跳过该页
                    return []
                r.raise_for_status()
                return json.loads(r.text or "[]")
            except Exception as e:
                last_err = e
                if attempt < retries:
                    time.sleep(0.3)
        raise last_err

    def get_spot(self, refresh: bool = False) -> pd.DataFrame:
        from ..snapshots import _spot_disk_fallback, save_snapshot_disk
        from ...lib.session import session_spot_ttl

        ttl = session_spot_ttl(settings.quote_cache_ttl)
        now = time.time()
        if not refresh and self._spot_ts and now - self._spot_ts < ttl:
            return self._spot_cache.get("all", pd.DataFrame())
        # 并发去重：已有抓取在进行中 → 直接返回最近成功快照（避免每次轮询都触发全量）
        if not refresh and self._fetching:
            if self._spot_ts and now - self._spot_ts < 600:
                return self._spot_cache.get("all", pd.DataFrame())
        with self._fetch_lock:
            if self._fetching:
                return self._spot_cache.get("all", pd.DataFrame())
            self._fetching = True
        try:
            rows: list[dict] = []
            last_err: Exception | None = None
            # 单轮抓取（4 workers + 页间延迟），失败页跳过，避免限流阻塞
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {
                    pool.submit(self._fetch_page, p, retries=1): p for p in range(1, 61)
                }
                for fut in as_completed(futures):
                    try:
                        page_rows = fut.result()
                        if page_rows:
                            rows.extend(page_rows)
                    except Exception as e:
                        last_err = e
                        logger.debug(
                            "新浪列表页 %s 失败: %s", futures[fut], str(e)[:80]
                        )
            if not rows:
                # 拉取失败：优先内存（10 分钟内），其次磁盘快照（30 分钟内，离线兜底）
                if self._spot_ts and now - self._spot_ts < 600:
                    logger.warning(
                        "新浪快照刷新失败，使用 %ds 前缓存", int(now - self._spot_ts)
                    )
                    return self._spot_cache.get("all", pd.DataFrame())
                disk = _spot_disk_fallback("sina", now)
                if disk is not None:
                    return disk
                raise RuntimeError(f"新浪全市场列表为空: {str(last_err)[:100]}")
            df = pd.DataFrame(rows)
            df = df[df["symbol"].str.startswith(("sh", "sz", "bj"))]
            out = pd.DataFrame(
                {
                    "code": df["symbol"].map(lambda s: f"{s[2:]}.{s[:2].upper()}"),
                    "name": df["name"].astype(str),
                    "price": pd.to_numeric(df["trade"], errors="coerce").fillna(0.0),
                    "pct_change": pd.to_numeric(
                        df["changepercent"], errors="coerce"
                    ).fillna(0.0),
                    "volume": (
                        pd.to_numeric(df["volume"], errors="coerce").fillna(0) / 100
                    ),  # 股→手
                    "amount": pd.to_numeric(df["amount"], errors="coerce").fillna(0.0),
                    "turnover_rate": pd.to_numeric(
                        df["turnoverratio"], errors="coerce"
                    ).fillna(0.0),
                    "pe": pd.to_numeric(df["per"], errors="coerce").fillna(0.0),
                    "pb": pd.to_numeric(df["pb"], errors="coerce").fillna(0.0),
                    "market_cap": pd.to_numeric(df["mktcap"], errors="coerce").fillna(
                        0.0
                    )
                    / 1e4,  # 万→亿
                    "float_cap": pd.to_numeric(df["nmc"], errors="coerce").fillna(0.0)
                    / 1e4,
                    "industry": "",
                    "source": "sina",
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                }
            )
            self._spot_cache["all"] = out
            self._spot_ts = now
            save_snapshot_disk("sina", out)
            return out
        finally:
            self._fetching = False

    # ---------- K 线（多周期） ----------
    def get_kline(
        self,
        code: str,
        period: str = "daily",
        start_date: str | None = None,
        end_date: str | None = None,
        days: int | None = None,
        compose_intraday: bool = False,
    ) -> pd.DataFrame:
        if period in ("weekly", "monthly"):
            daily = self.get_kline(
                code,
                "daily",
                start_date,
                end_date,
                days=days or 800,
                compose_intraday=compose_intraday,
            )
            return _resample_period(daily, period)
        scale = PERIOD_SCALE.get(period, 240)
        datalen = 1600 if scale == 240 else 800
        symbol = _sina_symbol(code)
        if symbol.startswith("bj"):
            return self._kline_qq_fallback(
                code, period, start_date, end_date, days
            )  # 北交所走腾讯
        # P2-9 盘中增量短路：kcache 单只路径（partial 命中/缓存最后 bar=昨日）的增量
        # 请求是 start_date=今日 + days 未指定 + compose_intraday=True，期望结果只有
        # 今日 bar——而新浪日线接口不支持服务端 start_date，实际会全量下载 1600 根。
        # 命中时只拉当日 1 分钟线聚合今日 bar（_compose_today_bar 含盘中窗口/三道
        # 防御/spot 校验，与 _compose_intraday_daily 同口径），免日线全量重拉。
        # 未命中回落完整日线拉取：收盘后（窗口外 → bar 为 None）拿真实 bar 覆盖
        # partial；长假缺口（start_date < 今日）、周/月透传（days=800）、冷启动
        # （days=800）不受影响。
        if scale == 240 and compose_intraday and days is None and start_date:
            from ...lib.session import now_cn

            today = now_cn().date().isoformat()  # 上海今日(YYYY-MM-DD)
            if start_date == today:
                bar = self._compose_today_bar(code, today)
                if bar is not None:
                    return pd.DataFrame([{"date": today, **bar, "pct_change": 0.0}])
        params = {"symbol": symbol, "scale": scale, "ma": "no", "datalen": datalen}
        # 限流/网络异常快速失败：单次重试后静默返回空，由上层缓存/跳过处理
        for attempt in range(2):
            try:
                r = SESSION.get(self._kline_url, params=params, timeout=6)
                if r.status_code == 456:
                    logger.debug("新浪K线限流 %s/%s", code, period)
                    return pd.DataFrame()
                r.raise_for_status()
                data = json.loads(r.text or "[]")
                if not data:
                    return pd.DataFrame()
                break
            except Exception as e:
                logger.debug("新浪K线失败 %s/%s: %s", code, period, str(e)[:80])
                if attempt == 0:
                    time.sleep(0.3)
                else:
                    return pd.DataFrame()
        df = pd.DataFrame(data)
        df["date"] = df["day"]
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["volume"] = df["volume"] / 100  # 股→手
        if "amount" in df.columns:
            df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
        else:
            df["amount"] = 0.0
        df["pct_change"] = df["close"].pct_change().fillna(0) * 100
        df = df.drop(columns=["day"], errors="ignore")
        if scale == 240 and compose_intraday:
            df = self._compose_intraday_daily(df, code)
        return _slice_kline(df, start_date, end_date, days)

    def _compose_today_bar(self, code: str, today: str) -> dict | None:
        """拉当日 1 分钟线聚合今日 bar（open/close/high/low/volume/amount）。

        仅盘中窗口（上海 09:30 ≤ t < 15:05，与 partial 判定同口径）合成；聚合后经
        三道防御校验 + spot 实时价一致性比对，任一失败返回 None（调用方静默回退）。

        供两处复用：
        - _compose_intraday_daily：全量日线缺当日 bar 时 concat 追加；
        - get_kline 盘中增量短路（P2-9）：partial 命中时只拉分钟线原地更新当日 bar。
        """
        from ...lib.session import now_cn

        try:
            now = now_cn()
            hm = now.hour * 60 + now.minute
            if not (9 * 60 + 30 <= hm < 15 * 60 + 5):
                return None  # 非盘中窗口（09:30 ≤ t < 15:05，与 partial 判定同口径）
            minute = self.get_kline(code, "1", days=800)  # scale=1 分支,无递归
            if minute is None or not len(minute):
                return None
            day_col = minute["date"].astype(str)
            today_min = minute[day_col.str.startswith(today)]
            if today_min.empty:
                return None
            o = float(today_min["open"].iloc[0])
            c = float(today_min["close"].iloc[-1])
            h = float(today_min["high"].max())
            l = float(today_min["low"].min())
            v = float(today_min["volume"].sum())
            a = (
                float(today_min["amount"].sum())
                if "amount" in today_min.columns
                else 0.0
            )
            # 防御 1:OHLC 结构一致性
            if h < max(o, c) or l > min(o, c) or h < l:
                return None
            # 防御 3:amount/volume 交叉校验(防单位错乱;amount 缺失=0 时跳过,
            # 视为未知而非错乱——新浪 1 分钟接口本就不返回 amount)
            if v > 0 and a > 0:
                avg = a / (v * 100)
                if not (l * 0.8 <= avg <= h * 1.2):
                    return None
            # 一致性:与新浪实时价(spot)比对;spot 不可用/获取失败 → 跳过校验
            try:
                spot = self._spot_cache.get("all")
                if spot is None or spot.empty:
                    spot = self.get_spot()
                if spot is not None and not spot.empty:
                    row = spot[
                        spot["code"].str.startswith(_sina_symbol(code)[2:] + ".")
                    ]
                    if not row.empty:
                        price = float(row.iloc[0]["price"])
                        if price > 0 and abs(c - price) > 0.01:
                            return None
            except Exception:
                pass  # spot 校验失败不阻塞主路径
            return {
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": v,
                "amount": a,
            }
        except Exception:
            return None  # 分钟线拉取失败/异常 → 静默回退

    def _compose_intraday_daily(self, df: pd.DataFrame, code: str) -> pd.DataFrame:
        """盘中日K缺当日 bar 时,用当日 1 分钟线聚合出当日 bar(仅 daily)。

        判定 → 拉分钟线 → 聚合 → 三道防御校验 + spot 一致性比对,任一失败
        静默返回原 df;成功则 concat 并重算 pct_change(与 kcache merge 同口径)。

        新浪日K接口盘中不返回当日 bar(最后 bar 停在昨日),而 1 分钟线盘中
        可用且含今日;收盘后由真实数据覆盖(partial 机制在 kcache 层处理)。
        聚合/防御/spot 校验实现在 _compose_today_bar(供 P2-9 盘中增量短路复用)。
        """
        from ...lib.session import now_cn

        if df is None or not len(df):
            return df
        try:
            today = now_cn().date().isoformat()  # 上海今日(YYYY-MM-DD)
            if str(df["date"].iloc[-1]) >= today:
                return df  # 已有当日 bar → 无需合成
        except Exception:
            return df
        bar = self._compose_today_bar(code, today)
        if bar is None:
            return df  # 非盘中窗口/分钟线失败/防御不过 → 静默回退原 df
        # 防御 2:合成 date 单调递增(晚于原 df 最后日期)
        if today <= str(df["date"].iloc[-1]):
            return df
        composed = pd.concat(
            [
                df,
                pd.DataFrame([{"date": today, **bar, "pct_change": 0.0}]),
            ],
            ignore_index=True,
        )
        composed["pct_change"] = composed["close"].pct_change().fillna(0) * 100
        return composed

    def _kline_qq_fallback(self, code: str, period: str, start_date, end_date, days):
        """北交所 K 线走腾讯 fqkline 接口。"""
        from .tencent import TencentProvider

        return TencentProvider().get_kline(code, period, start_date, end_date, days)

    # ---------- 新闻 ----------
    def get_news(self, code: str, limit: int = 10) -> list[dict]:
        """新浪财经滚动新闻 + 股票名/代码过滤（新浪无个股级过滤参数）。"""
        params = {"pageid": 153, "lid": "2517", "num": 40, "page": 1, "r": 0.5}
        try:
            r = SESSION.get(self._news_url, params=params, timeout=15)
            r.raise_for_status()
            data = json.loads(r.text or "{}").get("result", {}).get("data", [])
        except Exception:
            return []
        # 股票名（来自最近快照缓存，避免额外请求）
        name = ""
        try:
            spot = self._spot_cache.get("all")
            if spot is not None and not spot.empty:
                row = spot[spot["code"].str.startswith(_bare(code) + ".")]
                if not row.empty:
                    name = str(row.iloc[0]["name"])
        except Exception as e:
            logger.debug("从快照缓存取股票名失败 %s: %s", code, str(e)[:80])
        bare = _bare(code)
        out = []
        for item in data:
            title = (item.get("title") or "").replace("&nbsp;", " ").strip()
            if not title:
                continue
            if name and name not in title and bare not in title:
                continue
            if len(out) >= limit:
                break
            out.append(
                {
                    "title": title,
                    "url": item.get("url", ""),
                    "time": datetime.fromtimestamp(item.get("ctime", 0)).strftime(
                        "%Y-%m-%d %H:%M"
                    )
                    if item.get("ctime")
                    else "",
                    "source": "新浪财经",
                }
            )
        return out
