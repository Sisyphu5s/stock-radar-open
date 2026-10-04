import { useEffect, useRef } from 'react'
import { useCopilotStore } from '../stores/copilotStore'

/**
 * 向 copilot 注册页面上下文提供者：注册/注销只发生一次，不随每渲染 churn。
 * ctx.context 每次渲染都变（闭包读取页面最新状态）：ctxRef 持有最新 ctx，
 * 注册的 context/onFill 回调统一走 ctxRef.current，effect 仅依赖 key。
 * 用法（参照 useWorkbenchCopilot 的 latest-ref 模式）：
 *   useCopilotProvider('/research/backtests', {
 *     context: () => ({ dataset: ..., metrics: ... }),
 *     onFill: (expr) => setExpression(expr),
 *   })
 */
export function useCopilotProvider(
  key: string,
  ctx: { context: () => Record<string, unknown>; onFill?: (expr: string) => void },
) {
  const registerProvider = useCopilotStore((s) => s.registerProvider)
  const unregisterProvider = useCopilotStore((s) => s.unregisterProvider)
  const ctxRef = useRef(ctx)
  useEffect(() => { ctxRef.current = ctx })
  useEffect(() => {
    registerProvider(key, {
      key,
      context: () => ctxRef.current.context(),
      onFill: (expr: string) => ctxRef.current.onFill?.(expr),
    })
    return () => unregisterProvider(key)
  }, [key, registerProvider, unregisterProvider])
}
