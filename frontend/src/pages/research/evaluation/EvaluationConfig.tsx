import { Button, Flex, NumberInput, Switch, Text, Tooltip } from '@mantine/core'
import { useForm } from '@mantine/form'
import { IconAdjustments, IconPlayerPlay } from '@tabler/icons-react'
import { useEffect } from 'react'
import DatasetSelector from '../../../components/ui/DatasetSelector'
import FormRow from '../../../components/ui/FormRow'
import TaskProgress from '../../../components/ui/TaskProgress'
import { FactorExprPanel, LatexPreviewPanel } from '../shared/FactorExprPanel'

interface EvaluationConfigProps {
  expression: string
  onExpression: (v: string) => void
  selectedDs?: number
  onDataset: (v: number) => void
  horizon: number
  onHorizon: (v: number) => void
  /** T-03 Walk-forward 滚动验证:开关 + 窗数(2-5) */
  wfEnabled: boolean
  onWfEnabled: (v: boolean) => void
  wfWindows: number
  onWfWindows: (v: number) => void
  datasets: { id: number; name?: string; stock_count?: number }[]
  factors: { name: string; expression: string }[]
  examples: string[]
  running: boolean
  onRun: () => void
  onTune: () => void
  previewLatex: string | null
  resultLatex?: string | null
  job: any
}

/** 预测周期清空兜底默认值（原 antd v ?? 5 语义） */
const HORIZON_DEFAULT = 5
/** Walk-forward 窗数默认 3(clamp 2-5) */
const WF_WINDOWS_DEFAULT = 3

const numOr = (v: number | string, fb: number): number =>
  (typeof v === 'string' && v.trim() === '') ? fb : Number(v)

/**
 * 评估页左侧配置面板：因子选择 + 表达式 + 运行参数（受控面板 useForm：horizon
 * 快照 + 外部重灌同步）+ Walk-forward 滚动验证开关 + 公式预览。参数调优 / 运行评估按钮。
 */
export default function EvaluationConfig(p: EvaluationConfigProps) {
  // 受控面板 useForm：渲染用 form.values.horizon，变更双写 setFieldValue + onHorizon，
  // 外部重灌（URL 恢复 / 历史任务恢复）经 props 变化 → useEffect setValues 同步
  const form = useForm({
    initialValues: { horizon: p.horizon },
    validate: {
      horizon: (v) => (v != null && v >= 1 && v <= 20 ? null : '1~20'),
    },
  })
  useEffect(() => {
    form.setValues({ horizon: p.horizon })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.horizon])

  return (
    <>
      <FactorExprPanel
        expression={p.expression}
        onExpression={p.onExpression}
        factors={p.factors}
        examples={p.examples}
        textareaPlaceholder="输入因子表达式，如 rank(ts_mean(close,5) - ts_mean(close,20))"
      />

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
          <FormRow label="预测周期" width={56}>
            <Flex align="center" gap="var(--sr-pad-sm)">
              <NumberInput
                min={1} max={20} className="sr-ctl-h"
                style={{ flex: 1 }}
                value={form.values.horizon}
                onChange={(v) => {
                  const n = numOr(v, HORIZON_DEFAULT)
                  form.setFieldValue('horizon', n)
                  p.onHorizon(n)
                }}
              />
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>日</Text>
            </Flex>
          </FormRow>
          <FormRow label="Walk-forward" width={56}>
            <Flex align="center" gap="var(--sr-pad-md)" wrap="wrap">
              <Switch
                checked={p.wfEnabled}
                onChange={(e) => p.onWfEnabled(e.currentTarget.checked)}
                label="滚动验证"
              />
              {p.wfEnabled && (
                <Flex align="center" gap="var(--sr-pad-sm)" style={{ flex: '1 1 140px', minWidth: 0 }}>
                  <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>窗数</Text>
                  <NumberInput
                    min={2} max={5} className="sr-ctl-h" style={{ flex: 1, minWidth: 0 }}
                    value={p.wfWindows}
                    onChange={(v) => {
                      const n = typeof v === 'string' ? Number(v) : v
                      p.onWfWindows(n != null && n >= 2 && n <= 5 ? n : WF_WINDOWS_DEFAULT)
                    }}
                  />
                </Flex>
              )}
            </Flex>
            {p.wfEnabled && (
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)', marginTop: 'var(--sr-pad-xs)' }}>
                N 窗 anchored 滚动切分,拼接各窗样本外 OOS IC(因子固定,不做每窗重挖)
              </Text>
            )}
          </FormRow>
        </Flex>
        <Flex gap="var(--sr-pad-md)" style={{ marginTop: 'var(--sr-pad-xl)' }}>
          {/* disabled 按钮不触发 hover，外层 span 承接 Tooltip（原 antd 惯例） */}
          <Tooltip label={p.selectedDs ? undefined : '请先选择数据集'}>
            <span style={{ flex: 1 }}>
              <Button variant="default" leftSection={<IconAdjustments size={14} />} disabled={!p.selectedDs} onClick={p.onTune} style={{ width: '100%' }}>
                参数调优
              </Button>
            </span>
          </Tooltip>
          <Button variant="filled" leftSection={<IconPlayerPlay size={14} />} loading={p.running} onClick={p.onRun} style={{ flex: 1 }}>
            运行评估
          </Button>
        </Flex>
      </div>

      <LatexPreviewPanel tex={p.previewLatex ?? p.resultLatex ?? null} />

      {p.job && p.job.status !== 'done' && (
        <Flex direction="column" gap="var(--sr-pad-sm)">
          <TaskProgress job={p.job} typeLabel={{ evaluate: '因子评估', walk_forward: 'Walk-forward 验证' }} />
          {p.job.status === 'failed' && (
            <Text c="red" style={{ fontSize: 'var(--sr-font-sm)' }}>{p.job.error}</Text>
          )}
        </Flex>
      )}
    </>
  )
}
