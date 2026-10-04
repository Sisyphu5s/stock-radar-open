import { Badge, Checkbox, Chip, Grid, GridCol, NumberInput, SegmentedControl, Select, Switch, Text } from '@mantine/core'
import { useForm } from '@mantine/form'
import { useEffect } from 'react'
import type { ReactNode } from 'react'
import CardShell from '../../../components/ui/CardShell'
import FormRow from '../../../components/ui/FormRow'

/** 算子公式显示：占位符 {x}/{y}/{w}/{k}/{p} → 直观公式（ts_mean(x,w)） */
function fmtOpDisplay(d: string): string {
  return d.replace(/\{x\}/g, 'x').replace(/\{y\}/g, 'y')
    .replace(/\{w\}/g, 'w').replace(/\{k\}/g, 'k').replace(/\{p\}/g, 'p')
}

/** GP 挖掘域共享常量（FmForm 表单 options / 主组件结果操作列 / ResultDetailDrawer 详情共用） */
export const TARGET_OPTIONS = [
  { value: 'ic', label: 'IC（预测方向）' },
  { value: 'ic_abs', label: '|IC|（方向无关）' },
  { value: 'icir', label: 'ICIR（稳健性）' },
  { value: 'ls_annual', label: '多空年化' },
  { value: 'composite', label: '综合评分' },
]

export const ALGORITHM_OPTIONS = [
  { value: 'gp', label: 'GP 符号回归（默认）' },
  { value: 'gplearn', label: 'gplearn（sklearn）' },
  { value: 'neural', label: 'neural（MLP 神经网络）' },
  { value: 'pysr', label: 'PySR（神经符号回归）' },
]

export const BACKEND_MODES = [
  { value: 'auto', label: 'auto' },
  { value: 'cpu', label: 'CPU' },
  { value: 'gpu', label: 'GPU' },
]

export const optionLabel = (opts: { value: string; label: string }[], v?: string | null) =>
  opts.find((o) => o.value === v)?.label ?? v ?? '—'

/** InputNumber 清空兜底默认值（与原 antd v ?? default 等价；validate 兜空值） */
const POP_DEFAULT = 120
const GENS_DEFAULT = 12
const HORIZON_DEFAULT = 5

/** 算法参数表单项值（受控面板 useForm：props 快照 + 外部重灌同步） */
interface AlgorithmFormValues {
  algorithm: string
  target: string
  backendMode: 'auto' | 'cpu' | 'gpu'
  popSize: number
  gens: number
  horizon: number
  penaltyComplexity: boolean
}

interface FmFormProps {
  features: string[]
  onFeatures: (v: string[]) => void
  opSet: string[]
  onOpSet: (v: string[]) => void
  opConfig: Record<string, Record<string, number[]>>
  onOpConfig: (v: Record<string, Record<string, number[]>>) => void
  opInfo: any
  algorithm: string
  onAlgorithm: (v: string) => void
  target: string
  onTarget: (v: string) => void
  backendMode: 'auto' | 'cpu' | 'gpu'
  onBackendMode: (v: 'auto' | 'cpu' | 'gpu') => void
  popSize: number
  onPopSize: (v: number) => void
  gens: number
  onGens: (v: number) => void
  horizon: number
  onHorizon: (v: number) => void
  penaltyComplexity: boolean
  onPenaltyComplexity: (v: boolean) => void
  backend: string
  /** 步骤编号徽章（主组件定义，三卡标题统一风格） */
  stepTitle: (no: number, text: string, icon?: ReactNode) => ReactNode
}

/** NumberInput 值归一化：空串 → 兜底默认（原 antd v ?? default 语义） */
const numOr = (v: number | string, fb: number): number =>
  (typeof v === 'string' && v.trim() === '') ? fb : Number(v)

