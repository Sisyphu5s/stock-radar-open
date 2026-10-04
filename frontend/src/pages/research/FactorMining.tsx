import { Flex, Text } from '@mantine/core'
import { openConfirmModal } from '@mantine/modals'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  createExperiment, getBackend, getExperiments,
  getJob, getOperators, getUniverses,
} from '../../api/client'
import { useDatasets } from '../../data/jobs'
import type { FactorResult, GpRunMeta, JobDetail } from '../../api/client'
import { errMsg } from '../../utils/format'
import type { SrColumn } from '../../components/ui/tableTypes'
import { useJobFlow } from '../../hooks/useJobFlow'
import { useJobRestore } from '../../hooks/useJobRestore'
import { useDatasetInit } from '../../hooks/useDatasetInit'
import { useResearchUrlSync } from '../../hooks/useResearchUrlSync'
import { useCopilotProvider } from '../../hooks/useCopilotProvider'
import { PageShell } from '../../components/ui'
import ResearchSplit from '../../components/ui/ResearchSplit'
import StatStrip from '../../components/ui/StatStrip'
import { useJobEvents } from '../../hooks/useJobEvents'
import { useViewport } from '../../app/useViewport'
import ConfigReadonly from './discovery/ConfigReadonly'
import FmForm from './discovery/FmForm'
import FmResultsPanel from './discovery/ResultsPanel'
import FmRunCards from './discovery/FmRunCards'
import ResultDetailDrawer, { buildLatexMap, makeResultColumns, stepTitle } from './discovery/ResultDetailDrawer'
import ResearchFlowBar from './shared/ResearchFlowBar'
import { useFmForm } from './discovery/useFmForm'
import { useResultCache, readResultCache } from './discovery/resultCache'
import { toast } from './shared/toast'
import './discovery/discovery.css'

/** GPU 失败回退确认（页面特有，onFailed 调用）：仅当尚未以 cpu 回退时弹出 */
const confirmGpuFallback = (err: string, onRetry: () => void) => {
  toast.error('GPU 计算失败，请确认是否回退: ' + err.slice(0, 120))
  openConfirmModal({
    title: 'GPU 计算失败',
    children: (
      <div>
        是否用 CPU 回退重试？
        <div style={{ marginTop: 'var(--sr-pad-md)' }}>
          <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>{err.slice(0, 300)}</Text>
        </div>
      </div>
    ),
    labels: { confirm: 'CPU 回退重试', cancel: '取消' },
    confirmProps: { color: 'red' },
    onConfirm: onRetry,
  })
}

