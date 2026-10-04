import { Button } from '@mantine/core'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  advanceVersion, createExperiment, createFactor, getFactors, getPublishSteps, toLatex,
} from '../../api/client'
import { useDatasets } from '../../data/jobs'
import { useJobFlow } from '../../hooks/useJobFlow'
import { useJobRestore } from '../../hooks/useJobRestore'
import { useDatasetInit } from '../../hooks/useDatasetInit'
import { useResearchUrlSync } from '../../hooks/useResearchUrlSync'
import { jobResultPath } from '../../utils/jobs'
import { useLatexPreview } from '../../hooks/useLatexPreview'
import { useCopilotProvider } from '../../hooks/useCopilotProvider'
import type { EvalResult, FactorInfo, FactorVersion, JobDetail, JobResultView } from '../../api/client'
import { errMsg, fmtRatio, fmtStab } from '../../utils/format'
import PageShell from '../../components/ui/PageShell'
import ResearchSplit from '../../components/ui/ResearchSplit'
import { StatStrip, TaskProgress } from '../../components/ui'
import { useViewport } from '../../app/useViewport'
import ResearchFlowBar from './shared/ResearchFlowBar'
import JobResultPlaceholder from './shared/JobResultPlaceholder'
import type { PublishStepItem } from './evaluation/EvaluationResults'
import EvaluationConfig from './evaluation/EvaluationConfig'
import EvaluationResults from './evaluation/EvaluationResults'
import TuneModal from './evaluation/TuneModal'
import { toast } from './shared/toast'
import './validation/run-layout.css'

const EXAMPLES = [
  'rank(ts_mean(close,5) - ts_mean(close,20))',
  'rank(-ts_std(close,20) / ts_mean(close,20))',
  'ts_corr(close, ts_delay(close,1), 10)',
  'rank(-abs(delta(volume,3)))',
  'rank(ts_mean(volume,5) / ts_mean(volume,20))',
]

