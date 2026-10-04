import { useEffect } from 'react'
import type { echarts } from '../utils/echartsSetup'

/**
 * ECharts 图表生命周期（resize 部分）：ResizeObserver 监听容器尺寸变化 → chart.resize()，
 * 卸载时 disconnect。init / setOption / dispose 仍由页面各自负责（图表数据/主题驱动重绘）。
 * 用法：`const ref = useRef<HTMLDivElement>(null); const chart = useRef<echarts.ECharts | null>(null); useEchartsLifecycle(ref, chart)`
 */
export function useEchartsLifecycle<T extends HTMLElement>(
  ref: React.RefObject<T | null>,
  chartRef: React.MutableRefObject<echarts.ECharts | null>,
) {
  useEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => {
      const c = chartRef.current
      if (c && !c.isDisposed()) c.resize()
    })
    ro.observe(el)
    return () => ro.disconnect()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ref])
}
