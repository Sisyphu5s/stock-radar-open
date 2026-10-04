import { Badge } from '@mantine/core'
import { useVirtualizer } from '@tanstack/react-virtual'
import { useRef } from 'react'
import SignalTag from '../../../components/ui/SignalTag'
import SignalMomentCell from '../../../components/ui/SignalMomentCell'
import { StarButton } from '../signalCenter/columns'
import type { StockGroup } from '../signalCenter/context'
import './mobileSignalList.css'

/** 信号卡片间距：与原 .sr-inbox-mlist 的 gap 一致（虚拟化后由 item 底部 padding 承担） */
const CARD_GAP = 8
/** 信号卡片初始估算高度（px）：measureElement 挂载后立即按实测修正 */
const CARD_ESTIMATE = 140

interface MobileSignalListProps {
  groups: StockGroup[]
  /** 当前时间尺度（1/5/15/30/60/daily/weekly/monthly）：决定时点显示精度 */
  period?: string
  labelOf: (code: string) => { text: string; color: string }
  /** 关注成员集合（页级单一订阅派生，P2-68）：星标 O(1) 成员查找，替代每行独立订阅 */
  watchedCodes: ReadonlySet<string>
  onOpen: (code: string) => void
}

/**
 * 移动端信号流紧凑列表：每只股票一张语义化 article 行卡。
 * - 整卡可点（role=button + Tab 聚焦 + Enter/Space 触发）进入 /stocks/:code
 * - 星标（StarButton，useWatchlistStar 收编）stopPropagation 单独切换关注
 * - 时点显示随周期变化（分钟 HH:mm / 日 MM-DD / 周月 YYYY-MM-DD），相对上下文词（盘中/今天/昨天/本周/本月）以小字叠加
 * - 行内 min-width:0 + ellipsis，不产生横向溢出
 */
export default function MobileSignalList({ groups, period = 'daily', labelOf, watchedCodes, onOpen }: MobileSignalListProps) {
  const listRef = useRef<HTMLDivElement | null>(null)
  // 虚拟化挂载到真实的滚动承载容器：
  // 向上找 overflow-y 为 auto/scroll 的祖先；若候选是"伪滚动容器"（高度随内容增长，
  // scrollHeight===clientHeight 且内容已超出一屏，如移动端布局下的 .sr-stream-body），
  // 说明滚动坐标由更上层承担，继续向上找，最终兜底页面滚动元素。
  // 每次调用均重新探测（数据量变化后容器角色可能切换，缓存会锁死错误容器）。
  const virtualizer = useVirtualizer({
    count: groups.length,
    getScrollElement: () => {
      let el: HTMLElement | null = listRef.current?.parentElement ?? null
      while (el) {
        const s = getComputedStyle(el)
        if (/(auto|scroll|overlay)/.test(s.overflowY || '')) {
          if (el.scrollHeight === el.clientHeight && el.scrollHeight > window.innerHeight) {
            el = el.parentElement
            continue
          }
          return el
        }
        el = el.parentElement
      }
      return document.scrollingElement ?? document.documentElement
    },
    estimateSize: () => CARD_ESTIMATE,
    overscan: 5,
    measureElement: (el) => el.getBoundingClientRect().height,
  })
  return (
    <div className="sr-inbox-mlist" ref={listRef}>
      <div style={{ height: virtualizer.getTotalSize(), position: 'relative', width: '100%' }}>
        {virtualizer.getVirtualItems().map((vi) => {
          const g = groups[vi.index]
          const extraCount = g.signals.length - 3
          return (
            <div
              key={g.code}
              data-index={vi.index}
              ref={virtualizer.measureElement}
              style={{ position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${vi.start}px)`, paddingBottom: CARD_GAP }}
            >
              <article
                className="sr-inbox-mcard"
                role="button"
                tabIndex={0}
                aria-label={`${g.name || g.code} ${g.code}，进入个股`}
                onClick={() => onOpen(g.code)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    onOpen(g.code)
                  }
                }}
              >
                <div className="sr-inbox-mcard-head">
                  <span className="sr-inbox-mcard-star">
                    <StarButton code={g.code} watched={watchedCodes.has(g.code)} />
                  </span>
                  <div className="sr-inbox-mcard-title">
                    <span className="sr-inbox-mcard-name">{g.name || '—'}</span>
                    <span className="sr-inbox-mcard-code">{g.code}</span>
                  </div>
                </div>

                <div className="sr-inbox-mcard-stats">
                  <span className="sr-inbox-mcard-stat">
                    <em>信号时点</em>
                    <span className="sr-inbox-mcard-time">
                      <SignalMomentCell period={period} triggeredAt={g.triggered_at} asOf={g.as_of} discoveredAt={g.discovered_at} />
                    </span>
                  </span>
                </div>

                <div className="sr-inbox-mcard-sigs">
                  {g.signals.slice(0, 3).map((s) => (
                    <SignalTag key={s} code={s} labelOf={labelOf} size="small" />
                  ))}
                  {extraCount > 0 && (
                    <Badge color="gray" variant="light" size="xs" className="sr-inbox-mcard-more">+{extraCount} 信号</Badge>
                  )}
                  {g.events.length > 1 && (
                    <Badge color="blue" variant="light" size="xs" className="sr-inbox-mcard-more">{g.events.length} 次命中</Badge>
                  )}
                  {g.signals.length === 0 && (
                    <Badge color="gray" variant="light" size="xs" className="sr-inbox-mcard-more">无信号</Badge>
                  )}
                </div>
              </article>
            </div>
          )
        })}
      </div>
    </div>
  )
}
