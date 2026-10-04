/**
 * 任务中心本地偏好与归档状态（localStorage 持久化，key 统一 sr-tasks-* 前缀）。
 * 参照 SignalCenter 的 usePersistentState 模式（utils/stateMemory.ts），但落 localStorage——
 * 筛选/排序/归档需要跨会话保留（sessionStorage 在关闭标签页即丢失）。
 * 无限滚动开关的持久化由 InfiniteScrollToggle（sr-tasks-infinite）承担，不在此重复。
 */
import { useCallback, useState } from 'react'

const KEY_PREFIX = 'sr-tasks-'

function readLocal<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(KEY_PREFIX + key)
    if (raw == null) return fallback
    return JSON.parse(raw) as T
  } catch {
    // localStorage 不可用或数据损坏时静默降级为默认值
    return fallback
  }
}

function writeLocal(key: string, value: unknown): void {
  try {
    localStorage.setItem(KEY_PREFIX + key, JSON.stringify(value))
  } catch {
    // localStorage 不可用（隐私模式/超限）时静默降级为会话内状态
  }
}

/** localStorage 持久化状态（JSON 序列化；用法与 usePersistentState 一致） */
export function usePersistentLocalState<T>(key: string, initial: T): [T, (next: T) => void] {
  const [value, setValue] = useState<T>(() => readLocal(key, initial))
  const set = useCallback((next: T) => {
    setValue(next)
    writeLocal(key, next)
  }, [key])
  return [value, set]
}

const ARCHIVE_KEY = 'archived'

/**
 * 归档状态：本地隐藏 + 统计排除（后端无软删，归档 id 集持久化于 localStorage
 * sr-tasks-archived，跨会话保留）。待后端提供软删字段后替换为服务端标记。
 */
export function useArchived() {
  const [ids, setIds] = useState<Set<number>>(() => {
    const saved = readLocal<unknown>(ARCHIVE_KEY, [])
    return Array.isArray(saved) ? new Set(saved.filter((x): x is number => typeof x === 'number')) : new Set<number>()
  })
  const persist = useCallback((next: Set<number>) => {
    setIds(new Set(next))
    writeLocal(ARCHIVE_KEY, [...next])
  }, [])
  const isArchived = useCallback((id: number) => ids.has(id), [ids])
  const archive = useCallback((idList: number[]) => {
    if (idList.length === 0) return
    const next = new Set(ids)
    for (const id of idList) next.add(id)
    persist(next)
  }, [ids, persist])
  const unarchive = useCallback((id: number) => {
    if (!ids.has(id)) return
    const next = new Set(ids)
    next.delete(id)
    persist(next)
  }, [ids, persist])
  return { isArchived, archive, unarchive }
}
