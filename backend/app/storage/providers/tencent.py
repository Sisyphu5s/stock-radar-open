"""腾讯行情网络适配器（bt-scan,storage/providers）。

TencentProvider：全市场快照（DB 代码列表 + 批量报价）+ 多周期 K 线。
纯网络 I/O，无业务状态；多源竞速/冷却/回退策略在 core/sources.py。

依赖注入约定：K 线成交量校验已上提 core/sources 门面（provider 不再校验，H1b）；
交易时段 TTL 经 lib.session 直连。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

import pandas as pd

from ...config import settings
from ...lib.codes import bare_code as _bare
from .base import SPOT_TIMEOUT_S, SESSION, QuoteProvider, _sina_symbol, _slice_kline

logger = logging.getLogger("stockradar.quote")


class TencentProvider(QuoteProvider):
    """腾讯行情：全市场快照（DB 代码列表 + 批量报价）+ 多周期 K 线 + 新闻。"""

    name = "tencent"

    def __init__(self):
        self._spot_cache: pd.DataFrame | None = None
        self._spot_ts = 0.0
        self._fetch_lock = threading.Lock()
        self._fetching = False

    # ---------- 全市场快照（qt.gtimg.cn 批量，每 50 只一批） ----------
    def get_spot(self, refresh: bool = False) -> pd.DataFrame:
        from ...lib.session import session_spot_ttl

        # TTL 与 Sina/Akshare 同口径：盘中 90s / 非交易时段基础值 ×30 放大
        ttl = session_spot_ttl(settings.quote_cache_ttl)
        now = time.time()
        if not refresh and self._spot_ts and now - self._spot_ts < ttl:
            return self._spot_cache if self._spot_cache is not None else pd.DataFrame()
        # 并发去重：已有抓取在进行中 → 直接返回最近成功快照（模式同 Sina/Akshare
        # Provider，避免每线程各打一轮 120 批请求的缓存击穿）
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
            from ..db import SessionLocal
            from ..models import Stock

            db = SessionLocal()
            try:
                codes = [s.code for s in db.query(Stock).limit(6000).all()]
            finally:
                db.close()
            if not codes:
                # 无股票列表（需新浪成功过一次）→ 报错由上层处理
                raise RuntimeError("腾讯源缺少股票代码列表（请先让新浪源成功运行一次）")

            rows: list[dict] = []
            now_str = datetime.now().isoformat(timespec="seconds")
            for i in range(0, len(codes), 50):
                batch = codes[i : i + 50]
                symbols = ",".join(_sina_symbol(c) for c in batch)
                try:
                    r = SESSION.get(
                        f"https://qt.gtimg.cn/q={symbols}", timeout=SPOT_TIMEOUT_S
                    )
                    text = r.content.decode("gbk", errors="ignore")
                except Exception as e:
                    logger.debug("腾讯快照批 %d 失败: %s", i, str(e)[:60])
                    continue
                for line in text.split(";"):
                    if '="' not in line:
                        continue
                    try:
                        f = line.split('="', 1)[1].rstrip('"').split("~")
                        if len(f) < 51:
                            continue
                        price = float(f[3] or 0)
                        pct = float(f[32] or 0)
                        if price <= 0 or abs(pct) > 100:  # 字段版本变化保护
                            continue
                        rows.append(
                            {
                                "code": f"{f[2]}.{'SH' if str(f[2]).startswith(('6', '9')) else 'BJ' if str(f[2]).startswith(('4', '8')) else 'SZ'}",
                                "name": f[1],
                                "price": price,
                                "pct_change": pct,
                                "volume": float(f[36] or 0),  # 手
                                "amount": float(f[37] or 0) * 1e4,  # 万元→元
                                "turnover_rate": float(f[38] or 0),
                                "pe": float(f[39] or 0),
                                "pb": float(f[46] or 0),
                                "market_cap": float(f[45] or 0),  # 亿
                                "float_cap": float(f[44] or 0),  # 亿
                                "industry": "",
                                "source": "tencent",
                                "timestamp": now_str,
                            }
                        )
                    except (ValueError, IndexError):
                        continue  # 坏行跳过（字段缺失/非数值）
            if not rows:
                raise RuntimeError("腾讯全市场快照为空")
            self._spot_cache = pd.DataFrame(rows)
            self._spot_ts = now
            # P2-49：成功拉取后落盘（离线兜底），与 sina/akshare 同口径——
            # 全源断网时 core/sources 磁盘兜底按 preference_order 遍历，缺
            # tencent.pkl 会让 tencent 源永无离线快照可用。
            from ..snapshots import save_snapshot_disk

            save_snapshot_disk("tencent", self._spot_cache)
            return self._spot_cache
        finally:
            self._fetching = False

    # ---------- K 线（多周期；1/5/15/30 分钟走新浪，60分/日/周/月走腾讯） ----------
    def get_kline(
        self,
        code: str,
        period: str = "daily",
        start_date: str | None = None,
        end_date: str | None = None,
        days: int | None = None,
        compose_intraday: bool = False,
    ) -> pd.DataFrame:
        if period in ("1", "5", "15", "30"):
            # 腾讯无 1/5/15/30 分钟历史 → 尝试新浪分钟
            from .sina import SinaProvider

            try:
                return SinaProvider().get_kline(
                    code, period, start_date, end_date, days
                )
            except Exception:
                return pd.DataFrame()
        bare = _bare(code)
        symbol = _sina_symbol(bare)  # 北交所(4/8 前缀) → bj 前缀
        freq = {"weekly": "week", "monthly": "month", "daily": "day", "60": "60m"}.get(
            period, "day"
        )
        url = f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get?param={symbol},{freq},,,{days or 500},qfq"
        try:
            r = SESSION.get(url, timeout=10)
            r.raise_for_status()
            payload = json.loads(r.text or "{}")
        except Exception:
            return pd.DataFrame()
        node = payload.get("data", {}).get(symbol, {})
        key = f"qfq{freq}" if f"qfq{freq}" in node else freq
        arr = node.get(key, [])
        if not arr:
            return pd.DataFrame()
        # 过滤含 dict 字段的行（腾讯对停牌/新股返回 dict 结构）
        clean = [
            x
            for x in arr
            if len(x) >= 6 and all(not isinstance(v, dict) for v in x[:7])
        ]
        if not clean:
            return pd.DataFrame()
        df = pd.DataFrame(
            [
                {
                    "date": str(x[0]),
                    "open": float(x[1]),
                    "close": float(x[2]),
                    "high": float(x[3]),
                    "low": float(x[4]),
                    "volume": float(x[5]),
                    "amount": float(x[6]) if len(x) > 6 else 0.0,
                }
                for x in clean
            ]
        )
        df["pct_change"] = df["close"].pct_change().fillna(0) * 100
        return _slice_kline(df, start_date, end_date, days)
