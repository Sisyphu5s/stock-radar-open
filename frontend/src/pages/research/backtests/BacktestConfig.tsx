import { Button, Checkbox, Flex, NumberInput, SegmentedControl, Select, Text } from '@mantine/core'
import { useForm } from '@mantine/form'
import { IconPlayerPlay } from '@tabler/icons-react'
import { useEffect, useState } from 'react'
import { api, BT_MODE_OPTIONS } from '../../../api/client'
import type { BTModeKey } from '../../../api/client'
import { OPTIMIZE_METHOD_OPTIONS } from '../../../api/portfolio'
import DatasetSelector from '../../../components/ui/DatasetSelector'
import FormRow from '../../../components/ui/FormRow'
import TaskProgress from '../../../components/ui/TaskProgress'
import EmptyState from '../../../components/ui/EmptyState'
import { fmtRatio } from '../../../utils/format'
import { FactorExprPanel, LatexPreviewPanel } from '../shared/FactorExprPanel'

type Direction = 'auto' | 'positive' | 'negative'

/** 因子来源：表达式（现状，Select/手输/示例）或神经网络因子（nn-models 选择） */
type ExprSource = 'expr' | 'nn'

/** GET /alpha/nn-models 列表项契约（client.ts 未收编，页面局部类型） */
interface NNModelInfo {
  id: number
  checkpoint: string | null
  architecture: { layers: number[]; activation: string } | null
  dataset_id: number
  horizon: number
  epochs: number
  train_ic: number | null
  val_ic: number | null
  train_loss: number | null
  val_loss: number | null
  created_at: string
}

const nnModelLabel = (m: NNModelInfo): string => {
  const layers = (m.architecture?.layers ?? []).join('x') || '?'
  const ic = m.train_ic != null ? fmtRatio(m.train_ic, 3) : '—'
  return `模型#${m.id} · MLP ${layers} · IC ${ic}(训练)`
}

/** 运行参数清空兜底默认值（原 antd v ?? default 语义；validate 兜空值） */
const TOP_PCT_DEFAULT = 20
const TRADE_INTERVAL_DEFAULT = 5
const COST_RATE_DEFAULT = 0.1
const HORIZON_DEFAULT = 5
/** T-02 成本默认(基点):滑点 0、佣金万 2.5(2.5bp)、印花税千 0.5(5bp) */
const SLIPPAGE_BPS_DEFAULT = 0
const COMMISSION_BPS_DEFAULT = 2.5
const STAMP_TAX_BPS_DEFAULT = 5
/** T-10 组合优化默认:权重上限 10%、选股数 20(与 Backtest.tsx 持久化初始值一致) */
const OPT_MAX_WEIGHT_DEFAULT = 10
const OPT_TOP_N_DEFAULT = 20

const numOr = (v: number | string, fb: number): number =>
  (typeof v === 'string' && v.trim() === '') ? fb : Number(v)

interface BacktestConfigProps {
  expression: string
  onExpression: (v: string) => void
  selectedDs?: number
  onDataset: (v: number) => void
  topPct: number
  onTopPct: (v: number) => void
  tradeInterval: number
  onTradeInterval: (v: number) => void
  costRate: number
  onCostRate: (v: number) => void
  /** T-02 交易成本拆分(基点):滑点/佣金(双边)/印花税(卖单边) */
  slippageBps: number
  onSlippageBps: (v: number) => void
  commissionBps: number
  onCommissionBps: (v: number) => void
  stampTaxBps: number
  onStampTaxBps: (v: number) => void
  horizon: number
  onHorizon: (v: number) => void
  modes: BTModeKey[]
  onModes: (v: BTModeKey[]) => void
  direction: Direction
  onDirection: (v: Direction) => void
  /** T-10 组合优化：受控（form.values 双写 + 外部重灌同步） */
  optEnabled: boolean
  onOptEnabled: (v: boolean) => void
  optMethod: string
  onOptMethod: (v: string) => void
  optMaxWeight: number
  onOptMaxWeight: (v: number) => void
  optTopN: number
  onOptTopN: (v: number) => void
  datasets: { id: number; name?: string; stock_count?: number }[]
  factors: { name: string; expression: string }[]
  examples: string[]
  running: boolean
  onRun: () => void
  /** NN 因子选择（受控；由 Backtest.tsx 传入以支撑提交 model_id）。未传时内部自持仅本地展示 */
  nnModelId?: number | null
  onNnModelId?: (id: number | null) => void
  previewLatex: string | null
  job: any
}

