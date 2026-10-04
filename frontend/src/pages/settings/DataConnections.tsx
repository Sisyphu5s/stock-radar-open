import { Alert, Badge, Button, SegmentedControl, Stack, Text, Tooltip } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconApi, IconCircleCheck, IconCircleX, IconClock, IconRefresh } from '@tabler/icons-react'
import { useEffect, useState } from 'react'
import QueryRedirect from '../../app/adapters/QueryRedirect'
import { CardState, FormRow } from '../../components/ui'
import { SettingBlock } from '../../templates'
import { formatFullTime } from '../../utils/time'
import { guardedInterval } from '../../utils/guardedInterval'
import { getDataSourceStatus, probeDataSources, switchDataSource } from '../../api/client'
import type { DataSourceStatus, SourceHealth } from '../../api/client'

/** antd message → @mantine/notifications（T-44：色/时长对齐 research/shared/toast.ts 契约） */
const notifySuccess = (m: string) => notifications.show({ message: m, color: 'teal', autoClose: 2500 })
const notifyError = (m: string) => notifications.show({ message: m, color: 'red', autoClose: 4000 })

/** antd 色名 → Mantine 色名（healthOf 返回 antd 色域，Badge 需 Mantine 色名） */
const ANT_TO_MANTINE: Record<string, string> = {
  default: 'gray', blue: 'blue', green: 'green', red: 'red', orange: 'orange', gold: 'yellow',
  volcano: 'orange', purple: 'grape', cyan: 'cyan', magenta: 'pink', geekblue: 'indigo',
  success: 'teal', processing: 'blue', error: 'red', warning: 'yellow',
}
const badgeColor = (c: string): string => ANT_TO_MANTINE[c] ?? 'gray'

const FALLBACK_NAMES: Record<string, string> = { akshare: 'akshare', sina: '新浪财经', tencent: '腾讯行情' }

/** 时间显示统一走 formatFullTime（字符串 naive ISO 直解，禁 new Date 按浏览器时区解析；
 *  number 分支 epoch ms 绝对时刻，秒级/毫秒级时间戳归一后可用） */
const fmtTime = (v: string | number | null | undefined): string => {
  if (v == null || v === '') return '—'
  if (typeof v === 'number') return formatFullTime(v > 1e12 ? v : v * 1000, { withSeconds: false })
  return formatFullTime(v, { withSeconds: false })
}

const fmtLatency = (ms: number | null | undefined): string => {
  if (ms == null) return '—'
  return ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms'
}

const fmtRemaining = (until: number | null): number | null => {
  if (until == null) return null
  const ts = until > 1e12 ? until : until * 1000
  return Math.max(0, Math.ceil((ts - Date.now()) / 1000))
}

function healthOf(s: SourceHealth): { color: string; text: string } {
  if (s.ok && s.active) return { color: 'green', text: '使用中' }
  if (s.ok) return { color: 'blue', text: '正常' }
  if (s.spot_blocked) return { color: 'orange', text: '冷却中' }
  if (s.degraded) return { color: 'gold', text: '慢源降级' }
  if (s.kline_blocked) return { color: 'volcano', text: 'K线受限' }
  if (s.last_ok == null && s.last_fail == null) return { color: 'default', text: '未探测' }
  return { color: 'red', text: '异常' }
}

function Cooldown({ until }: { until: number | null }) {
  const [, setNow] = useState(Date.now())
  useEffect(() => {
    // 冷却倒计时 UI 时钟：后台隐藏暂停（倒计时不可见），回前台由下一 tick 续上
    const t = guardedInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])
  const sec = fmtRemaining(until)
  if (sec == null) return null
  return <Badge color="orange" leftSection={<IconClock size={12} />}>冷却中 {sec}s</Badge>
}

function Meta({ label, value, tone }: { label: string; value: string; tone?: 'ok' | 'bad' | 'dim' }) {
  const color = value === '—' || tone === 'dim' ? 'var(--sr-text-3)' : tone === 'ok' ? 'var(--sr-success)' : tone === 'bad' ? 'var(--sr-error)' : 'var(--sr-text-2)'
  return (
    <div>
      <div style={{ fontSize: 'var(--sr-font-xs)', color: 'var(--sr-text-3)' }}>{label}</div>
      <div style={{ fontSize: 'var(--sr-font-sm)', color }}>{value}</div>
    </div>
  )
}

