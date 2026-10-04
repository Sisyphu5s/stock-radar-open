import { Alert, Badge, Button, Flex, Grid, GridCol, Group, Modal, NumberInput, Select, Text, Tooltip } from '@mantine/core'
import { useForm } from '@mantine/form'
import { IconAdjustments, IconCheck, IconDeviceFloppy } from '@tabler/icons-react'
import { useEffect, useMemo, useState } from 'react'
import type { SrColumn } from '../../../components/ui/tableTypes'
import type { FactorTunePayload, TuneGridItem, TuneParamMeta, TuneResult, TuneTarget } from '../../../api/client'
import { getTuneGlobal, saveTuneGlobal, tuneFactorExpression } from '../../../api/client'
import { applyGlobalParam } from '../../../utils/applyGlobalParam'
import { fmtNum, fmtPct, fmtRatio, fmtStab } from '../../../utils/format'
import FormulaCode from '../../../components/FormulaCode'
import { DataTable, TaskProgress } from '../../../components/ui'
import GlobalParamBar from './GlobalParamBar'
import { useTuneTask } from './useTuneTask'
import { toast } from '../shared/toast'

/** 网格点数上限：与后端 factor_tune 物化前算术检查(50)同一口径。 */
const TUNE_GRID_MAX = 50
/** 参数区间建议默认值（applyParamSuggestions 单一事实源；onChange 清空兜底同源）。 */
const TUNE_SUGGEST = {
  int: { min: 3, max: 20, step: 1 },
  float: { min: 0.005, max: 0.02, step: 0.005 },
}

const TUNE_TARGET_GROUPS: { label: string; options: { label: string; value: TuneTarget }[] }[] = [
  {
    label: '理论指标',
    options: [
      { label: 'IC（预测方向）', value: 'ic' },
      { label: '|IC|（方向无关）', value: 'ic_abs' },
      { label: 'ICIR（稳健性）', value: 'icir' },
      { label: 'RankIC（秩相关）', value: 'rank_ic' },
      { label: '稳定性（IC>0 占比）', value: 'stability' },
      { label: '换手率', value: 'turnover' },
    ],
  },
  {
    label: '实操指标',
    options: [
      { label: '多空年化', value: 'ls_annual' },
      { label: '夏普比率', value: 'sharpe' },
      { label: '最大回撤（越小越好）', value: 'max_drawdown' },
      { label: '胜率', value: 'win_rate' },
    ],
  },
  {
    label: '综合',
    options: [
      { label: '综合评分（IC×稳定性+夏普加权）', value: 'composite' },
    ],
  },
]
const TUNE_TARGET_LABEL = (v: string) =>
  TUNE_TARGET_GROUPS.flatMap((g) => g.options).find((t) => t.value === v)?.label ?? v

const TUNE_PARAM_LABEL: Record<string, string> = {
  window: '窗口 w',
  delay: '延迟 k',
  power: '幂次 w',
  pct: '缩尾分位 pct',
}

interface TuneFormValues {
  min: number
  max: number
  step: number
  target: TuneTarget
  horizon: number
}

interface TuneModalProps {
  open: boolean
  onClose: () => void
  expression: string
  /** 数据集可用性（未选则不可提交调优） */
  datasetAvailable: boolean
  /** 数据集 ID（调优提交需要；未选时为 undefined） */
  datasetId?: number
  /** 最优参数/全局参数应用回填表达式（父级 setExpression） */
  onApplied: (expr: string) => void
  /** 调优任务状态上报（父级横幅「调优任务 #N」） */
  onJobChange: (job: any, polling: boolean) => void
}

/** NumberInput 值归一化：空串 → 建议默认值（原 antd v ?? default 语义；validate 兜空值） */
const numOr = (v: number | string, fb: number): number =>
  (typeof v === 'string' && v.trim() === '') ? fb : Number(v)

/**
 * 参数调优 Modal（T-43：自 FactorEvaluation 拆出，useTuneTask 自持任务轮询）。
 * 内部状态：可调参数 probe / min-max-step-target-horizon 表单（useForm + validate）/
 * 网格预览与结果表（TuneGridItem 列定义内聚本文件）/ 全局参数加载与应用。
 * Modal 常驻（组件在父级恒挂载）：关闭后调优任务继续轮询，父级经 onJobChange 拿到 job 渲染横幅。
 */
