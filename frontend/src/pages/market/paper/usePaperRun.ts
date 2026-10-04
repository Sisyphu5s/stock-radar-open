import { useMemo, useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import { notifications } from '@mantine/notifications'
import { api, getExperimentsPage, getPaperProject, pauseJob, resumeJob, runPaperProject, updatePaperProject } from '../../../api/client'
import type { JobListItem, PaperProject, PaperProjectPayload } from '../../../api/client'
import { useJobFlow } from '../../../hooks/useJobFlow'
import { errMsg } from '../../../utils/format'
import type { RunJobState } from './ParamSummary'

/** antd message → @mantine/notifications（T-44：色/时长对齐 research/shared/toast.ts 契约；与页面级 notify 同契约,自包含） */
const notifySuccess = (m: string) => notifications.show({ message: m, color: 'teal', autoClose: 2500 })
const notifyError = (m: string) => notifications.show({ message: m, color: 'red', autoClose: 4000 })
const notifyWarning = (m: string) => notifications.show({ message: m, color: 'yellow', autoClose: 3200 })

export interface UsePaperRunOptions {
  /** 当前选中项目 id：null（无选中）时 submit 空跑（job_id 0，不进入轮询） */
  activeId: number | null
  /** 当前选中项目（多股判断：stocks>1 时不传 code，防 PATCH 改 p.code 与 stocks 关联表脱节） */
  activeProject: PaperProject | null
  /** 表单参数：提交/运行前持久化（PATCH）用 */
  code: string
  period: string
  signalCodes: string[]
  days: number
  /** 持久化/运行/重拉后更新列表项（页面视图 state 直写） */
  applyProject: (updated: PaperProject) => void
  /** 持久化/运行/重拉后回写详情（页面视图 state 直写） */
  setActiveDetail: Dispatch<SetStateAction<PaperProject | null>>
}

export interface PaperRunApi {
  expLoading: boolean
  expError: string
  /** 派生运行态（ParamSummary 契约 RunJobState）：progress/paused 实时来自共享池 job；终态/重置后回落 null */
  runJob: RunJobState | null
  runExperiment: () => Promise<void>
  pauseRunJob: () => Promise<void>
  resumeRunJob: () => Promise<void>
  cancelRunJob: () => Promise<void>
  /** 删除正在运行的项目时清理运行态（reset + runProjectId + runPaused，等价页面原四行组合） */
  clearRun: () => void
  /** T-130:刷新/重进后按 project_id 恢复运行中任务（pending/running/paused paper_experiment）；
   *  找到则接管轮询并返回 true；无匹配/已恢复/查询失败返回 false */
  restoreRunForProject: (projectId: number) => Promise<boolean>
}

/**
 * experiment 域（P2-19 自 PaperTrading.tsx 抽出，纯结构重构，运行时行为零变化）：
 * 实验任务统一走 useJobFlow（与回测/评估等研究页同一共享任务池，替代 waitForJob 页面局部轮询）：
 * submit 先持久化表单参数再按项目运行；轮询经共享池 15s 去重；done → 重拉详情 + success；
 * failed → error + 详情保持旧结果；终态自动停止订阅，任务在后台继续（任务中心可见）。
 * 切项目/卸载仅停止前端订阅，任务在后台继续——useJobFlow 的 optsRef 每渲染转发，
 * 回调闭包始终读最新参数，与页面内联实现等价。
 */
export function usePaperRun({
  activeId, activeProject, code, period, signalCodes, days, applyProject, setActiveDetail,
}: UsePaperRunOptions): PaperRunApi {
  const [expLoading, setExpLoading] = useState(false)
  const [expError, setExpError] = useState('')
  const [runProjectId, setRunProjectId] = useState<number | null>(null)
  const runProjectIdRef = useRef<number | null>(null)
  const [runPaused, setRunPaused] = useState(false)

  const { job, submit, reset, restore } = useJobFlow({
    submit: async () => {
      const pid = activeId
      if (pid == null) return { job_id: 0 }
      // 多股项目股票不可在编辑态修改：不传 code（防 PATCH 改 p.code 与 stocks 关联表脱节），仅持久化其余参数
      const multi = (activeProject?.stocks?.length ?? 1) > 1
      const payload: Partial<PaperProjectPayload> = { period, signals: signalCodes, days }
      if (!multi) payload.code = code
      const saved = await updatePaperProject(pid, payload)
      applyProject(saved)
      setActiveDetail(saved)
      const res = await runPaperProject(saved.id)
      applyProject(res.project)
      setActiveDetail(res.project)
      runProjectIdRef.current = saved.id
      setRunProjectId(saved.id)
      setRunPaused(false)
      return { job_id: res.job_id as number }
    },
    makeOptimistic: (id) => ({ id, status: 'pending', progress: 0, job_type: 'paper_experiment' }),
    onDone: (j) => {
      const pid = runProjectIdRef.current
      runProjectIdRef.current = null
      setRunProjectId(null)
      setRunPaused(false)
      // done → 重拉详情（run 结果已落库）+ success；详情拉取失败仅提示，不阻塞收尾
      const projId = pid ?? Number((j.params as Record<string, unknown> | undefined)?.project_id ?? 0)
      getPaperProject(projId)
        .then((fresh) => {
          applyProject(fresh)
          setActiveDetail((prev) => (prev && prev.id === fresh.id ? fresh : prev))
        })
        .catch(() => {})
        .finally(() => notifySuccess('实验运行完成'))
    },
    onFailed: (j) => {
      runProjectIdRef.current = null
      setRunProjectId(null)
      setRunPaused(false)
      notifyError('实验运行失败: ' + (j.error ?? '未知错误'))
    },
    onCancelled: () => {
      runProjectIdRef.current = null
      setRunProjectId(null)
      setRunPaused(false)
    },
  })

  // 派生运行态（ParamSummary 契约 RunJobState）：progress/paused 实时来自共享池 job；终态/重置后回落 null
  const runJob: RunJobState | null = useMemo(() => {
    if (!job || runProjectId == null) return null
    return {
      projectId: runProjectId,
      id: job.id,
      progress: Math.round(job.progress ?? 0),
      paused: runPaused || job.status === 'paused',
    }
  }, [job, runProjectId, runPaused])

  const runExperiment = async () => {
    if (!activeId) return
    // 多股项目股票不可在编辑态修改：不传 code（防 PATCH 改 p.code 与 stocks 关联表脱节），仅持久化其余参数
    const multi = (activeProject?.stocks?.length ?? 1) > 1
    if (!code && !multi) { notifyWarning('请先选择股票'); return }
    if (signalCodes.length === 0) { notifyWarning('请至少选择一个信号'); return }
    setExpLoading(true)
    setExpError('')
    try {
      // 提交/轮询/收尾统一走 useJobFlow（submit 内持久化参数 + 运行，返回 job_id 进入共享池）
      await submit()
    } catch (e) {
      if ((e as { response?: { status?: number } })?.response?.status === 409) {
        notifyWarning('该项目已有运行中的任务，可在任务中心查看进度')
      } else {
        setExpError('实验运行失败: ' + errMsg(e))
      }
    } finally {
      setExpLoading(false)
    }
  }

  const pauseRunJob = async () => {
    const rj = runJob
    if (!rj) return
    try {
      const res = await pauseJob(rj.id)
      setRunPaused(true)
      notifySuccess(res.message ?? '已暂停（在下一个计算检查点生效）')
    } catch (e) {
      notifyError('暂停失败: ' + ((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? errMsg(e)))
    }
  }

  const resumeRunJob = async () => {
    const rj = runJob
    if (!rj) return
    try {
      const res = await resumeJob(rj.id)
      setRunPaused(false)
      notifySuccess(res.message ?? '任务已恢复')
    } catch (e) {
      notifyError('恢复失败: ' + ((e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? errMsg(e)))
    }
  }

  const cancelRunJob = async () => {
    const rj = runJob
    if (!rj) return
    reset()
    runProjectIdRef.current = null
    setRunProjectId(null)
    setRunPaused(false)
    try {
      await api.delete(`/experiments/${rj.id}`)
      notifySuccess('任务已取消，可在任务中心查看')
    } catch {
      notifyWarning('取消请求已发送（任务可能已完成或已删除）')
    }
  }

  const clearRun = () => {
    reset()
    runProjectIdRef.current = null
    setRunProjectId(null)
    setRunPaused(false)
  }

  // T-130:刷新/重进后恢复运行任务——实验任务 ID 此前只存内存,刷新即丢失运行态(进度/暂停/取消不可达)
  const restoreRunForProject = useMemo(() => async (projectId: number): Promise<boolean> => {
    if (runProjectIdRef.current != null) return false
    try {
      const page = await getExperimentsPage({ job_type: 'paper_experiment', status: 'active', limit: 50 })
      const match = (page.items ?? []).find((j) =>
        Number((j.params as Record<string, unknown> | undefined)?.project_id) === projectId)
      if (!match) return false
      runProjectIdRef.current = projectId
      setRunProjectId(projectId)
      setRunPaused(match.status === 'paused')
      // 接管共享池轮询：restore(job, true) 后 useJob 15s 轮询直至终态,收尾回调照常触发
      restore(match as JobListItem, true)
      return true
    } catch {
      return false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  return {
    expLoading, expError, runJob, runExperiment, pauseRunJob, resumeRunJob, cancelRunJob, clearRun,
    restoreRunForProject,
  }
}
