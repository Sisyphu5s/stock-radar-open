import { Button, Modal, MultiSelect, NumberInput, SegmentedControl, Select, Stepper, Stack, Text, TextInput, Tooltip } from '@mantine/core'
import { useEffect, useState, type ReactNode } from 'react'
import { useStockSearch } from '../../../hooks/useStockSearch'
import { PAPER_CODE_RE } from './constants'
import './ProjectWizard.css'

/** 新建项目表单值（向导三步收集，最终一次性回调主组件创建） */
export interface ProjectWizardValues {
  /** 留空时主组件默认「未命名项目」 */
  name?: string
  kind: 'experiment' | 'watch'
  /** 首股代码（stocks[0]，兼容单股路径） */
  code: string
  /** 股票代码列表（≥1，≤20，多股批量） */
  stocks: string[]
  period: string
  signals: string[]
  /** 仅实验项目（watch 不传） */
  days?: number
}

interface ProjectWizardProps {
  open: boolean
  signalOptions: { value: string; label: string; description?: string }[]
  periodOptions: { label: string; value: string }[]
  /** 股票数量上限（meta.max_stocks，缺省 20） */
  maxStocks: number
  creating: boolean
  onCancel: () => void
  onCreate: (values: ProjectWizardValues) => void
}

/**
 * 三步创建向导：①类型 + 名称 → ②股票 + 周期 → ③信号 + 回溯天数（仅实验）。
 * 步骤校验不过不可下一步；股票多选（≤maxStocks，搜索下拉 + 手输代码），防抖 + seq 防竞态
 * （自旧 ProjectCreateModal 迁移）。创建成功后由主组件负责选中项目，不自动运行/监控。
 * Mantine 迁移（T-44）：Steps→Stepper；前跳校验由「下一步」按钮承担（标题前跳经
 * allowNextStepsSelect=false 禁掉——原 antd 的「满足校验时点标题前跳」降级为仅回退自由）。
 */
