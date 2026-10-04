import { useParams, useSearchParams } from 'react-router-dom'
import RunDetail from './runs/RunDetail'

/**
 * GP 进化只读详情：作为 /research/runs/:id 的页面入口。
 * 兼容两种挂载方式：
 *  - 新路由 /research/runs/:jobId → 直接读路径参数；
 *  - 旧路由 /research/gp-evolve?job=ID → 读 query 参数（RunJobAdapter 已做路径 → query 适配）。
 */
export default function GpEvolve() {
  const { jobId: pathJobId } = useParams<{ jobId: string }>()
  const [searchParams] = useSearchParams()
  const fromPath = Number(pathJobId)
  const fromQuery = Number(searchParams.get('job'))
  const jobId = Number.isFinite(fromPath) && fromPath > 0 ? fromPath
    : (Number.isFinite(fromQuery) && fromQuery > 0 ? fromQuery : NaN)
  return <RunDetail jobId={jobId} />
}
