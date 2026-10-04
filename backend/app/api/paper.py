"""模拟盘：指标信号实验与实时观察端点（纯计算，无持仓/资金模拟）+ 实验项目持久化。

- POST /api/v1/paper/experiment：历史指标信号实验（逐 bar 判定触发 + 收益统计）
- GET  /api/v1/paper/watch：实时指标观察（最新 bar 指标快照 + 命中信号）
- /api/v1/paper/projects*：多实验项目 CRUD + run（结果快照持久化到 paper_projects.result）
"""

from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..storage.db import get_db
from ..storage.models import (
    PaperAccount,
    PaperOrder,
    PaperPosition,
    PaperProject,
    PaperTrade,
    utcnow,
)
from ..storage.repos import paper as paper_repo
from ..core.paper.account import (
    equity_curve,
    prepare_kline,
    validate_order,
)
from ..lib.codes import normalize_code
from ..lib.timex import to_market_naive
from ..core.tasks.runner import cancel_paper_jobs, has_active_paper_job, submit
from ..core.tasks.errors import QueueFullError
from ..storage.klines import cached_kline
from ..storage.repos import stocks as stock_repo
from ..core.paper import (
    PAPER_MIN_BARS as MIN_BARS,
    PAPER_VALID_PERIODS as VALID_PERIODS,
    SUPPORTED_SIGNALS,
    SIGNAL_LABELS,
    SIGNAL_DESCRIPTIONS,
)
from ..core.paper import experiment as run_experiment
from ..core.paper import validate_signals
from ..core.paper import watch as run_watch
from ..lib.signals.engine import SIGNAL_TYPES

router = APIRouter(prefix="/paper", tags=["paper"])

logger = logging.getLogger("stockradar.paper")

# 观察项目快照限频：两次落库最小间隔（秒），防 15s 轮询高频写库
SNAPSHOT_MIN_INTERVAL_SEC = 60
# 多股项目股票数量上限
MAX_STOCKS = 20

# run 并发防重：同步端点跑在线程池，dict 读写必须加锁；锁不包住计算本身
_run_lock = threading.Lock()
_running: dict[int, bool] = {}

# experiment 项目 run 的「查重 + 提交」原子性：per-project 锁（单进程 uvicorn 场景下
# 消除 has_active_paper_job 查询与 submit 写库之间的竞态窗口，两个并发 run 只有一个通过）
_project_locks: dict[int, dict] = {}
_project_locks_guard = threading.Lock()


def _project_lock(pid: int) -> dict:
    """借出 per-project 并发锁条目（{lock, refs}）；用毕必须调 _release_project_lock 归还。

    refs 引用计数使无在借引用的条目在归还时即从 _project_locks 回收（P2-47：
    一次性项目 id 不再永久滞留）；并发等待期间条目保留，避免回收后新锁放行第二个 run。
    """
    with _project_locks_guard:
        entry = _project_locks.get(pid)
        if entry is None:
            entry = {"lock": threading.Lock(), "refs": 0}
            _project_locks[pid] = entry
        entry["refs"] += 1
        return entry


def _release_project_lock(pid: int, entry: dict) -> None:
    """归还 per-project 锁：无在借引用时从 _project_locks 移除（防慢泄漏）。"""
    with _project_locks_guard:
        entry["refs"] -= 1
        if entry["refs"] == 0:
            _project_locks.pop(pid, None)


# 分钟线 + 日线；周/月由日线重采样，与逐 bar 收益口径不一致，暂不支持。
# VALID_PERIODS / MIN_BARS 别名自 core/paper 单一事实源（PAPER_VALID_PERIODS /
# PAPER_MIN_BARS），api 层不再持有副本。


class ExperimentBody(BaseModel):
    code: str = Field(..., min_length=1, max_length=16)
    period: str = "daily"
    signals: list[str] = Field(..., min_length=1, max_length=10)
    days: int = Field(250, ge=1, le=800)


