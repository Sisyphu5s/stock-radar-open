import { fmtPct } from '../../../utils/format'

/** 迷你走势：只呈现实际 K 线数据；数据不足 2 点时不渲染（调用方显示占位）。
 *  涨跌色走 CSS 令牌：DOM SVG 可解析 var(--sr-up/--sr-down)（暗色自动提亮），
 *  免 useThemeStore 订阅与 JS 常量切换（P0-5 令牌化）。
 *  title 提示：代码 + 近 N 日 + 区间涨跌（首尾收盘对比）。 */
export default function Sparkline({ closes, code }: {
  closes?: number[] | null
  /** 代码：写入 title 提示（无则省略） */
  code?: string
}) {
  const pts = closes?.filter((v) => Number.isFinite(v)) ?? []
  if (pts.length < 2) return null
  const first = pts[0]
  const last = pts[pts.length - 1]
  const up = last >= first
  const chg = (last - first) / first
  // 区间涨跌着色与折线一致（涨跌色令牌）；title 补「近 N 日」——数据仅收盘序列无日期字段
  // （P2-11 要求日期信息，无日期数据时以点数标注区间跨度）
  const title = [code && `代码 ${code}`, `近 ${pts.length} 日`, `区间 ${chg >= 0 ? '+' : ''}${fmtPct(chg, 1)}`].filter(Boolean).join(' · ') || undefined
  // viewBox 固定 120×28（与旧 plots canvas 同尺寸），上下留白 3px 避免 stroke 被裁切；
  // 容器 .sr-spark-box 同为 120×28，preserveAspectRatio="none" 无拉伸，末值圆点保持正圆
  const W = 120
  const H = 28
  const PAD = 3
  const min = Math.min(...pts)
  const max = Math.max(...pts)
  const span = max - min || 1
  const step = (W - PAD * 2) / (pts.length - 1)
  const points = pts
    .map((v, i) => {
      const x = PAD + i * step
      const y = H - PAD - ((v - min) / span) * (H - PAD * 2)
      return `${x.toFixed(2)},${y.toFixed(2)}`
    })
    .join(' ')
  // 末值点坐标（与末点折线坐标一致）
  const lastX = W - PAD
  const lastY = H - PAD - ((last - min) / span) * (H - PAD * 2)
  return (
    <div className="sr-spark-box" role="img" aria-label="近期走势" title={title}>
      <svg
        width="100%"
        height="100%"
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        style={{ display: 'block' }}
        aria-hidden="true"
      >
        <polyline
          points={points}
          fill="none"
          stroke={up ? 'var(--sr-up)' : 'var(--sr-down)'}
          strokeWidth={1.4}
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        {/* 末值圆点：涨跌色令牌（内联 SVG 内渲染，容器等比无拉伸） */}
        <circle
          cx={lastX}
          cy={lastY}
          r={2.2}
          fill={up ? 'var(--sr-up)' : 'var(--sr-down)'}
        />
      </svg>
    </div>
  )
}
