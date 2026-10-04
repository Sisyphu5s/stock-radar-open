import { Badge, Button, Flex, Group, Indicator, Modal, Select, Stack, Text, TextInput } from '@mantine/core'
import { openConfirmModal } from '@mantine/modals'
import { notifications } from '@mantine/notifications'
import { IconPencil, IconPlus, IconRefresh, IconTrash } from '@tabler/icons-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  createPaperProject, deletePaperProject, getPaperMeta,
  getSignalCatalog, updatePaperProject,
} from '../../api/client'
import type {
  PaperExperimentResult, PaperMeta, PaperMultiExperimentResult, PaperMultiWatchResult, PaperProject,
  PaperProjectPayload, PaperWatchResult, SignalCatalogItem,
} from '../../api/client'
import { invalidatePaperProject, invalidatePaperProjects, usePaperProject, usePaperProjects } from '../../data/paper'
import { signalLabelMap } from '../../utils/signals'
import { formatFullTime } from '../../utils/time'
import { errMsg } from '../../utils/format'
import { periodLabel } from '../../utils/periods'
import { useStockSearch } from '../../hooks/useStockSearch'
import { useLatestRef } from '../../hooks/useStableSetter'
import PageShell from '../../components/ui/PageShell'
import EmptyState from '../../components/ui/EmptyState'
import SkeletonBlock from '../../components/ui/SkeletonBlock'
import { SegmentToolbar, StatStrip } from '../../components/ui'
import { useViewport } from '../../app/useViewport'
import ParamSummary from './paper/ParamSummary'
import ProjectWizard, { type ProjectWizardValues } from './paper/ProjectWizard'
import ExperimentResults from './paper/ExperimentResults'
import WatchResults from './paper/WatchResults'
import PortfolioView from './paper/PortfolioView'
import { restoreWatchFromStorage, usePaperWatch, WATCH_STORAGE_KEY } from './paper/usePaperWatch'
import { usePaperRun } from './paper/usePaperRun'
import { PAPER_PERIOD_OPTIONS } from './paper/constants'
import './PaperTrading.css'
import './paper/paper-shared.css'

/** antd message → @mantine/notifications（T-44：色/时长对齐 research/shared/toast.ts 契约） */
const notifySuccess = (m: string) => notifications.show({ message: m, color: 'teal', autoClose: 2500 })
const notifyError = (m: string) => notifications.show({ message: m, color: 'red', autoClose: 4000 })
const notifyWarning = (m: string) => notifications.show({ message: m, color: 'yellow', autoClose: 3200 })

/** 内置常用信号（catalog / paper meta 均不可用时的兜底）：code 与后端 /signals/catalog 对齐 */
const COMMON_SIGNALS: { code: string; name: string }[] = [
  { code: 'macd_golden_cross', name: 'MACD金叉' },
  { code: 'macd_dead_cross', name: 'MACD死叉' },
  { code: 'kdj_golden_cross', name: 'KDJ金叉' },
  { code: 'kdj_dead_cross', name: 'KDJ死叉' },
  { code: 'rsi_cross_up', name: 'RSI上穿' },
  { code: 'rsi_cross_down', name: 'RSI下穿' },
  { code: 'ma_golden_cross', name: '均线金叉' },
  { code: 'boll_breakout', name: '布林突破' },
  { code: 'volume_surge', name: '放量上涨' },
  { code: 'trend_breakout', name: '趋势突破' },
]

/** 项目类型中文标签（列表/下拉共用） */
const KIND_LABEL: Record<PaperProject['kind'], string> = { experiment: '实验', watch: '观察' }

/** 项目列表排序：后端契约按 updated_at desc（ISO 字符串字典序等价时间序） */
function sortProjects(list: PaperProject[]): PaperProject[] {
  return [...list].sort((a, b) => (a.updated_at < b.updated_at ? 1 : a.updated_at > b.updated_at ? -1 : 0))
}