class ProjectCreate(BaseModel):
    """创建项目：kind/code/signals 必填；name 缺省为「未命名项目」。

    stocks 可选（多股批量）：提供时写关联表且 code 以 stocks[0] 为准（兼容字段）；
    未提供则单股（stocks=[code]），与旧行为一致。
    """

    name: str | None = Field(None, max_length=64)
    kind: str = "experiment"  # experiment/watch，创建后不可改
    code: str = Field(..., min_length=1, max_length=16)
    stocks: list[str] | None = Field(None, min_length=1, max_length=20)
    period: str = "daily"
    signals: list[str] = Field(..., min_length=1, max_length=10)
    days: int = Field(250, ge=1, le=800)


class ProjectPatch(BaseModel):
    """部分更新：仅 name/code/stocks/period/signals/days 可改；kind/result 不在其中，天然不可改。"""

    name: str | None = Field(None, min_length=1, max_length=64)
    code: str | None = Field(None, min_length=1, max_length=16)
    stocks: list[str] | None = Field(None, min_length=1, max_length=20)
    period: str | None = None
    signals: list[str] | None = Field(None, min_length=1, max_length=10)
    days: int | None = Field(None, ge=1, le=800)


def _validate_stocks(stocks) -> list[str]:
    """校验股票代码列表：非空、项非空、去重保序、≤MAX_STOCKS；违规抛 HTTPException 400。"""
    if not stocks:
        raise HTTPException(400, "stocks 不能为空")
    if len(stocks) > MAX_STOCKS:
        raise HTTPException(400, f"stocks 最多 {MAX_STOCKS} 只")
    seen: set[str] = set()
    out: list[str] = []
    for s in stocks:
        if not isinstance(s, str) or not s.strip():
            raise HTTPException(400, f"非法股票代码项: {s!r}")
        s = s.strip()
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _serialize_project(
    p: PaperProject, db: Session, include_result: bool = True
) -> dict:
    """项目序列化；include_result=False 时 result 恒为 null（列表瘦身），并新增 has_result 标志。

    stocks 为 [{code, name}]（名称关联表快照，查无回退实时 DB 读）；旧单股项目
    无关联行 → 单元素列表（兼容）。code 恒为 p.code（首股）。
    """
    rows = paper_repo.project_stock_rows(db, p.id)
    if rows:
        stocks = [{"code": r.code, "name": r.name or _stock_name(r.code)} for r in rows]
    else:
        stocks = [{"code": p.code, "name": _stock_name(p.code)}]
    return {
        "id": p.id,
        "name": p.name,
        "kind": p.kind,
        "code": p.code,
        "stocks": stocks,
        "period": p.period,
        "signals": p.signals,
        "days": p.days,
        "result": p.result if include_result else None,
        "has_result": p.result is not None,
        "created_at": (
            to_market_naive(p.created_at).isoformat() if p.created_at else None
        ),
        "updated_at": (
            to_market_naive(p.updated_at).isoformat() if p.updated_at else None
        ),
    }


def _check_period(period: str) -> None:
    if period not in VALID_PERIODS:
        raise HTTPException(
            400, f"不支持周期 {period}，可选: {', '.join(VALID_PERIODS)}"
        )


def _stock_name(code: str) -> str:
    """股票名称（DB 读取，失败静默返回空串，不阻塞实验计算）。"""
    try:
        from ..storage.repos.stocks import stock_name

        for c in (code, normalize_code(code)):
            name = stock_name(c)
            if name:
                return name
    except Exception:
        pass
    return ""


def _run_experiment(code: str, period: str, signals: list[str], days: int) -> dict:
    """实验计算核心（experiment 端点与项目 run 共用）：校验 + 计算，返回 data（不含 code/name 外壳）。"""
    _check_period(period)
    try:
        signals = validate_signals(signals)
    except ValueError as e:
        raise HTTPException(400, str(e))
    k = cached_kline(code, period, max_rows=days)
    if k is None or k.empty:
        raise HTTPException(404, f"{code} 的 {period} K 线不可用")
    k = prepare_kline(k)
    if len(k) < MIN_BARS:
        raise HTTPException(
            422, f"{code} K 线数据不足（{len(k)} 根，至少 {MIN_BARS} 根）"
        )
    try:
        data = run_experiment(k, signals)
    except Exception as e:
        logger.exception("模拟盘实验计算失败 %s/%s", code, period)
        raise HTTPException(500, f"实验计算失败: {str(e)[:120]}")
    return data