export default function TuneModal({ open, onClose, expression, datasetAvailable, datasetId, onApplied, onJobChange }: TuneModalProps) {
  const [tuneParams, setTuneParams] = useState<TuneParamMeta[]>([])
  const [tuneLoading, setTuneLoading] = useState(false)
  const [tuneParamName, setTuneParamName] = useState<string | undefined>()
  const [tuneResult, setTuneResult] = useState<TuneResult | null>(null)
  const [tuneError, setTuneError] = useState<string | null>(null)
  const [globalParams, setGlobalParams] = useState<Record<string, string>>({})
  const [globalLoading, setGlobalLoading] = useState(false)
  const [applyingGlobal, setApplyingGlobal] = useState(false)

  // 调优区间/目标/周期表单（受控 useForm；probe 后 applyParamSuggestions 重灌建议值）
  const form = useForm<TuneFormValues>({
    initialValues: { min: 3, max: 20, step: 1, target: 'ic', horizon: 5 },
    validate: {
      min: (v) => (v != null && v >= 0 ? null : '≥0'),
      max: (v) => (v != null && v >= 0 ? null : '≥0'),
      step: (v) => (v != null && v > 0 ? null : '>0'),
      target: (v) => (v ? null : '必选'),
      horizon: (v) => (v != null && v >= 1 && v <= 20 ? null : '1~20'),
    },
  })

  // 参数调优任务（共享任务池）：提交参数读 getOpts 最新快照；onDone 收窄载荷、onFailed 置错误
  const { job: tuneJob, polling: tunePolling, submit: submitTune } = useTuneTask({
    getOpts: () => {
      if (datasetId == null || !tuneParamName) return null
      return {
        expression: expression.trim(),
        datasetId,
        horizon: form.values.horizon,
        target: form.values.target,
        // is_int 必须显式携带：后端默认 True，float 参数不传会被 round 去重导致网格失真
        param: {
          name: tuneParamName,
          min: form.values.min,
          max: form.values.max,
          step: form.values.step,
          is_int: tuneParams.find((m) => m.name === tuneParamName)?.is_int ?? true,
        },
      }
    },
    onDone: (r0: FactorTunePayload | undefined) => {
      // factor_tune 载荷收窄（判别联合）：available_params 后端为参数元信息列表，转名称数组
      setTuneResult({
        ...r0,
        expression: r0?.expression ?? '',
        target: r0?.target ?? 'ic',
        horizon: r0?.horizon ?? 5,
        available_params: (r0?.available_params ?? []).map((p) => p.name),
        param_meta: [],
      } as TuneResult)
    },
    onFailed: (err) => setTuneError('调优失败: ' + err),
  })

  // 调优任务状态上报父级（横幅「调优任务 #N」）；job/polling 引用变化才触发
  useEffect(() => {
    onJobChange?.(tuneJob, tunePolling)
  }, [tuneJob, tunePolling, onJobChange])

  // 打开 Modal 时加载全局参数
  useEffect(() => {
    if (!open) return
    let cancelled = false
    setGlobalLoading(true)
    getTuneGlobal()
      .then((g) => { if (!cancelled) setGlobalParams(g) })
      .catch(() => { if (!cancelled) setGlobalParams({}) })
      .finally(() => { if (!cancelled) setGlobalLoading(false) })
    return () => { cancelled = true }
  }, [open])

  // 打开 Modal 时探测可调参数（probe 模式固定返回 TuneResult，同步仅编译表达式）
  useEffect(() => {
    if (!open) return
    let cancelled = false
    setTuneResult(null)
    setTuneError(null)
    // probe 前清空上次成功的参数列表：失败/无参数时错误 Alert 不与旧参数列表并存
    setTuneParams([])
    setTuneParamName(undefined)
    setTuneLoading(true)
    tuneFactorExpression({
      expression: expression.trim(), dataset_id: datasetId ?? 0, probe: true,
    }).then((r) => {
      if (cancelled) return
      const pr = (r as TuneResult).param_meta ?? []
      setTuneParams(pr)
      if (pr.length === 0) {
        setTuneError('表达式中没有可调参数（如 ts_mean/ts_std/ts_delay/delta/ts_corr 等的窗口或延迟参数）')
      } else {
        const first = pr[0]
        setTuneParamName(first.name)
        applyParamSuggestions(first)
      }
    }).catch((e: any) => {
      if (cancelled) return
      setTuneError('获取可调参数失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
      setTuneParams([])
      setTuneParamName(undefined)
    }).finally(() => { if (!cancelled) setTuneLoading(false) })
    return () => { cancelled = true }
    // 仅 open 切换触发（原 openTune 点击即探测；expression 变更需重新打开）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  // 当前参数类型的建议默认值（NumberInput 清空兜底；与 applyParamSuggestions 同源）
  const tuneParamMeta = tuneParams.find((m) => m.name === tuneParamName)
  const tuneDefault = () => (tuneParamMeta?.is_int ? TUNE_SUGGEST.int : TUNE_SUGGEST.float)

  const applyParamSuggestions = (m: TuneParamMeta | undefined) => {
    if (!m) return
    const s = m.is_int ? TUNE_SUGGEST.int : TUNE_SUGGEST.float
    form.setValues({ min: s.min, max: s.max, step: s.step })
  }

  const runTune = async () => {
    if (!datasetAvailable) { toast.warning('请选择数据集'); return }
    if (!tuneParamName) { toast.warning('请选择要调优的参数'); return }
    setTuneResult(null)
    setTuneError(null)
    try {
      // 参数调优 = 后台任务（factor_tune，计入任务管理器）：提交/轮询/收尾统一走 useTuneTask
      await submitTune()
    } catch (e: any) {
      setTuneError('调优失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
    }
  }

  const applyBest = () => {
    if (!tuneResult?.best_expression) { toast.warning('暂无最优参数可应用'); return }
    onApplied(tuneResult.best_expression)
    toast.success('参数已回填表达式，请重新运行评估')
  }

  const saveGlobal = async () => {
    const key = tuneResult?.param?.name ?? tuneParamName
    if (!key || !tuneResult?.best) { toast.warning('暂无最优参数可保存'); return }
    const value = tuneResult.best.param_value
    try {
      await saveTuneGlobal({ [key]: value })
      toast.success(`已保存全局参数 ${key}=${value}`)
      setGlobalParams(await getTuneGlobal())
    } catch (e: any) {
      toast.error('保存全局参数失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
    }
  }

  const applyGlobal = () => {
    if (!expression.trim()) { toast.warning('请输入因子表达式'); return }
    const keys = Object.keys(globalParams)
    if (keys.length === 0) { toast.warning('暂无全局参数'); return }
    setApplyingGlobal(true)
    let next = expression.trim()
    try {
      for (const k of keys) {
        const r = applyGlobalParam(next, k, globalParams[k])
        if (!r.ok) {
          toast.warning(`无法自动应用：全局参数 ${k} 未精确匹配表达式（按算子名+出现位置匹配）`)
          return
        }
        next = r.expr
      }
      if (next === expression.trim()) {
        toast.warning('无法自动应用：表达式未包含与全局参数匹配的算子')
        return
      }
      onApplied(next)
      toast.success('已应用全局参数到表达式')
    } finally {
      setApplyingGlobal(false)
    }
  }

  const tuneGrid = tuneResult?.grid ?? []
  const bestValue = tuneResult?.best?.param_value
  // 网格点数预览：与后端 factor_tune 物化前算术检查 int((max-min)/step)+1 同一口径；区间非法时计 0
  const gridCount = useMemo(() => {
    if (form.values.step <= 0 || form.values.min > form.values.max) return 0
    return Math.floor((form.values.max - form.values.min) / form.values.step) + 1
  }, [form.values.min, form.values.max, form.values.step])
  const gridOverLimit = gridCount > TUNE_GRID_MAX
  const minGtMax = form.values.min > form.values.max
  // 当前参数 vs 已提交结果参数：不一致 = 结果过期，需重新调优（驱动「运行调优」主次）
  const paramsChanged = useMemo(() => {
    const pr = tuneResult?.param
    if (!pr) return false
    return pr.name !== tuneParamName || pr.min !== form.values.min || pr.max !== form.values.max || pr.step !== form.values.step
  }, [tuneResult, tuneParamName, form.values.min, form.values.max, form.values.step])
  // columns 引用稳定化（DataTable 已 memo）：deps 仅 bestValue（最优行高亮/加粗的唯一可变依赖），fmtNum/fmtPct 等为模块级稳定引用
  const tuneColumns = useMemo<SrColumn<TuneGridItem>[]>(() => [
    {
      title: '参数值', dataIndex: 'param_value', align: 'right', className: 'sr-num-col', minWidth: 90,
      render: (v, rec) => (
        <Group gap={4} wrap="nowrap">
          <span style={{ fontWeight: rec.param_value === bestValue ? 700 : 400 }}>{fmtNum(v, 4)}</span>
          {rec.param_value === bestValue && <Badge color="yellow" variant="light">最优</Badge>}
        </Group>
      ),
    },
    { title: '训练IC', dataIndex: 'train_ic', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v) => fmtRatio(v) },
    { title: 'ICIR', dataIndex: 'icir', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v) => fmtRatio(v) },
    { title: '多空年化', dataIndex: 'ls_annual', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v) => fmtPct(v) },
    { title: '夏普', dataIndex: 'ls_sharpe', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v) => fmtRatio(v) },
    { title: '最大回撤', dataIndex: 'max_drawdown', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v) => <span style={{ color: 'var(--sr-down)' }}>{fmtPct(v)}</span> },
    { title: '胜率', dataIndex: 'win_rate', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v) => fmtPct(v) },
    { title: '换手', dataIndex: 'turnover', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v) => fmtRatio(v, 3) },
    { title: '稳定性', dataIndex: 'stability', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v) => fmtStab(v) },
    { title: 'OOS IC', dataIndex: 'oos_ic', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v) => fmtRatio(v) },
    { title: '误差', dataIndex: 'error', align: 'right', className: 'sr-num-col', ellipsis: true, minWidth: 100, render: (v) => v ? <Text c="red" style={{ fontSize: 'var(--sr-font-sm)' }}>{String(v).slice(0, 80)}</Text> : null },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [bestValue])

  return (
    <Modal
      opened={open}
      onClose={onClose}
      title={<Group gap="var(--sr-pad-sm)" wrap="nowrap"><IconAdjustments style={{ color: 'var(--sr-accent)' }} />参数调优 · 网格搜索</Group>}
      size="min(860px, 96vw)"
      keepMounted
    >
      <Flex direction="column" gap="var(--sr-pad-lg)" style={{ width: '100%' }}>
        <div style={{
          fontSize: 'var(--sr-font-sm)', padding: 'var(--sr-pad-md) var(--sr-pad-lg)', borderRadius: 'var(--sr-radius-card)',
          background: 'var(--sr-block-bg)', border: '1px solid var(--sr-border)',
        }}>
          {expression.trim() ? <FormulaCode expr={expression.trim()} /> : '（空表达式）'}
        </div>

        <GlobalParamBar
          params={globalParams}
          loading={globalLoading}
          canSave={!!tuneResult?.best}
          onSave={() => void saveGlobal()}
          onApply={applyGlobal}
          applying={applyingGlobal}
        />

        <Grid gap={10}>
          <GridCol span={{ base: 24, sm: 14, md: 10, lg: 8 }}>
            <Flex direction="column" gap={4}>
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>可调参数</Text>
              <Select
                className="sr-ctl-h"
                placeholder="选择可调参数"
                value={tuneParamName ?? null}
                onChange={(v) => {
                  if (v != null) {
                    setTuneParamName(v)
                    applyParamSuggestions(tuneParams.find((m) => m.name === v))
                  }
                }}
                data={tuneParams.map((m) => ({
                  value: m.name,
                  label: `${m.op}.${TUNE_PARAM_LABEL[m.key] ?? m.key}（默认 ${m.default}）${m.count > 1 ? ` ×${m.count}` : ''}`,
                }))}
              />
            </Flex>
          </GridCol>
          <GridCol span={{ base: 8, sm: 4, md: 4, lg: 4 }}>
            <Flex direction="column" gap={4}>
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>min</Text>
              <NumberInput
                min={0} step={form.values.step} decimalScale={tuneParamMeta?.is_int ? 0 : 3} className="sr-ctl-h"
                error={minGtMax ? '最小值需不大于最大值' : undefined}
                value={form.values.min}
                onChange={(v) => form.setFieldValue('min', numOr(v, tuneDefault().min))}
              />
            </Flex>
          </GridCol>
          <GridCol span={{ base: 8, sm: 4, md: 4, lg: 4 }}>
            <Flex direction="column" gap={4}>
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>max</Text>
              <NumberInput
                min={0} step={form.values.step} decimalScale={tuneParamMeta?.is_int ? 0 : 3} className="sr-ctl-h"
                value={form.values.max}
                onChange={(v) => form.setFieldValue('max', numOr(v, tuneDefault().max))}
              />
            </Flex>
          </GridCol>
          <GridCol span={{ base: 8, sm: 4, md: 4, lg: 4 }}>
            <Flex direction="column" gap={4}>
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>step</Text>
              <NumberInput
                min={tuneParamMeta?.is_int ? 1 : 0.0001} step={form.values.step} decimalScale={tuneParamMeta?.is_int ? 0 : 3} className="sr-ctl-h"
                value={form.values.step}
                onChange={(v) => form.setFieldValue('step', numOr(v, tuneDefault().step))}
              />
            </Flex>
          </GridCol>
          <GridCol span={{ base: 12, sm: 8, md: 10, lg: 8 }}>
            <Flex direction="column" gap={4}>
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>目标</Text>
              <Select className="sr-ctl-h" value={form.values.target}
                onChange={(v) => { if (v != null) form.setFieldValue('target', v as TuneTarget) }}
                data={TUNE_TARGET_GROUPS.map((g) => ({ group: g.label, items: g.options }))} />
            </Flex>
          </GridCol>
          <GridCol span={{ base: 12, sm: 8, md: 8, lg: 6 }}>
            <Flex direction="column" gap={4}>
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>周期</Text>
              <NumberInput min={1} max={20} className="sr-ctl-h" suffix="日" value={form.values.horizon}
                onChange={(v) => form.setFieldValue('horizon', numOr(v, 5))} />
            </Flex>
          </GridCol>
        </Grid>

        {tuneParams.length > 0 && (
          <div>
            <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
              本次调优约 <Text span fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>{gridCount}</Text> 个网格点（上限 {TUNE_GRID_MAX}）
            </Text>
            {gridOverLimit && (
              <Alert color="yellow" title={`网格 ${gridCount} 点超过上限 ${TUNE_GRID_MAX}，请增大 step 或收窄范围`} style={{ marginTop: 'var(--sr-pad-md)' }} />
            )}
          </div>
        )}

        {tunePolling && (
          <TaskProgress job={tuneJob} typeLabel={{ factor_tune: '参数调优' }} />
        )}

        {tuneError && <Alert color="red" title={tuneError} />}

        {tuneResult && (
          <Flex direction="column" gap="var(--sr-pad-lg)" style={{ width: '100%' }}>
            <Alert
              color="teal"
              title={(
                <Group wrap="wrap" gap="var(--sr-pad-sm)">
                  <span>网格 {tuneGrid.length} 点 · 按目标“{TUNE_TARGET_LABEL(tuneResult.target)}”排序</span>
                  {tuneResult.best && tuneResult.meta && (
                    <span>训练 0~{tuneResult.meta.split?.train ?? ''} 日 / 样本外 {tuneResult.meta.split?.oos ?? ''} 日</span>
                  )}
                </Group>
              )}
            />
            {tuneResult.best_expression && (
              <Text style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-2)' }}>
                最优表达式：<FormulaCode expr={tuneResult.best_expression} />
              </Text>
            )}
            <DataTable<TuneGridItem>
              rowKey="param_value" pagination={false}
              columns={tuneColumns} dataSource={tuneGrid}
              onRow={(rec) => rec.param_value === bestValue
                ? { style: { background: 'color-mix(in srgb, var(--sr-accent) 10%, transparent)' } }
                : {}}
            />
          </Flex>
        )}
        {!tuneResult && !tuneError && (
          <Text style={{ margin: 0, textAlign: 'center', color: 'var(--sr-text-2)', fontSize: 'var(--sr-font-sm)' }}>
            {tuneLoading ? '正在探测可调参数…' : tunePolling ? '调优任务运行中，完成后结果将显示在此处' : '网格上限 50 点，后台任务'}
          </Text>
        )}

        <Group justify="flex-end" wrap="wrap" gap="var(--sr-pad-sm)" style={{ marginTop: 'var(--sr-pad-md)' }}>
          <Button variant="subtle" onClick={onClose}>关闭</Button>
          <Tooltip label={!tuneResult?.best ? '运行调优后可保存最优参数' : undefined}>
            <span>
              <Button variant="default" leftSection={<IconDeviceFloppy size={14} />} disabled={!tuneResult?.best} onClick={() => void saveGlobal()}>
                保存为全局参数
              </Button>
            </span>
          </Tooltip>
          <Tooltip label={tuneResult && !paramsChanged ? '参数未变更，结果仍有效' : undefined}>
            <Button
              variant={tunePolling ? 'default' : tuneResult && !paramsChanged ? 'default' : 'filled'}
              leftSection={<IconAdjustments size={14} />}
              loading={tunePolling}
              disabled={tuneLoading || minGtMax || gridOverLimit}
              onClick={() => void runTune()}
            >
              {tunePolling ? '调优中…' : '运行调优'}
            </Button>
          </Tooltip>
          <Tooltip label={!tuneResult?.best_expression ? '运行调优后可一键应用最优参数' : undefined}>
            <span>
              <Button
                variant={tuneResult?.best_expression ? 'filled' : 'default'}
                leftSection={<IconCheck size={14} />}
                disabled={!tuneResult?.best_expression}
                onClick={applyBest}
              >
                应用最优参数
              </Button>
            </span>
          </Tooltip>
        </Group>
      </Flex>
    </Modal>
  )
}
