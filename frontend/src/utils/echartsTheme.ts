import type { DataZoomComponentOption } from 'echarts'
import { semantic } from '../theme/tokens'

/**
 * ECharts 主题色板：canvas 不解析 CSS var()，必须按 isDark 用 JS 常量切换。
 * 语义色从 src/theme/tokens.ts 单一事实源派生（T-36）：accent / 涨跌 / 背景 / 边框 /
 * 文字 2/3 级 / 网格 —— 与 CSS 变量 --sr-* 完全同源，消除历史漂移
 * （暗色 accent #60a5fa→#4c9aff、text2 #94a3b8→#9aa5b1、grid 0.16→0.18 等，见 DESIGN §1.1）。
 * 涨跌色（up/down）经 tokens.semantic 与 K 线图 KlineChart 同源。
 * dataZoom / amber / accentArea 等基于 accent 的局部派生色保留原值（视觉零变化）。
 */
export function chartTheme(isDark: boolean) {
  const t = isDark ? 'dark' : 'light'
  return {
    accent: semantic.accent[t],
    up: semantic.up[t],
    down: semantic.down[t],
    /** 图表背景（热图中性档/单元格描边等场景） */
    bg: semantic.bg[t],
    /** 分隔线/边框（热图单元格描边等场景） */
    border: semantic.border[t],
    text2: semantic.text2[t],
    text3: semantic.text3[t],
    grid: semantic.chartGrid[t],
    dataZoomBg: isDark ? 'rgba(148,163,184,0.06)' : 'rgba(120,140,160,0.06)',
    dataZoomFill: isDark ? 'rgba(96,165,250,0.18)' : 'rgba(22,104,220,0.16)',
    dataZoomHandle: isDark ? '#e2e8f0' : '#fff',
    dataZoomHandleBorder: semantic.accent[t], // 亮 #1668dc / 暗 #4c9aff(P1-46 旧 #60a5fa → 现 accent 暗)
    amber: isDark ? '#fbbf24' : '#f59f00',
    accentAreaStrong: isDark ? 'rgba(96,165,250,0.28)' : 'rgba(22,104,220,0.28)',
    accentAreaWeak: isDark ? 'rgba(96,165,250,0.02)' : 'rgba(22,104,220,0.02)',
  }
}

/** 通用缩放组件（三页一致：inside + slider） */
export function chartDataZoom(isDark: boolean): DataZoomComponentOption[] {
  const t = chartTheme(isDark)
  return [
    { type: 'inside' },
    {
      type: 'slider', height: 14, bottom: 2, borderColor: 'transparent',
      backgroundColor: t.dataZoomBg, fillerColor: t.dataZoomFill,
      handleStyle: { color: t.dataZoomHandle, borderColor: t.dataZoomHandleBorder, borderWidth: 2 },
    },
  ]
}

/** 通用数值轴（value/scale/网格线/轴标签），页面可用展开后覆盖扩展（如 Backtest 的 log 模式） */
export function chartYAxis(isDark: boolean) {
  const t = chartTheme(isDark)
  return {
    type: 'value' as const,
    scale: true,
    splitLine: { lineStyle: { color: t.grid } },
    axisLabel: { fontSize: 10, color: t.text3 },
  }
}
