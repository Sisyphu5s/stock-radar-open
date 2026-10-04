import { Button, Flex, Group, Text } from '@mantine/core'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { errMsg, fmtPct, fmtRatio, fmtTime, pctColor } from '../../utils/format'
import { createExperiment, getExperiments, getFactors, getJob, toLatex } from '../../api/client'
import { useDatasets } from '../../data/jobs'
import { useJobFlow } from '../../hooks/useJobFlow'
import { useJobRestore } from '../../hooks/useJobRestore'
import { useDatasetInit } from '../../hooks/useDatasetInit'
import { useResearchUrlSync } from '../../hooks/useResearchUrlSync'
import { jobResultPath } from '../../utils/jobs'
import { useLatexPreview } from '../../hooks/useLatexPreview'
import { useCopilotProvider } from '../../hooks/useCopilotProvider'
import type { BacktestSummary, BTModeKey, JobDetail } from '../../api/client'
import FormulaCode from '../../components/FormulaCode'
import PageShell from '../../components/ui/PageShell'
import ResearchSplit from '../../components/ui/ResearchSplit'
import { StatusTag } from '../../components/ui'
import EmptyState from '../../components/ui/EmptyState'
import { usePersistentState } from '../../utils/stateMemory'
import { useViewport } from '../../app/useViewport'
import ResearchFlowBar from './shared/ResearchFlowBar'
import JobResultPlaceholder from './shared/JobResultPlaceholder'
import { useListInfinite, ListLoadMeta, ListInfiniteSentinel } from './shared/ListInfinite'
import InfiniteScrollToggle from '../../components/ui/InfiniteScrollToggle'
import BacktestConfig from './backtests/BacktestConfig'
import BacktestResults from './backtests/BacktestResults'
import MetricStat from '../../components/ui/MetricStat'
import StatStrip from '../../components/ui/StatStrip'
import { OPTIMIZE_METHOD_LABEL } from '../../api/portfolio'
import { toast } from './shared/toast'
import './validation/run-layout.css'

const EXAMPLES = [
  'rank(ts_mean(close,5) - ts_mean(close,20))',
  'rank(-ts_std(close,20) / ts_mean(close,20))',
  'ts_corr(close, ts_delay(close,1), 10)',
  'rank(ts_mean(volume,5) / ts_mean(volume,20))',
]

/** 个股代码校验（与 ChatPanel stock.navigate 双保险一致）：?code= 通道只认合法代码 */
const STOCK_CODE_RE = /^\d{6}(\.(SH|SZ|BJ))?$/i

const HIST_INF_KEY = 'sr-research-bt-hist-inf'

type Direction = 'auto' | 'positive' | 'negative'

interface BacktestFormState {
  expression: string
  selectedDs?: number
  topPct: number
  tradeInterval: number
  costRate: number
  /** T-02 交易成本(基点) */
  slippageBps: number
  commissionBps: number
  stampTaxBps: number
  horizon: number
  modes: BTModeKey[]
  direction: Direction
  /** T-10 组合优化：开关 + 方法 + 权重上限(%) + 选股数 */
  optEnabled: boolean
  optMethod: string
  optMaxWeight: number
  optTopN: number
}

const normDirection = (d: string | undefined): Direction => {
  if (d === 'long') return 'positive'
  if (d === 'short') return 'negative'
  return (d === 'positive' || d === 'negative' ? d : 'auto')
}

const normHorizon = (v: unknown): number => {
  const n = Number(v)
  return Number.isFinite(n) && n >= 1 && n <= 20 ? Math.round(n) : 5
}

