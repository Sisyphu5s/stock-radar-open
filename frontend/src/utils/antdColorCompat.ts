/**
 * antd 色名方言 → Mantine 色名 单一事实源(P1-45 收敛)。
 * ------------------------------------------------------------
 * tokens.ts 的 SIGNAL_CATEGORY_COLOR / SECTOR_COLOR / labelMap.color 均为 antd 色名
 * (历史遗留方言),Mantine 渲染处(Badge/Pill)必须翻译为 Mantine 色名。
 * 本表收敛原 SignalTag.tsx 与 workbench/tagColor.ts 的两份本地表(曾内容不一致,
 * SignalTag 多 success/error/processing/warning 四条语义兜底),两份均改 import 本表。
 * ANTD_TO_CSS 承载 antd 预设名 → CSS 合法色值(仅 volcano/geekblue 非 CSS 合法色名,
 * 固定语义色,非主题敏感),MultiSelectGrid 等直接取 background 的场景使用。
 */
export const ANTD_TO_MANTINE: Record<string, string> = {
  red: 'red',
  volcano: 'orange',
  orange: 'orange',
  gold: 'yellow',
  green: 'green',
  cyan: 'cyan',
  blue: 'blue',
  geekblue: 'indigo',
  purple: 'violet',
  magenta: 'grape',
  lime: 'lime',
  // antd 语义色(兜底)
  success: 'green',
  error: 'red',
  processing: 'blue',
  warning: 'orange',
  default: 'gray',
}

/** antd 色名 → Mantine 色名;未收录回落 'gray' */
export function antdToMantine(color?: string | null): string {
  return (color && ANTD_TO_MANTINE[color]) || 'gray'
}

/** antd 预设色名 → CSS 合法色值(仅 volcano/geekblue 非 CSS 合法色名) */
export const ANTD_TO_CSS: Record<string, string> = {
  volcano: '#d4380d',
  geekblue: '#2f54eb',
}
