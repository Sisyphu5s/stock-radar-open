import { ActionIcon, Badge, Button, Text, Tooltip } from '@mantine/core'
import { IconStarFilled } from '@tabler/icons-react'
import SignalTag from '../../../components/ui/SignalTag'
import SignalMomentCell from '../../../components/ui/SignalMomentCell'
import { fmtNum, fmtPct } from '../../../utils/format'
import { ageTagColor } from '../../../utils/signals'
import Sparkline from './Sparkline'
import { fmtClock, quoteTip } from './rowModel'
import type { WlRowView } from './rowModel'
import WatchlistGroupMenu from './WatchlistGroupMenu'
import type { WatchlistGroup } from '../../../api/watchlistGroups'

interface MobileWatchlistProps {
  rows: WlRowView[]
  visibleCount: number
  onLoadMore: () => void
  /** 无限滚动模式：隐藏「加载更多」按钮（由页面 sentinel 自动分批） */
  infinite?: boolean
  onOpen: (code: string) => void
  onUnfollow: (view: WlRowView) => void
  labelOf: (code: string) => { text: string; color: string }
  /** T-12 分组：行菜单（移入/移出分组）数据与回调 */
  groups: WatchlistGroup[]
  codeGroups: Map<string, ReadonlySet<number>>
  onToggleGroup: (code: string, groupId: number) => void
  onManageGroups: () => void
}

/** 触控目标（P1-a11y-5）：星标点击区 ≥28px，ActionIcon 内联定到 28px（见卡片头部） */

/** antd 色名（ageTagColor 出口）→ Mantine Badge 色名：'default' 非 Mantine 色名，落 gray 保视觉 */
const WL_TAG_COLOR: Record<string, string> = { red: 'red', orange: 'orange', blue: 'blue', default: 'gray' }
const wlTagColor = (c: string): string => WL_TAG_COLOR[c] ?? 'gray'

/** 无分组空集（行渲染复用，避免每次建 Set） */
const NO_GROUPS: ReadonlySet<number> = new Set()

/**
 * 移动端关注列表：稳定区域卡片（星标+标的+行情 / 走势 / 信号 / 风险+触发）。
 * 每批 20（手动「加载更多」或无限滚动 sentinel）；信号全部展示（去重、换行，不折叠 +N）；
 * 无 K 线走势显示占位。星标为卡片第一个元素，点击不触发行跳转。
 */
export default function MobileWatchlist({ rows, visibleCount, onLoadMore, infinite, onOpen, onUnfollow, labelOf, groups, codeGroups, onToggleGroup, onManageGroups }: MobileWatchlistProps) {
  const shown = rows.slice(0, visibleCount)
  return (
    <div className="sr-wlm-list">
      <div className="sr-wlm-cards">
        {shown.map((r) => {
          const pctColor = r.pct == null ? undefined : r.pct >= 0 ? 'var(--sr-up)' : 'var(--sr-down)'
          return (
            <div
              key={r.code}
              className="sr-wlm-card"
              role="button"
              tabIndex={0}
              onClick={() => onOpen(r.code)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(r.code) }
              }}
            >
              <div className="sr-wlm-sec sr-wlm-sec-head">
                <Tooltip label="取消关注">
                  <ActionIcon
                    variant="subtle" color="gray"
                    className="sr-wlm-star"
                    style={{ minWidth: 28, minHeight: 28, flexShrink: 0 }}
                    onClick={(e) => { e.stopPropagation(); onUnfollow(r) }}
                    aria-label={`取消关注 ${r.name}`}
                  >
                    <IconStarFilled style={{ color: 'var(--sr-star)' }} />
                  </ActionIcon>
                </Tooltip>
                <WatchlistGroupMenu
                  code={r.code}
                  memberGroupIds={codeGroups.get(r.code) ?? NO_GROUPS}
                  groups={groups}
                  onToggle={onToggleGroup}
                  onManage={onManageGroups}
                />
                <div className="sr-wlm-card-title">
                  <span className="sr-wlm-name">{r.name}</span>
                  <span className="sr-wlm-code">{r.code}</span>
                  {r.sector && <Badge variant="light" color="gray" className="sr-wlm-tag">{r.sector}</Badge>}
                </div>
                <div className="sr-wlm-quote">
                  <span className="sr-wlm-price">{r.price != null ? fmtNum(r.price, 2) : '—'}</span>
                  <span className="sr-wlm-pct" style={{ color: pctColor }}>
                    {r.pct != null ? fmtPct(r.pct / 100, 2) : '—'}
                  </span>
                </div>
              </div>
              <div className="sr-wlm-sec sr-wlm-sec-trend">
                {r.spark
                  ? <Sparkline closes={r.spark.closes} code={r.code} />
                  : <span className="sr-spark-ph" role="img" aria-label="暂无走势数据">—</span>}
              </div>
              <div className="sr-wlm-sec sr-wlm-sec-sig">
                {r.hasSig ? (
                  <div className="sr-wlm-sig-row">
                    {r.signals.map((s) => (
                      <SignalTag key={s} code={s} labelOf={labelOf} size="small" />
                    ))}
                  </div>
                ) : (
                  <Text style={{ color: 'var(--sr-text-2)', fontSize: 'var(--sr-font-xs)' }}>无信号</Text>
                )}
              </div>
              <div className="sr-wlm-sec sr-wlm-sec-meta">
                <div className="sr-wlm-risk">
                  <span className="sr-wl-risk-pair">
                    <span className="sr-wl-risk-label">波</span>{r.vol != null ? fmtPct(r.vol, 1) : '—'}
                  </span>
                  <span className="sr-wl-risk-pair">
                    <span className="sr-wl-risk-label">撤</span>{r.dd != null ? fmtPct(r.dd, 1) : '—'}
                  </span>
                </div>
                <div className="sr-wlm-trig">
                  {r.triggeredAt ? (
                    <SignalMomentCell period={r.period} triggeredAt={r.triggeredAt} asOf={r.asOf} discoveredAt={r.discoveredAt} extraTip={quoteTip(r.quoteAt)} compact />
                  ) : (
                    <span className="sr-wl-time" title="最新行情时点">{fmtClock(r.quoteAt) ?? '—'}</span>
                  )}
                  {r.age && <Badge color={wlTagColor(ageTagColor(r.age))} variant="light" size="sm" className="sr-wlm-tag">{r.age}</Badge>}
                  {r.isNew && <Badge color="red" variant="light" size="sm" className="sr-wlm-tag">新</Badge>}
                </div>
              </div>
            </div>
          )
        })}
      </div>
      {!infinite && visibleCount < rows.length && (
        <Button size="compact-sm" fullWidth onClick={onLoadMore} className="sr-wlm-more">
          加载更多（剩余 {rows.length - visibleCount} 只）
        </Button>
      )}
    </div>
  )
}
