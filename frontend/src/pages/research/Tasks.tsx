import { Button, Indicator, SegmentedControl } from '@mantine/core'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, createExperiment, pauseJob, resumeJob } from '../../api/client'
import type { JobDetail } from '../../api/client'
import { invalidateJob, invalidateJobs, invalidateJobsPage, useDatasets, useExperimentsPage, useJobState, useJobs } from '../../data/jobs'
import { useViewport } from '../../app/useViewport'
import { EmptyState, InfiniteScrollSentinel, PageShell, SkeletonBlock } from '../../components/ui'
import DesktopTaskTable from './tasks/desktopTable'
import MobileTaskList from './tasks/mobileList'
import TaskDetailDrawer from './tasks/detailDrawer'
import TodoPanel from './tasks/TodoPanel'
import TasksStats from './tasks/TasksStats'
import TasksFilters from './tasks/TasksFilters'
import type { SortKey } from './tasks/TasksFilters'
import BatchActions from './tasks/BatchActions'
import { useInfiniteTasks } from './tasks/useInfiniteTasks'
import { useArchived, usePersistentLocalState } from './tasks/taskPrefs'
import type { TaskRow } from './tasks/constants'
import { canRerun } from './tasks/constants'
import { toast } from './shared/toast'
import './tasks/tasks.css'

const PAGE_SIZE = 20
const INF_KEY = 'sr-tasks-infinite'
const STATUS_RANK: Record<string, number> = { pending: 0, running: 1, paused: 2, done: 3, failed: 4, cancelled: 5 }

/**
 * 唯一完整任务中心：类型/状态/关键词全部走服务端筛选（/experiments/page，真实分页 + 关键词模糊搜索），
 * 顶部统计来自全站共享任务池（最近 200 条快照，非全库总数）；长列表可开启无限滚动（offset 增量 + id 去重）。
 * 操作（暂停[协作式，下一检查点生效]/继续/取消/删除/重跑/归档）后统一失效全局、分页与详情缓存。
 * T-43 拆分：统计（TasksStats）/ 筛选（TasksFilters）/ 批量工具条（BatchActions）移至 tasks/ 子组件，
 * 本组件保留状态编排与操作执行器（runBatch）。
 */
