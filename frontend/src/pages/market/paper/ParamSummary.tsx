import { Autocomplete, Badge, Button, Group, Input, MultiSelect, NumberInput, Progress, Select, Stack, Text, Tooltip } from '@mantine/core'
import { useForm } from '@mantine/form'
import {
  IconDeviceFloppy, IconPencil, IconPlayerPause, IconPlayerPlay, IconPlayerStop, IconRefresh,
} from '@tabler/icons-react'
import { useEffect, useRef, useState, type ReactNode } from 'react'
import type { PaperProject, PaperProjectStock } from '../../../api/client'
import { periodLabel } from '../../../utils/periods'
import { formatFullTime } from '../../../utils/time'
import { PAPER_CODE_RE, WATCH_INTERVAL_MS, WATCH_INTERVAL_TEXT } from './constants'
import './ParamSummary.css'

/** epoch ms 时钟显示（绝对时刻无 naive ISO 时区歧义，统一走 formatFullTime 出口；T-44 去 slice 手裁） */
function fmtClockTime(t: number): string {
  return formatFullTime(t, { withYear: false })
}

/** 监控倒计时独立小组件：仅自身每秒重渲染（局部 useState+interval），页面其余部分不随 1s 时钟整页重渲染 */
function TradingClock({ lastUpdatedAt }: { lastUpdatedAt: number | null }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = window.setInterval(() => {
      if (document.visibilityState === 'hidden') return // 后台不空转,回前台下一 tick 续上
      setNow(Date.now())
    }, 1000)
    return () => window.clearInterval(t)
  }, [])
  const countdown = lastUpdatedAt
    ? Math.max(0, Math.ceil(WATCH_INTERVAL_MS / 1000 - (now - lastUpdatedAt) / 1000))
    : Math.ceil(WATCH_INTERVAL_MS / 1000)
  const updatedText = lastUpdatedAt ? fmtClockTime(lastUpdatedAt) : '—'
  return (
    <span className="sr-paper-watch-meta">
      更新时间 {updatedText} · 下次更新 {countdown}s
    </span>
  )
}

/** 实验任务运行态（提升于主组件；切走项目后不显示，任务在后台继续） */
export interface RunJobState {
  /** 关联项目 id：仅当前选中项目匹配时展示运行态 */
  projectId: number
  /** 任务 id（getJob/pauseJob/resumeJob/DELETE /experiments/{id} 使用） */
  id: number
  /** 进度 0-100（round 取整） */
  progress: number
  /** 协作式暂停中（在下一个计算检查点停下） */
  paused: boolean
}

interface ParamSummaryProps {
  project: PaperProject
  // ---- 表单状态（提升到主组件；编辑态内联修改，保存时 PATCH 持久化） ----
  /** 编辑态（提升到主组件：切项目需未保存提示，由主组件拦截） */
  editing: boolean
  onEditingChange: (v: boolean) => void
  code: string
  stockText: string
  /** 项目股票列表（多股编辑态只读展示；stocks 变更仅在创建向导支持） */
  stocks: PaperProjectStock[]
  period: string
  signalCodes: string[]
  days: number
  stockOptions: { value: string; label: string }[]
  signalOptions: { value: string; label: string; description?: string }[]
  periodOptions: { label: string; value: string }[]
  onStockTextChange: (v: string) => void
  onStockSelect: (code: string, label: string) => void
  onStockSearch: (q: string) => void
  onPeriodChange: (p: string) => void
  onSignalsChange: (codes: string[]) => void
  onDaysChange: (d: number | null) => void
  /** 保存回调：PATCH 持久化，返回是否成功（成功才退出编辑态） */
  onSave: () => Promise<boolean>
  saving: boolean
  // ---- experiment 运行态 ----
  expLoading: boolean
  runJob: RunJobState | null
  onRun: () => void
  onPauseJob: () => void
  onResumeJob: () => void
  onCancelJob: () => void
  // ---- watch 状态 ----
  watching: boolean
  lastUpdatedAt: number | null
  watchError: boolean
  onFetchWatch: () => void
  onStartWatch: () => void
  onStopWatch: () => void
}