/** cost_rate 归一化（提交 /100 与恢复 ×100 后对齐 4 位小数），快照比对共用一份，避免浮点误差误判 stale */
const r4 = (n: number) => Math.round(n * 10000) / 10000
/** T-02 成本默认(基点):滑点 0、佣金万 2.5、印花税千 0.5(与后端/表单默认一致) */
const SLIPPAGE_BPS_DEFAULT = 0
const COMMISSION_BPS_DEFAULT = 2.5
const STAMP_TAX_BPS_DEFAULT = 5
/** 基点归一化(快照比对共用,防 2.5 vs 2.5000001 误判 stale) */
const r2 = (n: number) => Math.round(n * 100) / 100
/** T-10 组合优化默认(与 BacktestConfig 表单默认一致;旧持久化缺字段时兜底) */
const OPT_METHOD_DEFAULT = 'mv'
const OPT_MAX_WEIGHT_DEFAULT = 10
const OPT_TOP_N_DEFAULT = 20
/** 优化方法白名单(快照/恢复共用,防未知值落表) */
const OPT_METHODS: readonly string[] = ['mv', 'risk_parity', 'min_var']

function HistoryCard({ h, onLoad }: { h: any; onLoad: () => void }) {
  const sum = (h.summary ?? {}) as BacktestSummary
  const annual = Number(sum.annual_return ?? NaN)
  return (
    <div
      onClick={onLoad}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onLoad() } }}
      style={{
        padding: 'var(--sr-pad-lg) var(--sr-pad-xl)', borderRadius: 'var(--sr-radius-card)',
        background: 'var(--sr-block-bg)', border: '1px solid var(--sr-border)',
        cursor: 'pointer', overflow: 'hidden',
      }}
    >
      <Group gap={6} wrap="nowrap">
        <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>任务 #{h.id}</Text>
        <StatusTag status={h.status} kind="job" />
      </Group>
      <div style={{ margin: 'var(--sr-pad-xs) 0' }}>
        <Text truncate style={{ width: '100%', fontSize: 'var(--sr-font-xs)', color: 'var(--sr-text-2)' }}>
          <FormulaCode expr={String(h.params?.expression ?? '')} />
        </Text>
      </div>
      {h.status === 'done' ? (
        <Flex wrap="wrap" gap="var(--sr-pad-xl)" style={{ fontSize: 'var(--sr-font-xs)' }}>
          <span>年化 <span style={{ fontWeight: 600, color: !Number.isNaN(annual) ? pctColor(annual) : 'var(--sr-text-1)' }}>{fmtPct(sum.annual_return)}</span></span>
          <span>夏普 <span style={{ fontWeight: 600 }}>{fmtRatio(sum.sharpe, 2)}</span></span>
          <span>回撤 <span style={{ fontWeight: 600 }}>{fmtPct(sum.max_drawdown)}</span></span>
        </Flex>
      ) : h.status === 'failed' ? (
        <Text c="red" truncate style={{ fontSize: 'var(--sr-font-xs)', display: 'block' }}>
          {sum.error ?? h.error ?? '任务执行失败'}
        </Text>
      ) : (
        <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>执行中…</Text>
      )}
      <div style={{ fontSize: 'var(--sr-font-meta)', color: 'var(--sr-text-3)', marginTop: 'var(--sr-pad-xs)' }}>{fmtTime(h.created_at)}</div>
    </div>
  )
}