export default function Tasks() {
  const isMobile = useViewport() === 'mobile'

  // 筛选/排序持久化（localStorage sr-tasks-* 前缀；无限滚动开关由 useInfiniteTasks 落 sr-tasks-infinite）
  const [typeFilter, setTypeFilter] = usePersistentLocalState<string>('type', 'all')
  const [statusFilter, setStatusFilter] = usePersistentLocalState<string>('status', 'all')
  const [keyword, setKeyword] = usePersistentLocalState<string>('keyword', '')
  const [sortBy, setSortBy] = usePersistentLocalState<SortKey>('sort', 'time')
  const [sortOrder, setSortOrder] = usePersistentLocalState<'asc' | 'desc'>('sort-order', 'desc')
  const [showArchived, setShowArchived] = usePersistentLocalState<boolean>('show-archived', false)
  const { isArchived, archive, unarchive } = useArchived() // 归档 id 集（本地标记，统计排除 + 列表隐藏）

  const [page, setPage] = useState(1)
  const [detailRow, setDetailRow] = useState<TaskRow | null>(null)
  const [selectedIds, setSelectedIds] = useState<number[]>([]) // 批量选择（仅分页态桌面），翻页/筛选/模式切换即清空

  // 暂停过渡定时器：按任务 id 存 Map（800ms 窗口内连续暂停多个任务互不干扰），执行后删除
  const pauseTimersRef = useRef(new Map<number, ReturnType<typeof setTimeout>>())
  useEffect(() => () => { for (const t of pauseTimersRef.current.values()) clearTimeout(t); pauseTimersRef.current.clear() }, [])

  const [view, setView] = useState<'jobs' | 'todos'>('jobs') // 计算任务 / 待办事项 视图切换

  // 服务端筛选参数（分页与无限滚动共用同一套；关键词防抖由 TasksFilters 内聚，keyword 即防抖后的值）
  const pageParams = {
    job_type: typeFilter !== 'all' ? typeFilter : undefined,
    status: statusFilter !== 'all' ? statusFilter : undefined,
    keyword: keyword.trim() || undefined,
  }

  // 无限滚动（默认关闭）：offset 0 增量加载、id 去重，筛选变化重置 generation；须先于 pageQuery 声明
  const inf = useInfiniteTasks(pageParams, INF_KEY)
  // 无限滚动模式不支持批量选择：切换即清空
  useEffect(() => { setSelectedIds([]) }, [inf.on])

  // 分页列表（服务端 total/offset/limit，15s 轮询）；inf.on 时无限滚动接管数据，本查询 enabled=false 不双路轮询
  const pageQuery = useExperimentsPage({
    limit: PAGE_SIZE,
    offset: (page - 1) * PAGE_SIZE,
    ...pageParams,
  }, !inf.on)
  const pageItems = useMemo(() => pageQuery.value?.items ?? [], [pageQuery.value])
  const total = pageQuery.value?.total ?? 0
  // 首屏失败后 value 恒 undefined：loading 必须叠加 loading 标志，否则失败后永久转圈
  const loading = pageQuery.loading && pageQuery.value === undefined
  const rawRows = inf.on ? inf.rows : pageItems
  const listTotal = inf.on ? inf.total : total
  const listLoading = inf.on ? inf.loading : loading

  // 桌面表格首载骨架：仅「无任何行且正在加载」时替换表格（分页首载/无限首载/筛选重置），有行后增量加载仍走表格+哨兵
  const showTableSkeleton = !isMobile && listLoading && rawRows.length === 0

  // 分页模式新任务提示：跟踪当前页最大 id，第一页静默吸收；非第一页轮询到更大 id 时顶部显示徽标，点击回第一页
  const maxSeenIdRef = useRef(0)
  const [newTaskCount, setNewTaskCount] = useState(0)
  useEffect(() => {
    const max = pageItems.reduce((m, r) => Math.max(m, r.id), 0)
    if (max === 0) return
    if (page === 1) {
      maxSeenIdRef.current = Math.max(maxSeenIdRef.current, max)
      setNewTaskCount(0)
    } else if (max > maxSeenIdRef.current) {
      setNewTaskCount(pageItems.filter((r) => r.id > maxSeenIdRef.current).length)
    }
  }, [pageItems, page])
  const handleNewTasksJump = () => { setNewTaskCount(0); setPage(1); setSelectedIds([]) }

  // 可见行 = 原始行（归档隐藏过滤）+ 前端排序（后端无 order_by；created_at 为 naive ISO 字典序即时间序）
  const visibleRows = useMemo(() => {
    const base = showArchived ? rawRows : rawRows.filter((r) => !isArchived(r.id))
    const sorted = [...base]
    const dir = sortOrder === 'asc' ? 1 : -1
    if (sortBy === 'type') {
      sorted.sort((a, b) => dir * ((a.job_type < b.job_type ? -1 : a.job_type > b.job_type ? 1 : 0)))
    } else if (sortBy === 'status') {
      sorted.sort((a, b) => dir * ((STATUS_RANK[a.status] ?? 99) - (STATUS_RANK[b.status] ?? 99)))
    } else {
      sorted.sort((a, b) => {
        const av = a.created_at ?? ''
        const bv = b.created_at ?? ''
        return av === bv ? 0 : dir * (av < bv ? -1 : 1)
      })
    }
    return sorted
  }, [rawRows, showArchived, isArchived, sortBy, sortOrder])

  // 归档隐藏使当前页变空（服务端分页 total 仍含归档）→ 自动回退一页，避免空页停留
  useEffect(() => {
    if (inf.on || loading) return
    if (page > 1 && visibleRows.length === 0) setPage(page - 1)
  }, [visibleRows.length, page, inf.on, loading])

  // 顶部统计：全站共享任务池快照（最近 200 条，非全库总数），pending 计入进行中，已归档不计入
  const allJobs = useJobs()
  const stats = useMemo(() => {
    const list = (allJobs ?? []).filter((j) => !isArchived(j.id))
    const pending = list.filter((j) => j.status === 'pending').length
    const running = list.filter((j) => j.status === 'running').length
    return {
      active: pending + running,
      pending,
      running,
      paused: list.filter((j) => j.status === 'paused').length,
      done: list.filter((j) => j.status === 'done').length,
      failed: list.filter((j) => j.status === 'failed').length,
      cancelled: list.filter((j) => j.status === 'cancelled').length,
      all: list.length,
    }
  }, [allJobs, isArchived])

  // 数据集名映射（全站共享 5min SWR）
  const datasets = useDatasets()
  const datasetName = useCallback((id?: number | null) => {
    if (id == null) return ''
    const d = datasets?.find((x) => Number(x.id) === Number(id))
    return d?.name ? String(d.name) : ''
  }, [datasets])

  const changeType = (v: string) => { setTypeFilter(v); setPage(1); setSelectedIds([]) }
  const changeStatus = (v: string) => { setStatusFilter(v); setPage(1); setSelectedIds([]) }
  const goPage = (p: number) => { setPage(p); setSelectedIds([]) }
  const handleKeyword = (v: string) => { setKeyword(v); setPage(1); setSelectedIds([]) } // TasksFilters 防抖后回调
  const handleShowArchived = (v: boolean) => { setShowArchived(v); setSelectedIds([]) }

  // 详情抽屉：列表快照 + 单任务轮询（15s）合并（完整 result / finished_at / 完整 error）
  const detailQ = useJobState(detailRow?.id)
  const detailJob = detailQ.value
  // T-128:加载/失败分离——查询失败不再被压成 undefined 永久转圈
  const detailLoading = detailRow != null && detailQ.loading && detailJob === undefined
  const detailError = detailRow != null && detailQ.error != null
  const drawerJob = useMemo((): JobDetail | null => {
    if (!detailRow) return null
    return { ...detailRow, ...(detailJob ?? {}) } as JobDetail
  }, [detailRow, detailJob])

  // 操作后统一失效：全局池 → 立即重拉；无限滚动开启时轻量刷新第一页并合并排序（DOM 不重建、滚动位置保持）；
  // delay > 0 时延迟执行（暂停过渡态专用：避免检查点未到造成 running→running 闪烁）
  const afterMutate = (id: number, close = false, delay = 0) => {
    const run = () => {
      invalidateJobs()
      if (inf.on) inf.refresh()
      if (close && detailRow?.id === id) setDetailRow(null)
      if (pageItems.length === 1 && page > 1) setPage(page - 1) // 当前页仅剩本条且非第一页 → 回退一页
    }
    if (delay > 0) {
      const prev = pauseTimersRef.current.get(id)
      if (prev) clearTimeout(prev)
      pauseTimersRef.current.set(id, setTimeout(() => {
        pauseTimersRef.current.delete(id)
        run()
      }, delay))
    } else {
      run()
    }
  }

  // 协作式暂停：仅 running 提供（下一计算检查点停下，可继续恢复）；延迟 800ms 再失效避免闪烁
  const handlePause = async (id: number) => {
    try {
      const res = await pauseJob(id)
      toast.success(res.message ?? `任务 #${id} 已暂停（在下一个计算检查点生效）`)
      afterMutate(id, false, 800)
    } catch (e: any) {
      toast.error('暂停失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
    }
  }
  const handleResume = async (id: number) => {
    try {
      const res = await resumeJob(id)
      toast.success(res.message ?? `任务 #${id} 已恢复`)
      afterMutate(id)
    } catch (e: any) {
      toast.error('恢复失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
    }
  }
  // 取消（running/pending/paused → 标记失败保留）/ 删除（done/failed → 删行），语义由后端 deleted 区分
  const handleCancel = async (id: number) => {
    try {
      const { data } = await api.delete(`/experiments/${id}`)
      toast.success(data.deleted ? `任务 #${id} 已删除` : `任务 #${id} 已取消`)
      if (data.deleted && inf.on) inf.removeLocal(id) // 删除本地即时移除，避免无限模式残留到轮询合并
      afterMutate(id, Boolean(data.deleted))
    } catch {
      toast.error(`取消任务 #${id} 失败`)
    }
  }
  const handleDelete = async (id: number) => {
    try {
      await api.delete(`/experiments/${id}`)
      toast.success(`任务 #${id} 已删除`)
      if (inf.on) inf.removeLocal(id)
      afterMutate(id, true)
    } catch {
      toast.error(`删除任务 #${id} 失败`)
    }
  }
  // 重新运行：复用原 job_type + params 创建 1 个新任务（原记录保留）
  const handleRerun = async (id: number) => {
    const src = inf.rows.find((r) => r.id === id)
      ?? pageItems.find((r) => r.id === id)
      ?? (drawerJob?.id === id ? drawerJob : null)
    if (!src) return
    try {
      const { job_id } = await createExperiment(src.job_type, src.params ?? {})
      toast.success(`已创建新任务 #${job_id}（复用原任务类型与参数）`)
      afterMutate(id)
    } catch (e: any) {
      toast.error('重新运行失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
    }
  }
  // 归档/取消归档：本地标记（隐藏 + 统计排除），不触发服务端；待后端软删支持
  const handleArchive = (id: number) => {
    archive([id])
    toast.success(`任务 #${id} 已归档（本地隐藏，待后端软删支持）`)
  }
  const handleUnarchive = (id: number) => {
    unarchive(id)
    toast.success(`任务 #${id} 已取消归档`)
  }

  // ---- 批量操作（仅分页态桌面；后端无批量端点 → 前端循环单任务接口 + Promise.allSettled 汇总） ----
  const selectedRows = useMemo(
    () => visibleRows.filter((r) => selectedIds.includes(r.id)),
    [visibleRows, selectedIds],
  )
  const hasRunning = selectedRows.some((r) => r.status === 'running')
  const hasNonTerminal = selectedRows.some((r) => ['pending', 'running', 'paused'].includes(r.status))
  const hasTerminal = selectedRows.some((r) => ['done', 'failed', 'cancelled'].includes(r.status))
  const hasRerunnable = selectedRows.some((r) => canRerun(r) && ['done', 'failed', 'cancelled'].includes(r.status))
  const hasVisible = selectedRows.some((r) => !isArchived(r.id))

  /** 批量执行器：并发调用单任务接口，期间 toast.loading 实时计数，结束汇总「成功 N / 失败 M」；
   *  成功后逐 id afterMutate（暂停走延迟避免闪烁）并从选择集移除；删除/取消命中详情抽屉则关闭。 */
  const runBatch = async (label: string, targets: TaskRow[], fn: (r: TaskRow) => Promise<void>, afterDelay = 0) => {
    if (targets.length === 0) { toast.info('所选任务均不适用于该操作'); return }
    const msgKey = `sr-batch-${label}-${Date.now()}`
    let done = 0
    const totalN = targets.length
    toast.loading(msgKey, `批量${label}… 0/${totalN}`)
    const tick = setInterval(() => {
      toast.update(msgKey, `批量${label}… ${done}/${totalN}`)
    }, 300)
    const okIds: number[] = []
    const results = await Promise.allSettled(targets.map(async (r) => {
      try {
        await fn(r)
        okIds.push(r.id)
      } finally {
        done += 1
      }
    }))
    clearInterval(tick)
    toast.dismiss(msgKey)
    const fail = results.filter((x) => x.status === 'rejected').length
    const ok = results.length - fail
    if (fail === 0) toast.success(`批量${label}完成：成功 ${ok} / 失败 ${fail}`)
    else toast.warning(`批量${label}完成：成功 ${ok} / 失败 ${fail}`)
    if (okIds.length > 0) {
      if (detailRow != null && okIds.includes(detailRow.id)) setDetailRow(null)
      okIds.forEach((id) => afterMutate(id, false, afterDelay))
      setSelectedIds((prev) => prev.filter((id) => !okIds.includes(id)))
    }
  }
  const batchPause = () => runBatch('暂停', selectedRows.filter((r) => r.status === 'running'), async (r) => { await pauseJob(r.id) }, 800)
  const batchCancel = () => runBatch('取消', selectedRows.filter((r) => ['pending', 'running', 'paused'].includes(r.status)), async (r) => { await api.delete(`/experiments/${r.id}`) })
  const batchDelete = () => runBatch('删除', selectedRows.filter((r) => ['done', 'failed', 'cancelled'].includes(r.status)), async (r) => { await api.delete(`/experiments/${r.id}`) })
  const batchRerun = () => runBatch('重跑', selectedRows.filter((r) => canRerun(r) && ['done', 'failed', 'cancelled'].includes(r.status)), async (r) => { await createExperiment(r.job_type, r.params ?? {}) })
  const batchArchive = () => {
    const targets = selectedRows.filter((r) => !isArchived(r.id))
    if (targets.length === 0) { toast.info('所选任务均已归档'); return }
    archive(targets.map((r) => r.id))
    toast.success(`已归档 ${targets.length} 项（本地隐藏，待后端软删支持）`)
    setSelectedIds([])
  }

  // T-52 满高:仅「计算任务 + 分页」态 fill(表格占满,DataTable grow 撑高);
  // 无限滚动需页面级滚动驱动哨兵、待办视图为流动内容,两者维持 flow(不 fill)
  // 单一事实源:PageShell fill 与桌面表格 grow 共用(fillActive 经 prop 传给 desktopTable)
  const fillActive = view === 'jobs' && !inf.on

  return (
    <PageShell fill={fillActive} className="sr-sticky-tools">
      <div className="sr-task-viewsw">
        <SegmentedControl
          size="xs"
          value={view}
          onChange={(v) => setView(v as 'jobs' | 'todos')}
          data={[{ label: '计算任务', value: 'jobs' }, { label: '待办事项', value: 'todos' }]}
        />
      </div>
      {view === 'jobs' ? (
      <>
      <TasksStats stats={stats} typeFilter={typeFilter} statusFilter={statusFilter} onType={changeType} onStatus={changeStatus} />
      <TasksFilters
        type={typeFilter} onType={changeType} status={statusFilter} onStatus={changeStatus}
        sortBy={sortBy} onSortBy={setSortBy} sortOrder={sortOrder} onSortOrder={setSortOrder}
        keyword={keyword} onKeyword={handleKeyword}
        showArchived={showArchived} onShowArchived={handleShowArchived} infOn={inf.on} onInf={inf.setOn}
      />

      {/* 批量操作工具条：分页态桌面选中后出现；按钮按所选任务状态智能启用 */}
      {!isMobile && !inf.on && selectedIds.length > 0 && (
        <BatchActions
          count={selectedIds.length} hasRunning={hasRunning} hasNonTerminal={hasNonTerminal}
          hasTerminal={hasTerminal} hasRerunnable={hasRerunnable} hasVisible={hasVisible}
          onPause={() => void batchPause()} onCancel={() => void batchCancel()} onDelete={() => void batchDelete()}
          onRerun={() => void batchRerun()} onArchive={batchArchive}
        />
      )}

      {/* 分页模式新任务提示：轮询返回更新的任务且不在第一页时显示，点击回第一页 */}
      {!inf.on && newTaskCount > 0 && (
        <div className="sr-task-new-badge">
          <Indicator label={newTaskCount} size={18} offset={6}>
            <Button size="xs" variant="outline" onClick={handleNewTasksJump}>有新任务，点击查看</Button>
          </Indicator>
        </div>
      )}

      {!inf.on && pageQuery.error && pageQuery.value === undefined ? (
        <EmptyState
          text={`分页加载失败: ${(pageQuery.error as Error | undefined)?.message ?? String(pageQuery.error)}`}
          onRetry={() => invalidateJobsPage()}
          padding="40px 0"
        />
      ) : isMobile ? (
        <MobileTaskList
          rows={visibleRows} total={listTotal} page={page} pageSize={PAGE_SIZE} loading={listLoading}
          infinite={inf.on} onPageChange={goPage} onOpen={setDetailRow} datasetName={datasetName}
          onPause={handlePause} onResume={handleResume} onCancel={handleCancel}
          onDelete={handleDelete} onRerun={handleRerun}
          isArchived={isArchived} onArchive={handleArchive} onUnarchive={handleUnarchive}
        />
      ) : showTableSkeleton ? (
        <SkeletonBlock rows={5} />
      ) : (
        <DesktopTaskTable
          rows={visibleRows} total={listTotal} page={page} pageSize={PAGE_SIZE} loading={listLoading}
          infinite={inf.on} onPageChange={goPage} onOpen={setDetailRow}
          onPause={handlePause} onResume={handleResume} onCancel={handleCancel}
          onDelete={handleDelete} onRerun={handleRerun} datasetName={datasetName}
          selectedIds={selectedIds} onSelectionChange={setSelectedIds}
          isArchived={isArchived} onArchive={handleArchive} onUnarchive={handleUnarchive}
          sortBy={sortBy} sortOrder={sortOrder} fillActive={fillActive}
        />
      )}

      {inf.on && !showTableSkeleton && (
        <>
          {!isMobile && (
            <div className="sr-task-inf-meta" role="status" aria-live="polite">已加载 {rawRows.length} / 共 {listTotal} 条</div>
          )}
          <InfiniteScrollSentinel
            hasMore={inf.hasMore} loadMore={inf.loadMore} enabled
            loading={inf.loading} error={inf.error} retry={inf.retry} resetKey={inf.filterKey}
            doneText="已加载全部任务" errorText="加载失败，请重试"
          />
        </>
      )}

      <TaskDetailDrawer
        open={detailRow != null} job={drawerJob}
        loading={detailLoading}
        error={detailError}
        onRetryDetail={() => { if (detailRow) invalidateJob(detailRow.id) }}
        datasetName={datasetName}
        archived={detailRow != null && isArchived(detailRow.id)} onClose={() => setDetailRow(null)}
        onPause={handlePause} onResume={handleResume} onCancel={handleCancel}
        onDelete={handleDelete} onRerun={handleRerun}
        onArchive={handleArchive} onUnarchive={handleUnarchive}
      />
      </>
      ) : (
        <TodoPanel />
      )}
    </PageShell>
  )
}
