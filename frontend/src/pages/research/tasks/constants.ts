/** 任务中心类型/状态筛选常量（Tasks.tsx / desktopTable / mobileList / detailDrawer 共用）。 */
import type { JobListItem } from '../../../api/client'

/** 任务中心行数据（列表项 = 后端 _job_to_dict 快照；详情抽屉在此基础上合并 JobDetail 的 result/finished_at） */
export type TaskRow = JobListItem

/** 任务表达式兜底：expr 缺失时用 params.name；模拟盘实验显示「模拟盘实验 #项目ID」 */
export function taskExpr(r: TaskRow): string {
  if (r.job_type === 'paper_experiment') {
    const pid = r.params?.project_id
    return pid != null ? `模拟盘实验 #${pid}` : '模拟盘实验'
  }
  return r.expr || String(r.params?.name ?? '')
}

/** 任务是否可安全重跑：job_type 在已知可创建类型内（createExperiment 校验的合法类型），
 *  params 来自后端 JSON 序列化，始终可复用。未知/新类型不提供重跑入口。 */
export function canRerun(r: TaskRow): boolean {
  return r.job_type in TYPE_LABELS
}

export const TYPE_OPTIONS = [
  { value: 'all', label: '全部类型' },
  { value: 'gp_run', label: '因子挖掘' },
  { value: 'evaluate', label: '因子评估' },
  { value: 'walk_forward', label: 'Walk-forward 验证' },
  { value: 'neural_train', label: '神经网络训练' },
  { value: 'rl_train', label: '强化学习训练' },
  { value: 'backtest', label: '回测' },
  { value: 'backtest_opt', label: '组合优化回测' },
  { value: 'alpha101_score', label: 'Alpha101全库评分' },
  { value: 'factor_tune', label: '因子参数调优' },
  { value: 'dataset_build', label: '数据集构建' },
  { value: 'market_scan', label: '全市场扫描' },
  { value: 'paper_experiment', label: '模拟盘实验' },
]

export const TYPE_LABELS: Record<string, string> = Object.fromEntries(
  TYPE_OPTIONS.filter((o) => o.value !== 'all').map((o) => [o.value, o.label]),
)

/**
 * 状态筛选。后端 /experiments/page 服务端支持：单状态精确过滤（pending/running/paused/done/failed/cancelled），
 * 'active' 为 排队中+运行中 的组合别名（不含 paused）；筛选在 count/分页前完成，total 为筛选后完整计数。
 */
export const STATUS_OPTIONS = [
  { value: 'all', label: '全部状态' },
  { value: 'active', label: '进行中（排队+运行）' },
  { value: 'pending', label: '排队中' },
  { value: 'running', label: '运行中' },
  { value: 'paused', label: '已暂停' },
  { value: 'done', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]