def _run_watch(code: str, period: str, signals: list[str]) -> tuple[dict, str]:
    """观察计算核心（watch 端点与项目 run 共用）：返回 (data, 最后 bar 日期)。"""
    _check_period(period)
    try:
        signals = validate_signals(signals)
    except ValueError as e:
        raise HTTPException(400, str(e))
    k = cached_kline(code, period, max_rows=120)
    if k is None or k.empty:
        raise HTTPException(404, f"{code} 的 {period} K 线不可用")
    k = prepare_kline(k)
    if len(k) < MIN_BARS:
        raise HTTPException(
            422, f"{code} K 线数据不足（{len(k)} 根，至少 {MIN_BARS} 根）"
        )
    try:
        data = run_watch(k, signals)
    except Exception as e:
        logger.exception("模拟盘观察计算失败 %s/%s", code, period)
        raise HTTPException(500, f"观察计算失败: {str(e)[:120]}")
    return data, str(k["date"].iloc[-1])


def _watch_payload(data: dict, last_date: str) -> dict:
    """服务层 watch 输出 → API 契约结构（与前端 PaperWatchResult 字段对齐）。

    服务返回 {indicators, triggered, recent}；前端类型为
    {snapshot, hit_signals, recent_triggers}——字段错位会导致快照/触发区全部空白，
    此处统一映射（历史遗留漂移修复）。
    """
    return {
        "snapshot": data.get("indicators"),
        "hit_signals": [
            t.get("signal") for t in data.get("triggered", []) if t.get("signal")
        ],
        "recent_triggers": data.get("recent", []),
        "updated_at": last_date,
    }


@router.post("/experiment")
def experiment(body: ExperimentBody):
    """指标信号历史实验：逐 bar 判定触发，输出触发事件与收益统计。

    body: {code, period='daily', signals(≤10, SIGNAL_TYPES 白名单), days(1~800)}。
    未实现逐 bar 规则的信号返回 400「暂不支持该信号做历史实验」。
    """
    data = _run_experiment(body.code, body.period, body.signals, body.days)
    return {
        "code": body.code,
        "name": _stock_name(body.code),
        "period": body.period,
        "days": body.days,
        **data,
    }


@router.get("/watch")
def watch(code: str, period: str = "daily", signals: str = ""):
    """实时指标观察：最新 bar 指标快照 + 命中信号 + 最近 5 个触发点。

    query: {code, period='daily', signals=逗号分隔（SIGNAL_TYPES 白名单）}。
    """
    sig_list = [s.strip() for s in signals.split(",") if s.strip()]
    data, last_date = _run_watch(code, period, sig_list)
    return {
        "code": code,
        "name": _stock_name(code),
        "period": period,
        **_watch_payload(data, last_date),
    }


@router.get("/meta")
def paper_meta():
    """模拟盘元信息：信号白名单（SUPPORTED_SIGNALS 单一事实源派生）+ 周期 + 上限。

    前端据此渲染信号选择器（不再硬编码），契约: {signals: [{key, label, description}],
    periods, max_signals, max_stocks, min_bars}。
    """
    return {
        "signals": [
            {
                "key": s,
                "label": SIGNAL_LABELS.get(s, s),
                "description": SIGNAL_DESCRIPTIONS.get(s, ""),
            }
            for s in SUPPORTED_SIGNALS
        ],
        "periods": list(VALID_PERIODS),
        "max_signals": 10,
        "max_stocks": MAX_STOCKS,
        "min_bars": MIN_BARS,
    }


# ---------------------------------------------------------------------------
# 实验项目持久化：CRUD + run（结果快照写回 paper_projects.result）
# ---------------------------------------------------------------------------


@router.get("/projects")
def list_projects(db: Session = Depends(get_db)):
    """全部实验项目，按 updated_at 倒序；result 恒为 null（列表瘦身，避免数百 KB 序列化），
    用 has_result 标志表示是否已有 run 快照。"""
    rows = paper_repo.list_projects(db)
    return {"data": [_serialize_project(r, db, include_result=False) for r in rows]}


