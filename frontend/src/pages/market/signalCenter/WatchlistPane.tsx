import { Badge, Button, Loader, Text } from '@mantine/core'
import { IconArrowRight } from '@tabler/icons-react'
import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import type { SignalEvent } from '../../../api/client'
import EmptyState from '../../../components/ui/EmptyState'
import SignalMomentCell from '../../../components/ui/SignalMomentCell'
import SignalTag from '../../../components/ui/SignalTag'
import { invalidateSignalEvents, useSignalEventsState } from '../../../data/market'
import { fmtPct } from '../../../utils/format'
import { groupSignalEvents } from '../../../utils/signals'
import { cnTodayOf, latestEventOf } from '../../../utils/time'
import { evPct, useSignalCenter } from './context'
import './watchlistPane.css'

/** 关注流参数：与关注页（WatchlistPage L157）同参数 → 参数化池同 key 共享一份请求与缓存。
 *  P2-74：窗口 30d → 14d（近 2 周），关注流只需近期信号，避免固定全量拉取 */
const WL_EVENTS_PARAMS = { watchlist_only: true, limit: 5000, time_range: '14d' }
/** 每行信号 Tag 展示数（前 N，超出折叠为 +M，任务卡「前 2+N」） */
const VISIBLE_SIGNALS = 2

/** 左栏关注流行（分组补充展示字段：best 取展示时刻最新，时点列与信号中心口径一致） */
interface WatchlistFlowRow {
  code: string
  name: string
  events: SignalEvent[]
  signals: string[]
  best: SignalEvent
  triggered_at: string | null
  as_of: string | null
  discovered_at: string | null
  /** 时点显示精度（取 best 事件周期；旧数据缺失回退日线） */
  period: string
}

/**
 * 关注流左栏（T-53）：常驻 Splitter 左栏 / 窄屏 Drawer 复用同一组件。
 * - 数据 = GET /signals/events {watchlist_only:true, limit:5000, time_range:'14d'}
 *   （数组端点，14d 窗口；与关注页共享参数化池，全站只发一份请求）
 * - 行 = 有信号的关注股票（14d 窗口），名称/现价涨跌/信号 Tag(前 2+N)/时点 compact；
 *   整行可点 → /stocks/:code
 * - 头部 = 关注 N · 今日新 X + 跳关注页入口
 * - 空态 = 无关注 → 引导添加；有关注但 14d 无信号 → 引导跳关注页
 */
export default function WatchlistPane() {
  const navigate = useNavigate()
  const c = useSignalCenter()
  const { labelOf, watchlistCount } = c

  const eventsState = useSignalEventsState(WL_EVENTS_PARAMS)
  const events = eventsState.value ?? []
  const loading = eventsState.loading && events.length === 0
  const error = eventsState.error && events.length === 0

  // 按股票合并（共享 utils/signals.groupSignalEvents），补充 best/时点/周期；展示时刻降序
  const rows = useMemo<WatchlistFlowRow[]>(() => {
    const out: WatchlistFlowRow[] = []
    for (const g of groupSignalEvents(events).values()) {
      const best = latestEventOf(g.events) ?? g.events[0]
      out.push({
        ...g,
        best,
        triggered_at: best?.triggered_at ?? null,
        as_of: best?.as_of ?? null,
        discovered_at: best?.scan_discovered_at ?? null,
        period: best?.period ?? 'daily',
      })
    }
    // 排序基于展示时刻 as_of ?? triggered_at（与信号中心时点列 sorter 同口径）
    out.sort((a, b) => (b.as_of ?? b.triggered_at ?? '').localeCompare(a.as_of ?? a.triggered_at ?? ''))
    return out
  }, [events])

  // 今日新（上海日历口径，与关注页 todayNewCount 一致）：14d 事件中触发日为今天的条数
  const todayNew = useMemo(() => {
    const today = cnTodayOf()
    return events.filter((e) => e.triggered_at?.slice(0, 10) === today).length
  }, [events])

  const openStock = (code: string) => navigate(`/stocks/${code}`)
  const openWatchlist = () => navigate('/watchlist')

  return (
    <div className="sr-wlp">
      {/* 顶部小头部：关注 N · 今日新 X + 跳转关注页（窄屏 Drawer title=关注流，此处不再重复标题） */}
      <div className="sr-wlp-head">
        <span className="sr-wlp-stats">关注 {watchlistCount} · 今日新 {todayNew}</span>
        <Button size="compact-sm" variant="subtle" className="sr-wlp-goto" px={6}
          onClick={openWatchlist} aria-label="前往关注页">
          <IconArrowRight size={12} style={{ marginRight: 2 }} />关注页
        </Button>
      </div>
      <div className="sr-wlp-list">
        {error ? (
          <EmptyState text="关注信号加载失败" onRetry={() => invalidateSignalEvents()} />
        ) : loading ? (
          <div className="sr-wlp-state">
            <Loader size="sm" />
            <Text span style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-2)' }}>加载关注信号…</Text>
          </div>
        ) : watchlistCount === 0 ? (
          <EmptyState
            description="暂无关注股票，可在「信号中心 / 个股工作台」点击星标添加关注"
            padding="24px 0"
          />
        ) : rows.length === 0 ? (
          <div className="sr-wlp-empty">
            <EmptyState description="关注股票近 14 日暂无信号" padding="24px 0" />
            <Button size="compact-sm" variant="subtle" onClick={openWatchlist}>前往关注页</Button>
          </div>
        ) : (
          rows.map((g) => {
            const price = g.best.evidence?.price
            const pct = evPct(g.best.evidence)
            const hasPct = Number.isFinite(pct)
            const extraCount = g.signals.length - VISIBLE_SIGNALS
            return (
              <div
                key={g.code}
                className="sr-wlp-row"
                role="button"
                tabIndex={0}
                aria-label={`${g.name || g.code} ${g.code}，进入个股`}
                onClick={() => openStock(g.code)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    openStock(g.code)
                  }
                }}
              >
                <div className="sr-wlp-line">
                  <span className="sr-wlp-name">{g.name || '—'}</span>
                  <span className="sr-wlp-code">{g.code}</span>
                  <span className="sr-wlp-quote">
                    <span className="sr-wlp-price">{price != null ? price : '—'}</span>
                    <span className="sr-wlp-pct" style={{ color: hasPct ? (pct >= 0 ? 'var(--sr-up)' : 'var(--sr-down)') : undefined }}>
                      {hasPct ? fmtPct(pct / 100) : '—'}
                    </span>
                  </span>
                </div>
                <div className="sr-wlp-line">
                  <span className="sr-wlp-sigs">
                    {g.signals.slice(0, VISIBLE_SIGNALS).map((s) => (
                      <SignalTag key={s} code={s} labelOf={labelOf} size="small" />
                    ))}
                    {extraCount > 0 && (
                      <Badge color="gray" variant="light" size="xs" className="sr-wlp-more" title={g.signals.join(', ')}>
                        +{extraCount}
                      </Badge>
                    )}
                  </span>
                  <span className="sr-wlp-moment">
                    <SignalMomentCell compact period={g.period} triggeredAt={g.triggered_at} asOf={g.as_of} discoveredAt={g.discovered_at} />
                  </span>
                </div>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
