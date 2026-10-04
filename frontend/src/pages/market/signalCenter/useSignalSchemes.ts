import { useCallback, useEffect, useState } from 'react'
import type { SignalScheme, SignalSchemeFilters } from './context'
import { isSignalMatch, isTimeRangeValue } from './context'

// ===== 命名筛选方案（T-13）：localStorage 持久化，上限 50 =====
/** 方案存储键：与 dir 的 sr-signal-dir 同域（筛选现场持久化）；方案需跨会话保留，故用 localStorage */
export const SCHEMES_KEY = 'sr-signal-schemes'
/** 方案上限：保存时截断，超出丢弃最旧 */
export const SCHEMES_LIMIT = 50

/** 逐条形状校验（含 filters 深校验）：损坏/旧结构数据直接丢弃，
 *  防 apply 把非法值写回筛选状态（loadSchemes 为唯一入口，持久化数据不可信） */
function isFilters(f: unknown): f is SignalSchemeFilters {
  if (typeof f !== 'object' || f === null) return false
  const o = f as Record<string, unknown>
  if (typeof o.period !== 'string' || !isTimeRangeValue(o.timeRange) || !isSignalMatch(o.signalMatch)) return false
  if (typeof o.watchlistOnly !== 'boolean' || typeof o.excludeToday !== 'boolean') return false
  if (!Array.isArray(o.sectors) || o.sectors.some((v) => typeof v !== 'string')) return false
  if (!Array.isArray(o.signalFilter) || o.signalFilter.some((v) => typeof v !== 'string')) return false
  if (typeof o.dir !== 'object' || o.dir === null) return false
  return Object.values(o.dir as Record<string, unknown>).every((v) => v === 0 || v === 1 || v === -1)
}

function isScheme(x: unknown): x is SignalScheme {
  if (typeof x !== 'object' || x === null) return false
  const o = x as Record<string, unknown>
  if (typeof o.id !== 'string' || typeof o.name !== 'string' || typeof o.createdAt !== 'string') return false
  return isFilters(o.filters)
}

function loadSchemes(): SignalScheme[] {
  try {
    const raw = JSON.parse(localStorage.getItem(SCHEMES_KEY) ?? '[]') as unknown
    if (!Array.isArray(raw)) return []
    return raw.filter(isScheme).slice(0, SCHEMES_LIMIT)
  } catch {
    return []
  }
}

/** 方案 id：优先 crypto.randomUUID（secure context），非安全上下文（http 非 localhost）回退时间戳+随机数 */
const genId = (): string =>
  typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`

/**
 * 命名筛选方案 store：save 快照 currentFilters（保存时筛选现场），apply 经 applyFilters
 * 回调写回筛选状态（SignalCenter 层组装：复用现有 setter + dir 持久化）。
 * activeId 语义 = 激活（已应用且与当前筛选一致）：筛选被手动改动后自动清除，高亮只在真正一致时保留。
 */
export function useSignalSchemes(opts: {
  /** 当前筛选现场快照（保存时写入方案；字段序固定，激活一致性比较依赖稳定序列化） */
  currentFilters: SignalSchemeFilters
  /** 把方案 filters 写回筛选状态（完整替换语义，含 dir） */
  applyFilters: (f: SignalSchemeFilters) => void
}) {
  const { currentFilters, applyFilters } = opts
  const [schemes, setSchemes] = useState<SignalScheme[]>(loadSchemes)
  const [activeId, setActiveId] = useState<string | null>(null)

  // 持久化：变更即写（量小：save/remove 为离散动作，无需防抖；首渲染幂等回写）
  useEffect(() => {
    try { localStorage.setItem(SCHEMES_KEY, JSON.stringify(schemes)) } catch { /* ignore */ }
  }, [schemes])

  // 保存当前筛选为方案：快照引用 currentFilters（React 状态对象不可变，setter 整体替换，引用安全）
  const save = useCallback((name: string) => {
    const n = name.trim()
    if (!n) return
    setSchemes((prev) => [...prev, {
      id: genId(),
      name: n,
      createdAt: new Date().toISOString(),
      filters: currentFilters,
    }].slice(-SCHEMES_LIMIT))
  }, [currentFilters])

  // 删除方案：同 id 激活标记一并清除
  const remove = useCallback((id: string) => {
    setSchemes((prev) => prev.filter((s) => s.id !== id))
    setActiveId((cur) => (cur === id ? null : cur))
  }, [])

  // 应用方案：完整快照替换（含 dir），激活标记 = 该方案 id
  const apply = useCallback((id: string) => {
    const s = schemes.find((x) => x.id === id)
    if (!s) return
    applyFilters(s.filters)
    setActiveId(id)
  }, [schemes, applyFilters])

  // 激活一致性：筛选状态被手动改动（与激活方案不一致）→ 清除激活标记
  useEffect(() => {
    if (!activeId) return
    const s = schemes.find((x) => x.id === activeId)
    if (!s || JSON.stringify(s.filters) !== JSON.stringify(currentFilters)) setActiveId(null)
  }, [activeId, schemes, currentFilters])

  return { schemes, save, remove, apply, activeId }
}
