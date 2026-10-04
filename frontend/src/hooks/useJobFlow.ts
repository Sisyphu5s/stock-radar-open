import { useCallback, useEffect, useRef, useState } from 'react'
import { useJob, invalidateJob } from '../data/jobs'
import type { JobLike } from '../data/jobs'
import type { JobDetail, JobListItem } from '../api/client'

export interface UseJobFlowOptions {
  /** 提交动作：返回 job_id。页面负责参数组装与提交前后置副作用（快照/缓存清理/上下文记录/成功消息）。
   *  提交失败时 hook 会停止轮询并向上抛错（页面 catch 弹错误消息），与页面原实现一致。 */
  submit: () => Promise<{ job_id: number }>
  /** 提交成功的乐观 job（默认 {id, status:'pending', progress:0}）。乐观对象形状可比 T 窄（缺详情字段） */
  makeOptimistic?: (jobId: number) => JobLike
  /** 轮询到 done 的收尾（页面特有：结果缓存/成功消息/GPU 回退等）；仅首次触发 */
  onDone?: (job: JobDetail) => void
  /** 轮询到 failed 的收尾（页面特有：错误消息等）；仅首次触发 */
  onFailed?: (job: JobDetail) => void
  /** 轮询到 cancelled 的收尾（页面特有）；仅首次触发；不传则仅停止轮询（按钮恢复可用） */
  onCancelled?: (job: JobDetail) => void
}

/** 终态：停止订阅与轮询（与后端终态常量同步：done/failed/cancelled） */
const TERMINAL_STATUS = new Set(['done', 'failed', 'cancelled'])

/**
 * 研究页「提交任务 → useJob 共享轮询 → done/failed 收尾」统一实现
 * （替代原 FactorMining/FactorEvaluation/Backtest 各自的 job/polling/activeJobId/handledJobRef 拷贝）：
 *
 * - 提交成功：本地乐观 job + 开启 useJob 订阅轮询 + invalidateJob 强制刷新
 * - 轮询值实时同步进 job（进度/状态 UI 行为不变）；done/failed 停止订阅与轮询
 * - handledJobRef 防重复收尾：onDone/onFailed 仅首次触发（原 FactorMining 语义；
 *   FactorEvaluation/Backtest 因 done/failed 后订阅即停、原实现本身也只会执行一次，等价）
 * - restore：URL ?job= 恢复任务用（页面恢复动作之后接管轮询开关）
 *
 * 页面特有逻辑（GPU 回退确认、结果缓存、快照绑定、成功/失败消息）通过 onDone/onFailed/submit
 * 回调留在页面，hook 只承载通用轮询骨架。
 *
 * 泛型 T：页面声明的 job 消费形状（默认 JobListItem；需 result 载荷的页面显式传 JobDetail
 * 或其局部扩展，见各调用方）。restore 参数随 T 收窄。乐观对象（makeOptimistic）与轮询
 * JobDetail 形状可比 T 宽/窄（乐观缺详情字段），hook 内统一按 T 收窄同步——与页面历史
 * useState<any> 的宽松语义一致，但类型契约由 T 承载：页面在乐观态读缺字段时走可选链，
 * 在 done 态读字段时得到精确类型（取代原 any 直读）。
 */
export function useJobFlow<T extends JobListItem = JobListItem>(options: UseJobFlowOptions): {
  job: T | null
  polling: boolean
  submit: () => Promise<void>
  reset: () => void
  restore: (job: T, startPolling: boolean) => void
} {
  const [job, setJob] = useState<T | null>(null)
  const [polling, setPolling] = useState(false)
  const [activeJobId, setActiveJobId] = useState<number | undefined>()
  const liveJob = useJob(activeJobId)
  const handledJobRef = useRef<number | null>(null)
  const optsRef = useRef(options)
  optsRef.current = options

  const submit = useCallback(async () => {
    try {
      const r = await optsRef.current.submit()
      // 乐观对象（JobLike 形状，缺 T 的必填详情字段如 created_at/params）无法静态满足 T，
      // 经 unknown 收窄是显式类型承诺：页面乐观态只读可选字段（status/progress/id），语义同原 any
      setJob((optsRef.current.makeOptimistic?.(r.job_id) ??
        { id: r.job_id, status: 'pending', progress: 0, job_type: '' }) as unknown as T)
      setPolling(true)
      setActiveJobId(r.job_id)
      invalidateJob(r.job_id)
    } catch (e) {
      setPolling(false)
      setActiveJobId(undefined)
      throw e
    }
  }, [])

  /** URL ?job= 恢复任务：接管 job 状态并按需开启轮询（running/pending）或停止（done/failed）。
   *  job 参数类型 T：兼容 getJob 返回的 JobDetail（详情 ⊇ 页面声明的 T）与页面乐观对象 */
  const restore = useCallback((j: T, startPolling: boolean) => {
    setJob(j)
    if (startPolling) {
      setPolling(true)
      setActiveJobId(j.id)
      invalidateJob(j.id)
    } else {
      setPolling(false)
    }
  }, [])

  const reset = useCallback(() => {
    setJob(null)
    setPolling(false)
    setActiveJobId(undefined)
    handledJobRef.current = null
  }, [])

  // useJob 共享轮询 → 同步到 job；done/failed/cancelled 停止订阅并首次收尾
  useEffect(() => {
    const j = liveJob
    if (!j) return
    // 轮询返回 JobDetail（详情 ⊇ 页面声明的 T），按 T 收窄后同步
    setJob(j as T)
    if (!TERMINAL_STATUS.has(j.status)) return
    setPolling(false)
    setActiveJobId(undefined)
    if (handledJobRef.current === j.id) return
    handledJobRef.current = j.id
    if (j.status === 'done') optsRef.current.onDone?.(j)
    else if (j.status === 'failed') optsRef.current.onFailed?.(j)
    else optsRef.current.onCancelled?.(j)
  }, [liveJob])

  return { job, polling, submit, reset, restore }
}
