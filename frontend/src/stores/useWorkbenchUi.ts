import { create } from 'zustand'
import type { CapitalType } from '../api/capital'

/**
 * 工作台 UI 状态 store(B2):
 * 财务/资金 section 的 Tab 选择提升出组件实例——StockWorkbench 桌面/窄屏两套 JSX
 * 随 useViewport 翻转整树重挂时(拖拽跨 992px 等),组件内部 useState 会复位,改为读
 * 写本 store 后重挂可恢复;多股保活实例共享同一 tab 选择(切股时 tab 保持一致,合理副作用)。
 * 不持久化(会话级 UI 偏好,刷新回默认值即可)。
 */

interface WorkbenchUiState {
  /** 财务历史 section 激活 Tab('overview' | 'balance' | 'income' | 'cash' | 'valuation') */
  financialTab: string
  /** 资金 section 激活 Tab(资金流/龙虎榜/两融/北向) */
  capitalTab: CapitalType
  setFinancialTab: (v: string) => void
  setCapitalTab: (v: CapitalType) => void
}

export const useWorkbenchUi = create<WorkbenchUiState>((set) => ({
  financialTab: 'overview',
  capitalTab: 'moneyflow',
  setFinancialTab: (financialTab) => set({ financialTab }),
  setCapitalTab: (capitalTab) => set({ capitalTab }),
}))
