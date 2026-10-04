import EmptyState from '../../../components/ui/EmptyState'

/** 结果区占位（研究页 job 三态收敛，Backtest/FactorEvaluation 共用，P0-1）：
 *  - failed → 错误态（回测失败/评估失败 + job.error，不再伪装成执行中/空态）
 *  - pending/running → 执行中
 *  - 其余（无任务 / done 无结果 / cancelled）→ 暂无结果
 *  注：EmptyState 错误分支仅渲染 title（text），故 job.error 并入 text 展示。 */
export default function JobResultPlaceholder({ job, failedText, emptyText = '暂无结果' }: {
  job?: { status?: string; error?: string } | null
  failedText: string
  emptyText?: string
}) {
  if (job?.status === 'failed') {
    return <EmptyState text={job.error ? `${failedText}：${job.error}` : failedText} />
  }
  if (job && (job.status === 'pending' || job.status === 'running')) {
    return <EmptyState description="任务执行中…结果将自动出现在这里" />
  }
  return <EmptyState description={emptyText} />
}