/** T-10 优化组合结果面板：仅 bt.params.weights 存在时渲染（backtest_opt 任务） */
function OptimizeResultPanel({ bt }: { bt: any }) {
  const params = bt?.params ?? {}
  const opt = params.optimize ?? {}
  const perf = opt.perf ?? {}
  const weights: { code: string; weight: number }[] = Array.isArray(params.weights)
    ? [...params.weights].sort((a, b) => b.weight - a.weight).slice(0, 20)
    : []
  const methodLabel = OPTIMIZE_METHOD_LABEL[String(params.method ?? '')] ?? String(params.method ?? '—')
  return (
    <div className="sr-run-panel" style={{ marginBottom: 'var(--sr-pad-md)' }}>
      <div className="sr-run-panel-title">优化组合</div>
      <Flex wrap="wrap" gap="var(--sr-pad-xl)" style={{ fontSize: 'var(--sr-font-sm)', marginBottom: 'var(--sr-pad-sm)' }}>
        <span>方法 <b>{methodLabel}</b></span>
        <span>权重上限 <b>{fmtPct(params.max_weight)}</b></span>
        <span>选股数 <b>{params.top_n ?? weights.length}</b></span>
      </Flex>
      <Flex wrap="wrap" gap="var(--sr-pad-sm)">
        <MetricStat label="年化收益" value={fmtPct(perf.annual_return)} />
        <MetricStat label="波动率" value={fmtPct(perf.volatility)} />
        <MetricStat label="夏普比率" value={fmtRatio(perf.sharpe, 2)} />
        <MetricStat label="换手率" value={fmtPct(opt.turnover)} />
      </Flex>
      <div style={{ marginTop: 'var(--sr-pad-md)' }}>
        <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)', marginBottom: 'var(--sr-pad-xs)' }}>权重明细（前 {weights.length}）</Text>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-pad-xs)' }}>
          {weights.map((w) => (
            <Flex key={w.code} justify="space-between" align="center">
              <Text style={{ fontSize: 'var(--sr-font-xs)' }}>{w.code}</Text>
              <Text fw={600} style={{ fontSize: 'var(--sr-font-xs)' }}>{fmtPct(w.weight)}</Text>
            </Flex>
          ))}
        </div>
      </div>
    </div>
  )
}

