/**
 * 参数调优任务统一封装（T-43：自 FactorEvaluation 拆出）。
 * ------------------------------------------------------------
 * 调优任务 = 后台任务（factor_tune，计入任务管理器）：提交/轮询/收尾统一走 useJobFlow 共享任务池。
 * 提交参数经 getOpts 读取最新快照（TuneModal 内部表单状态）；is_int 必须显式携带：
 * 后端默认 True，float 参数不传会被 round 去重导致网格失真。
 * 提交成功 / 完成消息在本层统一发出（toast）；onDone 透传收窄后的原始载荷，
 * onFailed 透传错误文案（TuneModal 负责 setTuneResult/setTuneError）。
 */
import { tuneFactorExpression } from '../../../api/client'
import type { FactorTunePayload, TuneParamRequest, TuneTarget } from '../../../api/client'
import { useJobFlow } from '../../../hooks/useJobFlow'
import { toast } from '../shared/toast'

export interface TuneSubmitOpts {
  expression: string
  datasetId: number
  horizon: number
  target: TuneTarget
  param: TuneParamRequest
}

export function useTuneTask(opts: {
  /** 每次 submit 时读取的最新调优参数快照（TuneModal 表单状态；null = 未就绪，submit 抛错） */
  getOpts: () => TuneSubmitOpts | null
  onDone?: (payload: FactorTunePayload | undefined) => void
  onFailed?: (err: string) => void
}): { job: any; polling: boolean; submit: () => Promise<void> } {
  const { job, polling, submit } = useJobFlow({
    submit: async () => {
      const o = opts.getOpts()
      if (!o) throw new Error('调优参数未就绪，请重新运行')
      const r = await tuneFactorExpression({
        expression: o.expression,
        dataset_id: o.datasetId,
        horizon: o.horizon,
        target: o.target,
        param: o.param,
      })
      if (!('job_id' in r)) throw new Error('参数调优未生成后台任务，请重试')
      toast.success(`调优任务 #${r.job_id} 已提交，可关闭弹窗继续浏览`)
      return { job_id: r.job_id }
    },
    makeOptimistic: (id) => ({ id, status: 'pending', progress: 0, result: null, job_type: 'factor_tune' }),
    onDone: (j) => {
      toast.success('调优任务完成')
      opts.onDone?.(j.result as FactorTunePayload | undefined)
    },
    onFailed: (j) => opts.onFailed?.(j.error ?? '调优任务失败'),
  })
  return { job, polling, submit }
}