@router.get("/projects/{pid}")
def get_project(pid: int, db: Session = Depends(get_db)):
    """单个项目完整信息（含 result 全量快照）；项目不存在 404。"""
    p = paper_repo.get_project(db, pid)
    if p is None:
        raise HTTPException(404, f"项目 {pid} 不存在")
    return _serialize_project(p, db)


@router.get("/projects/{pid}/watch")
def watch_project(pid: int, db: Session = Depends(get_db)):
    """观察项目实时轮询（多股批量）：复用 watch 计算，并限频落一条快照（供历史曲线）。

    仅 kind='watch' 项目可用；kind='experiment' → 400；项目不存在 404。
    快照限频 SNAPSHOT_MIN_INTERVAL_SEC（默认 60s）：距上一条不足间隔则不写库。
    外壳结构（单股与 GET /watch 一致；多股为 {multi: true, stocks: [...]}）。
    """
    p = paper_repo.get_project(db, pid)
    if p is None:
        raise HTTPException(404, f"项目 {pid} 不存在")
    if p.kind != "watch":
        raise HTTPException(400, "仅观察项目支持实时轮询")
    codes = paper_repo.project_codes(db, p)
    payloads = []
    for c in codes:
        data, last_date = _run_watch(c, p.period, p.signals)
        payloads.append(
            {
                "code": c,
                "name": _stock_name(c),
                "period": p.period,
                **_watch_payload(data, last_date),
            }
        )
    # 限频落快照：≥SNAPSHOT_MIN_INTERVAL_SEC 一条；prices 按股存最新收盘价
    now = utcnow()
    last_snap = paper_repo.latest_watch_snapshot(db, pid)
    if last_snap is None or (now - last_snap.ts).total_seconds() >= (
        SNAPSHOT_MIN_INTERVAL_SEC
    ):
        paper_repo.add_watch_snapshot(
            db,
            project_id=pid,
            ts=now,
            bar_date=payloads[0]["updated_at"],
            prices={pl["code"]: (pl["snapshot"] or {}).get("close") for pl in payloads},
            hit_signals=sorted({s for pl in payloads for s in pl["hit_signals"]}),
        )
    if len(payloads) == 1:
        return payloads[0]
    return {"multi": True, "stocks": payloads}


@router.get("/projects/{pid}/watch/history")
def watch_history(pid: int, limit: int = 60, db: Session = Depends(get_db)):
    """观察项目历史快照序列（时间升序，最近 limit 条，默认 60，上限 200），供前端画曲线。

    返回 {data: [{ts, bar_date, prices, hit_signals}...]}；项目不存在 404；
    experiment 项目或暂无快照返回空 data。
    """
    p = paper_repo.get_project(db, pid)
    if p is None:
        raise HTTPException(404, f"项目 {pid} 不存在")
    limit = max(1, min(200, limit))
    rows = paper_repo.list_watch_snapshots(db, pid, limit)
    return {
        "data": [
            {
                "ts": to_market_naive(r.ts).isoformat() if r.ts else None,
                "bar_date": r.bar_date,
                "prices": r.prices,
                "hit_signals": r.hit_signals,
            }
            for r in rows
        ]
    }


@router.post("/projects")
def create_project(body: ProjectCreate, db: Session = Depends(get_db)):
    """创建项目：kind 非法 400；period 非法 400；signals 未过白名单 400。

    stocks 可选（多股批量）：提供时按 stocks 写关联表且 code 以 stocks[0] 为准
    （兼容字段）；未提供则单股（关联表 = [code]）。
    """
    if body.kind not in ("experiment", "watch"):
        raise HTTPException(400, f"非法项目类型 {body.kind!r}，可选: experiment/watch")
    _check_period(body.period)
    try:
        signals = validate_signals(body.signals)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if body.stocks is not None:
        stock_codes = _validate_stocks(body.stocks)
        code = stock_codes[0]
    else:
        stock_codes = [body.code]
        code = body.code
    p = paper_repo.create_project(
        db,
        name=body.name.strip() if body.name and body.name.strip() else "未命名项目",
        kind=body.kind,
        code=code,
        period=body.period,
        signals=signals,
        days=body.days,
        stock_codes=stock_codes,
    )
    return _serialize_project(p, db)


