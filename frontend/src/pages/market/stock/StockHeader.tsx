import { Badge, Button, Text, Title, Tooltip } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconFlask, IconPhoto, IconRefresh, IconStar, IconStarFilled } from '@tabler/icons-react'
import { useNavigate } from 'react-router-dom'
import IconTextButton from '../../../components/ui/IconTextButton'
import { useWorkbench } from '../workbench/context'
import { useCopilotStore } from '../../../stores/copilotStore'
import { downloadDataUrl } from '../../../utils/export'
import { fmtPct } from '../../../utils/format'
import { formatFullTime } from '../../../utils/time'
import './StockHeader.css'

/** 个股固定实体头：名称/代码 · 价格/涨跌 · 来源/更新时间 · 关注星标 · 刷新 · 导出 · 研究此股 */
export default function StockHeader() {
  const c = useWorkbench()
  const { code, quote, errQuote, quoteTime, watchlisted, toggleWatch, wlPending, onRetry, period, klineRef } = c
  const navigate = useNavigate()
  const up = (quote?.pct_change ?? 0) >= 0
  // quoteTime 为服务器上海 naive ISO 字符串（quote.timestamp），字符串直解展示，禁 new Date 按浏览器时区
  const time = quoteTime ? formatFullTime(quoteTime, { withSeconds: false }) : '—'
  // 量比：后端 quote 响应已透传（P1-54 修复）；typeof 收窄防御——无字段显示「—」，禁止自行派生或伪造
  const rawVr = quote?.volume_ratio
  const volRatio = typeof rawVr === 'number' && Number.isFinite(rawVr) ? rawVr : null

  // 研究此股双通道（S7）：① ?code= URL 通道跳回测页预填该股（useResearchUrlSync 单向只读防双向干扰）；
  // ② 唤起 Copilot 面板并预填「分析 {code}」（store.draft，Sender 消费后清空）
  const openResearch = () => {
    navigate(`/research/backtests?code=${encodeURIComponent(code)}`)
    const s = useCopilotStore.getState()
    s.openPanel()
    s.setDraft(`分析 ${code}`)
  }

  // K 线截图导出：经工作台 context 的 klineRef 拿 KlineChart 句柄 → takeScreenshot（canvas→PNG dataURL）
  const exportKlinePng = async () => {
    const dataUrl = await klineRef.current?.takeScreenshot()
    if (!dataUrl) {
      notifications.show({ color: 'yellow', message: '图表尚未就绪，暂时无法截图' })
      return
    }
    downloadDataUrl(`${code}-${period}.png`, dataUrl)
  }

  return (
    <header className="sr-stk-header" aria-label="个股概览">
      <Tooltip label={watchlisted ? '取消关注' : '添加关注'}>
        <Button
          size="xs"
          variant={watchlisted ? 'filled' : 'default'}
          leftSection={watchlisted ? <IconStarFilled size={14} /> : <IconStar size={14} />}
          loading={wlPending}
          onClick={(e) => { e.stopPropagation(); toggleWatch() }}
          aria-pressed={watchlisted}
          aria-label={watchlisted ? '取消关注' : '添加关注'}
          style={{ flexShrink: 0 }}
        />
      </Tooltip>

      <div className="sr-stk-header-id" style={{ flex: '1 1 auto', minWidth: 0 }}>
        {/* 双 h1 打磨：MainLayout 顶栏保留 h1，个股名降为 h2（P2） */}
        <Title order={2} style={{ margin: 0, fontWeight: 700, fontSize: 22, lineHeight: 1.3, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {quote?.name ?? code}
        </Title>
        <Text size="sm" style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-3)', flexShrink: 0 }}>{code}</Text>
      </div>

      <div className="sr-stk-header-price" aria-label="最新价格">
        <span className={up ? 'sr-up' : 'sr-down'} style={{ fontSize: 'var(--sr-font-stat)', fontWeight: 700, lineHeight: 1 }}>
          {quote?.price != null ? Number(quote.price).toFixed(2) : '—'}
        </span>
        <Badge
          variant="light"
          radius="var(--sr-radius-tag)"
          size="sm"
          style={{
            fontWeight: 600,
            margin: 0,
            color: up ? 'var(--sr-up)' : 'var(--sr-down)',
            borderColor: up ? 'var(--sr-up)' : 'var(--sr-down)',
            background: `color-mix(in srgb, ${up ? 'var(--sr-up)' : 'var(--sr-down)'} 12%, transparent)`,
          }}
        >
          {quote?.pct_change != null ? `${up ? '+' : ''}${fmtPct(quote.pct_change / 100, 2)}` : '—'}
        </Badge>
        <span className="sr-stk-header-vr">量比 {volRatio != null ? volRatio.toFixed(2) : '—'}</span>
      </div>

      <div className="sr-stk-header-meta" style={{ fontSize: 'var(--sr-font-xs)', color: 'var(--sr-text-3)' }}>
        <span>行情源: {errQuote ? '不可用' : (quote?.source || '—')}</span>
        <span>更新: {time}</span>
      </div>

      <div style={{ flex: 1, minWidth: 8 }} />

      <div className="sr-stk-header-actions">
        {/* K 线截图导出：单项菜单平铺为直接按钮（T-67，零成本） */}
        <IconTextButton
          icon={<IconPhoto size={14} />}
          text="K线截图"
          tooltip="导出当前 K 线图为 PNG 文件"
          onClick={exportKlinePng}
        />
        <IconTextButton
          icon={<IconFlask size={14} />}
          text="研究此股"
          tooltip="到回测页预填该股研究，并唤起 AI 助手分析"
          onClick={openResearch}
        />
        <IconTextButton
          icon={<IconRefresh size={14} />}
          text="刷新"
          tooltip="刷新当前股票数据"
          onClick={onRetry}
        />
      </div>
    </header>
  )
}
