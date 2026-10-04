import { useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { notifications } from '@mantine/notifications'
import { getJob } from '../api/client'
import type { JobDetail } from '../api/client'

export interface UseJobRestoreHandlers {
  /** 任务类型名（job_type 校验失败警告文案用，如「回测任务」） */
  label: string
  /** done 且有结果 → 页面恢复结果视图（loadJob/loadExp/参数恢复等页面特有逻辑） */
  onDone: (job: JobDetail) => void
  /** pending/running → 页面接管轮询（通常 restore(job, true)） */
  onActive?: (job: JobDetail) => void
  /** failed/cancelled/done 无结果等其余状态 → 页面接管（通常 restore(job, false) 或状态提示） */
  onRest?: (job: JobDetail) => void
}

/**
 * 任务类型家族（P2-57）：深链恢复按「家族」校验而非严格相等。
 * backtest_opt（组合优化）与 backtest（回测）同属回测家族——结果结构/参数恢复逻辑同源
 * （Backtest.tsx loadJob 已按 isOptJob 处理 backtest_opt），深链 ?job= 应可互相恢复。
 * 键为页面注册的基础类型（useJobRestore 第一个参），值为该家族全部兼容类型。
 */
const JOB_TYPE_FAMILIES: Record<string, readonly string[]> = {
  backtest: ['backtest', 'backtest_opt'],
  // T-128:walk_forward 与 evaluate 同属评估家族（FactorEvaluation 恢复/展示 wf 载荷，深链 ?job= 互可恢复）
  evaluate: ['evaluate', 'walk_forward'],
}

/** job_type 是否与页面注册类型同族（无家族定义时严格相等，零回归） */
const isSameFamily = (pageType: string, jobType: string): boolean => {
  const family = JOB_TYPE_FAMILIES[pageType]
  return family ? family.includes(jobType) : jobType === pageType
}

/**
 * URL ?job= 参数恢复任务（JobBar / 任务管理跨页跳转）。Backtest / FactorEvaluation / FactorMining
 * 三处手写恢复 effect 的收敛（P1-4）：
 * - job_type 校验：Workbench 六个 Tab 共享 URL，防止挖掘/评估/回测任务串入本页（不匹配 → 警告）
 * - 同一 job 只恢复一次（appliedRef 去重，防 URL 回写触发重灌覆盖手动编辑）
 * - ?job= 从 URL 移除后重置水位，允许同一 id 之后再次恢复（FactorMining 原语义）
 * - 恢复失败（任务不存在）静默；组件卸载/URL 变化取消在途响应
 */
export function useJobRestore(jobType: string, handlers: UseJobRestoreHandlers): void {
  const [searchParams] = useSearchParams()
  // handlers 经 ref 读最新闭包：页面回调（loadJob/restore 等）随渲染刷新，effect 恒用最新
  const hRef = useRef(handlers)
  useEffect(() => { hRef.current = handlers })
  const appliedRef = useRef<number | null>(null)

  useEffect(() => {
    const id = Number(searchParams.get('job') ?? NaN)
    if (!id) {
      // 参数移除后重置水位，允许同一 job id 之后再次恢复
      appliedRef.current = null
      return
    }
    if (appliedRef.current === id) return
    appliedRef.current = id
    let cancelled = false
    getJob(id).then((j) => {
      if (cancelled) return
      const h = hRef.current
      if (j.job_type && !isSameFamily(jobType, j.job_type)) {
        notifications.show({
          message: `任务 #${id} 不是${h.label}（${j.job_type}），请到任务管理查看`,
          color: 'yellow',
          autoClose: 3200,
        })
        return
      }
      if (j.status === 'done' && j.result) { h.onDone(j); return }
      if (j.status === 'pending' || j.status === 'running') { h.onActive?.(j); return }
      h.onRest?.(j)
    }).catch(() => { /* 任务不存在 */ })
    return () => { cancelled = true }
  }, [searchParams]) // eslint-disable-line react-hooks/exhaustive-deps
}
