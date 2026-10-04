/** 数据集选择持久化 key：研究区四个页面共享「上次选择」 */
export const LAST_DATASET_KEY = 'sr-last-dataset'

export function readLastDataset(): number | null {
  try {
    const raw = localStorage.getItem(LAST_DATASET_KEY)
    if (raw == null) return null
    const v = Number(raw)
    return Number.isFinite(v) ? v : null
  } catch {
    return null
  }
}

export function saveLastDataset(id: number): void {
  try { localStorage.setItem(LAST_DATASET_KEY, String(id)) } catch { /* 忽略 */ }
}

/** 解析「当前偏好 ?? 上次选择 ?? 首个数据集」，与各页原有初始化逻辑保持一致。 */
export function resolveLastDataset(datasets: { id: number }[], preferred?: number): number | null {
  const last = readLastDataset()
  const cur = preferred ?? (datasets.some((d) => d.id === last) ? last : undefined)
  if (cur != null && datasets.some((d) => d.id === cur)) return cur
  return datasets[0]?.id ?? null
}
