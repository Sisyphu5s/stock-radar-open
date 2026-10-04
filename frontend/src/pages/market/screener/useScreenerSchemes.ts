import { useCallback, useEffect, useState } from 'react'
import type {
  ScreenerCondition,
  ScreenerConditionNode,
  ScreenerConditionTree,
  ScreenerField,
  ScreenerLogic,
  ScreenerOp,
  ScreenerUniverse,
} from '../../../api/screener'
import { SCREENER_FIELD_OPTIONS, SCREENER_OP_OPTIONS } from '../../../api/screener'

// ===== 命名选股方案（T-17）：localStorage 持久化，上限 50 =====
// 独立实现（只参照 useSignalSchemes 的模式，不 import）；键与信号中心互不干扰。
/** 方案存储键：跨会话保留选股条件组合 */
export const SCHEMES_KEY = 'sr-screener-schemes'
/** 方案上限：保存时截断，超出丢弃最旧 */
export const SCHEMES_LIMIT = 50

export interface ScreenerSchemeFilters {
  universe: ScreenerUniverse
  /** T-75 条件树（logic 在树内，顶层不再单独存 logic） */
  conditions: ScreenerConditionTree
}

export interface ScreenerScheme {
  id: string
  name: string
  createdAt: string
  filters: ScreenerSchemeFilters
}

// ===== 持久化数据不可信：逐条形状校验，损坏/旧结构直接丢弃（防 apply 写回非法值） =====
// T-75 兼容：旧扁平 filters {universe, logic, conditions: ScreenerCondition[]}
// 读取时归一为树 {universe, conditions: {logic, children}}，零崩溃。
const isField = (v: unknown): v is ScreenerField =>
  typeof v === 'string' && SCREENER_FIELD_OPTIONS.some((o) => o.value === v)
const isOp = (v: unknown): v is ScreenerOp =>
  typeof v === 'string' && SCREENER_OP_OPTIONS.some((o) => o.value === v)
const isUniverse = (v: unknown): v is ScreenerUniverse =>
  v === 'watchlist' || v === 'hs300' || v === 'all'
const isLogic = (v: unknown): v is ScreenerLogic => v === 'and' || v === 'or'

function isConditionLeaf(c: unknown): c is ScreenerCondition {
  if (typeof c !== 'object' || c === null) return false
  const o = c as Record<string, unknown>
  return isField(o.field) && isOp(o.op) && typeof o.value === 'number' && Number.isFinite(o.value)
}

/** 递归校验条件树节点：叶子 或 分组（children 逐层校验，支持任意深） */
function isNode(n: unknown): n is ScreenerConditionNode {
  if (typeof n !== 'object' || n === null) return false
  const o = n as Record<string, unknown>
  if ('children' in o || 'logic' in o) {
    return isLogic(o.logic) && Array.isArray(o.children) && o.children.every(isNode)
  }
  return isConditionLeaf(n)
}

/**
 * 归一化持久化 filters：接受新树格式 {universe, conditions: 树} 与
 * 旧扁平格式 {universe, logic, conditions: 叶子[]}；非法形状返回 null（丢弃）。
 */
function normalizeFilters(f: unknown): ScreenerSchemeFilters | null {
  if (typeof f !== 'object' || f === null) return null
  const o = f as Record<string, unknown>
  if (!isUniverse(o.universe)) return null
  const c = o.conditions
  // 新树格式
  if (typeof c === 'object' && c !== null && !Array.isArray(c) && isNode(c)) {
    return { universe: o.universe, conditions: c as ScreenerConditionTree }
  }
  // 旧扁平格式 → 单组树（逻辑进树内）
  if (isLogic(o.logic) && Array.isArray(o.conditions) && o.conditions.every(isConditionLeaf)) {
    return {
      universe: o.universe,
      // T-131:旧「空 conditions = 不限条件(全量)」契约保持——空数组映射为 or 空组
      //（后端 or 空组 = 恒 True = 全量），不得映射为 and 空组(= 恒 False = 0 行)
      conditions: {
        logic: o.conditions.length ? o.logic : 'or',
        children: o.conditions as ScreenerCondition[],
      },
    }
  }
  return null
}

function loadSchemes(): ScreenerScheme[] {
  try {
    const raw = JSON.parse(localStorage.getItem(SCHEMES_KEY) ?? '[]') as unknown
    if (!Array.isArray(raw)) return []
    const out: ScreenerScheme[] = []
    for (const x of raw) {
      if (typeof x !== 'object' || x === null) continue
      const o = x as Record<string, unknown>
      if (typeof o.id !== 'string' || typeof o.name !== 'string' || typeof o.createdAt !== 'string') continue
      const filters = normalizeFilters(o.filters)
      if (filters) out.push({ id: o.id, name: o.name, createdAt: o.createdAt, filters })
    }
    return out.slice(0, SCHEMES_LIMIT)
  } catch {
    return []
  }
}

/** 方案 id：优先 crypto.randomUUID（secure context），非安全上下文回退时间戳+随机数 */
const genId = (): string =>
  typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`

/**
 * 命名选股方案 store：save 快照当前条件现场；apply 经 applyFilters 回调写回
 * （ScreenerPage 层组装）。activeId 语义 = 激活（已应用且与当前条件一致）：
 * 条件被手动改动后自动清除，高亮只在真正一致时保留。
 */
export function useScreenerSchemes(opts: {
  currentFilters: ScreenerSchemeFilters
  applyFilters: (f: ScreenerSchemeFilters) => void
}) {
  const { currentFilters, applyFilters } = opts
  const [schemes, setSchemes] = useState<ScreenerScheme[]>(loadSchemes)
  const [activeId, setActiveId] = useState<string | null>(null)

  // 持久化：变更即写（save/remove 为离散动作，无需防抖；首渲染幂等回写）
  useEffect(() => {
    try { localStorage.setItem(SCHEMES_KEY, JSON.stringify(schemes)) } catch { /* ignore */ }
  }, [schemes])

  // 保存当前条件为方案：快照引用 currentFilters（React 状态对象不可变，setter 整体替换，引用安全）
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

  // 应用方案：完整快照替换（universe/logic/conditions），激活标记 = 该方案 id
  const apply = useCallback((id: string) => {
    const s = schemes.find((x) => x.id === id)
    if (!s) return
    applyFilters(s.filters)
    setActiveId(id)
  }, [schemes, applyFilters])

  // 激活一致性：条件被手动改动（与激活方案不一致）→ 清除激活标记
  useEffect(() => {
    if (!activeId) return
    const s = schemes.find((x) => x.id === activeId)
    if (!s || JSON.stringify(s.filters) !== JSON.stringify(currentFilters)) setActiveId(null)
  }, [activeId, schemes, currentFilters])

  return { schemes, save, remove, apply, activeId }
}