@router.patch("/projects/{pid}")
def update_project(pid: int, body: ProjectPatch, db: Session = Depends(get_db)):
    """部分更新 name/code/stocks/period/signals/days；result/kind 不可通过此端点修改；404 当项目不存在。

    结果失效语义：code/stocks/period/signals/days 任一变更都会使旧 result 失效
    （旧快照基于旧参数），一并清空，并在响应带 result_invalidated: true，前端据此提示
    「参数已变更，旧结果已失效」；仅改 name 不影响结果口径，result 保留且字段为 false。
    """
    p = paper_repo.get_project(db, pid)
    if p is None:
        raise HTTPException(404, f"项目 {pid} 不存在")
    changes = body.model_dump(exclude_unset=True)
    if "name" in changes:
        p.name = changes["name"]
    if "code" in changes:
        p.code = changes["code"]
    if "stocks" in changes:
        stock_codes = _validate_stocks(changes["stocks"])
        paper_repo.replace_project_stocks(db, pid, stock_codes)
        p.code = stock_codes[0]  # code 与首股同步，兼容旧前端
    if "period" in changes:
        _check_period(changes["period"])
        p.period = changes["period"]
    if "signals" in changes:
        try:
            p.signals = validate_signals(changes["signals"])
        except ValueError as e:
            raise HTTPException(400, str(e))
    if "days" in changes:
        p.days = changes["days"]
    # code/stocks/period/signals/days 任一变化都会使已生成的 result 失效
    # （旧快照基于旧参数），一并清空，避免前端展示过期结果
    invalidating = bool(
        changes.keys() & {"code", "stocks", "period", "signals", "days"}
    )
    if invalidating:
        p.result = None
    p.updated_at = utcnow()
    paper_repo.save_project(db, p)
    resp = _serialize_project(p, db)
    resp["result_invalidated"] = invalidating
    return resp


@router.delete("/projects/{pid}", status_code=204)
def delete_project(pid: int, db: Session = Depends(get_db)):
    """删除项目（含级联）：不存在 404。

    删除前先取消该项目的所有活动（pending/running/paused）paper_experiment 任务
    （走队列取消逻辑，任务中心不再残留运行中记录），随后清理 watch 快照与股票关联行，
    再删除项目本身。终态任务历史保留（任务中心独立管理，不随项目删除）。
    """
    p = paper_repo.get_project(db, pid)
    if p is None:
        raise HTTPException(404, f"项目 {pid} 不存在")
    cancel_paper_jobs(pid)
    paper_repo.delete_project(db, pid)
    # 清理 per-project 并发锁（避免 _project_locks 随项目创建无限增长）
    with _project_locks_guard:
        _project_locks.pop(pid, None)


