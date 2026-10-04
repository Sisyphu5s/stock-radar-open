import { useEffect, useRef } from 'react'
import type { RefObject } from 'react'
import { useCopilotStore } from '../../../stores/copilotStore'
import type { WorkbenchCtxApi } from './context'

/**
 * 向 copilot 注册 /stocks 上下文提供者。
 * context 每次渲染都变，但注册/注销只应发生一次：context() 回调通过 ctxRef.current 读取最新状态。
 * 返回 ctxRef 供调用方在每次渲染后同步最新 ctx（ctxRef.current = ctx）。
 * active=false(工作台非活跃保活 tab):不注册 provider——providers 按 key 单例,
 * 多实例同时注册会互相覆盖/提前注销,只允许当前激活 tab 占用 /stocks。
 */
export function useWorkbenchCopilot(active: boolean): RefObject<WorkbenchCtxApi | null> {
  const registerProvider = useCopilotStore((s) => s.registerProvider)
  const unregisterProvider = useCopilotStore((s) => s.unregisterProvider)
  const ctxRef = useRef<WorkbenchCtxApi | null>(null)
  useEffect(() => {
    if (!active) return
    registerProvider('/stocks', {
      key: '/stocks',
      context: () => {
        const C = ctxRef.current ?? { code: '', quote: null, klineData: null, period: 'daily', fundamental: null }
        const I = C.klineData?.indicators ?? {}
        const last = (f: string) => { const v = I[f]; return v ? v[v.length - 1] ?? null : null }
        return {
          stock_code: C.code,
          stock_name: C.quote?.name ?? '',
          price: C.quote?.price,
          pct_change: C.quote?.pct_change,
          data_source: C.quote?.source ?? '未知',
          market_status: C.quote ? `${C.quote.source} 行情` : '行情不可用',
          kline_range: C.klineData ? `${C.klineData.dates[0]} ~ ${C.klineData.dates[C.klineData.dates.length - 1]}` : '',
          kline: C.klineData?.close ?? [],
          period: C.period,
          indicators: {
            ma5: last('ma5'), ma10: last('ma10'), ma20: last('ma20'), ma60: last('ma60'),
            boll_upper: last('boll_upper'), boll_mid: last('boll_mid'), boll_lower: last('boll_lower'),
            rsi: last('rsi'), rsi6: last('rsi6'), rsi12: last('rsi12'), rsi24: last('rsi24'),
            kdj_k: last('kdj_k'), kdj_d: last('kdj_d'), kdj_j: last('kdj_j'),
            macd_dif: last('macd_dif'), macd_dea: last('macd_dea'), macd_hist: last('macd_hist'),
            volume_ratio: last('volume_ratio'), atr: last('atr'),
          },
          fundamental: C.fundamental ? { pe: C.fundamental.pe, pb: C.fundamental.pb, market_cap: C.fundamental.market_cap, source: C.fundamental.source } : {},
          selected_stocks: [C.code],
        }
      },
    })
    return () => unregisterProvider('/stocks')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active])
  return ctxRef
}
