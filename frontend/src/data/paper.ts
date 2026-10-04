import { getPaperProject, getPaperProjects } from '../api/client'
import type { PaperProject } from '../api/client'
import { queryClient, useQueryState } from './queryBase'
import type { QueryState } from './queryBase'

/** 模拟盘项目池（PaperTrading 列表）：首拉 / 全局刷新（sr-refresh）失效自动重拉；
 *  本地写操作（创建/删除/保存/运行结果回写）由页面直写 projects 视图 state，
 *  池缓存作数据源与重拉入口（P1-47 迁池原则：数据获取走池、视图状态留 useState）。
 *  无轮询——列表变更由写操作驱动，轮询只会放大无谓请求。 */
export function usePaperProjects(): QueryState<PaperProject[]> {
  return useQueryState({
    queryKey: ['paper', 'projects'],
    queryFn: getPaperProjects,
  })
}

/** 模拟盘项目详情池（PaperTrading 详情）：按 id 参数化，切换项目自动重拉（key 变 → 新订阅）。
 *  staleTime 0 → 切换 / 重新挂载即拉后端最新（与旧 loadDetail「每次切换都拉取」语义一致），
 *  保存/运行等写操作后切走再切回不会命中过期缓存。 */
export function usePaperProject(id: number | null): QueryState<PaperProject> {
  return useQueryState({
    queryKey: ['paper', 'project', id ?? ''],
    queryFn: () => getPaperProject(id as number),
    enabled: id != null,
    staleTime: 0,
  })
}

export function invalidatePaperProjects() {
  queryClient.invalidateQueries({ queryKey: ['paper', 'projects'] })
}

export function invalidatePaperProject(id: number) {
  queryClient.invalidateQueries({ queryKey: ['paper', 'project', id] })
}
