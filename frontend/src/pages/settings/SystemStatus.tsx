import { Badge, Button, Code, DataList } from '@mantine/core'
import { IconCircleCheck, IconCircleX, IconRefresh } from '@tabler/icons-react'
import { useCallback, useEffect, useState } from 'react'
import { SettingBlock } from '../../templates/settings'
import CardState from '../../components/ui/CardState'
import StatStrip from '../../components/ui/StatStrip'
import CacheStats from './CacheStats'
import { SourcesHealthTable } from './DataConnections'
import { getSystemHealth, getDataSourceStatus } from '../../api/client'
import type { SystemHealth, DataSourceStatus, SourceHealth } from '../../api/client'
import { formatFullTime } from '../../utils/time'
import { guardedInterval } from '../../utils/guardedInterval'

/**
 * 数据源健康三档聚合（对齐 DataConnections healthOf 展示语义）：
 * ok=可用（green/blue）；degraded=降级/受限/冷却/未探测（结论条降饱和）；bad=硬异常（结论条高亮）。
 */
export type SourceTier = 'ok' | 'degraded' | 'bad'
export function sourceHealthTier(s: SourceHealth): SourceTier {
  if (s.ok) return 'ok'
  if (s.spot_blocked || s.degraded || s.kline_blocked) return 'degraded'
  if (s.last_ok == null && s.last_fail == null) return 'degraded'
  return 'bad'
}

/** L0 结论条单项视图：text=展示文案，color=语义令牌色（--sr-success/--sr-error/--sr-text-3），
 *  reloadable=接口失败时可点重试（不显示"全部正常"假象）。 */
export interface OverviewItemView {
  text: string
  color?: string
  reloadable: boolean
}

export interface SystemOverviewView {
  /** 首次拉取未完成（数字位骨架）；重试不闪骨架 */
  loading: boolean
  service: OverviewItemView
  source: OverviewItemView
  /** 最近一次成功检查时刻（epoch ms，formatFullTime 数字分支直解） */
  checkedAt: number | null
  reload: () => void
}

/**
 * 系统总览（/settings 与 /status 同源，05 §5.7 L0 数据来源）：
 * 服务健康 + 数据源健康，30s 轮询 + 手动重试（与 SourcesHealthTable 同轮询模式）。
 * 三态：首次骨架；任一接口失败 → 对应结论降为「状态未知」且可点重试，绝不虚报"全部正常"；
 * 任一成功 → 记录 checkedAt 供「检查于」展示。
 */
export function useSystemOverview(): SystemOverviewView {
  const [health, setHealth] = useState<SystemHealth | null>(null)
  const [healthFail, setHealthFail] = useState(false)
  const [sources, setSources] = useState<DataSourceStatus | null>(null)
  const [sourcesFail, setSourcesFail] = useState(false)
  const [checkedAt, setCheckedAt] = useState<number | null>(null)
  const [first, setFirst] = useState(true)

  const load = useCallback(async () => {
    const [h, s] = await Promise.allSettled([getSystemHealth(), getDataSourceStatus()])
    if (h.status === 'fulfilled') { setHealth(h.value); setHealthFail(false) } else { setHealthFail(true) }
    if (s.status === 'fulfilled') { setSources(s.value); setSourcesFail(false) } else { setSourcesFail(true) }
    if (h.status === 'fulfilled' || s.status === 'fulfilled') setCheckedAt(Date.now())
    setFirst(false)
  }, [])

  useEffect(() => {
    void load()
    const t = guardedInterval(() => { void load() }, 30000)
    return () => clearInterval(t)
  }, [load])

  const service: OverviewItemView = (() => {
    if (healthFail) return { text: '状态未知', color: 'var(--sr-error)', reloadable: true }
    if (!health) return { text: '—', reloadable: false }
    if (health.status === 'ok') return { text: '运行中', color: 'var(--sr-success)', reloadable: false }
    return { text: health.status ?? '异常', color: 'var(--sr-error)', reloadable: false }
  })()

  const source: OverviewItemView = (() => {
    if (sourcesFail) return { text: '状态未知', color: 'var(--sr-text-3)', reloadable: true }
    if (!sources) return { text: '—', reloadable: false }
    const list = sources.sources
    const ok = list.filter((s) => s.ok).length
    const bad = list.some((s) => sourceHealthTier(s) === 'bad')
    const degraded = !bad && list.some((s) => sourceHealthTier(s) === 'degraded')
    // 异常源高亮（--sr-error）；失败源降饱和计数（--sr-text-3）；全正常 → --sr-success
    const color = bad ? 'var(--sr-error)' : degraded ? 'var(--sr-text-3)' : 'var(--sr-success)'
    return { text: `${ok}/${list.length} 正常`, color, reloadable: false }
  })()

  return { loading: first, service, source, checkedAt, reload: load }
}

