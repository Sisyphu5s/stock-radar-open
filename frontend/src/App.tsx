import { lazy, Suspense } from 'react'
import type { ComponentType } from 'react'
import { HashRouter, Navigate, Route, Routes } from 'react-router-dom'
import MainLayout from './layouts/MainLayout'
import RouteFallback from './app/RouteFallback'
import QueryRedirect from './app/adapters/QueryRedirect'

/** 路由级 code splitting：每个页面独立 chunk，加载期间显示 RouteFallback */
const load = (f: () => Promise<{ default: ComponentType }>) => lazy(f)

const SignalCenter = load(() => import('./pages/market/SignalCenter'))
const WatchlistPage = load(() => import('./pages/market/WatchlistPage'))
const ScreenerPage = load(() => import('./pages/market/screener/ScreenerPage'))
const ComparePage = load(() => import('./pages/market/compare/ComparePage'))
const StockWorkbench = load(() => import('./pages/market/StockWorkbench'))
const PaperTrading = load(() => import('./pages/market/PaperTrading'))
const Tasks = load(() => import('./pages/research/Tasks'))
const GpEvolve = load(() => import('./pages/research/GpEvolve'))
const FactorWorkbench = load(() => import('./pages/research/FactorWorkbench'))
const LlmCopilot = load(() => import('./pages/settings/LlmCopilot'))
const DataConnections = load(() => import('./pages/settings/DataConnections'))
const SystemSettings = load(() => import('./pages/settings/SystemSettings'))
const SystemStatus = load(() => import('./pages/settings/SystemStatus'))

/** 页级懒加载包装 */
const L = (Comp: ComponentType) => (
  <Suspense fallback={<RouteFallback />}>
    <Comp />
  </Suspense>
)

export default function App() {
  return (
    <HashRouter>
      <Routes>
          <Route element={<MainLayout />}>
            {/* ===== 新路由体系（主） ===== */}
            <Route path="/" element={<Navigate to="/signals" replace />} />
            <Route path="/market" element={<Navigate to="/signals" replace />} />
            <Route path="/signals" element={L(SignalCenter)} />
            <Route path="/watchlist" element={L(WatchlistPage)} />
            <Route path="/screener" element={L(ScreenerPage)} />
            <Route path="/compare" element={L(ComparePage)} />
            <Route path="/stocks/:code" element={L(StockWorkbench)} />
            <Route path="/paper" element={L(PaperTrading)} />
            {/* 旧组合仪表盘路由（T-46 模拟盘合并）→ 模拟盘账户视图；query 原样保留 */}
            <Route path="/paper/portfolio" element={<QueryRedirect to="/paper?view=portfolio" />} />
            <Route path="/tasks" element={L(Tasks)} />
            <Route path="/research/:tab?" element={L(FactorWorkbench)} />
            {/* 详情页不进工作台，保留独立路由 */}
            <Route path="/research/runs/:jobId" element={L(GpEvolve)} />
            <Route path="/settings" element={L(SystemSettings)} />
            <Route path="/status" element={L(SystemStatus)} />

            {/* ===== 旧路径别名（保留：任务结果跳转 jobResultPath / 深链仍指向旧路径；query 原样携带） ===== */}
            <Route path="/market/radar" element={<QueryRedirect to="/signals" />} />
            <Route path="/market/signals" element={<QueryRedirect to="/signals" />} />
            <Route path="/market/watchlist" element={<QueryRedirect to="/watchlist" />} />
            <Route path="/market/workbench" element={L(StockWorkbench)} />
            <Route path="/research/gp-evolve" element={L(GpEvolve)} />
            <Route path="/research/tasks" element={<QueryRedirect to="/tasks" />} />
            <Route path="/research/factor-mining" element={<QueryRedirect to="/research/discovery" />} />
            <Route path="/research/factor-library" element={<QueryRedirect to="/research/factors" />} />
            <Route path="/research/factor-eval" element={<QueryRedirect to="/research/evaluation" />} />
            <Route path="/research/alpha" element={<QueryRedirect to="/research/backtests" />} />
            <Route path="/settings/system" element={<QueryRedirect to="/settings" />} />
            <Route path="/settings/copilot" element={L(LlmCopilot)} />
            <Route path="/settings/data" element={L(DataConnections)} />

            {/* 未知路径 → 信号中心 */}
            <Route path="*" element={<Navigate to="/signals" replace />} />
          </Route>
        </Routes>
    </HashRouter>
  )
}
