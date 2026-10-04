"""模拟盘实验处理器(_run_paper + _prepare_kline + _writeback_paper_result)。

自 runner.py 逐字搬移,仅 import 路径调整;runner 依赖经 _deps._dyn 动态转发
(测试 monkeypatch Q.SessionLocal/Q.cached_kline 等穿透),周期/最小 bar 常量
直接绑定。
"""

from __future__ import annotations

from ..errors import JobCancelled
from ..runner import PAPER_MIN_BARS, PAPER_VALID_PERIODS, logger
from ._deps import _dyn

# runner 辅助/模块状态:动态转发(测试 monkeypatch 穿透)
_start_running = _dyn("_start_running")
_track = _dyn("_track")
_checkpoint = _dyn("_checkpoint")
_update = _dyn("_update")
_terminal = _dyn("_terminal")
# runner 顶层 import 的业务符号:动态转发(测试 monkeypatch Q.cached_kline 等穿透)
SessionLocal = _dyn("SessionLocal")
cached_kline = _dyn("cached_kline")
run_paper_experiment = _dyn("run_paper_experiment")
validate_signals = _dyn("validate_signals")
utcnow = _dyn("utcnow")
# SQLAlchemy 实体类 / update 构造器必须为真实对象(不能 _dyn 包装,否则
# db.get/db.query/update(实体) 触发 inspect 失败),测试不 patch 这些符号。
from ..runner import (  # noqa: E402
    PaperProject,
    PaperProjectStock,
    update,
)


def _prepare_kline(df) -> "object":
    """K 线按可解析日期升序排序去重（同日期保留最后一行），date 统一为字符串。

    与 api/paper.py 的 _prepare 同口径（此处独立实现，避免 queue 跨层依赖 API 模块）。
    """
    df = df.reset_index(drop=True)
    df["date"] = df["date"].astype(str)
    return (
        df.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    )


def _writeback_paper_result(project_id: int, result: dict) -> None:
    """任务完成同步回写 paper_projects.result + updated_at（新 Session）。

    失败静默记录日志不抛——项目 result 保持旧值，任务本身仍正常完成。
    """
    try:
        db = SessionLocal()
        try:
            db.execute(
                update(PaperProject)
                .where(PaperProject.id == project_id)
                .values(result=result, updated_at=utcnow())
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("模拟盘项目 %s result 回写失败", project_id)


def _run_paper(job_id: int, params: dict):
    """模拟盘实验（paper_experiment）：按项目配置整体计算，完成时同步回写项目 result。

    多股项目（paper_project_stocks 关联行）逐股循环计算：单股结果结构与原一致
    （{code, period, days, results, computed_bars}，兼容既有前端/测试）；多股结果
    为 {multi, period, days, stocks:[{code, name, results, computed_bars}...],
    summary}（stocks 明细 + 跨股汇总）。
    进度 0 → 50（计算后）→ 100，调用前后各一个协作控制点（支持暂停/取消）。
    校验失败（项目不存在/周期非法/信号白名单/K 线不可用或不足）→ 任务 failed + error。
    """
    try:
        if not _start_running(job_id, 0.0):
            return  # 已被取消
        _track(job_id)
        project_id = params.get("project_id")
        if not isinstance(project_id, int):
            raise ValueError("params.project_id 缺失或非法")
        db = SessionLocal()
        try:
            project = db.get(PaperProject, project_id)
            # 多股代码序列：关联表按 seq 排序；旧单股项目无关联行 → 回退 code 列
            rows = (
                db.query(PaperProjectStock)
                .filter(PaperProjectStock.project_id == project_id)
                .order_by(PaperProjectStock.seq)
                .all()
            )
            codes = [r.code for r in rows]
            stock_names = {r.code: r.name for r in rows}
        finally:
            db.close()
        if project is None:
            raise RuntimeError(f"模拟盘项目 {project_id} 不存在")
        period, days = project.period, project.days
        signals = list(project.signals or [])
        if not codes:
            codes = [project.code]
        _checkpoint(job_id)
        # 校验（与 api/paper.py 同语义，HTTPException 语义转字符串）
        if period not in PAPER_VALID_PERIODS:
            raise RuntimeError(
                f"不支持周期 {period}，可选: {', '.join(PAPER_VALID_PERIODS)}"
            )
        try:
            signals = validate_signals(signals)
        except ValueError as e:
            raise RuntimeError(str(e))
        per_stock: list[tuple[str, dict]] = []
        for code in codes:
            k = cached_kline(code, period, max_rows=days)
            if k is None or k.empty:
                raise RuntimeError(f"{code} 的 {period} K 线不可用")
            k = _prepare_kline(k)
            if len(k) < PAPER_MIN_BARS:
                raise RuntimeError(
                    f"{code} K 线数据不足（{len(k)} 根，至少 {PAPER_MIN_BARS} 根）"
                )
            _checkpoint(job_id)
            per_stock.append((code, run_paper_experiment(k, signals)))
        _checkpoint(job_id)
        _update(job_id, progress=50.0)
        if len(per_stock) == 1:
            code, data = per_stock[0]
            result = {"code": code, "period": period, "days": days, **data}
        else:
            stocks = [
                {"code": c, "name": stock_names.get(c, ""), **d} for c, d in per_stock
            ]
            all_results = [r for st in stocks for r in st.get("results", [])]
            wins = [
                r.get("win_rate") for r in all_results if r.get("win_rate") is not None
            ]
            result = {
                "multi": True,
                "period": period,
                "days": days,
                "stocks": stocks,
                "summary": {
                    "stock_count": len(stocks),
                    "total_triggers": sum(
                        int(r.get("triggers") or 0) for r in all_results
                    ),
                    "avg_win_rate": (round(sum(wins) / len(wins), 4) if wins else None),
                },
            }
        _checkpoint(job_id)
        # 同步回写项目 result（失败静默，任务仍完成）
        _writeback_paper_result(project_id, result)
        _terminal(
            job_id, status="done", progress=100.0, result=result, finished_at=utcnow()
        )
    except JobCancelled:
        return  # 用户取消：状态已由 delete_job 写入，项目 result 保持旧值
    except Exception as e:
        logger.exception("模拟盘实验任务失败")
        _terminal(job_id, status="failed", error=str(e)[:2000], finished_at=utcnow())
