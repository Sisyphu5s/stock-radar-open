import { useCallback, useEffect, useRef } from 'react'
import type { Dispatch, RefObject, SetStateAction } from 'react'

/**
 * 稳定 setter：latest-ref 保持 setter 引用恒定。
 *
 * 适用场景：usePersistentState 等每次渲染新建 setter 的函数（返回值引用不稳定），
 * 若直接放进 useCallback/useMemo 依赖会导致依赖每次渲染变化、回调/ctx 无谓重建。
 * 用法：const setStateStable = useStableSetter(setState)，后续一律调用 setStateStable，
 * 内部经 ref 转发到最新 setter（与手写 useRef + useEffect 同步等价，行为零差异）。
 *
 * 历史实现（P2-19 前）分散三处各自发明：
 * - useWorkbenchData.ts 的 setWbStateRef（本文件提炼为共享实现）
 * - SignalCenter.tsx 的 periodRef / PaperTrading.tsx 的 projectsRef、editingRef
 *   （属并行会话领地 wt-ux，只读未碰，待领地交接后收敛）
 */
export function useStableSetter<T>(setter: Dispatch<SetStateAction<T>>): Dispatch<SetStateAction<T>> {
  const ref = useRef(setter)
  useEffect(() => { ref.current = setter })
  return useCallback((action: SetStateAction<T>) => ref.current(action), [])
}

/**
 * 稳定 latest-ref：value 经 latest-ref 保持最新，返回的 ref 身份恒定。
 *
 * 适用场景：依赖恒稳（useCallback [] / useEffect 缺省该值）的回调/effect 需要读取某渲染期的
 * 最新值，例如 SignalCenter 的 periodRef（reload 恒稳回调读取当前周期）。与 useStableSetter
 * 同构的 latest-ref 模式：手写 useRef + useEffect 同步的等价抽象（effect 阶段赋值，行为零差异）。
 * 新增理由（P2-19）：收敛 SignalCenter.tsx 等手写 useRef+useEffect 同步的 latest-ref
 * （periodRef / loadInfiniteMoreRef 等），消除各页面各自发明的重复模式。
 */
export function useLatestRef<T>(value: T): RefObject<T> {
  const ref = useRef(value)
  useEffect(() => { ref.current = value })
  return ref
}
