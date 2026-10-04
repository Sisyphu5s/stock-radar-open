import { Button, Flex, Group, Modal, Progress, Text } from '@mantine/core'
import { IconChartBar, IconCheck, IconFileSearch, IconPlayerPlay } from '@tabler/icons-react'
import { useMemo } from 'react'
import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import type { SrColumn } from '../../../components/ui/tableTypes'
import type { FactorResult } from '../../../api/client'
import { toLatexBatch } from '../../../api/client'
import { fmtPct, fmtRatio, fmtStab, pctColor } from '../../../utils/format'
import FormulaCode from '../../../components/FormulaCode'
import LatexFormula from '../../../components/LatexFormula'
import { CardState, DataTable } from '../../../components/ui'
import DataList from '../shared/DataList'
import { ALGORITHM_OPTIONS, TARGET_OPTIONS, optionLabel } from './FmForm'

/** 指标单元格：空值灰横线；非零按涨跌色（pctColor）着色 */
export const MetricCell = ({ v, text }: { v: number | null | undefined; text: string }) => {
  if (v === null || v === undefined || Number.isNaN(v)) return <Text c="dimmed">—</Text>
  const color = v === 0 ? undefined : pctColor(v)
  return <span style={{ fontSize: 'var(--sr-font-sm)', color }}>{text}</span>
}

/** 步骤编号徽章（主组件五卡标题统一风格） */
export const stepTitle = (no: number, text: string, icon?: ReactNode) => (
  <Group gap="var(--sr-pad-sm)" wrap="nowrap">
    <span style={{
      display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
      width: 20, height: 20, borderRadius: 6, fontSize: 'var(--sr-font-sm)', fontWeight: 700,
      background: 'var(--sr-accent)', color: 'var(--sr-card-bg)',
    }}>{no}</span>
    {icon}
    <span>{text}</span>
  </Group>
)

/** 表达式列表 → LaTeX map（详情抽屉与结果表格共用一份构建逻辑） */
export const buildLatexMap = async (exprs: string[]): Promise<Record<string, string>> => {
  const items = await toLatexBatch(exprs)
  const m: Record<string, string> = {}
  items.forEach((i) => { m[i.expression] = i.latex })
  return m
}

export interface MakeResultColumnsOpts {
  latex: Record<string, string>
  withSelect?: boolean
  selectedExpr?: string | null
  onSelectExpr?: (v: string) => void
  /** 跨页跳转参数（?ds= 尾缀；空串不带） */
  dsQs?: string
  navigate: (path: string) => void
}

/**
 * 因子挖掘结果表列定义（含 LaTeX 映射；主组件结果表与详情 Modal 共用一份构建逻辑）。
 * latex 映射缺失时回退 FormulaCode 文本渲染；表达式点击选中（onSelectExpr 回调）。
 */
export const makeResultColumns = (opts: MakeResultColumnsOpts): SrColumn<FactorResult>[] => {
  const { latex, withSelect = true, selectedExpr = null, onSelectExpr = () => {}, dsQs = '', navigate } = opts
  return [
    {
      title: '因子表达式', dataIndex: 'expression',
      ellipsis: true, minWidth: 150,
      render: (v) => (
        <Text
          tabIndex={0}
          style={{ cursor: 'pointer' }}
          title={v}
          onClick={() => onSelectExpr(v)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault()
              onSelectExpr(v)
            }
          }}
        >
          {latex[v] ? <LatexFormula tex={latex[v]} /> : <FormulaCode expr={v} />}
        </Text>
      ),
    },
    { title: '复杂度', dataIndex: 'complexity', align: 'center' },
    { title: 'Train IC', dataIndex: 'train_ic', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v) => <MetricCell v={v} text={fmtRatio(v)} /> },
    { title: 'Val IC', dataIndex: 'val_ic', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v) => <MetricCell v={v} text={fmtRatio(v)} /> },
    { title: 'OOS IC', align: 'right', className: 'sr-num-col', minWidth: 70, render: (_, r) => <MetricCell v={r.oos?.ic} text={fmtRatio(r.oos?.ic)} /> },
    { title: '多空年化', align: 'right', className: 'sr-num-col', minWidth: 70, render: (_, r) => r.oos?.long_short_annual != null ? <MetricCell v={r.oos.long_short_annual} text={fmtPct(r.oos.long_short_annual)} /> : '—' },
    { title: '换手', align: 'right', className: 'sr-num-col', minWidth: 70, render: (_, r) => r.oos?.turnover != null ? fmtRatio(r.oos.turnover, 3) : '—' },
    { title: '稳定性', align: 'right', className: 'sr-num-col', minWidth: 70, render: (_, r) => r.oos?.stability != null ? fmtStab(r.oos.stability) : '—' },
    {
      title: '操作', align: 'center', minWidth: 170,
      render: (_, r) => (
        <Group gap={4} wrap="nowrap">
          <Button size="xs" variant="default" leftSection={<IconFileSearch size={14} />}
            onClick={() => navigate(`/research/evaluation?expr=${encodeURIComponent(r.expression)}${dsQs}`)}>
            评估
          </Button>
          <Button size="xs" variant="default" leftSection={<IconChartBar size={14} />}
            onClick={() => navigate(`/research/backtests?expr=${encodeURIComponent(r.expression)}${dsQs}`)}>
            回测
          </Button>
          {withSelect && (
            <Button
              size="xs" variant={selectedExpr === r.expression ? 'filled' : 'default'}
              leftSection={<IconCheck size={14} />}
              onClick={() => onSelectExpr(r.expression)}
            >
              {selectedExpr === r.expression ? '已选' : '选择'}
            </Button>
          )}
        </Group>
      ),
    },
  ]
}