/** 拉取数据源状态（GET /market/sources/status，走 api/client）→ 状态 + 错误信息 */
async function fetchSourceStatus(): Promise<{ data: DataSourceStatus | null; error: string | null }> {
  try {
    return { data: await getDataSourceStatus(), error: null }
  } catch (e: any) {
    return { data: null, error: errMsg(e) }
  }
}

/** axios 错误 → 展示文案：FastAPI detail 优先，其次 message */
const errMsg = (e: any): string => String(e?.response?.data?.detail ?? e?.message ?? e)

/**
 * 数据源健康表（只读，供系统状态页）：每源健康态 / 延迟 / 失败 / 最近成功失败 / 最近错误。
 * 30s 轮询 + 手动刷新；探测与切换操作在数据连接页（DataConnectionsSection）。
 * 与 DataConnectionsSection 各自独立轮询（同 URL 双挂载场景极少，保持组件自治）。
 */
export function SourcesHealthTable() {
  const [status, setStatus] = useState<DataSourceStatus | null>(null)
  const [loadErr, setLoadErr] = useState<string | null>(null)

  const load = async () => {
    const { data, error } = await fetchSourceStatus()
    setStatus(data)
    setLoadErr(error)
  }

  useEffect(() => {
    load()
    const t = guardedInterval(load, 30000)
    return () => clearInterval(t)
  }, [])

  return (
    <SettingBlock
      icon={<IconClock size={14} />}
      title={
        <>
          数据源健康
          <Button size="xs" leftSection={<IconRefresh size={12} />} onClick={load} style={{ marginLeft: 'auto' }}>刷新</Button>
        </>
      }
    >
      <CardState loading={!status && !loadErr} error={loadErr} onRetry={load} loadingText="正在获取状态…">
        {status && (
          <Stack gap={8}>
            {status.sources.map((s) => {
              const h = healthOf(s)
              const rem = s.spot_blocked ? fmtRemaining(s.blocked_until) : null
              return (
                <FormRow
                  key={s.key}
                  width="clamp(120px, 18vw, 180px)"
                  label={
                    <div style={{ minWidth: 'clamp(120px, 18vw, 180px)' }}>
                      <Text fw={700} style={{ color: 'var(--sr-text-1)' }}>{s.name}</Text>
                      <div style={{ fontSize: 'var(--sr-font-xs)', color: 'var(--sr-text-3)', fontFamily: 'monospace' }}>{s.key}</div>
                    </div>
                  }
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sr-pad-xl)', flexWrap: 'wrap', minWidth: 0 }}>
                    <Badge color={badgeColor(h.color)} leftSection={h.text === '使用中' ? <IconCircleCheck size={12} /> : undefined} radius="sm">{h.text}</Badge>
                    {rem != null && <Cooldown until={s.blocked_until} />}
                    <Meta label="延迟" value={fmtLatency(s.latency_ms)} />
                    <Meta label="失败次数" value={String(s.spot_fails ?? 0)} tone={(s.spot_fails ?? 0) > 0 ? 'bad' : undefined} />
                    <Meta label="最近成功" value={fmtTime(s.last_ok)} tone="ok" />
                    <Meta label="最近失败" value={fmtTime(s.last_fail)} tone={(s.last_fail ?? null) ? 'bad' : 'dim'} />
                    <div style={{ flex: 1, minWidth: 'clamp(100px, 12vw, 160px)' }}>
                      <div style={{ fontSize: 'var(--sr-font-xs)', color: 'var(--sr-text-3)' }}>最近错误</div>
                      <Tooltip label={s.last_error || undefined}>
                        <div style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-2)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: 'clamp(120px, 20vw, 200px)', cursor: s.last_error ? 'help' : 'default' }}>
                          {s.last_error ? <IconCircleX size={12} style={{ color: 'var(--sr-error)', marginRight: 'var(--sr-pad-xs)', verticalAlign: -2 }} /> : null}
                          {s.last_error || '—'}
                        </div>
                      </Tooltip>
                    </div>
                  </div>
                </FormRow>
              )
            })}
          </Stack>
        )}
      </CardState>
    </SettingBlock>
  )
}

