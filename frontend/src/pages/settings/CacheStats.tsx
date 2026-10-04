import { Badge, Button, Text } from '@mantine/core'
import { IconRefresh } from '@tabler/icons-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { SrColumn } from '../../components/ui/tableTypes'
import DataTable from '../../components/ui/DataTable'
import CardState from '../../components/ui/CardState'
import { SettingBlock } from '../../templates'
import { getCacheStats } from '../../api/client'
import type { CacheInstanceStats } from '../../api/client'

/** antd 色名 → Mantine 色名（命中率 Tag 色域转换） */
const ANT_TO_MANTINE: Record<string, string> = {
  default: 'gray', blue: 'blue', green: 'green', red: 'red', orange: 'orange', gold: 'yellow',
  volcano: 'orange', purple: 'grape', cyan: 'cyan', magenta: 'pink', geekblue: 'indigo',
  success: 'teal', processing: 'blue', error: 'red', warning: 'yellow',
}
const badgeColor = (c: string): string => ANT_TO_MANTINE[c] ?? 'gray'

/** 实例名 → 中文展示名（后端 _CACHE_INSTANCES 固定集合） */
const INSTANCE_LABEL: Record<string, string> = {
  panel: '行情面板',
  indicator: '指标',
  risk: '风险',
  tune: '调优',
  snapshot: '快照',
  news: '新闻',
  total: '合计',
}

interface Row extends CacheInstanceStats {
  key: string
  name: string
  isTotal: boolean
}

/**
 * 缓存统计：读取 /api/v1/system/cache-stats（走 api/client），展示各全局缓存实例的命中率 / 容量 / 命中次数。
 * 供「系统状态」页（/status）使用；挂载拉取一次 + 手动刷新；失败降级为可重试提示（不打断同页其余区块）。
 * 区块结构走 SettingBlock 模板（与同页 HealthBlock/SourcesHealthTable 一致）。
 */
export default function CacheStats() {
  const [stats, setStats] = useState<Record<string, CacheInstanceStats> | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setStats(await getCacheStats())
    } catch {
      setStats(null)
      setError('缓存统计加载失败（后端进程内累计，不影响使用）')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const rows = useMemo<Row[]>(
    () => (stats
      ? Object.entries(stats).map(([key, s]) => ({
          key,
          name: INSTANCE_LABEL[key] ?? key,
          isTotal: key === 'total',
          ...s,
        }))
      : []),
    [stats],
  )

  const columns = useMemo<SrColumn<Row>[]>(
    () => [
      {
        title: '实例',
        dataIndex: 'name',
        render: (name: string, r) => (r.isTotal ? <Text fw={700}>{name}</Text> : name),
      },
      {
        title: '命中率',
        dataIndex: 'hit_rate',
        align: 'right',
        render: (v: number, r) => {
          const hasAny = r.hits + r.misses > 0
          return (
            <Badge color={badgeColor(hasAny && v >= 0.8 ? 'green' : hasAny && v >= 0.5 ? 'blue' : 'default')} radius="sm">
              {hasAny ? `${(v * 100).toFixed(1)}%` : '—'}
            </Badge>
          )
        },
      },
      {
        title: '容量（条）',
        dataIndex: 'size',
        align: 'right',
        render: (v: number) => v.toLocaleString(),
      },
      {
        title: '命中 / 未命中',
        align: 'right',
        render: (_, r) => `${r.hits.toLocaleString()} / ${r.misses.toLocaleString()}`,
      },
    ],
    [],
  )

  return (
    <SettingBlock
      title={
        <>
          缓存统计
          <Button
            size="xs" variant="subtle" leftSection={<IconRefresh size={14} />}
            loading={loading} onClick={load} aria-label="刷新缓存统计"
            style={{ marginLeft: 'auto' }}
          />
        </>
      }
    >
      <CardState
        loading={loading && !stats}
        error={error}
        onRetry={load}
        loadingText="加载缓存统计…"
      >
        <DataTable
          rowKey="key"
          columns={columns}
          dataSource={rows}
          pagination={false}
          loading={loading}
          scrollX={false}
          emptyText="暂无缓存统计"
        />
      </CardState>
    </SettingBlock>
  )
}
