import { api } from './client'

/**
 * 自选股分组（T-12）：用户标签。数据与 UI 共享一份契约——
 * 分组列表带成员 codes，前端分组筛选 / 移入菜单 / 管理 Modal 均消费同一份。
 */
export interface WatchlistGroup {
  id: number
  name: string
  sort_order: number
  /** 分组内股票数 */
  count: number
  /** 成员代码（已排序） */
  codes: string[]
}

/** GET /market/watchlist/groups：全部分组及成员 */
export async function getWatchlistGroups(): Promise<WatchlistGroup[]> {
  const { data } = await api.get('/market/watchlist/groups')
  return data.data ?? []
}

/** POST /market/watchlist/groups：新建分组（重名 400 由 axios 抛错） */
export async function createWatchlistGroup(name: string): Promise<WatchlistGroup> {
  const { data } = await api.post('/market/watchlist/groups', { name })
  return data.data
}

/** PATCH /market/watchlist/groups/{id}：重命名分组 */
export async function renameWatchlistGroup(id: number, name: string): Promise<WatchlistGroup> {
  const { data } = await api.patch(`/market/watchlist/groups/${id}`, { name })
  return data.data
}

/** DELETE /market/watchlist/groups/{id}：删除分组（连带成员） */
export async function deleteWatchlistGroup(id: number): Promise<{ ok: boolean }> {
  const { data } = await api.delete(`/market/watchlist/groups/${id}`)
  return data
}

/** POST /market/watchlist/groups/{id}/items：批量移入分组（同组已有幂等跳过） */
export async function addWatchlistGroupItems(
  id: number,
  codes: string[],
): Promise<{ ok: boolean; added: number; codes: string[] }> {
  const { data } = await api.post(`/market/watchlist/groups/${id}/items`, { codes })
  return data
}

/** DELETE /market/watchlist/groups/{id}/items/{code}：移出分组（不在组内幂等） */
export async function removeWatchlistGroupItem(id: number, code: string): Promise<{ ok: boolean }> {
  const { data } = await api.delete(`/market/watchlist/groups/${id}/items/${code}`)
  return data
}

/** POST /market/watchlist/import：批量关注（已关注幂等；无效代码忽略并计数） */
export async function importWatchlistCodes(
  codes: string[],
): Promise<{ ok: boolean; added: number; total: number; invalid: number }> {
  const { data } = await api.post('/market/watchlist/import', { codes })
  return data
}

/** 解析导入文本（逗号/中文逗号/空格/换行/分号/竖线分隔）为代码数组，自动去除空项 */
export function parseCodesInput(text: string): string[] {
  return text
    .split(/[\s,，;；|]+/)
    .map((s) => s.trim())
    .filter(Boolean)
}

/** GET /market/watchlist/export.csv：blob 下载（带鉴权 header 场景走 axios，不用 window 跳转） */
export async function downloadWatchlistCsv() {
  const res = await api.get('/market/watchlist/export.csv', { responseType: 'blob' })
  const url = URL.createObjectURL(res.data as Blob)
  const a = document.createElement('a')
  const stamp = new Date().toISOString().slice(0, 10)
  a.href = url
  a.download = `watchlist-${stamp}.csv`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
