/**
 * 图表主题色令牌（canvas 不解析 CSS var()，按 isDark 用 JS 常量切换）——K 线图与 echarts 统计图共同消费的单一事实源。
 * 收敛来源：KlineChart 旧 8 组双主题常量（涨跌/轴/背景/边框/网格/十字线/轴星浮标/线色板/BOLL/均线色）
 * + echartsTheme.t.up/t.down + 暗色提亮特判（'#8020c0' → '#a78bfa'）。
 * 语义色（涨跌/背景/边框/网格）从 src/theme/tokens.ts 派生（与 CSS 变量 --sr-* 同源，T-36），
 * 其余图表专属色（十字线/标签/线色板/BOLL/均线）保留本地值。
 */
import { semantic } from '../theme/tokens'

/**
 * 副图/均线固定线色(明暗通用基础值;非主题敏感)。
 * 色值事实源即本表(禁止迁 tokens 结构——P1-46 决策,大改不做,记注释);
 * 暗色提亮紫由 subPurple / resolveChartColor 特判。
 * klineSubConfig 的 SUB_ANNOT 固定色引用本表常量,禁止各文件重复字面量。
 */
export const SUB_LINE_GOLD = '#f0c000'
export const SUB_LINE_BLUE = '#00a0f0'
export const SUB_LINE_ORANGE = '#f06000'
export const SUB_LINE_PURPLE = '#8020c0' // 暗色对应 '#a78bfa'(见 subPurple)

export interface ChartTokens {
  /** 涨跌色（红涨绿跌，与全站 --sr-up/--sr-down 一致） */
  up: string
  down: string
  /** 坐标轴文字 */
  axis: string
  /** 图表背景 */
  bg: string
  /** 分隔线/边框 */
  border: string
  /** 网格线 */
  grid: string
  /** 十字光标线色（亮色偏深、暗色偏亮，0.55/0.5 alpha 保证双主题清晰细线） */
  crosshair: string
  /** 轴星浮标/竖线列标签令牌（亮色=浅底深字、暗色=深底浅字，取代旧的固定 #334155 深底白字） */
  label: { bg: string; text: string; border: string }
  /** 副图线色板（暗色提亮紫 #a78bfa 承载于本表，亮色 #8020c0） */
  lineColors: string[]
  /** BOLL 三轨线色（亮色沿用原 #9aa7b4 中轴灰，暗色提亮灰蓝 #7f8d9a） */
  boll: string
  /** 主图均线色板（ma60 暗色提亮紫） */
  maColors: Record<string, string>
  /** 暗色提亮紫（'#8020c0' 的暗色对应 '#a78bfa'；resolveChartColor 特判使用） */
  subPurple: string
  /** 副图阈值线（中性细虚线，明暗通用） */
  threshold: string
}

/** 按主题取全套令牌（每次调用新建对象；调用方应放在渲染函数内，随 isDark 变化自然更新） */
export function chartColors(isDark: boolean): ChartTokens {
  const t = isDark ? 'dark' : 'light'
  return {
    up: semantic.up[t],
    down: semantic.down[t],
    axis: isDark ? '#8a98a8' : '#5b6b80',
    bg: semantic.bg[t],
    border: semantic.border[t],
    grid: semantic.chartGrid[t],
    crosshair: isDark ? 'rgba(148, 163, 184, 0.5)' : 'rgba(71, 85, 105, 0.55)',
    label: isDark
      ? { bg: 'rgba(30, 41, 59, 0.96)', text: '#e2e8f0', border: 'rgba(148, 163, 184, 0.28)' }
      : { bg: 'rgba(255, 255, 255, 0.96)', text: '#334155', border: 'rgba(16, 24, 40, 0.14)' },
    lineColors: isDark
      ? [SUB_LINE_GOLD, '#38bdf8', '#fb923c', '#a78bfa', '#ff6b6b', '#2fdd8f', '#22d3ee']
      : [SUB_LINE_GOLD, SUB_LINE_BLUE, SUB_LINE_ORANGE, SUB_LINE_PURPLE, '#d62025', '#067a4f', '#13c2c2'],
    boll: isDark ? '#7f8d9a' : '#9aa7b4',
    maColors: {
      ma5: SUB_LINE_GOLD, ma10: SUB_LINE_BLUE, ma20: SUB_LINE_ORANGE,
      ma60: isDark ? '#a78bfa' : SUB_LINE_PURPLE,
    },
    subPurple: isDark ? '#a78bfa' : SUB_LINE_PURPLE,
    threshold: 'rgba(120, 140, 160, 0.45)',
  }
}

/**
 * 副图配置色 → 实际渲染色（KlineChart 图例与引擎共用同一解析）：
 * 涨跌语义 CSS 变量 + 暗色提亮紫特判在此表收敛；其余色值原样返回。
 */
export function resolveChartColor(c: string, isDark: boolean): string {
  if (c === 'var(--sr-up)') return chartColors(isDark).up
  if (c === 'var(--sr-down)') return chartColors(isDark).down
  if (c === '#8020c0') return chartColors(isDark).subPurple
  return c
}
