import { Skeleton, Text } from '@mantine/core'
import { useIndicesState } from '../../../data/indices'
import type { IndexQuote } from '../../../api/client'
import { formatSignalTime } from '../../../utils/time'
import './indexStrip.css'

/** 8 只指数展示顺序与失败兜底名称（T-73）。
 *  正常数据以后端返回为准（name 以 API 契约为准）；数据源失败/空响应时用本地表
 *  兜底渲染名称位 + 占位符，保证指数条形状稳定、不闪跳（静默降级，不阻塞页面）。 */
const FALLBACK_ORDER: { code: string; name: string }[] = [
  { code: '000001.SH', name: '上证指数' },
  { code: '399001.SZ', name: '深证成指' },
  { code: '399006.SZ', name: '创业板指' },
  { code: '000300.SH', name: '沪深300' },
  { code: '.DJI', name: '道琼斯' },
  { code: '.IXIC', name: '纳斯达克' },
  { code: '.INX', name: '标普500' },
  { code: 'HSI', name: '恒生指数' },
]

function toneOf(pct: number | null): 'up' | 'down' | 'flat' {
  if (pct == null) return 'flat'
  if (pct > 0) return 'up'
  if (pct < 0) return 'down'
  return 'flat'
}

const TONE_COLOR = { up: 'var(--sr-up)', down: 'var(--sr-down)', flat: 'var(--sr-text-2)' } as const

const fmtPrice = (v: number): string =>
  v >= 10000 ? v.toLocaleString('en-US', { maximumFractionDigits: 1 })
    : v >= 1000 ? v.toLocaleString('en-US', { maximumFractionDigits: 1 })
      : v.toFixed(2)

const fmtPct = (v: number): string => `${v > 0 ? '+' : ''}${v.toFixed(2)}%`

/**
 * 全球核心指数条（T-73）：8 只指数（名称 / 现价 / 涨跌幅）横向条。
 * - 涨跌色 var(--sr-up/--sr-down)（0/缺失 = 主文字色）；数字 tabular-nums；
 * - 窄屏 flex-wrap 换行（组件不设固定宽度，随容器收缩）；
 * - loading 骨架（首拉）；数据失败静默降级——显示名称 + —，不报错不阻塞；
 * - 挂载于 SignalCenter（SignalStreamPanel）L0 结论条之下，保持结论条首屏。
 */
export default function IndexStrip() {
  const { value, loading } = useIndicesState()
  const quotes = value ?? []

  return (
    <div className="sr-index-strip" role="group" aria-label="全球指数">
      {FALLBACK_ORDER.map(({ code, name }) => {
        const q: IndexQuote | undefined = quotes.find((x) => x.code === code)
        const shown = q && q.price != null
        const pct = q?.pct_change ?? null
        const color = TONE_COLOR[shown ? toneOf(pct) : 'flat']
        return (
          <div className="sr-index-item" key={code} title={q?.updated_at ? `行情 ${formatSignalTime(q.updated_at, '60')}` : undefined}>
            <Text span className="sr-index-name">{q?.name ?? name}</Text>
            {loading && !shown ? (
              <Skeleton height={16} width={56} radius="sm" />
            ) : (
              <span className="sr-index-price" style={{ color }}>
                {shown ? fmtPrice(q!.price!) : '—'}
                <span className="sr-index-pct">{shown ? fmtPct(pct!) : ''}</span>
              </span>
            )}
          </div>
        )
      })}
    </div>
  )
}