@router.post("/projects/{pid}/run")
def run_project(pid: int, db: Session = Depends(get_db)):
    """按项目 kind 执行实验/观察；两种 kind 统一返回 {job_id, status, project} 外壳。

    - experiment：提交 paper_experiment 后台任务（DB 级并发防重），返回
      {job_id, status='pending', project（result 为旧值）}；任务完成由 worker 同步回写 result。
    - watch：保持同步计算落库（内存 _running 防重），返回
      {job_id=None, status='done', project（含最新 result）}；多股项目 result 为
      {multi: true, stocks: [...]}。
    同 pid 并发 run（experiment 为 DB 中活动的 paper_experiment 任务，watch 为内存标记）→ 409。
    """
    p = paper_repo.get_project(db, pid)
    if p is None:
        raise HTTPException(404, f"项目 {pid} 不存在")
    if p.kind == "experiment":
        # 项目级并发防重：per-project 锁包住「查活动任务 + 提交」——查询与写库之间无
        # 原子性（DB 无唯一约束），不加锁时两个并发 run 会同时通过（各自提交一个任务）
        entry = _project_lock(pid)
        try:
            with entry["lock"]:
                if has_active_paper_job(pid):
                    raise HTTPException(409, "该项目已有运行中的任务，请稍候")
                try:
                    job_id = submit("paper_experiment", {"project_id": pid})
                except QueueFullError as e:
                    raise HTTPException(429, str(e))
        finally:
            _release_project_lock(pid, entry)
        return {
            "job_id": job_id,
            "status": "pending",
            "project": _serialize_project(p, db),
        }
    with _run_lock:
        if _running.get(pid):
            raise HTTPException(409, "项目正在运行中，请稍候")
        _running[pid] = True
    try:
        codes = paper_repo.project_codes(db, p)
        if len(codes) == 1:
            data, last_date = _run_watch(codes[0], p.period, p.signals)
            p.result = _watch_payload(data, last_date)
        else:
            p.result = {
                "multi": True,
                "stocks": [
                    {
                        "code": c,
                        "name": _stock_name(c),
                        "period": p.period,
                        **_watch_payload(*_run_watch(c, p.period, p.signals)),
                    }
                    for c in codes
                ],
            }
        p.updated_at = utcnow()
        paper_repo.save_project(db, p)
        return {
            "job_id": None,
            "status": "done",
            "project": _serialize_project(p, db),
        }
    finally:
        with _run_lock:
            _running.pop(pid, None)


# ---------------------------------------------------------------------------
# T-01 模拟盘账户：资金 / 持仓 / 委托 / 成交流水 / 绩效
# 撮合语义：按 bar 收盘价撮合，bar_date 缺省取最新 bar；
# 限价未触发挂起、资金/持仓不足拒绝；涨跌停/停牌等约束属 T-02，本组端点不处理。
# ---------------------------------------------------------------------------

# 默认初始资金（元）；创建账户时可覆盖
DEFAULT_INITIAL_CASH = 1_000_000.0
# 撮合仅支持日线 bar 时点（分钟 bar 的盘中语义与绩效日线口径不一致）
ACCOUNT_PERIODS = ("daily",)


class AccountCreate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=64)
    initial_cash: float = Field(DEFAULT_INITIAL_CASH, gt=0, le=1e12)


class OrderCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=16)
    side: str  # buy/sell（validate_order 校验）
    order_type: str = "market"  # market/limit
    price: float | None = Field(None, gt=0)
    quantity: float = Field(..., gt=0)  # LOT_SIZE 整数倍（validate_order 校验）
    period: str = "daily"
    bar_date: str | None = None  # 缺省 = 最新 bar


def _serialize_account(a: PaperAccount) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "initial_cash": round(a.initial_cash, 2),
        "cash": round(a.cash, 2),
        "created_at": (
            to_market_naive(a.created_at).isoformat() if a.created_at else None
        ),
        "updated_at": (
            to_market_naive(a.updated_at).isoformat() if a.updated_at else None
        ),
    }


def _serialize_order(o: PaperOrder, name_map: dict[str, str] | None = None) -> dict:
    """委托序列化；name_map 为批量名称映射（code→name，列表端点一次查出），缺省逐条回退单查。"""
    name = (name_map or {}).get(o.code)
    if name is None:
        name = _stock_name(o.code)
    return {
        "id": o.id,
        "account_id": o.account_id,
        "code": o.code,
        "name": name,
        "side": o.side,
        "order_type": o.order_type,
        "price": o.price,
        "quantity": round(o.quantity, 0),
        "filled_qty": round(o.filled_qty, 0),
        "filled_avg_price": o.filled_avg_price,
        "status": o.status,
        "reject_reason": o.reject_reason,
        "bar_date": o.bar_date,
        "created_at": (
            to_market_naive(o.created_at).isoformat() if o.created_at else None
        ),
        "updated_at": (
            to_market_naive(o.updated_at).isoformat() if o.updated_at else None
        ),
    }


def _serialize_position(
    p: PaperPosition, name_map: dict[str, str] | None = None
) -> dict:
    """持仓序列化；name_map 为批量名称映射（列表端点一次查出），缺省逐条回退单查。"""
    name = (name_map or {}).get(p.code)
    if name is None:
        name = _stock_name(p.code)
    return {
        "account_id": p.account_id,
        "code": p.code,
        "name": name,
        "quantity": round(p.quantity, 0),
        "avg_cost": round(p.avg_cost, 4),
    }