export default function Backtest() {
  const navigate = useNavigate()
  const viewport = useViewport()
  const isMobile = viewport === 'mobile'
  const [searchParams] = useSearchParams()
  const urlDs = (() => {
    const v = Number(searchParams.get('ds'))
    return Number.isFinite(v) && v > 0 ? v : undefined
  })()
  const [factors, setFactors] = useState<any[]>([])
  const datasets = useDatasets() ?? []
  const [btForm, setBtForm] = usePersistentState<BacktestFormState>('bt:state', {
    expression: searchParams.get('expr') ?? EXAMPLES[0],
    selectedDs: undefined,
    topPct: 20,
    tradeInterval: 5,
    costRate: 0.1,
    slippageBps: SLIPPAGE_BPS_DEFAULT,
    commissionBps: COMMISSION_BPS_DEFAULT,
    stampTaxBps: STAMP_TAX_BPS_DEFAULT,
    horizon: normHorizon(searchParams.get('horizon')),
    modes: ['long_short', 'long', 'short'],
    direction: 'auto',
    optEnabled: false,
    optMethod: OPT_METHOD_DEFAULT,
    optMaxWeight: OPT_MAX_WEIGHT_DEFAULT,
    optTopN: OPT_TOP_N_DEFAULT,
  })
  const { expression, selectedDs, topPct, tradeInterval, costRate, slippageBps, commissionBps, stampTaxBps, horizon, modes, direction,
    optEnabled = false, optMethod = OPT_METHOD_DEFAULT, optMaxWeight = OPT_MAX_WEIGHT_DEFAULT, optTopN = OPT_TOP_N_DEFAULT } = btForm
  // NN 因子来源（BacktestConfig 上报；非空时提交 model_id 而非 expression）
  const [nnModelId, setNnModelId] = useState<number | null>(null)
  // 研究个股上下文（S7 ?code= 单向通道）：只读预填，不参与回写（useResearchUrlSync writeIf:false 防双向干扰）
  const [stockCode, setStockCode] = useState<string | undefined>(() => {
    const c = searchParams.get('code')
    return c && STOCK_CODE_RE.test(c) ? c : undefined
  })
  const setExpression = (v: string) => setBtForm((p) => ({ ...p, expression: v }))
  const setSelectedDs = (v: number | undefined) => setBtForm((p) => ({ ...p, selectedDs: v }))
  const setTopPct = (v: number) => setBtForm((p) => ({ ...p, topPct: v }))
  const setTradeInterval = (v: number) => setBtForm((p) => ({ ...p, tradeInterval: v }))
  const setCostRate = (v: number) => setBtForm((p) => ({ ...p, costRate: v }))
  const setSlippageBps = (v: number) => setBtForm((p) => ({ ...p, slippageBps: v }))
  const setCommissionBps = (v: number) => setBtForm((p) => ({ ...p, commissionBps: v }))
  const setStampTaxBps = (v: number) => setBtForm((p) => ({ ...p, stampTaxBps: v }))
  const setHorizon = (v: number) => setBtForm((p) => ({ ...p, horizon: v }))
  const setModes = (v: BTModeKey[]) => setBtForm((p) => ({ ...p, modes: v }))
  const setDirection = (v: Direction) => setBtForm((p) => ({ ...p, direction: v }))
  const setOptEnabled = (v: boolean) => setBtForm((p) => ({ ...p, optEnabled: v }))
  const setOptMethod = (v: string) => setBtForm((p) => ({ ...p, optMethod: v }))
  const setOptMaxWeight = (v: number) => setBtForm((p) => ({ ...p, optMaxWeight: v }))
  const setOptTopN = (v: number) => setBtForm((p) => ({ ...p, optTopN: v }))
  // 结果与提交时的参数快照绑定：参数变化后旧结果只读展示
  const lastRunSnapshot = useRef<string | null>(null)
  const [history, setHistory] = useState<any[]>([])
  const [histLoading, setHistLoading] = useState(false)
  const histInf = useListInfinite(history.length, HIST_INF_KEY, 8)

  // P2-79:runSnapshot 纯函数(只读 form 与模块常量)→ useCallback 稳定引用,
  // 避免每次渲染重建;submit 内 lastRunSnapshot 写入与 currentSnapshot useMemo 共用同一实现
  const runSnapshot = useCallback((f: BacktestFormState) => {
    return JSON.stringify({
      expression: f.expression.trim(), selectedDs: f.selectedDs,
      topPct: f.topPct, tradeInterval: f.tradeInterval,
      costRate: r4(f.costRate), horizon: f.horizon,
      modes: f.modes, direction: f.direction,
      slippageBps: r2(f.slippageBps), commissionBps: r2(f.commissionBps), stampTaxBps: r2(f.stampTaxBps),
      // T-10 组合优化:旧持久化缺字段按默认兜底(与解构默认一致,防 stale 误判)
      optEnabled: f.optEnabled ?? false,
      optMethod: OPT_METHODS.includes(f.optMethod) ? f.optMethod : OPT_METHOD_DEFAULT,
      optMaxWeight: f.optMaxWeight ?? OPT_MAX_WEIGHT_DEFAULT,
      optTopN: f.optTopN ?? OPT_TOP_N_DEFAULT,
    })
  }, [])

  // 提交 → useJob 共享轮询 → done/failed 收尾（成功/失败消息与快照绑定为页面特有，留在回调）
  const { job, polling, submit, restore } = useJobFlow<JobDetail>({
    submit: async () => {
      // T-10 组合优化:开启时任务类型切 backtest_opt,附加 method/max_weight(小数)/top_n;modes/direction 仍传(后端忽略)
      const r = await createExperiment(optEnabled ? 'backtest_opt' : 'backtest', {
        dataset_id: selectedDs,
        // NN 来源：传 model_id 不传 expression；表达式来源：现状不变
        ...(nnModelId != null ? { model_id: nnModelId } : { expression: expression.trim() }),
        top_pct: topPct / 100, bottom_pct: topPct / 100,
        trade_interval: tradeInterval,
        cost_rate: costRate / 100,
        // T-02 成本拆分(基点)恒传:滑点/佣金(双边)/印花税(卖单边)
        slippage_bps: slippageBps,
        commission_bps: commissionBps,
        stamp_tax_bps: stampTaxBps,
        horizon,
        modes,
        direction,
        ...(optEnabled
          ? { method: optMethod, max_weight: optMaxWeight / 100, top_n: optTopN }
          : {}),
      })
      toast.success(`回测任务 #${r.job_id} 已提交`)
      lastRunSnapshot.current = runSnapshot(btForm)
      return r
    },
    makeOptimistic: (id) => ({ id, status: 'pending', progress: 0, job_type: optEnabled ? 'backtest_opt' : 'backtest' }),
    onFailed: (j) => { toast.error('回测失败: ' + j.error) },
  })

  // 运行参数 → URL 双向同步（回写 replace 防历史污染；writtenRef 跳过自写值防覆盖手动编辑；expr 防抖 500ms 单独回写）
  const { sync } = useResearchUrlSync(
    {
      expr: {
        key: 'expr',
        parse: (raw: string | null) => raw || undefined, // 原 `e &&`：空串/缺失视为无（不重灌）
        serialize: (v: string) => v.trim() || undefined, // 原 expressionRef：空值立即删除、非空防抖回写
        debounceMs: 500,
      },
      ds: {
        key: 'ds',
        parse: (raw: string | null) => {
          const v = Number(raw)
          return Number.isFinite(v) && v > 0 ? v : undefined
        },
        serialize: (v: number | undefined) => (v != null ? String(v) : undefined),
        writeIf: (v) => v != null, // 未选数据集不回写不删除
      },
      horizon: {
        key: 'horizon',
        parse: (raw: string | null) => (raw != null ? normHorizon(raw) : undefined), // 原 has('horizon') && normHorizon
        serialize: (v: number) => String(v),
      },
      job: {
        key: 'job',
        parse: () => undefined, // 只写不读：?job= 恢复走页面 appliedJobRef effect
        serialize: (v: number | undefined) => (v != null ? String(v) : undefined),
        writeIf: (v) => v != null, // 仅在有任务时写 job 参数（避免挂载时删掉 URL ?job= 导致恢复流程被取消）
      },
      code: {
        key: 'code',
        parse: (raw: string | null) => (raw && STOCK_CODE_RE.test(raw) ? raw : undefined), // 个股研究上下文
        serialize: () => undefined, // 只读通道：永不状态→URL 回写；仅页面「移除」主动 sync 删除
        writeIf: () => false, // 防双向干扰：状态变化不写不删 URL，重灌仅在 URL 值变化时触发
      },
    },
    { expr: expression, ds: selectedDs, horizon, job: job?.id, code: stockCode },
    (k, v) => {
      if (k === 'expr') setExpression(v as string)
      else if (k === 'ds') setSelectedDs(v as number)
      else if (k === 'horizon') setHorizon(v as number)
      else if (k === 'code') setStockCode(v as string)
    },
    { carry: ['expr'] }, // 每次回写都携带当前表达式，保证 URL expr 与状态一致
  )

  const bt: any = job?.result?.backtest
  const meta: any = job?.result?.meta
  // L0 结论条数据源：主方式指标 = bt.modes[0].metrics（与 BacktestResults riskMode 同口径），
  // 基准并列 = bt.bench_metrics；bt 为空（未运行/运行中/失败）→ 骨架
  const btMode0 = bt?.modes?.[0]
  const bench = bt?.bench_metrics

  // 数据集来自共享层（useDatasets）：首次到达时解析默认选择（URL ?ds= 优先）
  useDatasetInit(datasets, urlDs, (v) => setSelectedDs(v ?? undefined))

  useEffect(() => {
    getFactors().then((f) => setFactors(f)).catch(() => {})
  }, [])

  useCopilotProvider('/research/backtests', {
    context: () => ({
      dataset: datasets.find((d) => d.id === selectedDs)?.name ?? '',
      stock_count: meta?.stocks,
      date_range: meta?.dates?.length ? `${meta.dates[0]} ~ ${meta.dates[meta.dates.length - 1]}` : '',
      // 研究个股上下文（S7 ?code= 预填）：空串表示未指定，随研究上下文上送
      code: stockCode ?? '',
      features: [],
      operators: [],
      expression: expression.trim(),
      metrics: bt?.modes?.[0]?.metrics
        ? { annual_return: bt.modes[0].metrics.annual_return, sharpe: bt.modes[0].metrics.sharpe, max_drawdown: bt.modes[0].metrics.max_drawdown }
        : {},
    }),
    onFill: (expr) => setExpression(expr),
  })

  // URL ?job= 参数恢复任务（JobBar 跨页跳转；同一 job 只恢复一次，避免 URL 回写触发重灌覆盖手动编辑）
  useJobRestore('backtest', {
    label: '回测任务',
    onDone: (j) => loadJob(j.id),
    onActive: (j) => restore(j, true),
    onRest: (j) => restore(j, false),
  })

  // 历史回测记录（结果入口）：T-128 组合优化回测(backtest_opt)一并展示——两类任务同属回测家族；
  // T-132:合并后按 created_at 降序(后端各自新→旧,但 plain/opt 拼接会跨类型乱序)
  const [histError, setHistError] = useState<string | null>(null)
  const loadHistory = async () => {
    setHistLoading(true)
    setHistError(null)
    try {
      const [plain, opt] = await Promise.all([getExperiments('backtest'), getExperiments('backtest_opt')])
      setHistory([...plain.data, ...opt.data].sort((a, b) => (a.created_at < b.created_at ? 1 : a.created_at > b.created_at ? -1 : 0)))
    } catch (e) {
      setHistError('历史回测加载失败: ' + errMsg(e))
    }
    finally { setHistLoading(false) }
  }
  useEffect(() => { loadHistory() }, [])

  /** 加载历史回测：完整恢复全部参数（表达式/数据集/比例/间隔/成本/周期/方式/方向）+ 快照 */
  const loadJob = async (id: number) => {
    try {
      const j = await getJob(id)
      if (j.status === 'done' && j.result) {
        restore(j, false)
        setExpression(String(j.params?.expression ?? ''))
        const dir = normDirection(String(j.params?.direction ?? ''))
        setDirection(dir)
        const h = normHorizon(j.params?.horizon)
        setHorizon(h)
        const ds = Number(j.params?.dataset_id) || undefined
        if (ds) setSelectedDs(ds)
        const tp = Number(j.params?.top_pct ?? 0) * 100
        if (Number.isFinite(tp)) setTopPct(Math.max(1, Math.min(100, Math.round(tp * 10) / 10)))
        const ti = Number(j.params?.trade_interval)
        if (Number.isFinite(ti) && ti >= 1) setTradeInterval(Math.round(ti))
        const cr = Number(j.params?.cost_rate ?? 0) * 100
        if (Number.isFinite(cr)) setCostRate(Math.round(cr * 1000) / 1000)
        // T-02 成本基点恢复:历史任务无新参 → 落默认值(与表单默认一致,不误判 stale)
        const sl = Number(j.params?.slippage_bps)
        const cb = Number(j.params?.commission_bps)
        const st = Number(j.params?.stamp_tax_bps)
        if (Number.isFinite(sl) && j.params?.slippage_bps != null) setSlippageBps(sl)
        else setSlippageBps(SLIPPAGE_BPS_DEFAULT)
        if (Number.isFinite(cb) && j.params?.commission_bps != null) setCommissionBps(cb)
        else setCommissionBps(COMMISSION_BPS_DEFAULT)
        if (Number.isFinite(st) && j.params?.stamp_tax_bps != null) setStampTaxBps(st)
        else setStampTaxBps(STAMP_TAX_BPS_DEFAULT)
        const m = (j.params?.modes as BTModeKey[] | undefined)
        if (Array.isArray(m) && m.length > 0) setModes(m)
        // T-10 组合优化恢复:历史任务可能为 backtest_opt;method/max_weight/top_n 缺失落默认
        const isOptJob = j.job_type === 'backtest_opt'
        const om = String(j.params?.method ?? '')
        const optMethodOk = OPT_METHODS.includes(om) ? om : OPT_METHOD_DEFAULT
        const omw = Number(j.params?.max_weight)
        const optMaxWeightOk = (Number.isFinite(omw) && omw > 0) ? Math.round(omw * 100) : OPT_MAX_WEIGHT_DEFAULT
        const otn = Number(j.params?.top_n)
        const optTopNOk = (Number.isFinite(otn) && otn >= 5 && otn <= 100) ? Math.round(otn) : OPT_TOP_N_DEFAULT
        setOptEnabled(isOptJob)
        setOptMethod(optMethodOk)
        setOptMaxWeight(optMaxWeightOk)
        setOptTopN(optTopNOk)
        // 快照与表单恢复使用同一归一化，保证恢复后不误判 stale
        lastRunSnapshot.current = JSON.stringify({
          expression: String(j.params?.expression ?? ''),
          selectedDs: ds ?? selectedDs,
          topPct: Math.round(Number(j.params?.top_pct ?? 0) * 100 * 10) / 10,
          tradeInterval: ti >= 1 ? Math.round(ti) : tradeInterval,
          costRate: r4(Number(j.params?.cost_rate ?? 0) * 100),
          horizon: h,
          modes: Array.isArray(m) && m.length ? m : modes,
          direction: dir,
          slippageBps: r2(sl),
          commissionBps: r2(cb),
          stampTaxBps: r2(st),
          optEnabled: isOptJob,
          optMethod: optMethodOk,
          optMaxWeight: optMaxWeightOk,
          optTopN: optTopNOk,
        })
        toast.success(`已加载历史回测 #${id}（参数已完整恢复）`)
      } else {
        toast.warning(`任务 #${id} 状态: ${j.status}`)
      }
    } catch (e) {
      toast.error('加载回测任务失败: ' + errMsg(e))
    }
  }

  // 实时 LaTeX 预览（400ms 防抖 + 竞态守卫）
  const previewLatex = useLatexPreview(expression, async (expr) => (await toLatex(expr)).latex)

  const run = async () => {
    if (nnModelId == null && !expression.trim()) { toast.warning('请输入因子表达式'); return }
    if (!selectedDs) { toast.warning('请选择数据集'); return }
    try {
      // 单次提交：createExperiment 已创建任务并返回 job_id；提交/轮询/收尾统一走 useJobFlow
      await submit()
    } catch (e: any) {
      toast.error('提交回测失败: ' + (e?.message ?? e))
    }
  }

  // 至少保留一种使用方式（防空 modes 崩溃：后端空 modes 无结果可展示）
  const setModesSafe = (v: BTModeKey[]) => {
    if (v.length === 0) { toast.warning('至少保留一种使用方式'); return }
    setModes(v)
  }

  const currentSnapshot = useMemo(() => runSnapshot(btForm), [btForm, runSnapshot])
  const stale = lastRunSnapshot.current !== null && lastRunSnapshot.current !== currentSnapshot

  return (
    <PageShell>
      <ResearchFlowBar
        current="backtests"
        dataset={datasets.find((d) => d.id === selectedDs) ?? null}
        expression={expression}
        job={job}
        onJob={() => navigate(job?.id ? jobResultPath(job.job_type, job.id, job.params) : '/tasks')}
        stepsHidden
      />
      {/* L0 结论条（05 §5.5 回测）：年化收益 · 夏普 · 最大回撤，vs 基准并列 sub；无结果 → 骨架 */}
      <StatStrip
        loading={bt == null}
        scrollable={isMobile}
        style={{ marginBottom: 'var(--sr-gap-row)' }}
        items={[
          {
            key: 'annual',
            label: '年化收益',
            value: btMode0 ? fmtPct(btMode0.metrics.annual_return) : null,
            tone: Number(btMode0?.metrics.annual_return ?? 0) > 0 ? 'up' : 'down',
            sub: bench ? `基准 ${fmtPct(bench.annual_return)}` : undefined,
          },
          {
            key: 'sharpe',
            label: '夏普',
            value: btMode0 ? fmtRatio(btMode0.metrics.sharpe, 2) : null,
            sub: bench ? `基准 ${fmtRatio(bench.sharpe, 2)}` : undefined,
          },
          {
            key: 'drawdown',
            label: '最大回撤',
            value: btMode0 ? fmtPct(btMode0.metrics.max_drawdown) : null,
            sub: bench ? `基准 ${fmtPct(bench.max_drawdown)}` : undefined,
          },
        ]}
      />
      <ResearchSplit
        config={
          <>
            {stockCode && (
              <div className="sr-run-panel" role="status" aria-label="研究个股">
                <div className="sr-run-panel-title">
                  研究个股
                  <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 'var(--sr-pad-sm)' }}>
                    <Text component="code" fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>{stockCode}</Text>
                    <Button
                      size="xs"
                      variant="subtle"
                      aria-label="移除研究个股"
                      onClick={() => {
                        setStockCode(undefined)
                        sync({ code: undefined })
                      }}
                    >
                      移除
                    </Button>
                  </span>
                </div>
              </div>
            )}
            <BacktestConfig
              expression={expression}
              onExpression={setExpression}
              selectedDs={selectedDs}
              onDataset={setSelectedDs}
              topPct={topPct}
              onTopPct={setTopPct}
              tradeInterval={tradeInterval}
              onTradeInterval={setTradeInterval}
              costRate={costRate}
              onCostRate={setCostRate}
              slippageBps={slippageBps}
              onSlippageBps={setSlippageBps}
              commissionBps={commissionBps}
              onCommissionBps={setCommissionBps}
              stampTaxBps={stampTaxBps}
              onStampTaxBps={setStampTaxBps}
              horizon={horizon}
              onHorizon={setHorizon}
              modes={modes}
              onModes={setModesSafe}
              direction={direction}
              onDirection={setDirection}
              optEnabled={optEnabled}
              onOptEnabled={setOptEnabled}
              optMethod={optMethod}
              onOptMethod={setOptMethod}
              optMaxWeight={optMaxWeight}
              onOptMaxWeight={setOptMaxWeight}
              optTopN={optTopN}
              onOptTopN={setOptTopN}
              datasets={datasets}
              factors={factors}
              examples={EXAMPLES}
              nnModelId={nnModelId}
              onNnModelId={setNnModelId}
              running={polling}
              onRun={() => {
                if (isMobile) { toast.info('运行回测请在桌面端操作'); return }
                run()
              }}
              previewLatex={previewLatex}
              job={job}
            />
            {/* 历史回测记录（结果入口） */}
            <div className="sr-run-panel">
              <div className="sr-run-panel-title">
                历史回测记录
                <span style={{ marginLeft: 'auto', fontWeight: 400, color: 'var(--sr-text-3)' }}>{history.length}</span>
                <InfiniteScrollToggle checked={histInf.on} onChange={histInf.setOn} storageKey={HIST_INF_KEY} />
              </div>
              {histLoading && history.length === 0 ? (
                <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>加载中…</Text>
              ) : histError && history.length === 0 ? (
                <EmptyState text={histError} onRetry={() => void loadHistory()} />
              ) : history.length === 0 ? (
                <EmptyState description="暂无回测记录" />
              ) : (
                <>
                  <div className="sr-history-list">
                    {(histInf.on ? history.slice(0, histInf.shown) : history).map((h) => (
                      <HistoryCard key={h.id} h={h} onLoad={() => loadJob(h.id)} />
                    ))}
                  </div>
                  <ListLoadMeta on={histInf.on} shown={histInf.shown} total={histInf.total} />
                  <ListInfiniteSentinel on={histInf.on} hasMore={histInf.hasMore} loadMore={histInf.loadMore} />
                </>
              )}
            </div>
          </>
        }
      >
        {bt ? (
          <>
            {Array.isArray(bt?.params?.weights) && bt.params.weights.length > 0 && (
              <OptimizeResultPanel bt={bt} />
            )}
            <BacktestResults
              bt={bt}
              meta={meta}
              stale={stale}
              onRerun={() => {
                if (isMobile) { toast.info('重新运行请在桌面端操作'); return }
                run()
              }}
            />
          </>
        ) : (
          <div className="sr-run-panel">
            <JobResultPlaceholder job={job} failedText="回测失败" emptyText="暂无回测结果" />
          </div>
        )}
      </ResearchSplit>
    </PageShell>
  )
}
