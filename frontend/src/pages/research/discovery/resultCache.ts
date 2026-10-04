/**
 * 因子挖掘结果缓存（T-43：自 FactorMining 页面迁出）。
 * 'sr-last-gp-result' localStorage 快照：上次任务结果（Top 20）供无任务时回看。
 */
import { useCallback, useState } from 'react'
import type { FactorResult } from '../../../api/client'

export const RESULT_CACHE_KEY = 'sr-last-gp-result'

export interface FmCacheInfo {
  ts: number
  params: { algorithm?: string; target?: string; pop_size?: number; generations?: number; horizon?: number }
  results: FactorResult[]
}

/** 读缓存快照（首载 / 加载按钮共用） */
export function readResultCache(): FmCacheInfo | null {
  try {
    const raw = localStorage.getItem(RESULT_CACHE_KEY)
    if (raw) {
      const c = JSON.parse(raw)
      if (c?.results?.length) return c
    }
  } catch { /* ignore */ }
  return null
}

/** 任务完成落缓存（params 为当前表单快照） */
export function writeResultCache(j: any, params: FmCacheInfo['params']) {
  try {
    localStorage.setItem(RESULT_CACHE_KEY, JSON.stringify({
      ts: Date.now(),
      params,
      results: (j?.result?.results ?? []).slice(0, 20),
    }))
  } catch { /* ignore */ }
}

/** 挖掘页缓存状态 + 操作（cacheInfo/加载/清空） */
export function useResultCache(formParams: () => FmCacheInfo['params']) {
  const [cacheInfo, setCacheInfo] = useState<FmCacheInfo | null>(null)
  const [cachedResults, setCachedResults] = useState<FactorResult[] | null>(null)
  const saveResultCache = useCallback((j: any) => {
    writeResultCache(j, formParams())
  }, [formParams])
  const clearResultCache = useCallback(() => {
    try { localStorage.removeItem(RESULT_CACHE_KEY) } catch { /* ignore */ }
    setCacheInfo(null)
    setCachedResults(null)
  }, [])
  return { cacheInfo, setCacheInfo, cachedResults, setCachedResults, saveResultCache, clearResultCache }
}