/** 数据连接区块（无 PageHeader / Card，供设置工作区嵌入）：源切换操作；健康表已迁系统状态页 */
export function DataConnectionsSection() {
  const [status, setStatus] = useState<DataSourceStatus | null>(null)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [probing, setProbing] = useState(false)
  const [switching, setSwitching] = useState(false)

  const nameOf = (key: string | null | undefined): string => {
    if (!key) return '—'
    return status?.sources.find((s) => s.key === key)?.name || FALLBACK_NAMES[key] || key
  }

  const load = async () => {
    const { data, error } = await fetchSourceStatus()
    setStatus(data)
    setLoadErr(error)
  }

  useEffect(() => {
    load()
    const t = guardedInterval(load, 30000)
    return () => clearInterval(t)
  }, [])

  const doSwitch = async (target: string) => {
    setSwitching(true)
    try {
      setStatus(await switchDataSource(target))
      notifySuccess(target === 'auto' ? '已切换为自动切换模式' : `已切换至「${nameOf(target)}」`)
    } catch (e: any) {
      notifyError('切换失败: ' + errMsg(e))
    } finally {
      setSwitching(false)
    }
  }

  const doProbe = async () => {
    setProbing(true)
    try {
      setStatus(await probeDataSources())
      notifySuccess('探测完成')
    } catch (e: any) {
      notifyError('探测失败: ' + errMsg(e))
    } finally {
      setProbing(false)
    }
  }

  const onModeChange = (v: string | number) => {
    const m = String(v)
    if (m === status?.mode) return
    if (m === 'auto') {
      doSwitch('auto')
    } else {
      doSwitch(status?.active ?? status?.preference?.[0] ?? 'akshare')
    }
  }

  return (
    <SettingBlock
      icon={<IconApi size={14} />}
      title={
        <>
          运行状态
          <Button size="xs" leftSection={<IconRefresh size={12} />} loading={probing} onClick={doProbe} style={{ marginLeft: 'auto' }}>立即探测</Button>
        </>
      }
    >
      <CardState loading={!status && !loadErr} error={loadErr} onRetry={load} loadingText="正在获取状态…">
        {status && (
          <Stack gap={12}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sr-gap-card)', flexWrap: 'wrap', minWidth: 0 }}>
              <SegmentedControl
                data={[
                  { label: '自动切换', value: 'auto' },
                  { label: '手动选择', value: 'manual' },
                ]}
                value={status.mode}
                disabled={switching}
                onChange={onModeChange}
              />
              <Badge color={status.mode === 'auto' ? 'blue' : 'orange'} leftSection={<IconCircleCheck size={12} />} radius="sm">
                {status.mode === 'auto' ? '自动切换中…' : '手动模式'}
              </Badge>
              <Tooltip label={`当前活跃数据源 ${nameOf(status.active)}`}>
                <span style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-2)' }}>
                  当前活跃源 <Text span fw={700} style={{ color: 'var(--sr-accent)' }}>{nameOf(status.active)}</Text>
                </span>
              </Tooltip>
              <span style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-3)' }}>
                切换原因: {status.last_switch_reason || '—'}
              </span>
              <span style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-3)' }}>
                最近切换: {fmtTime(status.last_switch_ts)}
              </span>
            </div>
          </Stack>
        )}
      </CardState>
      {loadErr && <Alert color="yellow" title={`状态获取失败: ${loadErr}（每 30s 自动重试）`} style={{ marginTop: 'var(--sr-gap-card)' }} />}
    </SettingBlock>
  )
}

/** 旧深链 /settings/data：合并到单一设置工作区（query 原样保留，如 ?section=data） */
export default function DataConnections() {
  return <QueryRedirect to="/settings" />
}
