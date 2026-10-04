import { getDatasets, getExperimentsPage, stableParamKey } from '../api/client'
import { pollInterval, queryClient, useQueryState, useQueryValue } from './queryBase'
import type { QueryState } from './queryBase'
import type { DatasetInfo, ExperimentsPageParams, JobDetail, JobListItem, PageResult } from '../api/client'

/** 任务域轮询节奏：15s SWR + 有订阅者时 15s 轮询（任务执行不受交易时段影响，无降频） */
const JOB_STALE = 15_000
const jobsInterval = pollInterval(15_000)

/** 数据集（5min SWR，全站共享；构建/删除后主动 invalidate；无轮询） */
function datasetsQuery() {
  return {
    queryKey: ['data', 'datasets'],
    queryFn: () => getDatasets(),
    staleTime: 300_000,
  }
}
export function useDatasets(): DatasetInfo[] | undefined {
  return useQueryValue<DatasetInfo[]>(datasetsQuery())
}
/** 数据集完整状态（value/loading/error）：供数据集管理页区分「查询未就绪」与「真的空列表」 */
export function useDatasetsState(): QueryState<DatasetInfo[]> {
  return useQueryState<DatasetInfo[]>(datasetsQuery())
}
export function invalidateDatasets() {
  queryClient.invalidateQueries({ queryKey: ['data', 'datasets'] })
}

/** 任务查询池：全站共享轮询（15s），有订阅者才发请求；完成/失败即通知 */
export interface JobLike {
  id: number
  job_type: string
  status: string
  progress?: number
  [key: string]: unknown
}

/** 任务分页查询：job_type/status/keyword 筛选 + 分页各自独立缓存（15s 轮询，与 jobs 池同节奏） */
function jobsPageQuery(params: ExperimentsPageParams = {}, enabled = true) {
  const merged = { limit: 20, offset: 0, ...params }
  // 空值过滤（与 stableParamKey 同语义）后直接以原始类型传参：number/boolean 不再经 stable key 往返降级为 string
  const clean = Object.fromEntries(Object.entries(merged).filter(([, v]) => v != null && v !== ''))
  return {
    // queryKey 身份仍用 stableParamKey 稳定字符串（同参数必得同 key）
    queryKey: ['jobs', 'page', stableParamKey(clean)],
    // queryFn 引用原始 merged（过滤空值后），不走 parseParamKey 往返——参数类型保真（limit/offset 缺省自动补齐）
    queryFn: () => getExperimentsPage(clean as ExperimentsPageParams),
    enabled,
    staleTime: JOB_STALE,
    refetchInterval: jobsInterval,
  }
}
/** 分页任务列表：返回完整 QueryState（value=PageResult/loading/error/fetchedAt）。
 *  job_type/status/keyword/分页任一参数变化即独立缓存（status 支持 active=pending+running 别名，keyword 服务端模糊搜索）；
 *  limit/offset 缺省自动补齐（limit=20, offset=0）。 */
export function useExperimentsPage(params: ExperimentsPageParams = {}, enabled = true): QueryState<PageResult<JobListItem>> {
  return useQueryState(jobsPageQuery(params, enabled))
}
export function invalidateJobsPage() {
  queryClient.invalidateQueries({ queryKey: ['jobs', 'page'] })
}

/** 任务列表（15s 轮询，多页同时看也只发一份）。
 *  enabled=false（如 JobBar 弹层关闭）时不订阅、不发任何请求——顶栏常驻不再持续全量拉 200 条，
 *  仅在任务中心/任务页打开时才保持 15s 轮询（条件轮询）。 */
export function useJobs(enabled = true): JobListItem[] | undefined {
  return useQueryValue({
    queryKey: ['jobs', 'list', 'all'],
    queryFn: () => {
      // 列表查询（后端 experiments 按新→旧返回；任务管理页需完整列表，limit 200）
      return import('../api/client').then((m) => m.getExperiments(undefined, 200)).then((r) => r.data)
    },
    enabled,
    staleTime: JOB_STALE,
    refetchInterval: jobsInterval,
  })
}
/** 单任务（15s 轮询，去重）；id 为空/非法时不订阅、不发起任何请求（避免哨兵请求 /experiments/none） */
export function useJob(id: number | undefined): JobDetail | undefined {
  return useJobState(id).value
}
/** 单任务完整状态（value/loading/error）：任务中心详情抽屉 / RunDetail 区分「加载中」与「失败/404」，
 *  失败不再被压成 undefined 永久转圈（T-128） */
export function useJobState(id: number | undefined): QueryState<JobDetail> {
  const valid = id !== undefined && Number.isFinite(id) && id > 0
  return useQueryState({
    queryKey: ['jobs', 'one', String(id ?? '')],
    queryFn: () => import('../api/client').then((m) => m.getJob(id as number)),
    enabled: valid,
    staleTime: JOB_STALE,
    refetchInterval: jobsInterval,
  })
}
export function invalidateJobs() {
  queryClient.invalidateQueries({ queryKey: ['jobs', 'list'] })
  queryClient.invalidateQueries({ queryKey: ['jobs', 'one'] })
  queryClient.invalidateQueries({ queryKey: ['jobs', 'page'] })
}
export function invalidateJob(id: number) {
  queryClient.invalidateQueries({ queryKey: ['jobs', 'list', 'all'] })
  queryClient.invalidateQueries({ queryKey: ['jobs', 'one', String(id)] })
}