interface ResultDetailDrawerProps {
  open: boolean
  onClose: () => void
  job: any
  loading: boolean
  latexMap: Record<string, string>
  /** 跨页跳转参数（评估/回测按钮携带 ?ds=；与主组件当前数据集一致） */
  selectedDs?: number
  /** 结果表表达式点击选中（主组件 state） */
  onSelectExpr?: (v: string) => void
  /** 加载到当前视图（主组件 loadExp + 关窗） */
  onLoad: (job: any) => void
}

/**
 * 挖掘任务详情 Modal（从 FactorMining 拆分）：参数 DataList + 失败错误 + 进度 +
 * 结果 DataTable（列定义 makeResultColumns 与本文件共用）+ 加载到当前视图按钮。
 */
export default function ResultDetailDrawer({ open, onClose, job, loading, latexMap, selectedDs, onSelectExpr, onLoad }: ResultDetailDrawerProps) {
  const navigate = useNavigate()
  const dsQs = selectedDs != null ? `&ds=${selectedDs}` : ''

  // columns 引用稳定化：deps 覆盖闭包内可变引用（latexMap、dsQs）
  const detailColumns = useMemo(
    () => makeResultColumns({ latex: latexMap, withSelect: false, onSelectExpr, dsQs, navigate }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [latexMap, selectedDs],
  )

  return (
    <Modal
      title={job ? `任务 #${job.id} 详情 · ${job.status}` : '任务详情'}
      opened={open}
      onClose={onClose}
      size="min(1080px, 96vw)"
      keepMounted
    >
      <CardState loading={loading} minHeight={160}>
        {job ? (
          <Flex direction="column" gap={12} style={{ width: '100%' }}>
            <DataList
              size="small"
              column={2}
              items={[
                { key: 'algo', label: '算法', children: optionLabel(ALGORITHM_OPTIONS, job.params?.algorithm) },
                { key: 'target', label: '优化目标', children: optionLabel(TARGET_OPTIONS, job.params?.target) },
                { key: 'pop', label: '种群×代数', children: `${job.params?.pop_size ?? 120}×${job.params?.generations ?? 12}` },
                { key: 'horizon', label: '预测周期', children: `${job.params?.horizon ?? 5} 日` },
                { key: 'dataset', label: '数据集', children: job.params?.dataset_id ?? '—' },
                { key: 'topn', label: 'top_n', children: job.params?.top_n ?? '—' },
                { key: 'penalty', label: '复杂度惩罚', children: job.params?.penalty_complexity ? '开启' : '关闭' },
                { key: 'backend', label: '计算模式', children: job.params?.backend ?? '—' },
                {
                  key: 'features', label: '特征字段', span: 2,
                  children: Array.isArray(job.params?.features) ? job.params.features.join('、') : '—',
                },
                {
                  key: 'ops', label: '算子集合', span: 2,
                  children: Array.isArray(job.params?.op_set) ? job.params.op_set.join('、') : '—',
                },
              ]}
            />
            {job.status === 'failed' && (
              <Text c="red" style={{ whiteSpace: 'pre-wrap' }}>{job.error}</Text>
            )}
            {(job.status === 'pending' || job.status === 'running') && (
              <Progress value={job.progress} size="sm" />
            )}
            {job.status === 'done' && job.result && (
              <>
                {job.result.meta && (
                  <DataList
                    size="small"
                    column={4}
                    items={[
                      { key: 'stocks', label: '股票数', children: job.result.meta.stocks },
                      { key: 'days', label: '总天数', children: job.result.meta.days },
                      {
                        key: 'split', label: '训练/验证/样本外',
                        children: job.result.meta.split ? `${job.result.meta.split.train}/${job.result.meta.split.val}/${job.result.meta.split.oos}` : '—',
                      },
                      {
                        key: 'range', label: '区间',
                        children: job.result.meta.dates ? `${job.result.meta.dates.train} ~ ${job.result.meta.dates.oos_end}` : '—',
                      },
                    ]}
                  />
                )}
                <DataTable
                  rowKey={(r: FactorResult) => r.expression}
                  columns={detailColumns}
                  dataSource={job.result.results ?? []}
                  pagination={{ pageSize: 8, showSizeChanger: false }}
                />
              </>
            )}
          </Flex>
        ) : null}
      </CardState>
      <Group justify="flex-end" gap="var(--sr-pad-sm)" mt="var(--sr-pad-lg)">
        <Button variant="default" onClick={onClose}>关闭</Button>
        <Button
          variant="filled"
          leftSection={<IconPlayerPlay size={14} />}
          disabled={job?.status !== 'done'}
          onClick={() => onLoad(job)}
        >
          加载到当前视图
        </Button>
      </Group>
    </Modal>
  )
}
