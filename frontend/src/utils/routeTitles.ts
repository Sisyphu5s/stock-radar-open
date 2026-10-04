/**
 * 适配路由（路径参数 → query）的标题映射：单一事实源。
 * MainLayout 顶栏路径标题 与 Copilot 页面快照/上下文标题共用，消除双份映射不一致
 * （原 MainLayout.adapterLabel + ChatPanel.ROUTE_TITLES/routeTitle）。
 * 语义 = 精确路径优先 + 动态前缀（等价于原 routeTitle 最长前缀匹配；无命中返回 null 由调用方兜底）。
 */
const ROUTE_TITLES: Record<string, string> = {
  // 系统页（固定渲染于侧栏/抽屉底部，不在菜单树中）
  '/settings': '系统设置',
  '/status': '系统状态',
  // 条件选股（T-17）
  '/screener': '条件选股',
  // 多股对比（工具页，不入菜单；入口 = 关注页「多股对比」按钮）
  '/compare': '多股对比',
  // 个股工作台：精确（命令面板 /stocks 逻辑叶子）+ 动态（/stocks/:code）
  '/stocks': '个股工作台',
  '/market/workbench': '个股工作台',
  // 任务详情：动态（/research/runs/:jobId）+ 旧路径别名（/research/gp-evolve）
  '/research/gp-evolve': '任务详情',
  // 研究子页（/research/:tab 子路由 → 工作台 tab 名，与 FactorWorkbench STEPS 标签一致）
  '/research/backtests': '回测',
  '/research/evaluation': '因子评估',
  '/research/discovery': '因子挖掘',
  '/research/datasets': '数据集',
  '/research/factors': '因子库',
  '/research/alpha101': 'Alpha101 库',
  '/research/neural': '神经网络',
  '/research/combine': '因子合成',
}

/** 适配路由标题：精确命中优先，动态前缀次之；无命中返回 null（调用方回退 pathname / 菜单层级） */
export function adapterLabel(pathname: string): string | null {
  if (pathname in ROUTE_TITLES) return ROUTE_TITLES[pathname]
  if (pathname.startsWith('/stocks/')) return '个股工作台'
  if (pathname.startsWith('/research/runs/')) return '任务详情'
  return null
}

/** 市场域路由判定：/market、/signals、/watchlist、/screener、/stocks（含动态 /stocks/:code）均属市场会话。
 *  Copilot 会话模式 / 页面快照域共用（原 ChatPanel 与 CopilotWindow 各一份逐字重复）。 */
export function isMarketPath(pathname: string): boolean {
  return pathname.startsWith('/market') || pathname.startsWith('/signals')
    || pathname.startsWith('/watchlist') || pathname.startsWith('/screener')
    || pathname.startsWith('/stocks')
}
