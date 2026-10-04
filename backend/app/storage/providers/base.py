"""行情数据源存储适配层:网络 I/O 直连的适配器基础(bt-scan)。

QuoteProvider 抽象基类 + 共享 HTTP session / 周期刻度 / 纯工具:
- 代码/周期/快照列纯工具(_sina_symbol/_slice_kline/_normalize_volume/...);
- akshare 网络调用封装(_ak_call/_ak_retry/_ak_error:硬超时 + 退避重试);
- 内置兜底股票池(_MOCK_STOCKS)。

归属约定(bt-scan,见 docs/API.md):
- 本目录 = 网络 I/O 直连的适配器,不承载业务状态;
- 多源竞速/冷却/回退策略/磁盘快照回退 → app/core/sources.py;
- 磁盘快照落盘/读取/离线兜底 → storage/snapshots.py(H1a 下沉);K 线成交量校验
  已上提 core/sources 门面(H1b);适配器不再反向 import core;交易时段 TTL
  经 lib.session 直连(纯计算);网络工具(_ak_call/_ak_retry 等)在本模块。

内部 DataFrame 列契约(快照):
    code, name, price, pct_change, volume(手), amount(元), turnover_rate,
    pe, pb, market_cap(亿), float_cap(亿), industry, source, timestamp
统一内部 DataFrame 列(K线):
    date, open, high, low, close, volume(手), amount(元), pct_change
周期: 1/5/15/30/60 (分钟), daily, weekly, monthly
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

from ...lib.codes import bare_code as _bare

logger = logging.getLogger("stockradar.quote")


class _ThreadLocalSession:
    """thread-local requests.Session 代理（P2-48）。

    requests.Session 非线程安全：连接池/headers/重定向等实例内部状态在并发
    复用下存在竞态。3 源竞速（src-race pool）、sina 页并行（4 workers）、
    akshare 超时兜底 daemon 线程同时打请求，共享同一 Session 有潜在串包/脏读。
    本代理按线程持有私有 Session：get/post 透明转发到当前线程的 Session，
    线程间连接池完全隔离、互不干扰；headers 每线程复制同一份默认值。

    兼容性：实例属性优先于类方法——测试 `monkeypatch.setattr(SESSION, "get",
    fake)` 直接替换实例方法仍生效（fake 不经线程转发，与 requests.Session
    行为一致）。requests.Session.request 的默认超时注入（_patch_requests_
    default_timeout）patch 的是类方法，对每线程私有实例同样生效。
    """

    def __init__(self, headers: dict | None = None) -> None:
        self._local = threading.local()
        self._headers = dict(headers or {})

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(self._headers)
            self._local.session = s
        return s

    def get(self, url: str, **kwargs):
        return self._session().get(url, **kwargs)

    def post(self, url: str, **kwargs):
        return self._session().post(url, **kwargs)

    def request(self, method: str, url: str, **kwargs):
        return self._session().request(method, url, **kwargs)

    def close(self) -> None:
        self._session().close()

    def __getattr__(self, name: str):
        # 其它属性（headers 等）转发到当前线程私有 Session
        return getattr(self._session(), name)


# P2-48：thread-local 代理替代模块级共享 Session——多线程（3 源竞速 + 页并行 +
# daemon 线程）各自持有私有连接池，消除 requests.Session 并发竞态，无需全局锁串行化。
SESSION = _ThreadLocalSession(
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://finance.sina.com.cn",
    }
)

# 网络超时分级(bt-scan 全站单一事实源,禁止散落字面量):
# - 快照 8s:全市场行情时效敏感,超时即判失败换源降级(akshare 内部慢通道同样受控,
#   由 core/sources 竞速/冷却机制吸收);
# - 历史 25s:K 线/财报等批量历史拉取重,给足时间(与 _ak_call 历史默认一致)。
SPOT_TIMEOUT_S = 8.0
KLINE_TIMEOUT_S = 25.0
# akshare 底层 requests 默认超时兜底（C13）：与旧 socket.setdefaulttimeout(10)
# 语义一致（连接+读各 10s），per-request 生效，天然线程安全。
_AK_REQUESTS_TIMEOUT_S = 10.0


# C13：akshare 底层几乎全部走 requests 且不传 timeout（全库 1116 处 requests
# 调用仅 39 处显式传参），旧实现用 socket.setdefaulttimeout（进程级全局副作用）
# 兜底，被迫用模块级锁串行化「set→fn→reset」整个临界区（P1-37 取舍）——
# 所有 akshare 通道（spot/kline/financials/capital/industry/hs300）进程内互斥，
# 扫描多 worker/预热/面板重建并发拉数据时后到请求全部排队，最坏 25s×N。
# requests 的 timeout 是每请求参数、线程安全：在 Session.request 未显式传
# timeout 时注入默认值，即可让全部 akshare 请求获得 TCP 层超时兜底，同时不再
# 触碰 socket 全局状态、无需锁，并发调用互不排队。项目自身 requests 调用
# （sina/tencent/core.sources）均显式传 timeout，不受注入影响。
def _patch_requests_default_timeout() -> None:
    """requests.Session.request 未显式传 timeout 时注入默认超时（幂等）。"""
    if getattr(requests.Session.request, "_ak_timeout_patched", False):
        return
    _orig = requests.Session.request

    def _request(self, method, url, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = _AK_REQUESTS_TIMEOUT_S
        return _orig(self, method, url, **kwargs)

    _request._ak_timeout_patched = True  # type: ignore[attr-defined]
    requests.Session.request = _request


_patch_requests_default_timeout()

PERIOD_SCALE = {"1": 1, "5": 5, "15": 15, "30": 30, "60": 60, "daily": 240}

# 交易时段换算：一天 24h 中仅 4h（240 分钟）交易，自然时间 = 交易分钟 × 6；
# ×1.5 再给午休/周末留余量（1 个自然日 ≈ 1.5 个交易日的自然时间）。
_MINUTE_WALL_FACTOR = 6.0 * 1.5

# ---------- 内置股票表（兜底股票池） ----------
_MOCK_STOCKS = [
    ("600519", "贵州茅台", "白酒"),
    ("000858", "五粮液", "白酒"),
    ("600809", "山西汾酒", "白酒"),
    ("000568", "泸州老窖", "白酒"),
    ("002304", "洋河股份", "白酒"),
    ("603288", "海天味业", "食品饮料"),
    ("600887", "伊利股份", "食品饮料"),
    ("300999", "金龙鱼", "食品饮料"),
    ("000333", "美的集团", "家电"),
    ("000651", "格力电器", "家电"),
    ("600036", "招商银行", "银行"),
    ("601398", "工商银行", "银行"),
    ("601939", "建设银行", "银行"),
    ("601288", "农业银行", "银行"),
    ("601988", "中国银行", "银行"),
    ("000001", "平安银行", "银行"),
    ("601318", "中国平安", "保险"),
    ("601628", "中国人寿", "保险"),
    ("600030", "中信证券", "券商"),
    ("600837", "海通证券", "券商"),
    ("601211", "国泰君安", "券商"),
    ("300059", "东方财富", "券商"),
    ("000002", "万科A", "房地产"),
    ("600048", "保利发展", "房地产"),
    ("601088", "中国神华", "煤炭"),
    ("600028", "中国石化", "石油石化"),
    ("601857", "中国石油", "石油石化"),
    ("601899", "紫金矿业", "有色金属"),
    ("603993", "洛阳钼业", "有色金属"),
    ("600111", "北方稀土", "有色金属"),
    ("600309", "万华化学", "化工"),
    ("002493", "荣盛石化", "化工"),
    ("600346", "恒力石化", "化工"),
    ("601012", "隆基绿能", "光伏"),
    ("600438", "通威股份", "光伏"),
    ("300274", "阳光电源", "光伏"),
    ("002594", "比亚迪", "新能源车"),
    ("300750", "宁德时代", "新能源车"),
    ("300014", "亿纬锂能", "锂电"),
    ("002460", "赣锋锂业", "锂电"),
    ("002466", "天齐锂业", "锂电"),
    ("000725", "京东方A", "电子"),
    ("000100", "TCL科技", "电子"),
    ("002475", "立讯精密", "电子"),
    ("002241", "歌尔股份", "电子"),
    ("603501", "韦尔股份", "半导体"),
    ("603986", "兆易创新", "半导体"),
    ("688256", "寒武纪", "半导体"),
    ("688041", "海光信息", "半导体"),
    ("688981", "中芯国际", "半导体"),
    ("688111", "金山办公", "软件"),
    ("600588", "用友网络", "软件"),
    ("002230", "科大讯飞", "软件"),
    ("300308", "中际旭创", "通信"),
    ("300502", "新易盛", "通信"),
    ("601138", "工业富联", "消费电子"),
    ("000063", "中兴通讯", "通信"),
    ("600031", "三一重工", "机械"),
    ("600585", "海螺水泥", "建材"),
    ("601668", "中国建筑", "建筑"),
    ("601390", "中国中铁", "建筑"),
    ("601800", "中国交建", "建筑"),
    ("002352", "顺丰控股", "物流"),
    ("002714", "牧原股份", "农业"),
    ("300498", "温氏股份", "农业"),
    ("600276", "恒瑞医药", "医药"),
    ("603259", "药明康德", "医药"),
    ("600436", "片仔癀", "医药"),
    ("000538", "云南白药", "医药"),
    ("300760", "迈瑞医疗", "医疗器械"),
    ("300015", "爱尔眼科", "医疗服务"),
    ("601888", "中国中免", "免税"),
    ("600900", "长江电力", "电力"),
    ("601601", "中国太保", "保险"),
]


class QuoteProvider:
    name = "base"

    def get_spot(self) -> pd.DataFrame:
        raise NotImplementedError

    def get_kline(
        self,
        code: str,
        period: str = "daily",
        start_date: str | None = None,
        end_date: str | None = None,
        days: int | None = None,
        compose_intraday: bool = False,
    ) -> pd.DataFrame:
        raise NotImplementedError

    def get_news(self, code: str, limit: int = 10) -> list[dict]:
        return []


def _sina_symbol(code: str) -> str:
    """600519.SH → sh600519；000001 → sz000001；8xxxxx → bj。"""
    bare = _bare(code)
    if bare.startswith(("6", "9")):
        return f"sh{bare}"
    if bare.startswith(("4", "8")):
        return f"bj{bare}"
    return f"sz{bare}"


def _ak_error(e: Exception, stage: str, code: str = "") -> RuntimeError:
    """akshare 调用失败分类: 网络/数据/结构三类，输出可排查信息。"""
    msg = str(e)[:200]
    if isinstance(e, (requests.ConnectionError, requests.Timeout, OSError)) or any(
        k in msg
        for k in (
            "Connection",
            "Timeout",
            "RemoteDisconnected",
            "timed out",
            "Max retries",
            "超时",
        )
    ):
        kind = "网络不可达"
    elif isinstance(e, KeyError):
        kind = "接口结构变更(缺少字段)"
    elif isinstance(e, ValueError):
        kind = "数据解析异常"
    else:
        kind = "未知异常"
    detail = f"{kind}: {stage}" + (f" {code}" if code else "") + f" -> {msg}"
    return RuntimeError(detail)


def _ak_call(fn, timeout: float = 25.0):
    """akshare 调用硬超时封装：网络卡死时由 daemon 线程兜底，超时抛 TimeoutError。

    C13：akshare 内部 requests 大多不传 timeout，TCP 层超时由本模块对
    requests.Session.request 注入默认值兜底（见 _patch_requests_default_timeout），
    不再依赖进程级 socket.setdefaulttimeout——旧实现的模块级串行锁随之移除，
    并发 akshare 调用互不排队。超时后 daemon 线程继续执行，至多悬挂到
    requests 默认超时（10s）自断，不阻塞进程退出。

    自建 daemon 线程 + concurrent.futures.Future：ThreadPoolExecutor 的 __exit__
    会等待任务结束导致硬超时失效且线程非 daemon；异常经 fut.set_exception
    原样回传调用方（而非仅 set_result 成功路径），避免异常被吞成超时。
    """
    from concurrent.futures import Future, TimeoutError as FutTimeout

    fut = Future()

    def _run():
        try:
            fut.set_result(fn())
        except BaseException as e:  # noqa: BLE001 - 异常原样回传调用方（含网络/解析错误）
            fut.set_exception(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    try:
        return fut.result(timeout=timeout)
    except FutTimeout:
        raise TimeoutError(f"akshare 调用超时(>{timeout:.0f}s)")


def _ak_retry(
    fn,
    stage: str,
    code: str = "",
    attempts: int = 4,
    timeout: float = KLINE_TIMEOUT_S,
):
    """akshare 调用封装: 失败重试（退避 0.5s/1.5s/3s），每次带硬超时，仍失败抛 _ak_error。

    timeout 按通道分级:快照传 SPOT_TIMEOUT_S(8s),历史保持默认 KLINE_TIMEOUT_S(25s)。
    """
    delays = (0.5, 1.5, 3.0)
    last_err: Exception | None = None
    for attempt in range(attempts):  # 首次 + (attempts-1) 次重试
        try:
            return _ak_call(fn, timeout=timeout)
        except Exception as e:
            last_err = e
            if attempt < attempts - 1:
                logger.warning(
                    "akshare %s 失败(第%d次重试): %s",
                    stage,
                    attempt + 1,
                    str(_ak_error(e, stage, code))[:120],
                )
                time.sleep(delays[min(attempt, len(delays) - 1)])
    raise _ak_error(last_err, stage, code) from last_err


def _recent_quarters(n: int = 4) -> list[str]:
    """最近 n 个季度末日期（YYYYMMDD），供业绩报表动态轮询（新季度未发布时回落上一期）。"""
    today = datetime.now()
    out: list[str] = []
    y, q = today.year, (today.month - 1) // 3  # q: 0=Q1 ... 3=Q4
    for i in range(n):
        qy, qq = y, q - i
        while qq < 0:
            qy -= 1
            qq += 4
        month = (qq + 1) * 3
        out.append(f"{qy}{month:02d}{30 if month in (6, 9) else 31}")
    return out


def _minute_window_minutes(days: int, period: str) -> int:
    """分钟线拉取窗口（自然时间分钟数）：`days` 根 bar × 周期分钟 = 所需交易分钟数，
    再按 24h/4h 交易时段比（×6）与午休/周末余量（×1.5）换算为自然时间，封顶一年。

    修正：旧实现 `days*240/period*period*1.5` 中 period 自消（240/period*period≡240），
    任意周期都拉成 days×360 分钟（days=400 → 100 天），60 分钟周期过拉、1 分钟周期
    足够但同样被拖到 100 天。现按 bar 周期换算：1/5/15/30/60 周期、days=400 时
    窗口分别约 2.5/12.5/37.5/75/150 天（覆盖 400 根 bar 所需交易时段且有余量）。

    下限守卫：1 分钟周期按换算仅 2.5 天窗口——长假（春节/国庆 7-9 天）后首次拉取
    或缓存清空重建时，窗口覆盖不到长假前的交易日，历史出现断档（1 分钟线无长假
    前数据）。下限 10 个自然日覆盖最长假，1 分钟 = 10 天 ≈ 2400 根 > min_depth(500)，
    且仅影响 1 分钟周期（其余周期换算本就 ≥ 12.5 天）。
    """
    MIN_WINDOW_MINUTES = 10 * 24 * 60
    window = int(days) * int(period) * _MINUTE_WALL_FACTOR
    return min(max(window, MIN_WINDOW_MINUTES), 60 * 24 * 365)


def _slice_kline(
    df: pd.DataFrame, start_date: str | None, end_date: str | None, days: int | None
) -> pd.DataFrame:
    """截取：start/end 日期区间，或最近 N 根（严格末尾 N 根）。"""
    if df is None or not len(df):
        return df
    if start_date and end_date:
        mask = (df["date"] >= start_date) & (df["date"] <= end_date)
    elif days:
        n = len(df)
        take = min(int(days), n)
        mask = df.index >= n - take
    elif start_date:
        # 单传 start_date（降级源增量拉取场景）：只保留 start 之后的数据，
        # 修复此前落入 else 分支返回全量的退化（增量退化为全量）
        mask = df["date"] >= start_date
    elif end_date:
        mask = df["date"] <= end_date
    else:
        mask = pd.Series(True, index=df.index)
    return df[mask].reset_index(drop=True)


def _resample_period(daily: pd.DataFrame, period: str) -> pd.DataFrame:
    """日线 → 周/月线。

    契约（信号时点）：周/月 bar 日期 = 组内最后一个真实交易日，当日收盘 15:00
    语义由 bar_market_time 统一负责。绝不使用 resample 的 W-FRI/ME 索引标签——
    当周/当月未结束时这类标签会指向未来日期（如 2026-07-31 周五所在周，用
    W-SUN/ME 标签会输出 2026-08-02/2026-08-31 之类未来非交易日）。因此日期取
    组内最后真实值（已按 _dt 升序 → 等价于 max(date)），输出层统一 strftime
    为 YYYY-MM-DD；列结构/升序排序保持既有契约。

    纯计算、无 I/O：由 core/sources.py 下沉至本模块（H1a 配套，消除 sina
    provider 对 core.sources 的反向 import）；core/sources 门面 re-export 兼容
    旧 import 面（core/scanning 与 tests 经 core.sources 引用）。
    """
    if daily is None or not len(daily):
        return daily
    df = daily.copy()
    df["_dt"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["_dt"]).sort_values("_dt")
    if not len(df):
        return pd.DataFrame(
            columns=[
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "pct_change",
            ]
        )
    rule = "W-FRI" if period == "weekly" else "ME"
    g = df.set_index("_dt").resample(rule)
    out = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
            "amount": g["amount"].sum(),
            "date": g[
                "date"
            ].last(),  # 组内最后一个真实交易日（禁用 resample 索引未来标签）
        }
    ).dropna()
    if not len(out):
        return pd.DataFrame(
            columns=[
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "pct_change",
            ]
        )
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    # 防御守卫：pandas 2.2+ 的 W 标签默认取组内最后日期、3.x 亦然，但不同版本的
    # closed/label 组合可能变化；无论标签语义如何，输出日期绝不允许晚于输入最大
    # 真实交易日（未来 period-end 日期一律剔除）。
    out = out[pd.to_datetime(out["date"]) <= df["_dt"].max()]
    out = out.reset_index(drop=True)
    out["pct_change"] = out["close"].pct_change().fillna(0) * 100
    return out[
        ["date", "open", "high", "low", "close", "volume", "amount", "pct_change"]
    ]


def _normalize_volume(vol: float, spot_vol: float | None) -> float:
    """成交量单位运行时校验（统一「手」）：与同股同刻 spot 快照量级比对，差 ~100 倍修正。

    假设：
    - spot 快照 volume 恒为「手」（各 provider 归一契约，见模块 docstring）；
    - 同一只股票同一天，日线当日 bar 成交量 ≈ spot 当日累计成交量（量级一致：
      盘后=全天总量，盘中=截至当前累计，同刻抓取接近）；
    - 仅双方 > 0 时校验，避免停牌/新股/0 值误判。

    校验逻辑（ratio = kline_vol / spot_vol）：
    - 0.5 ≤ ratio ≤ 2   → 单位一致，原样返回；
    - ratio > 50         → kline 偏大 ~100×（源把「股」当「手」返回）→ /100；
    - ratio < 1/50       → kline 偏小 ~100×（反向异常，如漏乘 100）→ ×100；
    - 其余中间量级       → 无法判定，原样返回（不臆改）。

    口径说明：盘中合成 bar（1 分钟线聚合）与收盘真实 bar 存在口径差——合成值
    只聚合到调用时刻，而 spot 为实时累计，尾盘最后几分钟内两者可差数倍；这类
    差异属于「时间基准差」而非「单位错误」，本校验只在 ≈100× 时修正，不收敛
    中间量级，避免把盘中正常差异误改。
    """
    if spot_vol is None or spot_vol <= 0 or vol <= 0:
        return vol
    ratio = vol / spot_vol
    if 0.5 <= ratio <= 2.0:
        return vol
    if ratio > 50.0:
        return vol / 100.0
    if ratio < 1 / 50.0:
        return vol * 100.0
    return vol