/**
 * 因子挖掘「步骤 2 搜索空间 + 步骤 3 算法参数」受控面板。
 * 搜索空间（features/opSet Checkbox + opConfig Chip 切换）为纯受控 props；
 * 算法参数走「受控面板 useForm」：渲染用 form.values，变更双写 setFieldValue + onXxx，
 * 外部重灌（持久化快照 / 恢复）经 props 变化 → useEffect setValues 同步。
 */
export default function FmForm(p: FmFormProps) {
  const form = useForm<AlgorithmFormValues>({
    initialValues: {
      algorithm: p.algorithm,
      target: p.target,
      backendMode: p.backendMode,
      popSize: p.popSize,
      gens: p.gens,
      horizon: p.horizon,
      penaltyComplexity: p.penaltyComplexity,
    },
    validate: {
      algorithm: (v) => (v ? null : '必选'),
      target: (v) => (v ? null : '必选'),
      backendMode: (v) => (v ? null : '必选'),
      popSize: (v) => (v != null && v >= 10 && v <= 1000 ? null : '10~1000'),
      gens: (v) => (v != null && v >= 1 && v <= 100 ? null : '1~100'),
      horizon: (v) => (v != null && v >= 1 && v <= 20 ? null : '1~20'),
    },
  })

  // 外部重灌（URL 恢复 / 持久化快照 / 历史任务恢复）→ props 变化 → 同步 form（依赖数组列全快照字段）
  useEffect(() => {
    form.setValues({
      algorithm: p.algorithm,
      target: p.target,
      backendMode: p.backendMode,
      popSize: p.popSize,
      gens: p.gens,
      horizon: p.horizon,
      penaltyComplexity: p.penaltyComplexity,
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.algorithm, p.target, p.backendMode, p.popSize, p.gens, p.horizon, p.penaltyComplexity])

  const toggleOpVal = (op: string, pk: string, v: number) => {
    p.onOpConfig({
      ...p.opConfig,
      [op]: {
        ...p.opConfig[op],
        [pk]: p.opConfig[op][pk].includes(v)
          ? p.opConfig[op][pk].filter((x) => x !== v)
          : [...p.opConfig[op][pk], v].sort((a, b) => a - b),
      },
    })
  }

  return (
    <>
      {/* ===== 步骤 2：搜索空间 ===== */}
      <CardShell className="sr-disc-step-card" title={p.stepTitle(2, '搜索空间')}>
        <div className="sr-disc-section">
          <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>特征字段:</Text>
          <div style={{ marginTop: 'var(--sr-pad-xs)' }}>
            <Checkbox.Group value={p.features} onChange={(v) => p.onFeatures(v)}>
              {(p.opInfo?.features ?? []).map((f: string) => (
                <Checkbox key={f} value={f} label={f} />
              ))}
            </Checkbox.Group>
          </div>
        </div>
        <div className="sr-disc-section">
          <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>算子集合（勾选参与符号回归）:</Text>
          <div style={{ marginTop: 'var(--sr-pad-xs)' }}>
            <Checkbox.Group value={p.opSet} onChange={(v) => p.onOpSet(v)}>
              {(p.opInfo?.data ?? []).map((o: any) => (
                <Checkbox key={o.name} value={o.name} label={fmtOpDisplay(o.display)} />
              ))}
            </Checkbox.Group>
          </div>
        </div>
        <div className="sr-disc-section">
          <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>算子参数（点击候选切换，随 op_config 提交）:</Text>
          <div style={{ marginTop: 'var(--sr-pad-xs)', display: 'flex', flexWrap: 'wrap', gap: 'var(--sr-pad-xs)' }}>
            {Object.entries(p.opConfig).filter(([op]) => p.opSet.includes(op)).map(([op, params]) => (
              <span key={op} style={{ display: 'inline-flex', alignItems: 'center', gap: 4, background: 'var(--sr-block-bg)', borderRadius: 8, padding: '2px 6px', border: '1px solid var(--sr-border)' }}>
                <Text style={{ fontSize: 'var(--sr-font-xs)' }}>{op}:</Text>
                {Object.entries(params).map(([pk, vals]) => (
                  <span key={pk} style={{ display: 'inline-flex', gap: 2, alignItems: 'center' }}>
                    <Text c="dimmed" style={{ fontSize: 'var(--sr-font-meta)' }}>{pk}=</Text>
                    {vals.map((v) => (
                      <Chip
                        key={v}
                        checked={vals.includes(v)}
                        onChange={() => toggleOpVal(op, pk, v)}
                        variant="outline"
                        size="xs"
                        radius="var(--sr-radius-tag)"
                        style={{ fontSize: 'var(--sr-font-meta)' }}
                      >
                        {v}
                      </Chip>
                    ))}
                  </span>
                ))}
              </span>
            ))}
          </div>
        </div>
      </CardShell>

      {/* ===== 步骤 3：算法参数（FormRow 标签定宽 80px，控件 100% 自适应） ===== */}
      <CardShell className="sr-disc-step-card" title={p.stepTitle(3, '算法参数')}>
        <Grid gap={12}>
          <GridCol span={{ base: 24, sm: 12 }}>
            <FormRow label="回归算法">
              <Select data={ALGORITHM_OPTIONS} value={form.values.algorithm}
                onChange={(v) => { if (v != null) { form.setFieldValue('algorithm', v); p.onAlgorithm(v) } }} />
            </FormRow>
          </GridCol>
          <GridCol span={{ base: 24, sm: 12 }}>
            <FormRow label="优化目标">
              <Select data={TARGET_OPTIONS} value={form.values.target}
                onChange={(v) => { if (v != null) { form.setFieldValue('target', v); p.onTarget(v) } }} />
            </FormRow>
          </GridCol>
          <GridCol span={{ base: 24, sm: 12 }}>
            <FormRow label="计算模式">
              <SegmentedControl fullWidth data={BACKEND_MODES} value={form.values.backendMode}
                onChange={(v) => { form.setFieldValue('backendMode', v as 'auto' | 'cpu' | 'gpu'); p.onBackendMode(v as 'auto' | 'cpu' | 'gpu') }} />
            </FormRow>
          </GridCol>
          <GridCol span={{ base: 24, sm: 12 }}>
            <FormRow label="种群">
              <NumberInput min={10} max={1000} value={form.values.popSize}
                onChange={(v) => { const n = numOr(v, POP_DEFAULT); form.setFieldValue('popSize', n); p.onPopSize(n) }} />
            </FormRow>
          </GridCol>
          <GridCol span={{ base: 24, sm: 12 }}>
            <FormRow label="代数">
              <NumberInput min={1} max={100} value={form.values.gens}
                onChange={(v) => { const n = numOr(v, GENS_DEFAULT); form.setFieldValue('gens', n); p.onGens(n) }} />
            </FormRow>
          </GridCol>
          <GridCol span={{ base: 24, sm: 12 }}>
            <FormRow label="预测周期">
              <NumberInput min={1} max={20} suffix="日" value={form.values.horizon}
                onChange={(v) => { const n = numOr(v, HORIZON_DEFAULT); form.setFieldValue('horizon', n); p.onHorizon(n) }} />
            </FormRow>
          </GridCol>
          <GridCol span={{ base: 24, sm: 12 }}>
            <FormRow label="复杂度惩罚">
              <Switch size="xs" checked={form.values.penaltyComplexity}
                onChange={(e) => { const v = e.currentTarget.checked; form.setFieldValue('penaltyComplexity', v); p.onPenaltyComplexity(v) }} />
            </FormRow>
          </GridCol>
          <GridCol span={{ base: 24, sm: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sr-pad-sm)', flexWrap: 'wrap' }}>
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>可用性取决于后端依赖</Text>
              <Badge variant="light" color={p.backend === 'mlx' ? 'green' : 'gray'} style={{ fontWeight: 500 }}>
                计算后端: {p.backend === 'mlx' ? 'MLX (Apple GPU)' : p.backend}
              </Badge>
            </div>
          </GridCol>
        </Grid>
      </CardShell>
    </>
  )
}
