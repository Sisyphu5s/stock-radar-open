import { Text } from '@mantine/core'
import { useMemo } from 'react'
import { formatDiscoveredGap, formatFullTime, resolveSignalMoment } from '../../utils/time'

/**
 * 信号时点双时单元格(统一组件,全站共用):
 * - 主行「信号」:理论原始时刻(bar 标签 triggered_at 的周期原生精度)+ 相对上下文词
 *   (盘中/今天收盘/昨天/本周/本月)——主行走状态机但 as_of 不参与 label(周/月周期
 *   显示 bar 周期起点而非数据截止日,修正 T-80 前「展示时刻=as_of」的误导);
 * - 副行「扫描」:扫描发现时刻(scan_discovered_at)+ 发现延迟;旧数据(null)仅主行;
 * - tooltip:as_of 数据截止完整时间 · bar 完整时间 · 相对词(状态机 title)∪ 扫描时刻 ∪ extraTip。
 * 纪律:时点语义一律走 resolveSignalMoment 状态机,不新写相对时间/格式分支;
 * 排序/筛选仍按 as_of ?? triggered_at(调用方 sorter 不变),本组件只负责展示。
 */
export default function SignalMomentCell({ period, triggeredAt, asOf, discoveredAt, extraTip, compact }: {
  period: string
  triggeredAt: string | null | undefined
  asOf?: string | null
  discoveredAt?: string | null
  /** 调用方附加 tooltip 行(如关注页行情快照时点);空则不渲染 */
  extraTip?: string | null
  /** 紧凑模式(浮窗/时间线):字号降一档 */
  compact?: boolean
}) {
  // 主行:理论原始时刻(as_of 不参与 label/hint,仅 tooltip 保留完整双时间)
  // P2-76:状态机求值收敛为一次——as_of 为空时 tooltip 复用主行结果 m.title(as_of 不参与→
  // 主行即 tooltip 完整时间,免二次求值);仅传入 as_of 时(后端盘中标记双时间)才独立求值。
  // useMemo 缓存:(period,triggeredAt,asOf) 不变时大表重渲不再执行状态机(旧实现每渲染两次)。
  const m = useMemo(() => resolveSignalMoment(period, triggeredAt, null), [period, triggeredAt])
  const tip = useMemo(
    () => (asOf ? resolveSignalMoment(period, triggeredAt, asOf).title : m.title),
    [asOf, m, period, triggeredAt],
  )
  const gap = formatDiscoveredGap(triggeredAt, discoveredAt)
  const scanFull = discoveredAt ? `扫描 ${formatFullTime(discoveredAt)}${gap ? ` · ${gap}` : ''}` : null
  const title = [tip, scanFull, extraTip].filter(Boolean).join('\n') || undefined
  const mainFont = compact ? 'var(--sr-font-xs)' : 'var(--sr-font-sm)'
  const hintFont = compact ? 'var(--sr-font-meta)' : 'var(--sr-font-xs)'
  const subFont = compact ? 'var(--sr-font-meta)' : 'var(--sr-font-xs)'
  return (
    <>
      <Text
        span
        title={title}
        style={{ fontSize: mainFont, color: 'var(--sr-text-2)', fontVariantNumeric: 'tabular-nums' }}
      >
        <span style={{ marginRight: 4, color: 'var(--sr-text-3)', fontWeight: 600 }}>信号</span>
        {m.label}
        {m.hint && (
          <span style={{ marginLeft: 4, color: m.intraday ? 'var(--sr-warning)' : 'var(--sr-text-3)', fontSize: hintFont }}>{m.hint}</span>
        )}
      </Text>
      {scanFull && (
        <Text span style={{ display: 'block', marginTop: 2, color: 'var(--sr-text-3)', fontSize: subFont }}>
          <span style={{ marginRight: 4, fontWeight: 600 }}>扫描</span>
          {formatFullTime(discoveredAt, { withYear: false, withSeconds: false })}{gap ? ` · ${gap}` : ''}
        </Text>
      )}
    </>
  )
}