/** 服务健康 + 版本信息（GET /api/v1/health，走 api/client，只读；手动刷新按钮重拉） */
function HealthBlock() {
  const [health, setHealth] = useState<SystemHealth | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  const load = useCallback(async () => {
    setRefreshing(true)
    try {
      setHealth(await getSystemHealth())
      setError(null)
    } catch (e: any) {
      setHealth(null)
      setError(String(e?.message ?? e))
    } finally {
      setRefreshing(false)
    }
  }, [])

  useEffect(() => { load() }, [load])

  return (
    <SettingBlock
      title={
        <>
          服务健康与版本
          <Button
            size="xs"
            leftSection={<IconRefresh size={12} />}
            loading={refreshing}
            onClick={load}
            style={{ marginLeft: 'auto' }}
            aria-label="刷新服务状态"
          >
            刷新
          </Button>
        </>
      }
      desc="版本信息由后端 /api/v1/health 提供；下方缓存统计与数据源健康每 30s 自动刷新。"
    >
      <CardState loading={!health && !error} error={error} onRetry={load} loadingText="正在获取服务状态…">
        {health && (
          <DataList size="sm" orientation="horizontal" withDivider labelWidth={104}>
            <DataList.Item>
              <DataList.ItemLabel>服务状态</DataList.ItemLabel>
              <DataList.ItemValue>
                {health.status === 'ok'
                  ? <Badge color="green" leftSection={<IconCircleCheck size={12} />}>运行中</Badge>
                  : <Badge color="red" leftSection={<IconCircleX size={12} />}>{health.status ?? '异常'}</Badge>}
              </DataList.ItemValue>
            </DataList.Item>
            <DataList.Item>
              <DataList.ItemLabel>应用</DataList.ItemLabel>
              <DataList.ItemValue><Code>{health.app ?? '—'}</Code></DataList.ItemValue>
            </DataList.Item>
            <DataList.Item>
              <DataList.ItemLabel>GP 计算后端</DataList.ItemLabel>
              <DataList.ItemValue><Badge color="blue">{health.gp_backend ?? '—'}</Badge></DataList.ItemValue>
            </DataList.Item>
            <DataList.Item>
              <DataList.ItemLabel>原生加速</DataList.ItemLabel>
              <DataList.ItemValue>
                {health.native_available
                  ? <Badge color="green">可用</Badge>
                  : <Badge variant="default">不可用（回退 numpy）</Badge>}
              </DataList.ItemValue>
            </DataList.Item>
          </DataList>
        )}
      </CardState>
    </SettingBlock>
  )
}

/**
 * 系统状态页（/status，只读）：聚合 服务健康/版本 / 缓存统计 / 数据源健康，
 * 设置页仅保留可写项。区块结构走 SettingBlock 模板组合（T4）。
 * L0 结论条（05 §5.7）：服务运行中/异常 · 数据源 N/M 正常 · 检查于 HH:mm；
 * 接口失败 = 状态未知 + 可点重试（不显示"全部正常"假象），色走语义令牌。
 */
export default function SystemStatus() {
  const ov = useSystemOverview()
  const tint = (v: OverviewItemView) => (v.color ? <span style={{ color: v.color }}>{v.text}</span> : v.text)
  return (
    <div className="sr-page">
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-gap-card)', width: '100%', minWidth: 0 }}>
        <StatStrip
          loading={ov.loading}
          items={[
            { key: 'service', label: '服务', value: tint(ov.service), onClick: ov.service.reloadable ? ov.reload : undefined },
            { key: 'source', label: '数据源', value: tint(ov.source), onClick: ov.source.reloadable ? ov.reload : undefined },
            {
              key: 'checked',
              label: '检查于',
              value: ov.checkedAt != null ? formatFullTime(ov.checkedAt, { withYear: false, withSeconds: false }) : '—',
            },
          ]}
        />
        <HealthBlock />
        <CacheStats />
        <SourcesHealthTable />
      </div>
    </div>
  )
}