def _serialize_trade(t: PaperTrade, name_map: dict[str, str] | None = None) -> dict:
    """成交流水序列化；name_map 为批量名称映射（列表端点一次查出），缺省逐条回退单查。"""
    name = (name_map or {}).get(t.code)
    if name is None:
        name = _stock_name(t.code)
    return {
        "id": t.id,
        "account_id": t.account_id,
        "order_id": t.order_id,
        "code": t.code,
        "name": name,
        "side": t.side,
        "price": round(t.price, 4),
        "quantity": round(t.quantity, 0),
        "amount": round(t.amount, 2),
        "fee": round(t.fee, 2),
        "bar_date": t.bar_date,
        "created_at": (
            to_market_naive(t.created_at).isoformat() if t.created_at else None
        ),
    }


def _get_account(db: Session, aid: int) -> PaperAccount:
    acc = paper_repo.get_account(db, aid)
    if acc is None:
        raise HTTPException(404, f"账户 {aid} 不存在")
    return acc


@router.get("/accounts")
def list_accounts(db: Session = Depends(get_db)):
    """全部模拟盘账户（updated_at 倒序）；外层 {data: [...]}。"""
    rows = paper_repo.list_accounts(db)
    return {"data": [_serialize_account(r) for r in rows]}


@router.post("/accounts")
def create_account(body: AccountCreate, db: Session = Depends(get_db)):
    """创建模拟盘账户：name 缺省「模拟账户」，initial_cash 缺省 DEFAULT_INITIAL_CASH。"""
    name = body.name.strip() if body.name and body.name.strip() else "模拟账户"
    acc = paper_repo.create_account(db, name=name, initial_cash=body.initial_cash)
    return _serialize_account(acc)


@router.get("/accounts/{aid}")
def get_account(aid: int, db: Session = Depends(get_db)):
    """单个账户（资金概况）；不存在 404。"""
    return _serialize_account(_get_account(db, aid))


@router.delete("/accounts/{aid}", status_code=204)
def delete_account(aid: int, db: Session = Depends(get_db)):
    """删除账户（含持仓/委托/成交级联清理）；不存在 404。"""
    _get_account(db, aid)
    paper_repo.delete_account(db, aid)


@router.get("/accounts/{aid}/orders")
def list_orders(aid: int, db: Session = Depends(get_db)):
    """账户委托列表（created_at 倒序，新在前）；不存在 404。

    股票名称走 stock_names 批量查询（N+1 修复：不再每条自建 SessionLocal 单查）。
    """
    _get_account(db, aid)
    rows = paper_repo.list_orders(db, aid)
    name_map = stock_repo.stock_names(db, {r.code for r in rows})
    return {"data": [_serialize_order(r, name_map) for r in rows]}


@router.get("/accounts/{aid}/positions")
def list_positions(aid: int, db: Session = Depends(get_db)):
    """账户持仓（code 升序），附最新收盘价估值：市值 / 浮动盈亏 / 盈亏比例。

    现价取该股最新日线 bar 收盘（cached_kline 兜底，行情缺失字段为 null）。
    """
    _get_account(db, aid)
    rows = paper_repo.list_positions(db, aid)
    out = []
    name_map = stock_repo.stock_names(db, {p.code for p in rows})
    for p in rows:
        close = None
        k = cached_kline(p.code, "daily", max_rows=20, refresh_if_stale=False)
        if k is not None and not k.empty:
            k = prepare_kline(k)
            close = float(k["close"].iloc[-1]) if len(k) else None
        base = _serialize_position(p, name_map)
        base["close"] = round(close, 4) if close is not None else None
        base["market_value"] = (
            round(p.quantity * close, 2) if close is not None else None
        )
        base["pnl"] = (
            round((close - p.avg_cost) * p.quantity, 2) if close is not None else None
        )
        base["pnl_pct"] = (
            round(close / p.avg_cost - 1, 6)
            if close is not None and p.avg_cost
            else None
        )
        out.append(base)
    return {"data": out}


