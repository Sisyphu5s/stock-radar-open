import { useCallback, useEffect, useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import { notifications } from '@mantine/notifications'
import { getPaperProjectWatch, updatePaperProject } from '../../../api/client'
import type { PaperProject, PaperProjectPayload } from '../../../api/client'
import { errMsg } from '../../../utils/format'
import { WATCH_INTERVAL_MS } from './constants'

/** antd message → @mantine/notifications(T-44:色/时长对齐 research/shared/toast.ts 契约;与页面级 notify 同契约,自包含) */
const notifyWarning = (m: string) => notifications.show({ message: m, color: 'yellow', autoClose: 3200 })
const notifyError = (m: string) => notifications.show({ message: m, color: 'red', autoClose: 4000 })

/** localStorage 键:记录最近监控中的 watch 项目 id(刷新后自动恢复轮询) */
export const WATCH_STORAGE_KEY = 'sr-paper-watching'

/** 恢复上次监控项目:storage 中 id 存在且为 watch 项目 → 返回 id;否则清除 storage 并返回 null */
export function restoreWatchFromStorage(list: PaperProject[]): number | null {
  const savedId = Number(localStorage.getItem(WATCH_STORAGE_KEY))
  if (!savedId || Number.isNaN(savedId)) return null
  const proj = list.find((p) => p.id === savedId)
  if (proj?.kind === 'watch') return savedId
  localStorage.removeItem(WATCH_STORAGE_KEY)
  return null
}

export interface UsePaperWatchOptions {
  /** 当前选中项目 id:null(无选中)时轮询/启动均 no-op */
  activeId: number | null
  /** 当前选中项目(watch 域判定 kind 与多股判断的依据) */
  activeProject: PaperProject | null
  /** 表单参数:启动监控前校验与持久化(PATCH)用 */
  code: string
  signalCodes: string[]
  period: string
  /** 持久化成功后更新列表项(页面视图 state 直写) */
  applyProject: (updated: PaperProject) => void
  /** 轮询结果合并 / 持久化回写详情(页面视图 state 直写) */
  setActiveDetail: Dispatch<SetStateAction<PaperProject | null>>
}

export interface PaperWatchApi {
  watching: boolean
  setWatching: Dispatch<SetStateAction<boolean>>
  watchLoading: boolean
  watchError: boolean
  setWatchError: Dispatch<SetStateAction<boolean>>
  lastUpdatedAt: number | null
  fetchWatch: () => Promise<void>
  startWatch: () => Promise<void>
  stopWatch: () => void
}

/**
 * watch 域(多股监控,P2-19 自 PaperTrading.tsx 抽出,纯结构重构,运行时行为零变化):
 * 15s 只读轮询 + 启动/停止 + 切非 watch 项目自动停止。数据源与页面一致:
 * watch 轮询端点(只读不落库)直取,结果经 setActiveDetail 合并进详情视图 state
 * (P1-47 记账:watch 15s 轮询保留手写 fetch——其结果与详情池(staleTime 0 无轮询)职责分离,
 * 迁池需重建「轮询合并 + in-flight 守卫」语义,成本高收益低)。
 */
export function usePaperWatch({
  activeId, activeProject, code, signalCodes, period, applyProject, setActiveDetail,
}: UsePaperWatchOptions): PaperWatchApi {
  const [watching, setWatching] = useState(false)
  const [watchLoading, setWatchLoading] = useState(false)
  const [watchError, setWatchError] = useState(false)
  const [lastUpdatedAt, setLastUpdatedAt] = useState<number | null>(null)

  const watchInflight = useRef(false)

  // 卸载守卫（P2-62）：interval 清理只停后续 tick，拦不住已在途的 fetch——其 resolve/catch
  // 仍会执行。卸载瞬间置 false，在途请求返回后不再 setState（防卸载后 setState 残留更新）。
  const aliveRef = useRef(true)
  useEffect(() => {
    aliveRef.current = true
    return () => { aliveRef.current = false }
  }, [])

  const fetchWatch = useCallback(async () => {
    if (!activeId) return
    // 守卫:切换帧内 activeProject 已变(watch→experiment)时跳过请求,绝不 setWatchError(防残留脏错误态)
    if (activeProject?.kind !== 'watch') return
    if (watchInflight.current) return // in-flight guard:请求慢于 15s 时不叠加并发
    watchInflight.current = true
    try {
      // 只读轮询端点(不落库),返回数据合并进详情
      const data = await getPaperProjectWatch(activeId)
      if (!aliveRef.current) return // 卸载瞬间在途请求:丢弃结果,不 setState
      setActiveDetail((prev) => (prev && prev.id === activeId ? { ...prev, result: data } : prev))
      setLastUpdatedAt(Date.now())
      setWatchError(false)
    } catch {
      if (!aliveRef.current) return
      setWatchError(true) // 轮询失败静默:不弹 message,仅顶部提示 + 手动刷新
    } finally {
      watchInflight.current = false
    }
  }, [activeId, activeProject?.kind, setActiveDetail])

  const startWatch = async () => {
    if (!activeId) return
    const multi = (activeProject?.stocks?.length ?? 1) > 1
    if (!code && !multi) { notifyWarning('请先选择股票'); return }
    if (signalCodes.length === 0) { notifyWarning('请至少选择一个信号'); return }
    setWatchError(false)
    try {
      // 先持久化监控参数,再启动轮询(watch 项目不传 days;多股项目不传 code)
      const payload: Partial<PaperProjectPayload> = { period, signals: signalCodes }
      if (!multi) payload.code = code
      const saved = await updatePaperProject(activeId, payload)
      applyProject(saved)
      setActiveDetail(saved)
      setLastUpdatedAt(null)
      setWatching(true)
      localStorage.setItem(WATCH_STORAGE_KEY, String(activeId))
    } catch (e) {
      notifyError('保存监控参数失败: ' + errMsg(e))
    }
  }

  const stopWatch = () => {
    setWatching(false)
    setWatchError(false)
    localStorage.removeItem(WATCH_STORAGE_KEY)
  }

  // 切换到非 watch 项目时自动停止监控并清除恢复标记
  useEffect(() => {
    if (activeId == null) return
    if (activeProject?.kind !== 'watch') {
      setWatching(false)
      setWatchError(false)
      localStorage.removeItem(WATCH_STORAGE_KEY)
    }
  }, [activeProject?.kind, activeId])

  useEffect(() => {
    if (!watching) return
    if (activeProject?.kind !== 'watch') return // 守卫:切到 experiment 项目的当帧不发请求、不置 watchLoading
    setWatchLoading(true)
    void fetchWatch().finally(() => { if (aliveRef.current) setWatchLoading(false) })
    const t = window.setInterval(() => {
      if (document.visibilityState === 'hidden') return // 后台不空转,回前台下一 tick 续上
      void fetchWatch()
    }, WATCH_INTERVAL_MS)
    return () => window.clearInterval(t)
  }, [watching, fetchWatch, activeProject?.kind])

  return {
    watching, setWatching,
    watchLoading, watchError, setWatchError,
    lastUpdatedAt,
    fetchWatch, startWatch, stopWatch,
  }
}
