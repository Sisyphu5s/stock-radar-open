/**
 * 因子挖掘「结果与历史」面板（T-43：自 FactorMining 页面迁出，主组件 ≤400 行）。
 * ------------------------------------------------------------
 * 内聚：结果 Tabs 导航态（visited set 保持挂载，切回不丢表格状态）、结果表 + 已选操作、
 * metaView 摘要、上次结果缓存卡、历史任务列表（ExperimentHistory）装配。
 */
import { Button, Flex, Group, Tabs, Text } from '@mantine/core'
import { openConfirmModal } from '@mantine/modals'
import { IconArrowRight, IconChartBar, IconFlask } from '@tabler/icons-react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { formatFullTime } from '../../../utils/time'
import { CardShell, DataTable, EmptyState, MetricStat } from '../../../components/ui'
import FormulaCode from '../../../components/FormulaCode'
import type { FactorResult } from '../../../api/client'
import type { SrColumn } from '../../../components/ui/tableTypes'
import ExperimentHistory from './ExperimentHistory'
import { stepTitle } from './ResultDetailDrawer'

export interface FmResultsPanelProps {
  /** 结果行（job.result.results ?? 缓存结果） */
  results: FactorResult[]
  columns: SrColumn<FactorResult>[]
  selectedExpr: string | null
  selectedDs?: number
  job: any
  meta: any
  cacheInfo: any
  /** 加载缓存结果到当前视图 */
  onLoadCache: () => void
  onClearCache: () => void
  onSelectExpr: (expr: string) => void
  /** 历史任务列表 */
  history: any[]
  histError: string | null
  onReloadHistory: () => void
  onOpenDetail: (h: any) => void
  onOpenEvolve: (h: any) => void
}

/** 结果与历史 Tabs 卡（steps 5）：结果表 / 缓存卡 / 空态 + 历史任务 */
export default function FmResultsPanel(p: FmResultsPanelProps) {
  const navigate = useNavigate()
  const [resultTab, setResultTab] = useState<'result' | 'history'>('result')
  const [visitedTabs, setVisitedTabs] = useState<Set<string>>(() => new Set(['result']))
  const goResultTab = (k: string | null) => {
    const key = k ?? 'result'
    setResultTab(key as 'result' | 'history')
    setVisitedTabs((prev) => new Set(prev).add(key))
  }
  const { results, selectedExpr, selectedDs, job, meta, cacheInfo } = p
  const jobStatus = job?.status
  const dsQs = selectedDs != null ? `&ds=${selectedDs}` : ''
  const goEval = (expr: string) => navigate(`/research/evaluation?expr=${encodeURIComponent(expr)}${dsQs}`)
  const goBt = (expr: string) => navigate(`/research/backtests?expr=${encodeURIComponent(expr)}${dsQs}`)

  const metaView = jobStatus === 'done' ? (
    <Flex wrap="wrap" gap="var(--sr-pad-sm)" style={{ marginTop: 'var(--sr-pad-md)' }}>
      <MetricStat label="股票数" value={meta.stocks} />
      <MetricStat label="总天数" value={meta.days} />
      <MetricStat label="训练/验证/样本外" value={meta.split ? `${meta.split.train}/${meta.split.val}/${meta.split.oos}` : '—'} />
      <MetricStat label="区间" value={meta.dates ? `${meta.dates.train} ~ ${meta.dates.oos_end}` : '—'} />
    </Flex>
  ) : null

  const resultsContent = (
    <Flex direction="column" gap={10} style={{ width: '100%' }}>
      {results.length > 0 && (
        <>
          {jobStatus === 'done' && selectedExpr && (
            <Group wrap="wrap" gap="var(--sr-pad-sm)">
              <Text c="dimmed">已选: <Text component="code" truncate style={{ fontSize: 'var(--sr-font-sm)', maxWidth: 'min(240px, 55vw)' }}><FormulaCode expr={selectedExpr} /></Text></Text>
              <Button variant="default" leftSection={<IconArrowRight size={14} />} disabled={!selectedDs || !selectedExpr}
                onClick={() => goEval(selectedExpr)}>
                评估此因子
              </Button>
              <Button variant="default" leftSection={<IconChartBar size={14} />} disabled={!selectedDs || !selectedExpr}
                onClick={() => goBt(selectedExpr)}>
                回测此因子
              </Button>
            </Group>
          )}
          <DataTable
            rowKey={(r) => r.expression}
            columns={p.columns}
            dataSource={results}
            pagination={{ pageSize: 8, showSizeChanger: false }}
            fillWidth
            onRow={(r) => r.expression === selectedExpr
              ? { style: { background: 'color-mix(in srgb, var(--sr-accent) 10%, transparent)' } }
              : {}}
          />
          {metaView}
        </>
      )}
      {results.length === 0 && cacheInfo && !job && (
        <CardShell title="上次结果（缓存）"
          extra={
            <Group gap={4} wrap="nowrap">
              <Button size="xs" variant="filled" onClick={p.onLoadCache}>加载</Button>
              <Button size="xs" variant="default" onClick={() => openConfirmModal({
                title: '清空本地缓存的结果？',
                labels: { confirm: '清空', cancel: '取消' },
                confirmProps: { color: 'red' },
                onConfirm: p.onClearCache,
              })}>清空</Button>
            </Group>
          }>
          <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
            缓存于 {formatFullTime(cacheInfo.ts)} ·
            {cacheInfo.params?.algorithm ?? 'gp'} · {cacheInfo.params?.target ?? 'ic'} ·
            {cacheInfo.params?.pop_size ?? 120}×{cacheInfo.params?.generations ?? 12} ·
            {cacheInfo.results.length} 条结果
          </Text>
        </CardShell>
      )}
      {results.length === 0 && !cacheInfo && (
        <EmptyState description={jobStatus === 'pending' || jobStatus === 'running'
          ? '任务运行中…结果将自动出现在这里'
          : '暂无实验结果'} />
      )}
    </Flex>
  )

  return (
    <CardShell title={stepTitle(5, '结果与历史', <IconFlask style={{ color: 'var(--sr-accent)' }} />)}>
      <Tabs value={resultTab} onChange={goResultTab} className="sr-fm-tabs">
        <Tabs.List>
          <Tabs.Tab value="result">结果</Tabs.Tab>
          <Tabs.Tab value="history">历史任务 ({p.history.length})</Tabs.Tab>
        </Tabs.List>
      </Tabs>
      {visitedTabs.has('result') && (
        <div style={{ display: resultTab === 'result' ? undefined : 'none' }}>{resultsContent}</div>
      )}
      {visitedTabs.has('history') && (
        <div style={{ display: resultTab === 'history' ? undefined : 'none' }}>
          <ExperimentHistory
            history={p.history}
            error={p.histError}
            onReload={p.onReloadHistory}
            onOpenDetail={p.onOpenDetail}
            onOpenEvolve={p.onOpenEvolve}
          />
        </div>
      )}
    </CardShell>
  )
}