/**
 * 回测页左侧配置面板：因子 + 参数 + 使用方式 + 公式预览。
 * 运行参数为「受控面板 useForm」（快照 + 外部重灌同步）：渲染用 form.values，
 * 变更双写 setFieldValue + onXxx；恢复历史任务时 props 变化 → useEffect setValues。
 */
export default function BacktestConfig(p: BacktestConfigProps) {
  const [source, setSource] = useState<ExprSource>('expr')
  const [nnModels, setNnModels] = useState<NNModelInfo[]>([])
  const [nnLoading, setNnLoading] = useState(false)
  const [nnErr, setNnErr] = useState<string | null>(null)
  const [innerModelId, setInnerModelId] = useState<number | null>(null)
  // 受控优先（Backtest.tsx 提交需要拿到 model_id）；未受控时内部自持
  const modelId = p.onNnModelId ? (p.nnModelId ?? null) : innerModelId
  const setModelId = (id: number | null) => {
    if (p.onNnModelId) p.onNnModelId(id)
    else setInnerModelId(id)
  }

  // 受控面板 useForm（快照字段全列依赖数组）
  const form = useForm({
    initialValues: {
      topPct: p.topPct,
      tradeInterval: p.tradeInterval,
      costRate: p.costRate,
      horizon: p.horizon,
      modes: p.modes,
      direction: p.direction,
      slippageBps: p.slippageBps,
      commissionBps: p.commissionBps,
      stampTaxBps: p.stampTaxBps,
      optEnabled: p.optEnabled,
      optMethod: p.optMethod,
      optMaxWeight: p.optMaxWeight,
      optTopN: p.optTopN,
    },
    validate: {
      topPct: (v) => (v != null && v >= 5 && v <= 50 ? null : '5~50'),
      tradeInterval: (v) => (v != null && v >= 1 && v <= 20 ? null : '1~20'),
      costRate: (v) => (v != null && v >= 0 && v <= 1 ? null : '0~1'),
      horizon: (v) => (v != null && v >= 1 && v <= 20 ? null : '1~20'),
      modes: (v) => (Array.isArray(v) && v.length > 0 ? null : '至少保留一种'),
      direction: (v) => (v ? null : '必选'),
      slippageBps: (v) => (v != null && v >= 0 && v <= 100 ? null : '0~100'),
      commissionBps: (v) => (v != null && v >= 0 && v <= 100 ? null : '0~100'),
      stampTaxBps: (v) => (v != null && v >= 0 && v <= 100 ? null : '0~100'),
      optMaxWeight: (v) => (v != null && v >= 5 && v <= 50 ? null : '5~50'),
      optTopN: (v) => (v != null && v >= 5 && v <= 100 ? null : '5~100'),
    },
  })
  // 外部重灌（URL 恢复 / 历史任务恢复）→ props 变化 → 同步 form
  useEffect(() => {
    form.setValues({
      topPct: p.topPct,
      tradeInterval: p.tradeInterval,
      costRate: p.costRate,
      horizon: p.horizon,
      modes: p.modes,
      direction: p.direction,
      slippageBps: p.slippageBps,
      commissionBps: p.commissionBps,
      stampTaxBps: p.stampTaxBps,
      optEnabled: p.optEnabled,
      optMethod: p.optMethod,
      optMaxWeight: p.optMaxWeight,
      optTopN: p.optTopN,
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.topPct, p.tradeInterval, p.costRate, p.horizon, p.modes, p.direction, p.slippageBps, p.commissionBps, p.stampTaxBps, p.optEnabled, p.optMethod, p.optMaxWeight, p.optTopN])

  const loadNnModels = async () => {
    setNnLoading(true)
    setNnErr(null)
    try {
      const { data } = await api.get<{ data: NNModelInfo[] }>('/alpha/nn-models')
      setNnModels(data.data ?? [])
    } catch (e: any) {
      setNnErr('神经网络模型加载失败: ' + (e?.message ?? e))
    } finally {
      setNnLoading(false)
    }
  }
  // 首次切到 NN 来源时加载（成功即缓存，来源来回切换不重复拉取）
  useEffect(() => {
    if (source === 'nn' && nnModels.length === 0 && !nnErr && !nnLoading) void loadNnModels()
  }, [source]) // eslint-disable-line react-hooks/exhaustive-deps

  const onSourceChange = (s: ExprSource) => {
    setSource(s)
    if (s === 'expr') setModelId(null) // 切回表达式来源时清空 NN 选择，防误提交 model_id
  }

  return (
    <>
      <FactorExprPanel
        expression={p.expression}
        onExpression={p.onExpression}
        factors={p.factors}
        examples={p.examples}
        textareaPlaceholder="因子表达式"
        header={
          <SegmentedControl
            fullWidth
            value={source}
            onChange={(v) => onSourceChange(v as ExprSource)}
            data={[
              { value: 'expr', label: '公式因子' },
              { value: 'nn', label: '神经网络因子' },
            ]}
          />
        }
      >
        {source === 'nn' ? (
          <div style={{ marginTop: 'var(--sr-pad-md)' }}>
            {nnErr ? (
              <Text c="red" style={{ fontSize: 'var(--sr-font-sm)' }}>{nnErr}</Text>
            ) : nnModels.length === 0 && !nnLoading ? (
              <EmptyState description="暂无神经网络模型，请先在「神经网络研究」训练" />
            ) : (
              <Select
                searchable
                style={{ width: '100%' }}
                className="sr-ctl-h"
                placeholder={nnLoading ? '加载模型中…' : '选择神经网络模型'}
                loading={nnLoading}
                value={modelId != null ? String(modelId) : null}
                onChange={(v) => setModelId(v != null ? Number(v) : null)}
                data={nnModels.map((m) => ({ value: String(m.id), label: nnModelLabel(m) }))}
              />
            )}
          </div>
        ) : null}
      </FactorExprPanel>

      <div className="sr-run-panel">
        <div className="sr-run-panel-title">运行参数</div>
        <Flex direction="column" gap="var(--sr-pad-sm)">
          <FormRow label="数据集" width={56}>
            <DatasetSelector
              datasets={p.datasets.map((d) => ({ id: d.id, name: `${d.name ?? `数据集 #${d.id}`} (${d.stock_count ?? '?'}只)` }))}
              value={p.selectedDs}
              onChange={p.onDataset}
              width="100%"
            />
          </FormRow>
          <FormRow label="多空比例" width={56}>
            <Flex gap="var(--sr-pad-sm)" align="center" wrap="wrap">
              <NumberInput
                min={5} max={50} className="sr-ctl-h" suffix="%"
                style={{ flex: '1 1 130px', minWidth: 0 }}
                value={form.values.topPct}
                onChange={(v) => {
                  const n = numOr(v, TOP_PCT_DEFAULT)
                  form.setFieldValue('topPct', n)
                  p.onTopPct(n)
                }}
              />
              <Flex align="center" gap="var(--sr-pad-sm)" style={{ flex: '1 1 150px', minWidth: 0 }}>
                <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>调仓</Text>
                <NumberInput
                  min={1} max={20} className="sr-ctl-h" suffix="日"
                  style={{ flex: 1, minWidth: 0 }}
                  value={form.values.tradeInterval}
                  onChange={(v) => {
                    const n = numOr(v, TRADE_INTERVAL_DEFAULT)
                    form.setFieldValue('tradeInterval', n)
                    p.onTradeInterval(n)
                  }}
                />
              </Flex>
            </Flex>
          </FormRow>
          <FormRow label="单边成本" width={56}>
            <Flex gap="var(--sr-pad-sm)" align="center" wrap="wrap">
              <NumberInput
                min={0} max={1} step={0.05} className="sr-ctl-h" suffix="%"
                style={{ flex: '1 1 130px', minWidth: 0 }}
                value={form.values.costRate}
                onChange={(v) => {
                  const n = numOr(v, COST_RATE_DEFAULT)
                  form.setFieldValue('costRate', n)
                  p.onCostRate(n)
                }}
              />
              <Flex align="center" gap="var(--sr-pad-sm)" style={{ flex: '1 1 150px', minWidth: 0 }}>
                <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>预测周期</Text>
                <NumberInput
                  min={1} max={20} className="sr-ctl-h" suffix="日"
                  style={{ flex: 1, minWidth: 0 }}
                  value={form.values.horizon}
                  onChange={(v) => {
                    const n = numOr(v, HORIZON_DEFAULT)
                    form.setFieldValue('horizon', n)
                    p.onHorizon(n)
                  }}
                />
              </Flex>
            </Flex>
          </FormRow>
          <FormRow label="交易成本" width={56}>
            <Flex gap="var(--sr-pad-sm)" align="center" wrap="wrap">
              <NumberInput
                min={0} max={100} step={1} className="sr-ctl-h" suffix="bp"
                style={{ flex: '1 1 130px', minWidth: 0 }}
                value={form.values.slippageBps}
                onChange={(v) => {
                  const n = numOr(v, SLIPPAGE_BPS_DEFAULT)
                  form.setFieldValue('slippageBps', n)
                  p.onSlippageBps(n)
                }}
              />
              <Flex align="center" gap="var(--sr-pad-sm)" style={{ flex: '1 1 150px', minWidth: 0 }}>
                <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>滑点</Text>
                <NumberInput
                  min={0} max={100} step={0.5} className="sr-ctl-h" suffix="bp"
                  style={{ flex: 1, minWidth: 0 }}
                  value={form.values.commissionBps}
                  onChange={(v) => {
                    const n = numOr(v, COMMISSION_BPS_DEFAULT)
                    form.setFieldValue('commissionBps', n)
                    p.onCommissionBps(n)
                  }}
                />
              </Flex>
            </Flex>
            <Flex gap="var(--sr-pad-sm)" align="center" wrap="wrap" style={{ marginTop: 'var(--sr-pad-sm)' }}>
              <Flex align="center" gap="var(--sr-pad-sm)" style={{ flex: '1 1 130px', minWidth: 0 }}>
                <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>佣金</Text>
                <NumberInput
                  min={0} max={100} step={0.5} className="sr-ctl-h" suffix="bp"
                  style={{ flex: 1, minWidth: 0 }}
                  value={form.values.commissionBps}
                  onChange={(v) => {
                    const n = numOr(v, COMMISSION_BPS_DEFAULT)
                    form.setFieldValue('commissionBps', n)
                    p.onCommissionBps(n)
                  }}
                />
              </Flex>
              <Flex align="center" gap="var(--sr-pad-sm)" style={{ flex: '1 1 150px', minWidth: 0 }}>
                <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>印花税</Text>
                <NumberInput
                  min={0} max={100} step={0.5} className="sr-ctl-h" suffix="bp"
                  style={{ flex: 1, minWidth: 0 }}
                  value={form.values.stampTaxBps}
                  onChange={(v) => {
                    const n = numOr(v, STAMP_TAX_BPS_DEFAULT)
                    form.setFieldValue('stampTaxBps', n)
                    p.onStampTaxBps(n)
                  }}
                />
              </Flex>
            </Flex>
            <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)', marginTop: 'var(--sr-pad-xs)' }}>
              基点=万分之一：滑点买升卖降 · 佣金双边 · 印花税仅卖出
            </Text>
          </FormRow>
          <FormRow label="使用方式" width={56}>
            <Checkbox.Group
              value={form.values.modes as string[]}
              onChange={(v) => {
                const m = v as BTModeKey[]
                form.setFieldValue('modes', m)
                p.onModes(m)
              }}
            >
              {BT_MODE_OPTIONS.map((o) => (
                <Checkbox key={o.value} value={o.value} label={o.label} />
              ))}
            </Checkbox.Group>
          </FormRow>
          <FormRow label="方向" width={56}>
            <Select
              style={{ width: '100%', minWidth: 0, maxWidth: 220 }} className="sr-ctl-h"
              value={form.values.direction}
              onChange={(v) => {
                if (v != null) {
                  form.setFieldValue('direction', v as Direction)
                  p.onDirection(v as Direction)
                }
              }}
              data={[
                { value: 'auto', label: '自动' },
                { value: 'positive', label: '正向' },
                { value: 'negative', label: '反向' },
              ]}
            />
          </FormRow>
          <FormRow label="优化模式" width={56}>
            <SegmentedControl
              fullWidth
              value={form.values.optEnabled ? 'on' : 'off'}
              onChange={(v) => {
                const on = v === 'on'
                form.setFieldValue('optEnabled', on)
                p.onOptEnabled(on)
              }}
              data={[
                { value: 'off', label: '关闭' },
                { value: 'on', label: '开启' },
              ]}
            />
          </FormRow>
          {form.values.optEnabled && (
            <>
              <FormRow label="优化方法" width={56}>
                <Select
                  style={{ width: '100%', minWidth: 0, maxWidth: 220 }} className="sr-ctl-h"
                  value={form.values.optMethod}
                  onChange={(v) => {
                    if (v != null) {
                      form.setFieldValue('optMethod', v)
                      p.onOptMethod(v)
                    }
                  }}
                  data={OPTIMIZE_METHOD_OPTIONS}
                />
              </FormRow>
              <FormRow label="权重上限" width={56}>
                <Flex align="center" gap="var(--sr-pad-sm)" wrap="wrap">
                  <NumberInput
                    min={5} max={50} className="sr-ctl-h" suffix="%"
                    style={{ flex: '1 1 130px', minWidth: 0 }}
                    value={form.values.optMaxWeight}
                    onChange={(v) => {
                      const n = numOr(v, OPT_MAX_WEIGHT_DEFAULT)
                      form.setFieldValue('optMaxWeight', n)
                      p.onOptMaxWeight(n)
                    }}
                  />
                  <Flex align="center" gap="var(--sr-pad-sm)" style={{ flex: '1 1 150px', minWidth: 0 }}>
                    <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>选股数量</Text>
                    <NumberInput
                      min={5} max={100} className="sr-ctl-h"
                      style={{ flex: 1, minWidth: 0 }}
                      value={form.values.optTopN}
                      onChange={(v) => {
                        const n = numOr(v, OPT_TOP_N_DEFAULT)
                        form.setFieldValue('optTopN', n)
                        p.onOptTopN(n)
                      }}
                    />
                  </Flex>
                </Flex>
              </FormRow>
            </>
          )}
        </Flex>
        <Button
          variant="filled"
          leftSection={<IconPlayerPlay size={14} />}
          loading={p.running}
          onClick={p.onRun}
          fullWidth
          style={{ marginTop: 'var(--sr-pad-xl)' }}
        >
          运行回测
        </Button>
      </div>

      <LatexPreviewPanel tex={p.previewLatex} />

      {p.job && p.job.status !== 'done' && (
        <Flex direction="column" gap="var(--sr-pad-sm)">
          <TaskProgress job={p.job} typeLabel={{ backtest: '回测' }} />
          {p.job.status === 'failed' && (
            <Text c="red" style={{ fontSize: 'var(--sr-font-sm)' }}>{p.job.error}</Text>
          )}
        </Flex>
      )}
    </>
  )
}
