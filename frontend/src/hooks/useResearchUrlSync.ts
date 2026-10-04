import { useCallback, useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'

export interface ResearchUrlParam<T> {
  /** URL 参数名（?key=...） */
  key: string
  /** URL 原始字符串 → 状态值；返回 undefined 表示「URL 无此参数」：该值不参与重灌。
   *  恒返回 undefined（只写不读的参数，如 job）可令该参数仅回写、永不重灌。 */
  parse: (raw: string | null) => T | undefined
  /** 状态值 → URL 字符串；返回 undefined 表示「删除该参数」（删除立即执行，不受防抖影响） */
  serialize: (v: T) => string | undefined
  /** 状态变化 → 回写防抖毫秒（默认 0 立即；仅 serialize 为 undefined 的删除立即执行、不防抖） */
  debounceMs?: number
  /** 回写守卫：返回 false 则本次状态变化不回写 URL（如数据集未选时不写、页面只读不回写） */
  writeIf?: (v: T) => boolean
  /** 是否参与 URL → 状态重灌（默认 true；初始 state 已从 URL 读取、重灌会覆盖手动选择的参数置 false） */
  refill?: boolean
  /** 重灌时是否跳过「组件自写值」（默认 true：URL 值 == writtenRef 自写记录时不重灌，防回写触发重灌覆盖手动编辑） */
  skipSelfWritten?: boolean
}

/**
 * 研究页「URL 参数 ↔ 状态」双向同步统一实现（替代原 FactorMining/FactorEvaluation/Backtest 各自的
 * writtenRef + syncUrl + 回写 effect + URL 重灌 effect 拷贝）。行为基准与原实现逐字节等价：
 *
 * - 回写：状态变化 → 按 spec 序列化写回 URL（replace，不污染历史）；debounceMs>0 防抖，
 *   serialize 返回 undefined（删除）时立即执行；writeIf 返回 false 时不写不删。
 * - writtenRef：记录「组件自身最后一次写入的 URL 值」（原始字符串），URL 重灌时跳过自写值。
 * - 重灌：URL 变化（?key= 解析值 != 当前状态 且 非自写）→ applyRefill 回灌状态；skipSelfWritten:false
 *   关闭自写跳过（无回写页面，如 FactorMining 的 ?ds= 重灌原实现无 writtenRef）。
 * - carry：sync 时自动携带的伴随参数（原 syncUrl 每次写入都带当前表达式语义，保证 URL expr 与状态一致）；
 *   patch 中已含的 key 不覆盖。
 * - 防抖 timer 每参数独立持有，互不重置（等价于原逐参数独立 useEffect）。
 *
 * 用法（FactorEvaluation/Backtest 同构）：
 *   const { sync } = useResearchUrlSync(
 *     {
 *       expr: { key: 'expr', parse: (r) => r || undefined, serialize: (v) => v.trim() || undefined, debounceMs: 500 },
 *       ds:   { key: 'ds', parse: dsParse, serialize: (v) => v != null ? String(v) : undefined, writeIf: (v) => v != null },
 *       job:  { key: 'job', parse: () => undefined, serialize: (v) => v?.id != null ? String(v.id) : undefined, writeIf: (v) => v?.id != null },
 *     },
 *     { expr: expression, ds: selectedDs, job: job?.id },
 *     (k, v) => { if (k === 'expr') setExpression(v as string) },
 *     { carry: ['expr'] },
 *   )
 */
export function useResearchUrlSync<TState extends Record<string, unknown>>(
  params: { [K in keyof TState]: ResearchUrlParam<TState[K]> },
  state: TState,
  applyRefill: (key: keyof TState, value: TState[keyof TState]) => void,
  options?: { carry?: (keyof TState)[] },
): { sync: (patch: Partial<Record<keyof TState, string | undefined>>) => void } {
  const [searchParams, setSearchParams] = useSearchParams()
  // 自写记录：组件最后一次写入 URL 的原始字符串（重灌时跳过，防覆盖手动编辑）
  const writtenRef = useRef<Record<string, string>>({})
  const stateRef = useRef(state)
  stateRef.current = state
  const paramsRef = useRef(params)
  paramsRef.current = params
  const applyRefillRef = useRef(applyRefill)
  applyRefillRef.current = applyRefill
  const carryRef = useRef<(keyof TState)[]>(options?.carry ?? [])
  carryRef.current = options?.carry ?? []
  const timersRef = useRef<Record<string, number>>({})

  // 卸载清理防抖 timer
  useEffect(() => () => {
    for (const k of Object.keys(timersRef.current)) window.clearTimeout(timersRef.current[k])
  }, [])

  const sync = useCallback((patch: Partial<Record<keyof TState, string | undefined>>) => {
    // 携带参数（原 syncUrl 自动带当前表达式语义）：patch 已含的 key 不覆盖
    const all: Record<string, string | undefined> = { ...patch }
    for (const k of carryRef.current) {
      if (k in all) continue
      const spec = (paramsRef.current as unknown as Record<string, ResearchUrlParam<unknown>>)[k as string]
      const v = stateRef.current[k]
      if (spec.writeIf && !spec.writeIf(v)) continue
      all[k as string] = spec.serialize(v)
    }
    for (const [k, v] of Object.entries(all)) {
      if (v && v !== '') writtenRef.current[k] = v
      else delete writtenRef.current[k]
    }
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      for (const [k, v] of Object.entries(all)) {
        if (v && v !== '') next.set(k, v)
        else next.delete(k)
      }
      return next
    }, { replace: true })
  }, [setSearchParams])

  // 回写：依赖 state 全量引用每次渲染重跑，但 prev 比较后仅处理「值变化」的参数；
  // 防抖 timer 按 key 独立清理/创建，互不重置（与逐参数独立 useEffect 等价）
  const prevStateRef = useRef<TState | null>(null)
  useEffect(() => {
    const prev = prevStateRef.current
    prevStateRef.current = state
    for (const [key, spec] of Object.entries(params)) {
      const k = key as keyof TState
      if (prev !== null && prev[k] === state[k]) continue
      const v = state[k] as any
      if (spec.writeIf && !spec.writeIf(v)) continue
      const s = (spec as ResearchUrlParam<any>).serialize(v)
      if (s === undefined) {
        if (timersRef.current[key] != null) { window.clearTimeout(timersRef.current[key]); delete timersRef.current[key] }
        sync({ [key]: undefined } as Partial<Record<keyof TState, string | undefined>>)
      } else if (spec.debounceMs && spec.debounceMs > 0) {
        if (timersRef.current[key] != null) window.clearTimeout(timersRef.current[key])
        timersRef.current[key] = window.setTimeout(() => {
          delete timersRef.current[key]
          sync({ [key]: s } as Partial<Record<keyof TState, string | undefined>>)
        }, spec.debounceMs)
      } else {
        if (timersRef.current[key] != null) { window.clearTimeout(timersRef.current[key]); delete timersRef.current[key] }
        sync({ [key]: s } as Partial<Record<keyof TState, string | undefined>>)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state, sync])

  // 重灌：URL 变化 → 解析值 != 当前状态 且（可选）非自写 → applyRefill
  useEffect(() => {
    for (const [key, spec] of Object.entries(params)) {
      if (spec.refill === false) continue
      const raw = searchParams.get(spec.key)
      const u = (spec as ResearchUrlParam<any>).parse(raw)
      if (u === undefined) continue
      const cur = stateRef.current[key as keyof TState]
      if (u === cur) continue
      if (spec.skipSelfWritten !== false && writtenRef.current[spec.key] === raw) continue
      applyRefillRef.current(key as keyof TState, u)
    }
    // 依赖与原页面一致（仅 searchParams）：params/applyRefill 为常量配置，经 ref 读取最新
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams])

  return { sync }
}