export default function PaperTrading() {
  const viewport = useViewport()
  const isMobile = viewport === 'mobile'
  // 深链：/paper?project={id}（任务中心 paper 任务跳转携带），列表加载完成后选中；
  //       /paper?view=portfolio（旧 /paper/portfolio 重定向落点）直达账户视图；
  //       /paper?new=1（命令面板「新建模拟盘项目」动作落点）自动打开创建向导
  const [searchParams, setSearchParams] = useSearchParams()

  // ---- 双视图（T-46 模拟盘合并）：项目 / 账户，URL ?view= 为单一事实源（可寻址/可深链） ----
  const [view, setView] = useState<'projects' | 'portfolio'>(() =>
    searchParams.get('view') === 'portfolio' ? 'portfolio' : 'projects',
  )
  const changeView = (v: string) => {
    const next: 'projects' | 'portfolio' = v === 'portfolio' ? 'portfolio' : 'projects'
    setView(next)
    setSearchParams((prev) => {
      const sp = new URLSearchParams(prev)
      if (next === 'portfolio') sp.set('view', 'portfolio')
      else sp.delete('view')
      return sp
    })
  }

  const [catalog, setCatalog] = useState<SignalCatalogItem[]>([])

  // ---- 项目列表 ----
  // 视图状态（本地写操作：创建/删除/保存/运行结果回写直写此处）；数据源为池订阅 projectsQ
  // （P1-47 迁池：数据获取走池、视图状态留 useState——池只承担首拉/全局刷新失效重拉，
  //  写操作不 invalidate 池，避免乐观视图被缓存旧值覆盖）
  const [projects, setProjects] = useState<PaperProject[]>([])
  const projectsQ = usePaperProjects()
  // 首载 loading：仅首拉且无数据时显示骨架（invalidate 重拉保留旧列表，不闪骨架）
  const initLoading = projectsQ.loading && !projectsQ.value
  const listError = projectsQ.error ? '项目列表加载失败: ' + errMsg(projectsQ.error) : ''
  const [activeId, setActiveId] = useState<number | null>(null)
  const [mutating, setMutating] = useState(false)
  const [wizardOpen, setWizardOpen] = useState(false)
  const [renaming, setRenaming] = useState<PaperProject | null>(null)
  const [renameName, setRenameName] = useState('')

  // ---- 选中项目详情（列表瘦身契约：列表项 result 恒为 null，完整 result 只来自详情/run/watch） ----
  // 数据源为池订阅 detailQ（staleTime 0：切换项目即拉后端最新，与旧 loadDetail 每次拉取语义一致；
  //  本地写（保存/run 回写/watch 合并）仍直写 activeDetail 视图 state，池 value 仅在重拉后同步）
  const [activeDetail, setActiveDetail] = useState<PaperProject | null>(null)
  const detailQ = usePaperProject(activeId)
  const detailLoading = detailQ.loading && !detailQ.value
  const detailError = !!detailQ.error
  const projectsRef = useLatestRef(projects)

  // ---- 表单状态（选中项目回填；运行/监控前以 PATCH 持久化到项目） ----
  const [code, setCode] = useState('')
  const [stockText, setStockText] = useState('')
  const [period, setPeriod] = useState('daily')
  const [signalCodes, setSignalCodes] = useState<string[]>([])
  const [days, setDays] = useState(250)

  // ---- 摘要条编辑态（提升自 ParamSummary：切项目未保存需确认，防编辑内容静默丢失） ----
  const [editing, setEditing] = useState(false)
  const editingRef = useLatestRef(editing)

  /** 切换项目：编辑态下先确认（丢弃未保存修改）再切换 */
  const switchProject = (id: number | null) => {
    if (id === activeId) return
    if (editingRef.current) {
      openConfirmModal({
        title: '有未保存的修改',
        children: '当前项目参数已修改但尚未保存，切换项目将丢弃这些修改。',
        labels: { confirm: '丢弃并切换', cancel: '留在当前' },
        confirmProps: { color: 'red' },
        onConfirm: () => {
          setEditing(false)
          setActiveId(id)
        },
      })
      return
    }
    setActiveId(id)
  }

  // ---- 股票搜索（AutoComplete，摘要条编辑态使用；防抖 + 竞态守卫统一走 useStockSearch） ----
  const stockSearch = useStockSearch()

  // ---- 实验任务运行态（usePaperRun 自包含，P2-19 拆分；useJobFlow 共享任务池轮询，
  //      切项目/卸载仅停止前端订阅，任务在后台继续） ----

  // ---- 信号目录：优先 catalog（含 code 映射），失败回退内置常用列表 ----
  // P1-47 记账（保留手写 fetch）：catalog/meta 为一次性静态配置（信号/周期白名单、上限），无轮询
  // 无失效联动，迁池收益低；watch 15s 轮询同理保留——其结果经 setActiveDetail 合并进视图 state，
  // 与详情池（staleTime 0 无轮询）职责分离，迁池需重建「轮询合并 + in-flight 守卫」语义，成本高收益低。
  useEffect(() => {
    getSignalCatalog()
      .then((list) => setCatalog(Array.isArray(list) ? list : []))
      .catch(() => setCatalog([]))
  }, [])

  // ---- paper 元信息（GET /paper/meta）：信号白名单 + 周期白名单单一事实源；失败降级内置列表 ----
  const [paperMeta, setPaperMeta] = useState<PaperMeta | null>(null)
  useEffect(() => {
    getPaperMeta()
      .then((m) => setPaperMeta(m))
      .catch(() => setPaperMeta(null)) // 降级：COMMON_SIGNALS / PAPER_PERIOD_OPTIONS，后端白名单校验兜底
  }, [])

  const signalOptions = useMemo(() => {
    if (paperMeta && paperMeta.signals.length > 0) {
      return paperMeta.signals.map((s) => ({
        value: s.key,
        label: `${s.label}（${s.key}）`,
        description: s.description || undefined,
      }))
    }
    return COMMON_SIGNALS.map((c) => ({ value: c.code, label: `${c.name}（${c.code}）` }))
  }, [paperMeta])

  const periodOptions = useMemo(() => {
    if (paperMeta && paperMeta.periods.length > 0) {
      return paperMeta.periods.map((v) => ({ value: v, label: periodLabel(v) }))
    }
    return PAPER_PERIOD_OPTIONS
  }, [paperMeta])

  const labelMap = useMemo(() => signalLabelMap(catalog), [catalog])
  const labelOf = useCallback((c: string) => labelMap[c] ?? { text: c, color: 'default' }, [labelMap])

  const activeProject = useMemo(
    () => projects.find((p) => p.id === activeId) ?? null,
    [projects, activeId],
  )

  /** 用最新项目对象替换列表项并重排（updated_at desc） */
  const applyProject = useCallback((updated: PaperProject) => {
    setProjects((prev) => sortProjects(prev.map((p) => (p.id === updated.id ? updated : p))))
  }, [])

  // ---- 选中项目 → 详情池同步（activeId 为 null 清空；池 value 就绪写视图 state；
  //  value 的 result 为空但本地已有同项目详情（run 刚写入）时保留旧值，避免覆盖最新结果——
  //  与旧 loadDetail 守卫语义一致；拉取失败时用列表项基本信息兜底（result=null），
  //  保证 watch 轮询可合并、表单不阻塞。竞态由池按 key 隔离，无需 detailSeq 守卫） ----
  useEffect(() => {
    if (activeId == null) {
      setActiveDetail(null)
      return
    }
    const v = detailQ.value
    if (v) {
      setActiveDetail((prev) => (prev && prev.id === v.id && prev.result != null && v.result == null ? prev : v))
      return
    }
    if (detailQ.error) {
      setActiveDetail((prev) => {
        if (prev && prev.id === activeId) return prev
        const lp = projectsRef.current.find((x) => x.id === activeId)
        return lp ? { ...lp } : prev
      })
    }
  }, [activeId, detailQ.value, detailQ.error])

  // ---- 深链参数：/paper?project={id}（无参数或非法 → null） ----
  const deepLinkId = useMemo(() => {
    const v = searchParams.get('project')
    const n = v ? Number(v) : NaN
    return Number.isNaN(n) ? null : n
  }, [searchParams])

  // ---- T-130 URL 双向同步：?view= 与 ?project= 可寻址/深链 ----
  // view：URL 变化（浏览器前进后退/外部直达/命令面板）→ 同步视图（不再只读首帧）
  useEffect(() => {
    setView(searchParams.get('view') === 'portfolio' ? 'portfolio' : 'projects')
  }, [searchParams])
  // project：URL ?project= 变化 → 选中对应项目（幂等；不存在时由 settleInitialSelection 兜底）
  useEffect(() => {
    if (deepLinkId == null) return
    setActiveId((cur) => (cur === deepLinkId ? cur : deepLinkId))
  }, [deepLinkId])
  // activeId 变化（用户切换/创建/恢复）→ 回写 ?project=（replace 不污染历史；幂等跳过同值）
  useEffect(() => {
    if (activeId == null) return
    setSearchParams((prev) => {
      if (prev.get('project') === String(activeId)) return prev
      const next = new URLSearchParams(prev)
      next.set('project', String(activeId))
      return next
    }, { replace: true })
  }, [activeId, setSearchParams])

  /** 列表加载完成后的选中策略：深链优先 → 恢复上次监控 → 默认第一个 */
  const settleInitialSelection = useCallback((sorted: PaperProject[]) => {
    if (deepLinkId != null) {
      const target = sorted.find((p) => p.id === deepLinkId)
      setActiveId(target ? target.id : (sorted.length > 0 ? sorted[0].id : null))
      return
    }
    const restoreId = restoreWatchFromStorage(sorted)
    if (restoreId != null) {
      // 刷新后自动恢复监控（表单回填/详情拉取/轮询 effect 随 activeId 与 watching 驱动）
      setWatching(true)
      setActiveId(restoreId)
    } else {
      setActiveId((cur) => cur ?? (sorted.length > 0 ? sorted[0].id : null))
    }
  }, [deepLinkId])

  // ---- 首载/池重拉：列表池 value 就绪 → 排序写入视图 state + 深链/恢复监控/默认第一个选中 ----
  // （旧首载 effect 的手写 fetch + cancelled 守卫已由池订阅替代；池重拉（全局刷新失效）
  //  触发同样走此同步路径，settleInitialSelection 幂等——非深链路径 setActiveId(cur => cur ?? …)
  //  不会覆盖用户已选中项）
  useEffect(() => {
    if (!projectsQ.value) return
    const sorted = sortProjects(projectsQ.value)
    setProjects(sorted)
    settleInitialSelection(sorted)
  }, [projectsQ.value, settleInitialSelection])

  // ---- 深链：/paper?new=1（命令面板动作）→ 自动打开创建向导并落在项目视图 ----
  // T-130:消费后立即删除 new 参数(瞬态意图),且依赖 searchParams——已位于 /paper 时
  // 命令面板再次导航 /paper?new=1 同样触发(原实现仅首载读取,重复动作失效)
  useEffect(() => {
    if (searchParams.get('new') !== '1') return
    setView('projects')
    setWizardOpen(true)
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete('new')
      return next
    }, { replace: true })
  }, [searchParams, setSearchParams])

  // ---- 切换项目 → 回填表单参数 ----
  useEffect(() => {
    const p = projects.find((x) => x.id === activeId)
    if (!p) return
    setCode(p.code)
    setStockText(p.code)
    setPeriod(p.period)
    setSignalCodes(p.signals ?? [])
    setDays(p.days ?? 250)
    // 仅在 activeId 变化时回填，避免覆盖用户正在编辑的表单
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId])

  // ---- 结果派生（当前项目 result，按 kind 区分；列表项 result 恒 null，详情/轮询写入 activeDetail） ----
  const activeDetailOk = activeDetail && activeDetail.id === activeId ? activeDetail : null
  const expResult: PaperExperimentResult | PaperMultiExperimentResult | null =
    activeDetailOk?.kind === 'experiment' && activeDetailOk.result
      ? (activeDetailOk.result as PaperExperimentResult | PaperMultiExperimentResult)
      : null
  const watchData: PaperWatchResult | PaperMultiWatchResult | null =
    activeDetailOk?.kind === 'watch' && activeDetailOk.result
      ? (activeDetailOk.result as PaperWatchResult | PaperMultiWatchResult)
      : null

  // ==================== experiment：run 任务化（usePaperRun 自包含） ====================
  const {
    expLoading, expError, runJob,
    runExperiment, pauseRunJob, resumeRunJob, cancelRunJob, clearRun,
    restoreRunForProject,
  } = usePaperRun({
    activeId,
    activeProject,
    code, period, signalCodes, days,
    applyProject, setActiveDetail,
  })

  // ---- T-130 刷新/重进后恢复运行任务：选中 experiment 项目时按 project_id 查找
  // 活动 paper_experiment 任务并接管轮询（restoreTriedRef 防重复查询/恢复） ----
  const restoreTriedRef = useRef<number | null>(null)
  useEffect(() => {
    if (activeId == null || runJob != null) return
    if (restoreTriedRef.current === activeId) return
    const p = projects.find((x) => x.id === activeId)
    if (!p || p.kind !== 'experiment') return
    restoreTriedRef.current = activeId
    void restoreRunForProject(activeId)
  }, [activeId, projects, runJob, restoreRunForProject])

  /** 摘要条编辑态保存：PATCH 持久化表单参数（experiment 含 days，watch 不含；多股项目不传 code）；
   *  result_invalidated=true 时提示旧结果失效。必填校验已收敛到 ParamSummary 表单（save 前置 validate）。 */
  const saveParams = async (): Promise<boolean> => {
    if (!activeId) return false
    const multi = (activeProject?.stocks?.length ?? 1) > 1
    setSaving(true)
    try {
      const payload: Partial<PaperProjectPayload> = { period, signals: signalCodes }
      if (!multi) payload.code = code
      if (activeProject?.kind === 'experiment') payload.days = days
      const updated = await updatePaperProject(activeId, payload)
      applyProject(updated)
      setActiveDetail(updated)
      if (updated.result_invalidated) {
        notifyWarning('参数已变更，旧结果已失效，请重新运行')
      } else {
        notifySuccess('参数已保存')
      }
      return true
    } catch (e) {
      notifyError('保存失败: ' + errMsg(e))
      return false
    } finally {
      setSaving(false)
    }
  }

  // ==================== watch：15s 只读轮询（usePaperWatch 自包含，P2-19 拆分） ====================
  const [saving, setSaving] = useState(false)
  const {
    watching, setWatching,
    watchLoading, watchError, setWatchError,
    lastUpdatedAt, fetchWatch, startWatch, stopWatch,
  } = usePaperWatch({
    activeId,
    activeProject,
    code, signalCodes, period,
    applyProject, setActiveDetail,
  })

  // ==================== 项目 CRUD ====================
  const handleCreate = async (values: ProjectWizardValues) => {
    setMutating(true)
    try {
      const created = await createPaperProject({
        name: values.name?.trim() || '未命名项目',
        kind: values.kind,
        code: values.code.trim(),
        stocks: values.stocks,
        period: values.period,
        signals: values.signals ?? [],
        days: values.kind === 'experiment' ? (values.days ?? 250) : undefined,
      })
      setProjects((prev) => sortProjects([...prev, created]))
      setActiveId(created.id)
      setWizardOpen(false)
      // 创建后不自动运行/监控：提示用户显式点击「运行实验」/「开始监控」
      if (created.kind === 'experiment') {
        notifySuccess(`已创建「${created.name}」，点击运行开始实验`)
      } else {
        notifySuccess(`已创建「${created.name}」，点击开始监控`)
      }
    } catch (e) {
      notifyError('创建失败: ' + errMsg(e))
    } finally {
      setMutating(false)
    }
  }

  const openRename = (p: PaperProject) => {
    setRenameName(p.name)
    setRenaming(p)
  }

  const handleRename = async () => {
    if (!renaming) return
    const name = renameName.trim()
    if (!name) { notifyWarning('名称不能为空'); return }
    setMutating(true)
    try {
      const updated = await updatePaperProject(renaming.id, { name })
      applyProject(updated)
      if (renaming.id === activeId) setActiveDetail(updated)
      notifySuccess('已重命名')
      setRenaming(null)
    } catch (e) {
      notifyError('重命名失败: ' + errMsg(e))
    } finally {
      setMutating(false)
    }
  }

  const handleDelete = async (p: PaperProject) => {
    setMutating(true)
    try {
      await deletePaperProject(p.id)
      notifySuccess(`已删除「${p.name}」`)
      const rest = projects.filter((x) => x.id !== p.id)
      setProjects(rest)
      // 删除当前项目 → 自动切换到列表第一个（或空态），并清除监控恢复标记；
      // 后端 DELETE 已级联取消该项目未完成任务（活动任务被取消，终态任务历史保留）
      if (activeId === p.id) {
        if (runJob?.projectId === p.id) {
          clearRun()
        }
        setEditing(false) // 项目已删除，编辑态一并丢弃
        // 删除监控中的项目：同步停止监控态（防下一项 watch 被静默自动轮询 / 删空后 interval 空转）
        setWatching(false)
        setWatchError(false)
        localStorage.removeItem(WATCH_STORAGE_KEY)
        setActiveId(rest.length > 0 ? rest[0].id : null)
      }
    } catch (e) {
      notifyError('删除失败: ' + errMsg(e))
    } finally {
      setMutating(false)
    }
  }

  // ==================== 渲染 ====================
  const runJobActive = runJob != null && runJob.projectId === activeId

  // 列表加载失败重试：失效列表池强制重拉（P1-47 迁池；错误态由 projectsQ.error 派生，
  //  invalidate 后重拉成功即自动消失）
  const loadListErrorRetry = () => {
    invalidatePaperProjects()
  }

  const projectStatus = (p: PaperProject): { text: string; watching: boolean; running: boolean } => {
    // 后台运行任务标识：前端跟踪的 runJob 即使切走项目也保留，任务在后台继续（任务中心可见）
    if (runJob != null && runJob.projectId === p.id) {
      return { text: runJob.paused ? '已暂停' : '运行中', watching: false, running: true }
    }
    if (watching && p.id === activeId && p.kind === 'watch') return { text: '监控中', watching: true, running: false }
    return { text: p.has_result || p.result != null ? '已运行' : '未运行', watching: false, running: false }
  }

  // ---- L0 结论条（项目视图，05 §5.4）：运行中项目 N · 当日收益 X% · 监控中 M ----
  // 数字全部派生自现有状态/数据（runJob=运行中任务、watching=监控轮询态），口径与 T-46 一致；
  // 「当日收益」需聚合运行中项目的组合收益口径，当前项目数据模型（回测统计/触发点）无此数据，
  // 按 05 §5.4「无则 —」兜底显示占位，待账户级绩效接入后复用。

  return (
    <PageShell fill>
      <div className="sr-paper-page">
        {/* 双视图切换（T-46 模拟盘合并：项目 / 账户，?view= 可寻址） */}
        <SegmentToolbar
          value={view}
          onChange={changeView}
          options={[{ value: 'projects', label: '项目' }, { value: 'portfolio', label: '账户' }]}
        />
        {view === 'projects' ? (
          <>
            {/* L0 结论条（项目视图，05 §5.4）：运行中项目 N · 当日收益 X% · 监控中 M；initLoading 时数字位骨架不空白 */}
            {!listError && (
              <StatStrip
                loading={initLoading}
                items={[
                  { key: 'running', label: '运行中项目', value: runJob != null ? 1 : 0, sub: `共 ${projects.length} 个项目` },
                  { key: 'day-pnl', label: '当日收益', value: '—', tone: 'plain', sub: '暂无聚合收益数据' },
                  { key: 'watching', label: '监控中', value: watching ? 1 : 0, sub: 'watch 实时监控' },
                ]}
              />
            )}
        {initLoading ? (
          <div className="sr-paper-state">
            <SkeletonBlock rows={4} />
          </div>
        ) : listError ? (
          <EmptyState text={listError} onRetry={loadListErrorRetry} padding="40px 0" />
        ) : (
          <Flex className="sr-paper-layout" gap={16}>
            {/* 左侧：项目列表（窄屏折叠为顶部 Select） */}
            <aside className="sr-paper-proj-panel">
              <Group className="sr-paper-proj-head" justify="space-between" align="center">
                <Text fw={700} style={{ fontSize: 'var(--sr-font-title)' }}>模拟项目</Text>
                <Button size="xs" leftSection={<IconPlus size={14} />} onClick={() => setWizardOpen(true)}>
                  新建
                </Button>
              </Group>
              {projects.length === 0 ? (
                <EmptyState description="暂无项目，点击「新建」创建" padding="24px 0" />
              ) : isMobile ? (
                <Select<number>
                  className="sr-paper-proj-select"
                  placeholder="选择项目"
                  size="xs"
                  value={activeId}
                  data={projects.map((p) => ({
                    value: p.id,
                    label: `${p.name}（${KIND_LABEL[p.kind]}·${projectStatus(p).text}）`,
                  }))}
                  onChange={(id) => { if (id != null) switchProject(id) }}
                />
              ) : (
                <div className="sr-paper-proj-list">
                  {projects.map((p) => {
                    const isActive = p.id === activeId
                    const st = projectStatus(p)
                    return (
                      <div
                        key={p.id}
                        role="button"
                        tabIndex={0}
                        aria-pressed={isActive}
                        className={'sr-paper-proj-item' + (isActive ? ' sr-paper-proj-item-active' : '')}
                        onClick={() => switchProject(p.id)}
                        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') switchProject(p.id) }}
                      >
                        <div className="sr-paper-proj-item-head">
                          <span className="sr-paper-proj-name" title={p.name}>{p.name}</span>
                          <Badge color={p.kind === 'experiment' ? 'blue' : 'grape'} radius="sm">
                            {KIND_LABEL[p.kind]}
                          </Badge>
                        </div>
                        <div className="sr-paper-proj-item-meta">
                          <span className="sr-paper-proj-status">
                            {(st.running || st.watching) && <Indicator color="blue" size={8} processing inline />}
                            {st.text}
                            {(p.stocks?.length ?? 1) > 1 && <span className="sr-paper-proj-stocks">· {p.stocks.length} 股</span>}
                          </span>
                          <span className="sr-paper-proj-time" title="项目最后更新时刻">{formatFullTime(p.updated_at, { withSeconds: false })}</span>
                        </div>
                        <div className="sr-paper-proj-item-actions">
                          <Button size="xs" variant="subtle" leftSection={<IconPencil size={14} />} aria-label="重命名"
                            onClick={(e) => { e.stopPropagation(); openRename(p) }} />
                          <Button size="xs" variant="subtle" color="red" leftSection={<IconTrash size={14} />} aria-label="删除"
                            onClick={(e) => {
                              e.stopPropagation()
                              openConfirmModal({
                                title: '删除该项目？',
                                children: `将删除「${p.name}」及其运行结果，并取消该项目未完成任务`,
                                labels: { confirm: '删除', cancel: '取消' },
                                confirmProps: { color: 'red' },
                                onConfirm: () => void handleDelete(p),
                              })
                            }} />
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </aside>

            {/* 右侧：参数摘要条（顶部）+ 结果区（flex:1 内部滚动，fill 满高） */}
            <div className="sr-paper-main">
              {!activeProject ? (
                <div className="sr-paper-main-empty">
                  <EmptyState
                    description="选择左侧项目查看参数与结果，或点击左侧「新建」创建实验 / 监控项目"
                    padding="48px 0"
                  />
                </div>
              ) : detailLoading && !activeDetailOk ? (
                <div className="sr-paper-state">
                  <SkeletonBlock variant="card" rows={3} />
                </div>
              ) : (
                <>
                  <ParamSummary
                    key={activeProject.id}
                    project={activeProject}
                    editing={editing}
                    onEditingChange={setEditing}
                    code={code}
                    stockText={stockText}
                    stocks={activeProject.stocks ?? []}
                    period={period}
                    signalCodes={signalCodes}
                    days={days}
                    stockOptions={stockSearch.options}
                    signalOptions={signalOptions}
                    periodOptions={periodOptions}
                    onStockTextChange={setStockText}
                    onStockSelect={(c, label) => {
                      setCode(c)
                      if (label) setStockText(label)
                      stockSearch.onSelect(c, label)
                    }}
                    onStockSearch={stockSearch.onQuery}
                    onPeriodChange={setPeriod}
                    onSignalsChange={setSignalCodes}
                    onDaysChange={(d) => setDays(d ?? 250)}
                    onSave={saveParams}
                    saving={saving}
                    expLoading={expLoading}
                    runJob={runJob}
                    onRun={() => void runExperiment()}
                    onPauseJob={() => void pauseRunJob()}
                    onResumeJob={() => void resumeRunJob()}
                    onCancelJob={() => void cancelRunJob()}
                    watching={watching}
                    lastUpdatedAt={lastUpdatedAt}
                    watchError={watchError}
                    onFetchWatch={() => void fetchWatch()}
                    onStartWatch={() => void startWatch()}
                    onStopWatch={stopWatch}
                  />
                  {detailError && (
                    <div className="sr-paper-detail-error" role="alert">
                      <Text style={{ fontSize: 'var(--sr-font-xs)' }}>详情加载失败，部分结果可能不可用</Text>
                      <Button
                        size="xs"
                        variant="subtle"
                        leftSection={<IconRefresh size={12} />}
                        onClick={() => activeId != null && invalidatePaperProject(activeId)}
                        style={{ padding: 0, height: 'auto' }}
                      >
                        重试
                      </Button>
                    </div>
                  )}
                  <div className="sr-paper-results">
                    {activeProject.kind === 'experiment' ? (
                      <ExperimentResults
                        result={expResult}
                        days={days}
                        busy={expLoading || (runJobActive && !expResult)}
                        error={expError}
                        labelOf={labelOf}
                        onRetry={() => void runExperiment()}
                      />
                    ) : (
                      <WatchResults
                        data={watchData}
                        projectId={activeProject.id}
                        watching={watching}
                        loading={watchLoading}
                        period={activeProject.period}
                        lastUpdatedAt={lastUpdatedAt}
                        labelOf={labelOf}
                        onFetchWatch={() => void fetchWatch()}
                      />
                    )}
                  </div>
                </>
              )}
            </div>
          </Flex>
        )}
          </>
        ) : (
          <PortfolioView />
        )}

        <ProjectWizard
          open={wizardOpen}
          signalOptions={signalOptions}
          periodOptions={periodOptions}
          maxStocks={paperMeta?.max_stocks ?? 20}
          creating={mutating}
          onCancel={() => setWizardOpen(false)}
          onCreate={(v) => void handleCreate(v)}
        />
        <Modal
          opened={renaming !== null}
          onClose={() => setRenaming(null)}
          title="重命名项目"
          centered
        >
          <Stack gap="var(--sr-gap-row)">
            <TextInput
              value={renameName}
              onChange={(e) => setRenameName(e.currentTarget.value)}
              placeholder="项目名称"
              maxLength={30}
              onKeyDown={(e) => { if (e.key === 'Enter') void handleRename() }}
            />
            <Group justify="flex-end" gap="var(--sr-gap-ctl)">
              <Button variant="default" onClick={() => setRenaming(null)}>取消</Button>
              <Button loading={mutating} onClick={() => void handleRename()}>保存</Button>
            </Group>
          </Stack>
        </Modal>
      </div>
    </PageShell>
  )
}
