import { Navigate, useLocation } from 'react-router-dom'

/**
 * 保留 query 的同路径跳转（旧别名路由 → 新路由）：
 * - `to` 支持带固定参数的 pathname（如 "/paper?view=portfolio"，T-126）；
 * - 当前 URL 的 query 合并进目标（已有同名键不覆盖），?job=/&ds=/&expr=/&section= 等原样携带。
 */
export default function QueryRedirect({ to }: { to: string }) {
  const { search } = useLocation()
  const [path, qs] = to.split('?')
  const merged = new URLSearchParams(qs ?? '')
  for (const [k, v] of new URLSearchParams(search)) if (!merged.has(k)) merged.set(k, v)
  const s = merged.toString()
  return <Navigate to={{ pathname: path, search: s ? `?${s}` : '' }} replace />
}