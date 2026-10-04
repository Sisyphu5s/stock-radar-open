import { Alert, Badge, Text, Timeline } from '@mantine/core'
import { IconInfoCircle, IconWorld } from '@tabler/icons-react'
import { useEffect, useState } from 'react'
import CardShell from '../../../../components/ui/CardShell'
import CardState from '../../../../components/ui/CardState'
import EmptyState from '../../../../components/ui/EmptyState'
import { toMarketEpochMs, formatFullTime } from '../../../../utils/time'
import { useWorkbench } from '../context'
import { antdToMantine } from '../tagColor'

/** 数据源最近一次切换判定窗口（ms）：窗口内视为「切换中」，新闻条数可能不全 */
const SWITCH_WINDOW_MS = 5 * 60e3

/** 新闻动态 */
export function NewsSection() {
  const c = useWorkbench()
  const { news, errNews, onRetry, loading } = c
  // 数据源切换中提示（可选消费 /market/sources/status：last_switch_ts 最近 5 分钟内 → 提示；
  // 拉取失败静默，不阻塞新闻主体）
  const [switching, setSwitching] = useState(false)
  useEffect(() => {
    let disposed = false
    fetch('/api/v1/market/sources/status')
      .then((r) => r.json().catch(() => null))
      .then((d) => {
        if (disposed) return
        const ts: unknown = d?.data?.last_switch_ts
        if (typeof ts !== 'string') return
        const t = toMarketEpochMs(ts)
        setSwitching(t > 0 && Date.now() - t < SWITCH_WINDOW_MS)
      })
      .catch(() => {})
    return () => { disposed = true }
  }, [])
  return (
    <CardShell className="sr-news-timeline" icon={<IconWorld size={18} />} title="新闻动态"
      extra={news.length ? <Text span size="xs" className="sr-wb-sec-text" style={{ color: 'var(--sr-text-3)' }}>最近 {news.length} 条</Text> : undefined}>
      {errNews ? (
        <EmptyState text={errNews} onRetry={onRetry} />
      ) : news.length ? (
        <>
          {switching && (
            <Alert color="blue" icon={<IconInfoCircle size={16} />} title="数据源切换中"
              style={{ marginBottom: 'var(--sr-pad-sm)' }}>新闻条数可能不全</Alert>
          )}
          <Timeline active={news.length - 1}>
            {news.map((n, i) => (
              <Timeline.Item key={i} color="var(--sr-accent)">
                <div style={{ paddingBottom: 'var(--sr-pad-xs)' }}>
                  <a href={n.url} target="_blank" rel="noreferrer"
                    style={{ fontSize: 'var(--sr-font-title)', lineHeight: 1.5, color: 'var(--sr-text-1)', display: 'block' }}>
                    {n.title}
                  </a>
                  <div style={{ marginTop: 2, display: 'flex', alignItems: 'center', gap: 'var(--sr-pad-sm)' }}>
                    <Badge variant="light" color={antdToMantine('blue')} radius="var(--sr-radius-tag)" size="sm"
                      style={{ fontSize: 'var(--sr-font-meta)', lineHeight: '16px', padding: '0 var(--sr-pad-xs)' }}>{n.source}</Badge>
                    {/* 时间：后端 naive 串（'%Y-%m-%d %H:%M'，空格分隔，上海时区）——formatFullTime
                        字符串直解（内部归一空格→T），展示 MM-DD HH:mm，禁 new Date 按浏览器时区 */}
                    <Text span size="xs" className="sr-wb-sec-text" style={{ color: 'var(--sr-text-3)' }}>
                      {formatFullTime(n.time, { withYear: false, withSeconds: false })}
                    </Text>
                  </div>
                </div>
              </Timeline.Item>
            ))}
          </Timeline>
        </>
      ) : loading ? (
        // 首载加载态：加载中不显示「暂无」，避免误导
        <CardState loading minHeight={80} loadingText="加载中…">{null}</CardState>
      ) : (
        // 空态描述走 span(display:block)：Mantine EmptyState 的 description 渲染为 <p>，
        // 内部不能放 div 块(validateDOMNesting 报 div-in-p 回归,T-55 保活壳暴露)——行内元素合法
        <EmptyState description={
          <>
            <span>暂无该股票新闻</span>
            <span style={{ display: 'block', marginTop: 4, opacity: 0.75 }}>数据源：东财 / 新浪，可能受过滤规则影响</span>
          </>
        } />
      )}
    </CardShell>
  )
}
