/**
 * 因子挖掘步骤卡（T-43：自 FactorMining 页面迁出）：
 * 步骤 1 数据集 + 步骤 4 运行（含任务进度/SSE 监控/失败错误）。
 */
import { Button, Flex, Group, Text } from '@mantine/core'
import { IconPlayerPlay } from '@tabler/icons-react'
import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { CardShell, DatasetSelector, TaskProgress } from '../../../components/ui'
import MonitorPanel from '../shared/MonitorPanel'
import type { JobEvent } from '../../../hooks/useJobEvents'

export interface FmRunCardsProps {
  datasets: any[]
  universes: { key: string; name: string }[]
  selectedDs?: number
  onDataset: (v: number | undefined) => void
  /** 步骤编号徽章（ResultDetailDrawer.stepTitle 注入，风格统一） */
  stepTitle: (no: number, text: string, icon?: ReactNode) => ReactNode
  polling: boolean
  onRun: () => void
  job: any
  jobStatus?: string
  events: JobEvent[] | null
}

/** 步骤 1（数据集）+ 步骤 4（运行）两块步骤卡 */
export default function FmRunCards(p: FmRunCardsProps) {
  const navigate = useNavigate()
  const { datasets, universes, selectedDs, onDataset, stepTitle } = p
  return (
    <>
      <CardShell className="sr-disc-step-card" title={stepTitle(1, '数据集')}>
        <Group wrap="wrap" gap="var(--sr-pad-sm)">
          <DatasetSelector
            datasets={datasets.map((d: any) => ({ id: d.id, name: `${d.name} (${d.stock_count}只 / ${d.row_count}行)` }))}
            value={selectedDs}
            onChange={onDataset}
            width="min(280px, 100%)"
          />
          <Button variant="default" onClick={() => navigate('/research/datasets')}>管理数据集</Button>
          <Text c="dimmed">universe: {universes.map((u) => `${u.key}:${u.name}`).join(' / ')}</Text>
        </Group>
      </CardShell>

      <CardShell className="sr-disc-step-card" title={stepTitle(4, '运行')}>
        <Flex direction="column" gap={10} style={{ width: '100%' }}>
          <Button variant="filled" leftSection={<IconPlayerPlay size={16} />} loading={p.polling} onClick={p.onRun} fullWidth>
            {p.jobStatus === 'running' || p.jobStatus === 'pending' ? '任务运行中…' : '开始符号回归'}
          </Button>
          {p.job && (
            <>
              <TaskProgress job={p.job} typeLabel={{ gp_run: 'GP 挖掘' }} />
              <MonitorPanel job={p.job} events={p.events} />
              {p.job.status === 'failed' && <Text c="red" style={{ fontSize: 'var(--sr-font-sm)' }}>{p.job.error}</Text>}
            </>
          )}
        </Flex>
      </CardShell>
    </>
  )
}
