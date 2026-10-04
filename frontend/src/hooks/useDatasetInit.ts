import { useEffect, useRef } from 'react'
import { resolveLastDataset } from '../utils/useLastDataset'

/**
 * 研究页「数据集首次到达解析默认选择」统一实现
 * （替代原 FactorMining/FactorEvaluation/Backtest/Alpha101 各自的 dsInitRef + resolveLastDataset 拷贝）：
 *
 * - 仅首次解析（datasets 非空到达且未初始化过），之后用户手动选择不受影响
 * - preferred 为 URL ?ds= 或页内兜底偏好；resolveLastDataset 内部优先级：preferred ?? 上次选择 ?? 首个
 * - onInit 为页面写入动作；需要「仅未选择时才解析」守卫（如 Alpha101 的 if (!dsId)）时在 onInit 闭包内实现
 */
export function useDatasetInit(
  datasets: { id: number }[],
  preferred: number | undefined,
  onInit: (dsId: number | undefined) => void,
): void {
  const dsInitRef = useRef(false)
  useEffect(() => {
    if (dsInitRef.current || !datasets.length) return
    dsInitRef.current = true
    onInit(resolveLastDataset(datasets, preferred) ?? undefined)
    // 仅 datasets 首次非空到达触发；preferred/onInit 为调用方闭包（与原页面 eslint-disable 语义一致）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datasets])
}