export default function FactorEvaluation() {
  const navigate = useNavigate()
  const viewport = useViewport()
  const isMobile = viewport === 'mobile'
  const [searchParams] = useSearchParams()
  const urlDs = (() => {
    const v = Number(searchParams.get('ds'))
    return Number.isFinite(v) && v > 0 ? v : undefined
  })()
  const [factors, setFactors] = useState<FactorInfo[]>([])
  const datasets = useDatasets() ?? []
  const [selectedDs, setSelectedDs] = useState<number | undefined>()
  const [expression, setExpression] = useState(() => searchParams.get('expr') ?? EXAMPLES[0])
  const [horizon, setHorizon] = useState(() => {
    const v = Number(searchParams.get('horizon'))
    return Number.isFinite(v) && v >= 1 && v <= 20 ? v : 5
  })
  // T-03 Walk-forward 滚动验证开关(默认关)与窗数(默认 3,范围 2-5)
  const [wfEnabled, setWfEnabled] = useState(false)
  const [wfWindows, setWfWindows] = useState(3)
  // 结果与提交时的 expression/dataset/horizon 快照绑定：参数变化后旧结果只读展示、禁止发布
  const lastRunSnapshot = useRef<string | null>(null)
  const runSnapshot = (expr: string, ds: number | undefined, h: number, wfOn: boolean, wfN: number) =>
    JSON.stringify({ expression: expr.trim(), selectedDs: ds, horizon: h, wfEnabled: wfOn, wfWindows: wfN })
  // 参数调优 Modal 开关 + 任务状态（TuneModal 经 onJobChange 上报，供关闭后横幅使用）
  const [tuneOpen, setTuneOpen] = useState(false)
  const [tuneJob, setTuneJob] = useState<any>(null)
  const onJobChange = useCallback((job: any) => {
    setTuneJob(job)
  }, [])
  const [pubSteps, setPubSteps] = useState<PublishStepItem[]>([])
  const [publishing, setPublishing] = useState(false)
  const [advancing, setAdvancing] = useState(false)
  const [pubError, setPubError] = useState<string | null>(null)

  // 提交 → useJob 共享轮询 → done/failed 收尾（成功/失败消息与快照绑定为页面特有，留在回调）
  const { job, polling, submit, restore } = useJobFlow<JobDetail>({
    submit: async () => {
      // T-03:Walk-forward 开关打开 → 提交 walk_forward 任务类型(滚动验证 OOS IC)
      const jobType = wfEnabled ? 'walk_forward' : 'evaluate'
      const r = await createExperiment(jobType, {
        dataset_id: selectedDs,
        expression: expression.trim(),
        horizon,
        ...(wfEnabled ? { n_windows: wfWindows } : {}),
      })
      toast.success(`${wfEnabled ? 'Walk-forward 验证' : '评估'}任务 #${r.job_id} 已提交`)
      lastRunSnapshot.current = runSnapshot(expression, selectedDs, horizon, wfEnabled, wfWindows)
      return r
    },
    makeOptimistic: (id) => ({ id, status: 'pending', progress: 0, job_type: wfEnabled ? 'walk_forward' : 'evaluate' }),
    onFailed: (j) => { toast.error('评估失败: ' + j.error) },
  })

  // 运行参数 → URL 双向同步（回写 replace 防历史污染；writtenRef 跳过自写值防覆盖手动编辑；expr 防抖 500ms 单独回写）
  useResearchUrlSync(
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
        parse: (raw: string | null) => (raw != null ? Number(raw) : undefined),
        serialize: (v: number) => String(v),
        refill: false, // 原页面不回灌 horizon（初始 state 已从 URL 读取）
      },
      job: {
        key: 'job',
        parse: () => undefined, // 只写不读：?job= 恢复走页面 appliedJobRef effect
        serialize: (v: number | undefined) => (v != null ? String(v) : undefined),
        writeIf: (v) => v != null, // 仅在有任务时写 job 参数（避免挂载时删掉 URL ?job= 导致恢复流程被取消）
      },
    },
    { expr: expression, ds: selectedDs, horizon, job: job?.id },
    (k, v) => {
      if (k === 'expr') setExpression(v as string)
      else if (k === 'ds') setSelectedDs(v as number)
    },
    { carry: ['expr'] }, // 每次回写都携带当前表达式，保证 URL expr 与状态一致
  )

  useEffect(() => {
    Promise.all([getFactors(), getPublishSteps()])
      .then(([f, s]) => {
        setFactors(f)
        setPubSteps(s as PublishStepItem[])
      })
      .catch((e) => { toast.error('加载因子库/发布步骤失败: ' + errMsg(e)) })
  }, [])

  // 数据集来自共享层（useDatasets）：首次到达时解析默认选择（URL ?ds= 优先）
  useDatasetInit(datasets, urlDs, (v) => setSelectedDs(v ?? undefined))

  useCopilotProvider('/research/evaluation', {
    context: () => ({
      dataset: datasets.find((d) => d.id === selectedDs)?.name ?? '',
      stock_count: meta?.stocks,
      // 评估页无真实时间范围数据（meta 仅 stocks/days/split_date）→ 省略键，
      // 后端 build_research_context 对缺失字段不再输出占位空值
      features: ['open', 'high', 'low', 'close', 'volume', 'amount'],
      operators: [],
      expression: expression.trim(),
      metrics: res ? {
        ic: res.ic, rank_ic: res.rank_ic, long_short_annual: res.long_short_annual,
        turnover: res.turnover, stability: res.stability,
        oos_ic: oos?.ic, oos_stability: oos?.stability,
      } : {},
    }),
    onFill: (expr) => setExpression(expr),
  })

  // URL ?job= 参数恢复任务（JobBar 跨页跳转；同一 job 只恢复一次，避免 URL 回写触发重灌覆盖手动编辑）。
  // 恢复任务同时回填表单参数并绑定快照（与当前表单参数不一致时禁止发布）
  const restoreParams = (j: JobDetail, startPolling: boolean) => {
    restore(j, startPolling)
    if (j.params?.expression) setExpression(String(j.params.expression))
    const h = Number(j.params?.horizon)
    if (Number.isFinite(h) && h >= 1 && h <= 20) setHorizon(h)
    const ds = Number(j.params?.dataset_id)
    if (Number.isFinite(ds) && ds > 0) setSelectedDs(ds)
    lastRunSnapshot.current = runSnapshot(
      String(j.params?.expression ?? ''),
      Number(j.params?.dataset_id) || undefined,
      Number(j.params?.horizon) || 5,
      false, // 历史 evaluate 任务无 walk-forward
      3,
    )
  }
  useJobRestore('evaluate', {
    label: '评估任务',
    onDone: (j) => restoreParams(j, false),
    onActive: (j) => restoreParams(j, true),
    onRest: (j) => restoreParams(j, false),
  })

  // 实时 LaTeX 预览（400ms 防抖 + 竞态守卫）
  const previewLatex = useLatexPreview(expression, async (expr) => (await toLatex(expr)).latex)

  const run = async () => {
    if (!expression.trim()) { toast.warning('请输入因子表达式'); return }
    if (!selectedDs) { toast.warning('请选择数据集'); return }
    try {
      // 单次提交：createExperiment 已创建任务并返回 job_id；提交/轮询/收尾统一走 useJobFlow
      await submit()
    } catch (e: any) {
      toast.error('提交评估失败: ' + (e?.message ?? e))
    }
  }

  const openTune = () => {
    if (!expression.trim()) { toast.warning('请输入因子表达式'); return }
    setTuneOpen(true)
  }

  const res: EvalResult | undefined = job?.result?.result
  // 宽视图 JobResultView 未声明 oos 载荷形状（evaluate 载荷见 EvaluatePayload）：页面局部断言
  const oos: EvalResult | undefined = (job?.result?.oos ?? undefined) as EvalResult | undefined
  // walk_forward 载荷不在宽视图 JobResultView 中（见 WalkForwardPayload）：页面局部断言
  const wf: any = (job?.result as (JobResultView & { walk_forward?: unknown }) | null | undefined)?.walk_forward
  const meta = job?.result?.meta as { stocks?: number; days?: number; split_date?: string | null } | undefined

  // L0 结论条(05 §5.3):IC · RankIC · OOS IC;结果存在时正负着色,无结果骨架。
  // OOS IC 优先取 oos 结果,walk_forward 模式无 oos 时取 wf.summary.mean_ic(拼接全窗均值)
  const hasEvalResult = !!(res || oos || wf)
  const evalTone = (v: number | null | undefined): 'up' | 'down' | 'plain' =>
    v == null ? 'plain' : v > 0 ? 'up' : v < 0 ? 'down' : 'plain'
  const oosIc = oos?.ic ?? wf?.summary?.mean_ic ?? null
  // 结果绑定提交时快照（expression/dataset/horizon/wf 开关）；任一参数变化即视为过期
  const stale = lastRunSnapshot.current !== null &&
    lastRunSnapshot.current !== runSnapshot(expression, selectedDs, horizon, wfEnabled, wfWindows)
  // 调优任务运行态：Modal 关闭后工作台内嵌「调优任务 #N」入口（共享任务池轮询）
  const tuneActive = !!tuneJob && (tuneJob.status === 'pending' || tuneJob.status === 'running')

  const matchedFactor: FactorInfo | undefined = factors.find((f) => f.expression === expression.trim())

  const stepName = (key: string) => pubSteps.find((s) => s.key === key)?.name ?? key

  const createAndPublish = async () => {
    if (isMobile) { toast.info('发布请在桌面端操作'); return }
    // 参数已变更：禁止用旧参数的结果发布（需重新运行评估）
    if (stale) { toast.warning('参数已变更，当前结果基于旧参数，请重新运行评估后再发布'); return }
    if (!res) return
    setPublishing(true)
    setPubError(null)
    try {
      const r = await createFactor({
        name: `评估因子 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`,
        expression: expression.trim(),
        description: `来自因子评估 · IC=${fmtRatio(res.ic)} · 稳定性=${fmtStab(res.stability)}`,
        dataset_id: selectedDs,
        metrics: {
          train_ic: res.ic,
          train_rank_ic: res.rank_ic,
          oos_ic: oos?.ic,
          oos_rank_ic: oos?.rank_ic,
          stability: oos?.stability ?? res.stability,
          turnover: oos?.turnover ?? res.turnover,
          complexity: res.complexity,
          return_annual: oos?.long_short_annual ?? res.long_short_annual,
        },
      })
      toast.success(`因子「${r?.factor?.name ?? ''}」已创建为草稿，按序完成三步检查后自动发布`)
      setFactors(await getFactors())
    } catch (e: any) {
      const detail = e?.response?.data?.detail ?? e?.message ?? String(e)
      setPubError(detail)
      if (String(detail).includes('已存在')) {
        toast.warning('该表达式已在因子库中存在，可直接推进发布流程')
        setFactors(await getFactors().catch(() => []))
      } else {
        toast.error('创建因子失败: ' + detail)
      }
    } finally {
      setPublishing(false)
    }
  }

  const advanceStep = async (step: string) => {
    const matchedVersion: FactorVersion | undefined = matchedFactor
      ? [...(matchedFactor.versions ?? [])].sort((a, b) => b.version - a.version)[0]
      : undefined
    if (!matchedVersion) return
    setAdvancing(true)
    setPubError(null)
    try {
      const updated = await advanceVersion(matchedVersion.id, step)
      toast.success(`步骤「${stepName(step)}」完成 → ${updated.status === 'published' ? '已发布' : updated.status}`)
      setFactors(await getFactors())
    } catch (e: any) {
      const detail = e?.response?.data?.detail ?? e?.message ?? String(e)
      setPubError(detail)
      toast.error('步骤推进失败: ' + detail)
    } finally {
      setAdvancing(false)
    }
  }

  return (
    <PageShell>
      <ResearchFlowBar
        current="evaluation"
        dataset={datasets.find((d) => d.id === selectedDs) ?? null}
        expression={expression}
        job={job}
        onJob={() => navigate(job?.id ? jobResultPath(job.job_type, job.id, job.params) : '/tasks')}
        stepsHidden
      />
      {/* L0 结论条(05 §5.3):IC · RankIC · OOS IC;结果未到数字位骨架 */}
      <StatStrip
        loading={!hasEvalResult}
        scrollable={isMobile}
        style={{ marginBottom: 'var(--sr-gap-row)', flexShrink: 0 }}
        items={[
          { key: 'ic', label: 'IC', value: res?.ic != null ? fmtRatio(res.ic) : '—', tone: evalTone(res?.ic) },
          { key: 'rank-ic', label: 'RankIC', value: res?.rank_ic != null ? fmtRatio(res.rank_ic) : '—', tone: evalTone(res?.rank_ic) },
          { key: 'oos-ic', label: 'OOS IC', value: oosIc != null ? fmtRatio(oosIc) : '—', tone: evalTone(oosIc) },
        ]}
      />
          {tuneActive && !tuneOpen && (
        <div className="sr-run-panel" style={{ marginBottom: 'var(--sr-pad-lg)' }}>
          <TaskProgress
            job={tuneJob}
            typeLabel={{ factor_tune: '参数调优' }}
            extra={<Button size="xs" variant="filled" onClick={() => setTuneOpen(true)}>查看进度</Button>}
          />
        </div>
      )}
      <ResearchSplit
        config={
          <EvaluationConfig
            expression={expression}
            onExpression={setExpression}
            selectedDs={selectedDs}
            onDataset={setSelectedDs}
            horizon={horizon}
            onHorizon={setHorizon}
            wfEnabled={wfEnabled}
            onWfEnabled={setWfEnabled}
            wfWindows={wfWindows}
            onWfWindows={(v) => setWfWindows(Math.max(2, Math.min(5, v)))}
            datasets={datasets}
            factors={factors}
            examples={EXAMPLES}
            running={polling}
            onRun={() => {
              if (isMobile) { toast.info('运行评估请在桌面端操作'); return }
              run()
            }}
            onTune={() => {
              if (isMobile) { toast.info('参数调优请在桌面端操作'); return }
              openTune()
            }}
            previewLatex={previewLatex}
            resultLatex={(job?.result?.result as (EvalResult & { latex?: string }) | undefined)?.latex}
            job={job}
          />
        }
      >
        {res || wf ? (
          <EvaluationResults
            res={res}
            oos={oos}
            wf={wf}
            meta={meta}
            stale={stale}
            onRerun={() => {
              if (isMobile) { toast.info('重新运行请在桌面端操作'); return }
              run()
            }}
            matchExpr={expression}
            factors={factors}
            pubSteps={pubSteps}
            publishing={publishing}
            advancing={advancing}
            pubError={pubError}
            onCreate={createAndPublish}
            onAdvance={advanceStep}
            onGoLibrary={() => navigate('/research/factors')}
            onGoBacktest={() => navigate(`/research/backtests?expr=${encodeURIComponent(expression.trim())}&ds=${selectedDs ?? ''}&horizon=${horizon}`)}
            readOnly={isMobile}
          />
        ) : (
          <div className="sr-run-panel">
            <JobResultPlaceholder job={job} failedText="评估失败" emptyText="暂无评估结果" />
          </div>
        )}
      </ResearchSplit>

      {/* 参数调优 Modal（自持 useTuneTask 轮询；关闭后任务继续，横幅入口保留） */}
      <TuneModal
        open={tuneOpen}
        onClose={() => setTuneOpen(false)}
        expression={expression}
        datasetAvailable={selectedDs != null}
        datasetId={selectedDs}
        onApplied={(expr) => setExpression(expr)}
        onJobChange={onJobChange}
      />
    </PageShell>
  )
}
