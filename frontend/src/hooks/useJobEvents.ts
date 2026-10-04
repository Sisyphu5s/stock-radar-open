import { useEffect, useMemo, useState } from 'react'

/** SSE 任务事件（与后端 GET /api/v1/experiments/{job_id}/events 契约对齐） */
export interface JobEvent {
  type: 'phase' | 'progress' | 'done' | 'failed' | 'cancelled'
  job_id: number
  ts: number
  phase?: string
  progress?: number
  status?: string
  error?: string
}

const MAX_EVENTS = 200

/** 终态事件类型：任务不再产生新事件（与后端终态常量同步：done/failed/cancelled）。
 *  收到终态即 es.close()，停止 EventSource 断线自动重连（浏览器 ~3s 重连无限循环），连接与事件 ring 可被 GC。 */
const TERMINAL_TYPES = new Set(['done', 'failed', 'cancelled'])

/** 解析事件 data JSON；坏数据返回 null（调用方跳过） */
function parseEvent(data: string): JobEvent | null {
  try {
    const obj = JSON.parse(data) as Partial<JobEvent>
    if (!obj || typeof obj.type !== 'string') return null
    return obj as JobEvent
  } catch {
    return null
  }
}

/**
 * 任务 SSE 事件订阅：EventSource GET /api/v1/experiments/{jobId}/events（相对路径，走 vite dev proxy）。
 *
 * - replay（首连历史重放，data 为事件数组）初始化 events；phase/progress/done/failed 追加，内部最多保留 200 条
 * - livePhase = 最新 phase 事件的阶段文案
 * - connected 跟随 onopen=true / onerror=false；EventSource 断线自动重连，重连后服务端重放、events 重置
 * - jobId 为空/变化：关闭旧连接并重置 events=null；组件卸载/effect 清理自动 close
 */
export function useJobEvents(jobId?: number): { events: JobEvent[] | null; connected: boolean; livePhase?: string } {
  const [events, setEvents] = useState<JobEvent[] | null>(null)
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    setEvents(null)
    setConnected(false)
    if (jobId == null) return
    const es = new EventSource(`/api/v1/experiments/${jobId}/events`)
    es.onopen = () => setConnected(true)
    es.onerror = () => setConnected(false)
    // 首连重放：data 为事件数组，整体初始化（覆盖断线重连前的旧流）
    es.addEventListener('replay', (ev) => {
      try {
        const raw = (ev as MessageEvent).data
        const arr = JSON.parse(raw)
        if (Array.isArray(arr)) {
          const filtered = arr.filter((x) => x && typeof x.type === 'string').slice(-MAX_EVENTS)
          setEvents(filtered)
          // 重放历史已含终态（任务在首连前已完成）→ 无需再等实时流，直接关闭
          if (filtered.some((x) => TERMINAL_TYPES.has(x.type))) es.close()
        }
      } catch { /* 单条坏数据跳过 */ }
    })
    const append = (ev: Event) => {
      const e = parseEvent((ev as MessageEvent).data)
      if (!e) return
      setEvents((prev) => (prev ? [...prev, e].slice(-MAX_EVENTS) : [e]))
      // 终态事件：连接后续不会再有事件，立即关闭（否则浏览器对已结束流 ~3s 自动重连死循环）
      if (TERMINAL_TYPES.has(e.type)) es.close()
    }
    es.addEventListener('phase', append)
    es.addEventListener('progress', append)
    es.addEventListener('done', append)
    es.addEventListener('failed', append)
    es.addEventListener('cancelled', append)
    return () => es.close()
  }, [jobId])

  const livePhase = useMemo(() => {
    if (!events) return undefined
    for (let i = events.length - 1; i >= 0; i--) {
      const e = events[i]
      if (e.type === 'phase' && typeof e.phase === 'string' && e.phase.length > 0) return e.phase
    }
    return undefined
  }, [events])

  return { events, connected, livePhase }
}
