import { Text } from '@mantine/core'
import { useEffect, useMemo, useState } from 'react'
import type { CombinedTuneResponse, TuneCandidate } from '../../../../../api/client'
import { useWorkbench } from '../../context'

/**
 * 调优结果区 UI 状态与派生（TuneSection 主组件瘦身）：
 * - 结果区 Tabs 受控 activeKey（'__overview__' = 对比总表固定首位）
 * - 「重置全局」in-flight（网络 PUT）：请求未返回时按钮 loading + 防并发
 * - resultKeys 推导（有结果或有错误的指标 key，选中顺序优先）
 * - 两模式结果并存标记（hasIndividual / hasCombined）
 * - 应用态 / 最优候选 / 对比总表目标说明 / 组合增益标注
 */
export function useTuneResults() {
  const c = useWorkbench()
  const {
    tuneInds, tuneResults, tuneErrors, resetGlobalParams, manualParams, tunedInds,
  } = c

  // 结果区 Tabs 受控 key（'__overview__' = 对比总表固定首位）
  const [activeKey, setActiveKey] = useState('__overview__')
  // 结果区「重置全局」in-flight（网络 PUT）：请求未返回时按钮 loading + 防并发
  const [resettingKey, setResettingKey] = useState<string | null>(null)
  const resetGlobal = async (k: string) => {
    if (resettingKey != null) return
    setResettingKey(k)
    try { await resetGlobalParams(k) } finally { setResettingKey(null) }
  }

  // 有结果或有错误的指标 key：当前选中顺序优先，历史结果/错误补充（防改选后结果不可见）；
  // 只保留真实存在结果/错误的 key——取消产生、未执行到的指标不渲染空 Tab
  const resultKeys = useMemo(() => {
    const keys: string[] = []
    const seen = new Set<string>()
    const push = (k: string) => {
      if (k === '__combined__' || seen.has(k)) return
      seen.add(k)
      keys.push(k)
    }
    tuneInds.forEach(push)
    Object.keys(tuneResults).forEach(push)
    Object.keys(tuneErrors).forEach(push)
    return keys.filter((k) => tuneResults[k] != null || tuneErrors[k] != null)
  }, [tuneInds, tuneResults, tuneErrors])

  // activeKey 失效（指标被移除/清空）时兜底回对比总表
  useEffect(() => {
    if (activeKey !== '__overview__' && !resultKeys.includes(activeKey)) setActiveKey('__overview__')
  }, [resultKeys, activeKey])

  const combo = tuneResults['__combined__'] as CombinedTuneResponse | undefined
  const comboErr = tuneErrors['__combined__']
  // 两模式结果并存不互斥：hasIndividual/hasCombined 分别标记两模式结果是否已在（切换模式时保留，
  // 用「上次结果（逐个/整体）」标记提示查看另一模式结果）
  const hasIndividual = resultKeys.length > 0
  const hasCombined = combo != null || comboErr != null
  const hasAny = hasIndividual || hasCombined
  const isApplied = (k: string): boolean => manualParams[k] != null || tunedInds.includes(k)
  const bestOf = (k: string): TuneCandidate | null => {
    const res = tuneResults[k]
    return res && !('combined' in res) ? res.best : null
  }
  // 对比总表目标说明取首个有值的单指标结果（同参数跑批，targets/horizon 一致）
  const firstRes = (() => {
    for (const k of resultKeys) {
      const r = tuneResults[k]
      if (r && !('combined' in r)) return r
    }
    return undefined
  })()

  // 组合增益标注：组合 IC vs 单指标最优（如实展示，组合不一定赢）
  const comboGain = (() => {
    if (!combo?.best || !combo.singles) return null
    const ics = Object.values(combo.singles).map((s) => s.best_ic).filter((x): x is number => x != null)
    if (!ics.length) return null
    return (
      <Text span size="xs" className="sr-tune-target-note" aria-label="组合增益对比" style={{ color: 'var(--sr-text-3)' }}>
        组合 IC {combo.best.ic?.toFixed(3) ?? '—'} vs 单指标最优 {Math.max(...ics).toFixed(3)}
      </Text>
    )
  })()

  return {
    activeKey, setActiveKey, resultKeys,
    resettingKey, resetGlobal,
    combo, comboErr,
    hasIndividual, hasCombined, hasAny,
    isApplied, bestOf, firstRes, comboGain,
  }
}

export type TuneResultsState = ReturnType<typeof useTuneResults>