export default function ProjectWizard({ open, signalOptions, periodOptions, maxStocks, creating, onCancel, onCreate }: ProjectWizardProps) {
  const [step, setStep] = useState(0)
  const [kind, setKind] = useState<'experiment' | 'watch'>('experiment')
  const [name, setName] = useState('')
  const [stockCodes, setStockCodes] = useState<string[]>([])
  const [period, setPeriod] = useState('daily')
  const [signals, setSignals] = useState<string[]>([])
  const [days, setDays] = useState(250)
  const stockSearch = useStockSearch()

  // 每次打开重置全部状态（依赖仅 open：stockSearch/periodOptions 引用不稳定，误加依赖会触发 setState 循环）
  useEffect(() => {
    if (!open) return
    setStep(0)
    setKind('experiment')
    setName('')
    setStockCodes([])
    setPeriod('daily') // daily 恒在 paper 周期白名单（VALID_PERIODS），默认值安全
    setSignals([])
    setDays(250)
    stockSearch.clear()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  /** 第 i 步校验：①恒可过（名称可空）；②股票必选 ≥1；③信号必选 ≥1 */
  const canNextFrom = (i: number) => i === 0 ? true : i === 1 ? stockCodes.length >= 1 : signals.length >= 1
  const canNext = canNextFrom(step)

  const submit = () => {
    if (creating) return
    onCreate({
      name: name.trim() || undefined,
      kind,
      code: stockCodes[0].trim(),
      stocks: stockCodes.map((c) => c.trim()),
      period,
      signals,
      days: kind === 'experiment' ? days : undefined,
    })
  }

  /** 信号下拉渲染：description 非空 → Tooltip（meta 白名单单一事实源，加载失败时无 description） */
  const renderSignalOption = (label: ReactNode, desc: string | undefined) =>
    desc ? <Tooltip label={desc}>{label}</Tooltip> : <>{label}</>

  return (
    <Modal
      opened={open}
      onClose={onCancel}
      title="新建项目"
      centered
      size="clamp(340px, 44vw, 480px)"
      padding="var(--sr-card-pad)"
    >
      <div className="sr-paper-wizard">
        <Stepper
          size="sm"
          contentPadding={0}
          active={step}
          // 仅允许回退到已过步骤；前跳校验由「下一步」按钮承担（防从第 1 步直点第 3 步标题绕过股票校验）
          allowNextStepsSelect={false}
          onStepClick={(s) => { if (s < step) setStep(s) }}
        >
          <Stepper.Step label="类型" />
          <Stepper.Step label="股票与周期" />
          <Stepper.Step label="信号" />
        </Stepper>
        <div className="sr-paper-wizard-body">
          {step === 0 && (
            <Stack gap={14}>
              <div>
                <div className="sr-paper-wizard-label">项目类型</div>
                <SegmentedControl
                  fullWidth
                  value={kind}
                  onChange={(v) => setKind(v as 'experiment' | 'watch')}
                  data={[
                    { label: '指标信号实验', value: 'experiment' },
                    { label: '实时指标监控', value: 'watch' },
                  ]}
                />
              </div>
              <div>
                <div className="sr-paper-wizard-label">项目名称</div>
                <TextInput
                  value={name}
                  onChange={(e) => setName(e.currentTarget.value)}
                  placeholder="留空默认「未命名项目」"
                  maxLength={30}
                  onKeyDown={(e) => { if (e.key === 'Enter' && canNext) setStep(step + 1) }}
                />
              </div>
            </Stack>
          )}
          {step === 1 && (
            <Stack gap={14}>
              <div>
                <div className="sr-paper-wizard-label">股票（可多选，最多 {maxStocks} 只）</div>
                <MultiSelect
                  searchable
                  placeholder="输入代码 / 名称搜索选择"
                  maxValues={maxStocks}
                  value={stockCodes}
                  data={stockSearch.options}
                  onSearchChange={stockSearch.onQuery}
                  onChange={(vals) => {
                    // 手输过滤非法项；搜索选中项（code）恒合法；选中后清空在途搜索（useStockSearch.onSelect 副作用）
                    const next = vals.filter((v) => PAPER_CODE_RE.test(v.trim()))
                    const added = next.find((v) => !stockCodes.includes(v))
                    if (added != null) stockSearch.onSelect(added)
                    setStockCodes(next)
                  }}
                />
                <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)', marginTop: 4 }}>
                  {stockCodes.length === 0
                    ? '从搜索结果中选择，或输入代码搜索（如 600519 或 600519.SH）'
                    : `已选 ${stockCodes.length} 只股票`}
                </Text>
              </div>
              <div>
                <div className="sr-paper-wizard-label">周期</div>
                <Select size="xs" value={period} data={periodOptions} onChange={(v) => setPeriod(v ?? 'daily')} />
              </div>
            </Stack>
          )}
          {step === 2 && (
            <Stack gap={14}>
              <div>
                <div className="sr-paper-wizard-label">信号（可多选，至少 1 个）</div>
                <MultiSelect
                  size="xs"
                  placeholder="选择一个或多个信号"
                  value={signals}
                  data={signalOptions}
                  renderOption={({ option }) => renderSignalOption(option.label, (option as { description?: string }).description)}
                  onChange={setSignals}
                />
                {signals.length === 0 && (
                  <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)', marginTop: 4 }}>请至少选择一个信号</Text>
                )}
              </div>
              {kind === 'experiment' && (
                <div>
                  <div className="sr-paper-wizard-label">回溯天数</div>
                  <NumberInput
                    min={60}
                    max={800}
                    step={10}
                    value={days}
                    onChange={(d) => setDays(Number(d) || 250)}
                  />
                </div>
              )}
            </Stack>
          )}
        </div>
        <div className="sr-paper-wizard-footer">
          <Button variant="default" disabled={step === 0} onClick={() => setStep(step - 1)}>上一步</Button>
          {step < 2 ? (
            <Button disabled={!canNext} onClick={() => setStep(step + 1)}>下一步</Button>
          ) : (
            <Button loading={creating} disabled={!canNext} onClick={submit}>创建</Button>
          )}
        </div>
      </div>
    </Modal>
  )
}