export default function FactorMining() {
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const viewport = useViewport()
  const isMobile = viewport === 'mobile'
  const urlDs = (() => {
    const v = Number(searchParams.get('ds'))
    return Number.isFinite(v) && v > 0 ? v : undefined
  })()
  const datasets = useDatasets() ?? []
  const [universes, setUniverses] = useState<any[]>([])
  const {
    fmForm, setSelectedDs, setOpSet, setFeatures, setPopSize,
    setGens, setHorizon, setTarget, setAlgorithm, setBackendMode,
  } = useFmForm()
  const { selectedDs, opSet, features, popSize, gens, horizon, target, algorithm, backendMode } = fmForm
  const [opInfo, setOpInfo] = useState<any>(null)
  const [penaltyComplexity, setPenaltyComplexity] = useState(false)
  // GPU 回退上下文与提交参数暂存（页面特有：onFailed 判断回退时机、run 透传 backendOverride）
  const runCtxRef = useRef<{ backendOverride?: 'auto' | 'cpu' | 'gpu' } | null>(null)
  const runOptsRef = useRef<{ backendOverride?: 'auto' | 'cpu' | 'gpu' } | null>(null)
  const [backend, setBackend] = useState('')
  const [selectedExpr, setSelectedExpr] = useState<string | null>(null)
  const [expHistory, setExpHistory] = useState<any[]>([])
  const [opConfig, setOpConfig] = useState<Record<string, Record<string, number[]>>>({})
  const { cacheInfo, setCacheInfo, cachedResults, setCachedResults, saveResultCache, clearResultCache } =
    useResultCache(() => ({ algorithm, target, pop_size: popSize, generations: gens, horizon }))
  const [detailOpen, setDetailOpen] = useState(false)
  const [detailJob, setDetailJob] = useState<any>(null)
  const [detailLatex, setDetailLatex] = useState<Record<string, string>>({})
  const [detailLoading, setDetailLoading] = useState(false)

  // 历史挖掘实验记录：首载 + 任务生命周期（提交成功/完成/失败）失效重拉。
  // 原实现依赖 polling 全量重拉：改为显式失效点，histSeq 防并发响应互相覆盖
  const histSeq = useRef(0)
  const [histError, setHistError] = useState<string | null>(null)
  const loadHistory = async () => {
    const seq = ++histSeq.current
    try {
      const d = await getExperiments('gp_run')
      if (seq === histSeq.current) { setExpHistory(d.data); setHistError(null) }
    } catch (e) {
      if (seq === histSeq.current) setHistError('历史实验加载失败: ' + errMsg(e))
    }
  }

  // 提交 → useJob 共享轮询 → done/failed 收尾；GPU 失败回退确认（Modal）为页面特有，留在 onFailed
  const { job, polling, submit, restore } = useJobFlow<JobDetail>({
    submit: async () => {
      const opts = runOptsRef.current
      const r = await createExperiment('gp_run', {
        dataset_id: selectedDs,
        features,
        op_set: opSet,
        op_config: Object.fromEntries(Object.entries(opConfig).filter(([op]) => opSet.includes(op))),
        pop_size: popSize, generations: gens, horizon, top_n: 8,
        target, algorithm, penalty_complexity: penaltyComplexity,
        backend: opts?.backendOverride ?? backendMode,
      })
      toast.success(`实验任务 #${r.job_id} 已提交`)
      runCtxRef.current = { backendOverride: opts?.backendOverride }
      return r
    },
    makeOptimistic: (id) => ({ id, status: 'pending', progress: 0, result: null, job_type: 'gp_run' }),
    onDone: (j) => {
      saveResultCache(j)
      loadHistory() // 任务完成：历史列表刷新到终态
      toast.success(`实验完成: ${j.result?.results?.length ?? 0} 个因子`)
    },
    onFailed: (j) => {
      loadHistory() // 任务失败：历史列表刷新到失败态
      const err = j.error ?? ''
      if (err.includes('[GPU_FAILED]') && runCtxRef.current?.backendOverride !== 'cpu') {
        confirmGpuFallback(err, () => run({ backendOverride: 'cpu' }))
      } else {
        toast.error('实验失败: ' + err)
      }
    },
  })
  // SSE 实时事件订阅：阶段流水 + 进度 + 事件日志（MonitorPanel 消费；断线自动重连/降级轮询）
  const { events } = useJobEvents(job?.id)

  useEffect(() => {
    const c = readResultCache()
    if (c) setCacheInfo(c)
  }, [])

  const loadBase = async () => {
    try {
      const [uni, ops, bk] = await Promise.all([
        getUniverses(), getOperators(), getBackend(),
      ])
      setUniverses(uni)
      setOpInfo(ops)
      setBackend(bk.backend)
      if (!opSet.length) setOpSet(ops.default_set)
      if (!features.length) setFeatures(ops.features)
      if (Object.keys(opConfig).length === 0 && ops?.editable_ops) {
        const cfg: Record<string, Record<string, number[]>> = {}
        for (const [op, params] of Object.entries(ops.editable_ops)) {
          cfg[op] = { ...(params as Record<string, number[]>) }
        }
        setOpConfig(cfg)
      }
    } catch (e: any) {
      toast.error('加载基础数据失败: ' + (e?.message ?? e))
    }
  }

  useEffect(() => { loadBase() }, [])

  // 数据集来自共享层（useDatasets）：首次到达时解析默认选择（URL ?ds= 或持久化偏好优先）
  useDatasetInit(datasets, urlDs ?? selectedDs, (v) => setSelectedDs(v ?? undefined))

  // URL ?ds= 参数重灌（同路由换参重选数据集）。本页不回写 URL（writeIf 恒 false），
  // 且原实现无 writtenRef 自写跳过（skipSelfWritten:false）——URL 值不同即重灌，与原行为一致
  useResearchUrlSync(
    {
      ds: {
        key: 'ds',
        parse: (raw: string | null) => {
          const v = Number(raw)
          return Number.isFinite(v) && v > 0 ? v : undefined
        },
        serialize: (v: number | undefined) => (v != null ? String(v) : undefined),
        writeIf: () => false,
        skipSelfWritten: false,
      },
    },
    { ds: selectedDs },
    (_, v) => setSelectedDs(v as number),
  )

  // URL ?job= 参数恢复任务（Tasks 页跨页跳转；job_type 校验：Workbench 六个 Tab 共享 URL，
  // 防止评估/回测任务串入本页；同一 job 只恢复一次，URL 移除后重置水位允许再次恢复）
  useJobRestore('gp_run', {
    label: '符号回归任务',
    onDone: (j) => loadExp(j.id),
    onActive: (j) => toast.info(`任务 #${j.id} 状态: ${j.status}，完成前可到任务管理查看详情`),
    onRest: (j) => toast.warning(`任务 #${j.id} 状态: ${j.status}`),
  })

  // 历史挖掘实验记录：首载一次（提交成功/完成/失败由 loadHistory 显式失效重拉）
  useEffect(() => { loadHistory() }, [])

  const run = async (opts?: { backendOverride?: 'auto' | 'cpu' | 'gpu' }) => {
    if (!selectedDs) { toast.warning('请先选择数据集'); return }
    clearResultCache()
    runOptsRef.current = opts ?? null
    try {
      // 单次提交：createExperiment 已创建任务并返回 job_id；提交/轮询/收尾统一走 useJobFlow
      await submit()
      loadHistory() // 提交成功：新任务立即出现在历史列表
    } catch (e: any) {
      toast.error('提交实验失败: ' + (e?.message ?? e))
    }
  }

  useCopilotProvider('/research/discovery', {
    context: () => ({
      dataset: datasets.find((d) => d.id === selectedDs)?.name ?? '',
      stock_count: meta.stocks,
      date_range: meta.dates ? `${meta.dates.train} ~ ${meta.dates.oos_end}` : '',
      features,
      operators: opSet,
      expression: selectedExpr ?? '',
      metrics: job?.result?.results?.find((r: FactorResult) => r.expression === selectedExpr) ?? {},
    }),
    onFill: (expr) => setSelectedExpr(expr),
  })

  const loadExp = async (id: number) => {
    try {
      const j = await getJob(id)
      if (j.status === 'done' && j.result) {
        restore(j, false); toast.success(`已加载历史实验 #${id}`)
      } else toast.warning(`任务 #${id} 状态: ${j.status}`)
    } catch (e) {
      toast.error('加载实验任务失败: ' + errMsg(e))
    }
  }

  // 详情加载：detailSeq 守卫，慢响应不覆盖后发起的新请求（参照 PaperTrading loadDetail 模式）
  const detailSeq = useRef(0)
  const openDetail = async (id: number) => {
    const seq = ++detailSeq.current
    setDetailOpen(true)
    setDetailLoading(true)
    setDetailJob(null)
    setDetailLatex({})
    try {
      const j = await getJob(id)
      if (seq !== detailSeq.current) return
      setDetailJob(j)
      const exprs = (j?.result?.results ?? []).map((r: FactorResult) => r.expression)
      if (exprs.length) {
        const m = await buildLatexMap(exprs)
        if (seq !== detailSeq.current) return
        setDetailLatex(m)
      }
    } catch (e: any) {
      if (seq !== detailSeq.current) return
      toast.error('加载任务详情失败: ' + (e?.message ?? e))
    } finally {
      if (seq === detailSeq.current) setDetailLoading(false)
    }
  }

  const [latexMap, setLatexMap] = useState<Record<string, string>>({})

  const meta = (job?.result?.meta ?? {}) as GpRunMeta

  // 拉取结果表达式的 LaTeX
  useEffect(() => {
    if (!job?.result?.results?.length) return
    const exprs = job.result.results.map((r: FactorResult) => r.expression)
    buildLatexMap(exprs).then(setLatexMap).catch(() => { /* 保持文本回退 */ })
  }, [job])

  const results: FactorResult[] = job?.result?.results ?? cachedResults ?? []

  // 当前数据集作为跨页跳转参数（URL ?ds= 优先于 sr-last-dataset 兜底）
  const dsQs = selectedDs != null ? `&ds=${selectedDs}` : ''

  // columns 引用稳定化：DataTable 已 memo，避免无关 setState（opConfig/轮询等）触发全表重渲染
  const columns = useMemo<SrColumn<FactorResult>[]>(
    () => makeResultColumns({ latex: latexMap, withSelect: true, selectedExpr, onSelectExpr: setSelectedExpr, dsQs, navigate }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [latexMap, selectedExpr, selectedDs],
  )

  const jobStatus = job?.status

  const ctxDataset = datasets.find((d) => d.id === selectedDs) ?? null
  // L0 结论条(05 §5.3):数据集维度信息——股票数=成分数;总天数=面板总行数/成分数(每只股票行数一致,取整)
  const ctxStocks = ctxDataset?.stock_count as number | undefined
  const ctxDays = (() => {
    const sc = ctxDataset?.stock_count as number | undefined
    const rc = ctxDataset?.row_count as number | undefined
    return sc && rc ? Math.round(rc / sc) : undefined
  })()
  const goJob = () => { if (job?.id) navigate(`/research/runs/${job.id}`); else navigate('/tasks') }

  const cfgSnapshot = {
    datasetName: ctxDataset?.name,
    features, opSet, algorithm, backendMode, backend,
    popSize, gens, horizon, target, penaltyComplexity,
  }

  const runCards = (
    <FmRunCards
      datasets={datasets}
      universes={universes}
      selectedDs={selectedDs}
      onDataset={setSelectedDs}
      stepTitle={stepTitle}
      polling={polling}
      onRun={() => run()}
      job={job}
      jobStatus={jobStatus}
      events={events}
    />
  )

  return (
    <PageShell>
      <ResearchFlowBar
        current="discovery"
        discoveryView="gp"
        dataset={ctxDataset}
        expression={selectedExpr}
        job={job}
        onJob={goJob}
        stepsHidden
      />

      {/* L0 结论条(05 §5.3):当前数据集维度——股票数/总天数;数据集未到或未选中时数字位骨架 */}
      <StatStrip
        loading={!datasets || !ctxDataset}
        scrollable={isMobile}
        style={{ marginBottom: 'var(--sr-gap-row)', flexShrink: 0 }}
        items={[
          { key: 'dataset', label: '当前数据集', value: ctxDataset?.name ?? '—' },
          { key: 'stocks', label: '股票数', value: ctxStocks ?? '—' },
          { key: 'days', label: '总天数', value: ctxDays ?? '—' },
        ]}
      />

      {isMobile && <ConfigReadonly cfg={cfgSnapshot} />}

      {/* 分栏/堆叠由 ResearchSplit 容器实测宽驱动(M3):桌面双栏(配置 sticky 自滚)/窄容器纵向堆叠 */}
      <ResearchSplit
        config={
          !isMobile && (
            <div className="sr-disc-config-col">
              <Flex direction="column" gap={12}>
                {runCards}
                <FmForm
                  features={features}
                  onFeatures={setFeatures}
                  opSet={opSet}
                  onOpSet={setOpSet}
                  opConfig={opConfig}
                  onOpConfig={setOpConfig}
                  opInfo={opInfo}
                  algorithm={algorithm}
                  onAlgorithm={setAlgorithm}
                  target={target}
                  onTarget={setTarget}
                  backendMode={backendMode}
                  onBackendMode={setBackendMode}
                  popSize={popSize}
                  onPopSize={setPopSize}
                  gens={gens}
                  onGens={setGens}
                  horizon={horizon}
                  onHorizon={setHorizon}
                  penaltyComplexity={penaltyComplexity}
                  onPenaltyComplexity={setPenaltyComplexity}
                  backend={backend}
                  stepTitle={stepTitle}
                />
              </Flex>
            </div>
          )
        }
      >
        <Flex direction="column" gap={12}>
          {isMobile && runCards}
          <FmResultsPanel
            results={results}
            columns={columns}
            selectedExpr={selectedExpr}
            selectedDs={selectedDs}
            job={job}
            meta={meta}
            cacheInfo={cacheInfo}
            onLoadCache={() => setCachedResults(cacheInfo?.results ?? null)}
            onClearCache={clearResultCache}
            onSelectExpr={setSelectedExpr}
            history={expHistory}
            histError={histError}
            onReloadHistory={() => void loadHistory()}
            onOpenDetail={(h) => openDetail(h.id)}
            onOpenEvolve={(h) => navigate(`/research/runs/${h.id}`)}
          />
        </Flex>
      </ResearchSplit>

      <ResultDetailDrawer
        open={detailOpen}
        onClose={() => setDetailOpen(false)}
        job={detailJob}
        loading={detailLoading}
        latexMap={detailLatex}
        selectedDs={selectedDs}
        onSelectExpr={setSelectedExpr}
        onLoad={(j) => { loadExp(j.id); setDetailOpen(false) }}
      />
    </PageShell>
  )
}