@router.get("/accounts/{aid}/trades")
def list_trades(aid: int, limit: int = 200, db: Session = Depends(get_db)):
    """账户成交流水（bar_date 倒序，新在前，最近 limit 条，默认 200 上限 1000）。

    股票名称走 stock_names 批量查询（N+1 修复：不再每条自建 SessionLocal 单查）。
    """
    _get_account(db, aid)
    limit = max(1, min(1000, limit))
    rows = paper_repo.list_trades(db, aid, limit)
    name_map = stock_repo.stock_names(db, {r.code for r in rows})
    return {"data": [_serialize_trade(r, name_map) for r in rows]}


@router.post("/accounts/{aid}/orders")
def place_order(aid: int, body: OrderCreate, db: Session = Depends(get_db)):
    """委托下单并撮合（按 bar 收盘价）。

    语义：市价单按 bar 价全额成交；限价买 bar 价 ≤ 委托价 / 限价卖 bar 价 ≥ 委托价
    才成交，否则委托挂起（pending）；触发但资金/持仓不足 → 整单拒绝（status=rejected
    + reject_reason）。bar_date 缺省取该股最新 bar；成交记录写入成交流水。
    编排（取 K 线 → prepare → resolve bar → 撮合判定 → 条件写回 → 落库，
    含 P0-29 并发防护）在 core.paper.orders（place_order_bar /
    place_order_service），api 层仅参数校验 + HTTP 语义 + 序列化。
    """
    _get_account(db, aid)
    code = normalize_code(body.code.strip())
    if body.period not in ACCOUNT_PERIODS:
        raise HTTPException(400, f"模拟盘撮合仅支持周期: {'/'.join(ACCOUNT_PERIODS)}")
    try:
        validate_order(body.side, body.order_type, body.price, body.quantity)
    except ValueError as e:
        raise HTTPException(400, str(e))
    from ..core.paper.orders import place_order_bar, place_order_service

    try:
        bar = place_order_bar(code, body.period, body.bar_date)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if bar is None:
        raise HTTPException(404, f"{code} 的日线行情不可用")
    bar_price, bar_date = bar
    order = place_order_service(
        db,
        aid,
        {
            "code": code,
            "side": body.side,
            "order_type": body.order_type,
            "price": body.price,
            "quantity": body.quantity,
        },
        bar_price,
        bar_date,
    )
    return _serialize_order(order)


@router.delete("/accounts/{aid}/orders/{oid}")
def cancel_order(aid: int, oid: int, db: Session = Depends(get_db)):
    """撤单：仅 pending 委托可撤（filled/canceled/rejected 不可撤，返回 400）。

    委托不属于该账户 → 404。撤单后返回更新后的委托对象。
    """
    _get_account(db, aid)
    try:
        o = paper_repo.cancel_order(db, aid, oid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if o is None:
        raise HTTPException(404, f"委托 {oid} 不存在")
    return _serialize_order(o)


@router.get("/accounts/{aid}/performance")
def account_performance(aid: int, db: Session = Depends(get_db)):
    """账户绩效：重放成交流水 + 按日线收盘估值 → 净值曲线与指标。

    返回 {curve: [{date, equity}], metrics: {total_return, annualized_return,
    max_drawdown, sharpe, win_rate, trade_count, winning_trades, initial_cash,
    final_equity, days}}。无成交 → curve 为空、指标为 null（前端空态）。
    """
    acc = _get_account(db, aid)
    rows = paper_repo.list_trades_asc(db, aid)
    trades = [
        {
            "code": r.code,
            "side": r.side,
            "price": r.price,
            "quantity": r.quantity,
            "fee": r.fee,
            "bar_date": r.bar_date,
        }
        for r in rows
    ]

    def price_lookup(code: str) -> dict:
        k = cached_kline(code, "daily", max_rows=800, refresh_if_stale=False)
        if k is None or k.empty:
            return {}
        k = prepare_kline(k)
        return dict(zip(k["date"].astype(str), k["close"]))

    return equity_curve(acc.initial_cash, trades, price_lookup)
