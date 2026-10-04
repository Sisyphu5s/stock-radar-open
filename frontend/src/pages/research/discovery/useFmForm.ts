/**
 * 因子挖掘表单状态（T-43：自 FactorMining 页面迁出，spec「FmForm/hook」的 hook 侧）。
 * ------------------------------------------------------------
 * 表单态持久化：'fm:state'（sessionStorage，usePersistentState）。
 * 字段 setter 统一合并 patch，页面不再逐个手写；FmForm（组件）消费字段与回调。
 */
import { usePersistentState } from '../../../utils/stateMemory'

export interface FmFormState {
  selectedDs?: number
  opSet: string[]
  features: string[]
  popSize: number
  gens: number
  horizon: number
  target: string
  algorithm: string
  backendMode: 'auto' | 'cpu' | 'gpu'
}

export const FM_FORM_DEFAULTS: FmFormState = {
  selectedDs: undefined,
  opSet: [],
  features: [],
  popSize: 120,
  gens: 12,
  horizon: 5,
  target: 'ic',
  algorithm: 'gp',
  backendMode: 'auto',
}

export function useFmForm() {
  const [fmForm, setFmForm] = usePersistentState<FmFormState>('fm:state', FM_FORM_DEFAULTS)
  const patch = (p: Partial<FmFormState>) => setFmForm((prev) => ({ ...prev, ...p }))
  return {
    fmForm,
    setSelectedDs: (v: number | undefined) => patch({ selectedDs: v }),
    setOpSet: (v: string[]) => patch({ opSet: v }),
    setFeatures: (v: string[]) => patch({ features: v }),
    setPopSize: (v: number) => patch({ popSize: v }),
    setGens: (v: number) => patch({ gens: v }),
    setHorizon: (v: number) => patch({ horizon: v }),
    setTarget: (v: string) => patch({ target: v }),
    setAlgorithm: (v: string) => patch({ algorithm: v }),
    setBackendMode: (v: 'auto' | 'cpu' | 'gpu') => patch({ backendMode: v }),
  }
}