/**
 * 参数摘要条：默认 chip 行「{code} · {period} · {n} 信号 · {days} 天」+ [编辑]；
 * 编辑态内联表单（自旧 PaperForm 迁移，同源表单状态）+ [保存][取消]；
 * watch 项目附加监控状态区（每 15 秒轮询 / 停止 / 错误重试），主按钮「开始监控」；
 * experiment 项目主按钮「运行实验」，任务运行中显示进度 + 暂停/恢复/取消。
 * Mantine 迁移（T-44）：antd Form → @mantine/form useForm（表单值仍受控提升于父级，
 * 控件 onChange 同步 setFieldValue + 写父级，校验走 form.validate()，错误行内展示）。
 */
export default function ParamSummary(props: ParamSummaryProps) {
  const {
    project, editing, onEditingChange, code, stockText, stocks, period, signalCodes, days, stockOptions, signalOptions, periodOptions,
    onStockTextChange, onStockSelect, onStockSearch, onPeriodChange, onSignalsChange, onDaysChange,
    onSave, saving, expLoading, runJob, onRun, onPauseJob, onResumeJob, onCancelJob,
    watching, lastUpdatedAt, watchError, onFetchWatch, onStartWatch, onStopWatch,
  } = props
  /** 编辑态表单实例：股票/信号必填校验收敛（save 前置 form.validate()） */
  const form = useForm({
    initialValues: { stock: stockText, period, signals: signalCodes, days },
    validate: {
      stock: () => (isMultiStock || code ? undefined : '请先选择股票'),
      signals: (v: string[]) => (v.length >= 1 ? undefined : '请至少选择一个信号'),
    },
  })
  /** 区分「下拉选择后的 onChange」与「用户手输」：onOptionSubmit 先于 onChange 触发，选中后跳过下一次 onChange 的清空逻辑 */
  const selectedRef = useRef(false)
  const isExperiment = project.kind === 'experiment'
  /** 多股项目：股票不可在编辑态改（stocks 变更仅创建向导支持，避免 PATCH code 与关联表脱节） */
  const isMultiStock = (stocks?.length ?? 1) > 1

  /** 保存：先 form.validate() 校验（失败 → 行内错误，不发请求），通过后 PATCH 持久化（父级 saveParams 读取提升状态） */
  const save = async () => {
    const { hasErrors } = form.validate()
    if (hasErrors) return // 校验失败：保持编辑态，错误由表单行内展示
    const ok = await onSave()
    if (ok) onEditingChange(false)
  }

  const runActive = runJob != null && runJob.projectId === project.id

  /** 信号下拉渲染：description 非空 → Tooltip（meta 白名单单一事实源，加载失败时无 description） */
  const renderSignalOption = (label: ReactNode, desc: string | undefined) =>
    desc ? <Tooltip label={desc}>{label}</Tooltip> : <>{label}</>

  const experimentActions = runActive ? (
    <div className="sr-paper-runbar">
      <Progress value={runJob.progress} size="sm" style={{ flex: '1 1 140px', minWidth: 140, maxWidth: 200 }} />
      <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
        {runJob.paused ? '已暂停' : `运行中 ${runJob.progress}%`}
      </Text>
      {runJob.paused ? (
        <Button size="xs" leftSection={<IconPlayerPlay size={14} />} onClick={onResumeJob}>恢复</Button>
      ) : (
        <Button size="xs" leftSection={<IconPlayerPause size={14} />} onClick={onPauseJob}>暂停</Button>
      )}
      <Button size="xs" color="red" variant="outline" leftSection={<IconPlayerStop size={14} />} onClick={onCancelJob}>取消</Button>
    </div>
  ) : (
    <Button size="xs" leftSection={<IconPlayerPlay size={14} />} loading={expLoading} onClick={onRun}>
      运行实验
    </Button>
  )

  const watchActions = (
    <>
      {watching && (
        <div className="sr-paper-watch-head">
          <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>{WATCH_INTERVAL_TEXT}轮询更新</Text>
          <TradingClock lastUpdatedAt={lastUpdatedAt} />
        </div>
      )}
      {watchError && (
        <span className="sr-paper-watch-error">
          更新失败
          <Button size="xs" variant="subtle" leftSection={<IconRefresh size={12} />} onClick={onFetchWatch} style={{ padding: 0, height: 'auto' }}>
            重试
          </Button>
        </span>
      )}
      {watching ? (
        <>
          <Button size="xs" variant="subtle" leftSection={<IconRefresh size={14} />} onClick={onFetchWatch} aria-label="手动刷新">刷新</Button>
          <Button size="xs" color="red" variant="outline" leftSection={<IconPlayerPause size={14} />} onClick={onStopWatch}>停止监控</Button>
        </>
      ) : (
        <Button size="xs" leftSection={<IconPlayerPlay size={14} />} onClick={onStartWatch}>开始监控</Button>
      )}
    </>
  )

  return (
    <div className="sr-paper-summary">
      {editing ? (
        <Stack gap="var(--sr-gap-row)" role="group" aria-label="项目参数编辑">
          {isMultiStock ? (
            <div>
              <Input.Label>股票（多股）</Input.Label>
              <div className="sr-paper-stock-readonly">
                {stocks.map((s) => (
                  <Badge key={s.code} variant="default" radius="sm">{s.name || s.code}（{s.code}）</Badge>
                ))}
                <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
                  股票不可在此修改，需删除项目后重建
                </Text>
              </div>
            </div>
          ) : (
            <div>
              <Input.Label>股票</Input.Label>
              <Autocomplete
                id="sr-paper-stock"
                placeholder="输入代码 / 名称搜索"
                value={stockText}
                data={stockOptions}
                // 搜索由 useStockSearch 防抖驱动（onChange 里 onStockSearch），禁用内置过滤避免把结果滤掉
                filter={(input) => input.options}
                error={form.errors.stock}
                onOptionSubmit={(v) => {
                  selectedRef.current = true
                  const opt = stockOptions.find((o) => o.value === v)
                  onStockSelect(v, opt?.label ?? v)
                }}
                onChange={(v) => {
                  if (selectedRef.current) { selectedRef.current = false; return } // 下拉选中后的 onChange，由 onOptionSubmit 统一写入
                  form.setFieldValue('stock', v)
                  onStockTextChange(v)
                  onStockSearch(v)
                  // 手输合法直接认可；空或非空非法一律清空 code（防旧 code 残留静默提交错误股票）
                  const t = v.trim()
                  if (t && PAPER_CODE_RE.test(t)) onStockSelect(t, '')
                  else onStockSelect('', '')
                }}
              />
              {code && (
                <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)', marginTop: 2 }}>已选：{code}</Text>
              )}
            </div>
          )}
          <div>
            <Input.Label>周期</Input.Label>
            <Select
              id="sr-paper-period"
              size="xs"
              data={periodOptions}
              value={period}
              onChange={(v) => {
                const next = v ?? period
                form.setFieldValue('period', next)
                onPeriodChange(next)
              }}
            />
          </div>
          <div>
            <Input.Label required>信号（可多选）</Input.Label>
            <MultiSelect
              id="sr-paper-signals"
              size="xs"
              placeholder="选择一个或多个信号"
              data={signalOptions}
              value={signalCodes}
              error={form.errors.signals}
              renderOption={({ option }) => renderSignalOption(option.label, (option as { description?: string }).description)}
              onChange={(v) => {
                form.setFieldValue('signals', v)
                onSignalsChange(v)
              }}
            />
          </div>
          {isExperiment && (
            <div>
              <Input.Label>回溯天数</Input.Label>
              <NumberInput
                id="sr-paper-days"
                size="xs"
                min={60}
                max={800}
                step={10}
                value={days}
                onChange={(d) => {
                  const num = typeof d === 'number' ? d : (d === '' ? null : Number(d))
                  form.setFieldValue('days', num ?? 250)
                  onDaysChange(num)
                }}
              />
            </div>
          )}
          <div className="sr-paper-actions">
            <Button leftSection={<IconDeviceFloppy size={14} />} loading={saving} onClick={() => void save()}>保存</Button>
            <Button variant="default" onClick={() => onEditingChange(false)}>取消</Button>
          </div>
        </Stack>
      ) : (
        <Group className="sr-paper-summary-view" align="center" justify="space-between" gap={8}>
          <span className="sr-paper-summary-chips">
            {isMultiStock ? `${code} 等 ${stocks.length} 只` : code} · {periodLabel(period)} · {signalCodes.length} 信号{isExperiment ? ` · ${days} 天` : ''}
          </span>
          <Group gap={6} align="center">
            {isExperiment ? experimentActions : watchActions}
            <Button size="xs" leftSection={<IconPencil size={14} />} onClick={() => onEditingChange(true)}>编辑</Button>
          </Group>
        </Group>
      )}
    </div>
  )
}
