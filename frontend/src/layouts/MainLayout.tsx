import { ActionIcon, AppShell, Badge, Button, Drawer, Group, Loader, NavLink, Stack, Text, Tooltip } from '@mantine/core'
import {
  IconArrowLeft,
  IconArrowRight,
  IconLayoutSidebarLeftCollapse,
  IconLayoutSidebarLeftExpand,
  IconMenu2,
  IconMoon,
  IconPower,
  IconRefresh,
  IconSearch,
  IconSettings,
  IconSun,
  IconX,
} from '@tabler/icons-react'
import clsx from 'clsx'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useEffect, useRef, useState } from 'react'
import { connectMarketStream, disconnectMarketStream } from '../data/marketStream'
import { getSystemHealth } from '../api/client'
import type { ComponentType } from 'react'
import ErrorBoundary from '../components/ErrorBoundary'
import HermesWidget from '../components/HermesWidget'
import CommandPalette, { OPEN_COMMAND_PALETTE_EVENT } from '../components/CommandPalette'
import StockSearch from '../components/StockSearch'
import BrandLogo from '../components/BrandLogo'
import JobBar from '../components/JobBar'
import WatchlistWidget from '../components/watchlist/WatchlistWidget'
import BottomNav from '../components/layout/BottomNav'
import { useViewport } from '../app/useViewport'
import { useThemeStore } from '../stores/useAppStore'
import { useKlineFullscreen } from '../stores/klineFullscreen'
import { flattenMenuLeaves, menuGroups, type MenuLeaf } from './menu'
import { getDebugMode, runHeadlessChecks, startDebugLayout, toggleDebug } from '../utils/debugLayout'
import type { DebugMode } from '../utils/debug/debugTypes'
import { adapterLabel } from '../utils/routeTitles'

/** 侧栏宽（与 tokens.css --sr-sider-w=200px 对齐）：展开 200 / 折叠图标轨 64 */
const SIDER_W = 200
const SIDER_COLLAPSED_W = 64

/**
 * 逻辑叶子：仅参与高亮/路径标题解析，不渲染进菜单（独立于 flattenMenuLeaves，
 * 避免污染命令面板页面列表）。/stocks/:code 与 /market/workbench → 「个股工作台」。
 */
const LOGICAL_LEAVES: MenuLeaf[] = [{ key: '/stocks', label: '个股工作台' }]

/**
 * 最长前缀匹配选中叶子（与 routeTitle 同思路）：
 * pathname === key 精确命中优先；否则取 startsWith(key + '/') 中 key 最长者——
 * /stocks/600519.SH 命中逻辑叶子 /stocks。
 */
function resolveSelectedLeaf(pathname: string): MenuLeaf | undefined {
  const leaves = [...flattenMenuLeaves(), ...LOGICAL_LEAVES]
  const exact = leaves.find((c) => pathname === c.key)
  if (exact) return exact
  let best: MenuLeaf | undefined
  for (const c of leaves) {
    if (pathname.startsWith(c.key + '/') && (!best || c.key.length > best.key.length)) best = c
  }
  return best
}

/** 侧栏折叠持久化 key（>=1200 用户可控；992-1199 强制图标轨不写此值） */
const NAV_COLLAPSED_KEY = 'sr-nav-collapsed-v2'

function readNavCollapsed(): boolean {
  try { return localStorage.getItem(NAV_COLLAPSED_KEY) === '1' } catch { return false }
}

function writeNavCollapsed(collapsed: boolean) {
  try { localStorage.setItem(NAV_COLLAPSED_KEY, collapsed ? '1' : '0') } catch { /* ignore */ }
}

export default function MainLayout() {
  const navigate = useNavigate()
  const location = useLocation()
  const viewport = useViewport()
  const [drawerOpen, setDrawerOpen] = useState(false)
  // 退出页：系统设置页执行退出后写 localStorage 标记并 reload，本组件挂载时检测
  const [exited] = useState(() => {
    try { return localStorage.getItem('sr-exit-requested') === '1' } catch { return false }
  })
  // 展开态（>=1200）用户可控折叠；992-1199 图标轨、窄屏抽屉不受此值影响
  const [userCollapsed, setUserCollapsed] = useState<boolean>(readNavCollapsed)
  const theme = useThemeStore((st) => st.theme)
  const toggleTheme = useThemeStore((st) => st.toggleTheme)
  // T-117 K 线全屏：隐藏壳层 chrome（侧栏/顶栏/底导），图表区占满视口；类驱动，AppShell 结构不动
  const klineFs = useKlineFullscreen((s) => s.on)
  const [refreshing, setRefreshing] = useState(false)
  // 刷新 loading 的 500ms 复位定时器：卸载时清理（避免卸载后 setState）
  const refreshTimerRef = useRef<number | null>(null)
  // Debug 三态（off/visual/headless）：初始按 localStorage 判定，事件/路由变化实时同步
  const [debugMode, setDebugModeState] = useState<DebugMode>(getDebugMode)
  // visual 懒加载的 UI 组件（fe-dbg-ui）；未就绪时渲染内置面板兜底
  const [debugUi, setDebugUi] = useState<{ Panel: ComponentType; Overlay: ComponentType } | null>(null)

  // 最长前缀匹配选中项（精确命中优先；/stocks/:code → 逻辑叶子 /stocks）
  const selected = resolveSelectedLeaf(location.pathname) ?? { key: '', label: '' }

  // T-55 路由级重挂 key：/stocks/:code 稳定（个股工作台多 tab 保活，切股不重挂），
  // /research 八 tab 稳定（T-126：FactorWorkbench 内部 visited set + display 保持已访问面板
  // 挂载，切 tab 不丢表单/轮询/结果状态；/research/runs/:id 详情页不在其列），
  // 其余路由随 pathname（路由变化重挂，行为不变）
  const isResearchTab = /^\/research(\/(datasets|discovery|neural|evaluation|backtests|factors|alpha101|combine))?$/.test(location.pathname)
  const routeKey = /^\/stocks\/[^/]+$/.test(location.pathname) ? 'stocks' : isResearchTab ? 'research' : location.pathname

  // 层级路径：菜单已全扁平，选中叶子直接显示自身；适配路由/无选中项时兜底
  const pathLabel = adapterLabel(location.pathname)
    ?? (selected?.key ? selected.label : 'Stock Radar')

  // 前进/后退可用性：react-router 在 history.state.idx 记录当前栈位置。
  // window.history.length 是浏览器全局会话历史（含进入 SPA 前的页面），直接
  // 用「idx < length-1」判前进，会在外部页面残留于 SPA 前方时误开前进按钮，
  // 把用户带出应用。改为本地 ref 记录应用挂载以来到达过的最大 idx（spaMaxRef）：
  // 只有「当前位置之后还有本应用 push 过的条目」才允许前进，前进只在本应用栈内。
  const navIdx = typeof window.history.state?.idx === 'number' ? window.history.state.idx : 0
  const spaMaxRef = useRef<number | null>(null)
  useEffect(() => {
    if (spaMaxRef.current == null || navIdx > spaMaxRef.current) spaMaxRef.current = navIdx
  }, [navIdx])
  const canGoBack = navIdx > 0
  const canGoForward = spaMaxRef.current != null && navIdx < spaMaxRef.current

  const handleGlobalRefresh = () => {
    window.dispatchEvent(new CustomEvent('sr-refresh'))
    setRefreshing(true)
    if (refreshTimerRef.current != null) window.clearTimeout(refreshTimerRef.current)
    refreshTimerRef.current = window.setTimeout(() => setRefreshing(false), 500)
  }

  // 行情推送接线(T-04):壳层常驻订阅,引用计数归零自动断开;推送活跃时行情轮询自动放宽
  useEffect(() => {
    connectMarketStream()
    return () => disconnectMarketStream()
  }, [])

  useEffect(() => () => {
    if (refreshTimerRef.current != null) window.clearTimeout(refreshTimerRef.current)
  }, [])

  useEffect(() => {
    // 路由变化时关闭抽屉
    setDrawerOpen(false)
  }, [location.pathname])

  // Ctrl+Shift+D 切换 debug（visual↔off）；监听 'sr-debug-mode' 同步三态
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && (e.key === 'D' || e.key === 'd')) {
        e.preventDefault()
        toggleDebug()
      }
    }
    const onMode = (e: Event) => {
      const m = (e as CustomEvent).detail?.mode
      if (m === 'off' || m === 'visual' || m === 'headless') setDebugModeState(m)
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('sr-debug-mode', onMode)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('sr-debug-mode', onMode)
    }
  }, [])

  // visual：懒加载 DebugPanel/DebugOverlay（fe-dbg-ui，默认导出）；加载失败回退内置面板
  useEffect(() => {
    if (debugMode !== 'visual') {
      setDebugUi(null)
      return
    }
    let alive = true
    Promise.all([
      import('../components/debug/DebugPanel'),
      import('../components/debug/DebugOverlay'),
    ])
      .then(([panel, overlay]) => {
        if (alive) setDebugUi({ Panel: panel.default, Overlay: overlay.default })
      })
      .catch(() => { /* import 失败：保持 null，渲染内置面板兜底 */ })
    return () => { alive = false }
  }, [debugMode])

  // visual：启动边缘检测（清理函数由 React 卸载时调用）
  useEffect(() => {
    if (debugMode !== 'visual') return
    return startDebugLayout()
  }, [debugMode])

  // headless：不启动贴边 outline（无视觉），仅跑检查一次 + 每 10s 防抖重跑，console 分组输出
  useEffect(() => {
    if (debugMode !== 'headless') return
    const run = () => { if (document.visibilityState === 'hidden') return; runHeadlessChecks() }
    run()
    const id = window.setInterval(run, 10000)
    return () => window.clearInterval(id)
  }, [debugMode])

  // 路由/query 变化（含同文档 hash 导航 ?sr_debug=1）时同步 debug 状态
  useEffect(() => {
    setDebugModeState(getDebugMode())
  }, [location])

  const showSider = viewport === 'expanded' || viewport === 'rail'
  const showDrawerTrigger = viewport === 'drawer' || viewport === 'mobile'
  // >=1200 按用户偏好；992-1199 自动图标轨（收起）；窄屏不渲染侧栏
  const isExpanded = viewport === 'expanded'
  const siderCollapsed = isExpanded ? userCollapsed : true
  // AppShell navbar 宽度：仅展开态跟随用户折叠偏好，其余形态图标轨 64
  const navWidth = isExpanded ? (userCollapsed ? SIDER_COLLAPSED_W : SIDER_W) : SIDER_COLLAPSED_W

  const toggleSiderCollapsed = () => {
    setUserCollapsed((c) => {
      const next = !c
      writeNavCollapsed(next)
      return next
    })
  }

  /** 侧栏/抽屉共用菜单：collapsed=true 时隐藏文字仅图标（图标轨）且隐藏分组标题；false 恒展开（含分组标题） */
  const renderNavMenu = (collapsed: boolean) => (
    <div className="sr-nav-menu">
      {menuGroups.map((g) => (
        <div key={g.key} className="sr-nav-group">
          {!collapsed && (
            <span
              className="sr-nav-group-title"
              style={{
                display: 'block',
                padding: '10px 16px 2px',
                fontSize: 'var(--sr-font-xs)',
                fontWeight: 600,
                letterSpacing: '0.04em',
                color: 'var(--sr-text-3)',
              }}
            >
              {g.title}
            </span>
          )}
          {g.items.map((item) => (
            <NavLink
              key={item.key}
              className="sr-nav-link"
              variant="subtle"
              label={collapsed ? undefined : item.label}
              leftSection={item.icon}
              active={selected.key === item.key}
              onClick={() => { navigate(item.key); setDrawerOpen(false) }}
              classNames={{ section: 'sr-nav-link-section' }}
            />
          ))}
        </div>
      ))}
    </div>
  )

  /** 侧栏底部固定区：仅折叠/展开按钮（系统设置/状态已并入系统菜单组，T-46 不再驻底） */
  const renderSiderFoot = () => (
    <div className="sr-sider-foot">
      <Tooltip label={siderCollapsed ? '展开侧栏' : '收起侧栏'} position="right">
        <Button
          variant="subtle" size="xs"
          leftSection={siderCollapsed ? <IconLayoutSidebarLeftExpand size={16} /> : <IconLayoutSidebarLeftCollapse size={16} />}
          onClick={toggleSiderCollapsed}
          className="sr-sider-foot-btn"
          aria-label={siderCollapsed ? '展开侧栏' : '收起侧栏'}
          classNames={{ label: 'sr-foot-btn-label' }}
        >
          <span>{siderCollapsed ? '展开侧栏' : '收起侧栏'}</span>
        </Button>
      </Tooltip>
    </div>
  )

  // T-81 版本自检：health.frontend_synced===false → 前端构建落后于后端代码(部署管道断裂),
  // 页内横幅提示而非静默崩坏;dev 环境字段为 null → 不显示。30s 轮询(发布后自动消失)。
  // P1-69 修复:状态/effect 前置到条件 return(exited 退出页)之前,保持 Rules of Hooks
  const [versionMismatch, setVersionMismatch] = useState(false)
  useEffect(() => {
    let cancelled = false
    const check = async () => {
      if (document.visibilityState === 'hidden') return // 后台不轮询,回前台由下个 tick 续上
      try {
        const h = await getSystemHealth()
        if (!cancelled) setVersionMismatch(h.frontend_synced === false)
      } catch { /* 后端不可达等场景不打扰用户 */ }
    }
    void check()
    const t = window.setInterval(() => void check(), 30_000)
    return () => { cancelled = true; window.clearInterval(t) }
  }, [])

  if (exited) {
    return (
      <div className="sr-exit-bg">
        <Stack align="center" gap={6}>
          <IconPower size={44} color="var(--sr-text-1)" aria-hidden />
          <Text fw={700} size="lg" c="var(--sr-text-1)">系统已安全退出</Text>
          <Text size="sm" c="var(--sr-text-2)">市场扫描已停止，数据库连接已释放</Text>
          <Group gap={10} mt={10}>
            <Badge color="blue" variant="light" style={{ fontSize: 13, padding: '4px 14px', fontWeight: 400 }}>
              重新启动: 运行 README 中的后端命令
            </Badge>
            <Button
              variant="filled"
              leftSection={<IconPower size={14} />}
              onClick={() => {
                try { localStorage.removeItem('sr-exit-requested') } catch { /* ignore */ }
                window.location.reload()
              }}
            >
              重新进入
            </Button>
          </Group>
        </Stack>
      </div>
    )
  }

  return (
    <AppShell
      layout="alt"
      header={{ height: 'calc(var(--sr-header-h) + var(--sr-safe-top))' }}
      navbar={showSider ? { width: navWidth, breakpoint: 'md', collapsed: { mobile: true } } : undefined}
      padding={0}
      withBorder={false}
      className={clsx('sr-shell', klineFs && 'sr-kline-fs')}
    >
      {showSider && (
        <AppShell.Navbar className={clsx('sr-sider', siderCollapsed && 'sr-nav-collapsed')}>
          <div className="sr-sider-brand">
            <BrandLogo collapsed={siderCollapsed} />
          </div>
          {renderNavMenu(siderCollapsed)}
          {renderSiderFoot()}
        </AppShell.Navbar>
      )}
      <AppShell.Header className="sr-header">
        {showDrawerTrigger ? (
          <Tooltip label="打开导航">
            <ActionIcon variant="subtle" size="sm" className="sr-header-ico" aria-label="打开导航" onClick={() => setDrawerOpen(true)}>
              <IconMenu2 size={15} />
            </ActionIcon>
          </Tooltip>
        ) : (
          <>
            <Tooltip label="后退">
              <ActionIcon variant="subtle" size="sm" className="sr-header-ico" aria-label="后退" disabled={!canGoBack} onClick={() => navigate(-1)}>
                <IconArrowLeft size={15} />
              </ActionIcon>
            </Tooltip>
            <Tooltip label="前进">
              <ActionIcon variant="subtle" size="sm" className="sr-header-ico" aria-label="前进" disabled={!canGoForward} onClick={() => navigate(1)}>
                <IconArrowRight size={15} />
              </ActionIcon>
            </Tooltip>
          </>
        )}
        {/* 路径显示：语义 h1，作为所有页面唯一可见名称（PageHeader 不再渲染标题） */}
        <h1 className="sr-header-path" title={pathLabel}>
          {pathLabel}
        </h1>
        <div className="sr-header-flex" />
        {/* 股票搜索：窄屏自动收缩 */}
        <div className="sr-header-search">
          <StockSearch />
        </div>
        {/* 命令面板可见入口：dispatch 事件由 CommandPalette 监听打开（Cmd+K 快捷键机制不变）；
            <768 隐藏防顶栏溢出（移动端已无余量容纳额外图标） */}
        <Tooltip label="命令面板（⌘K）">
          <ActionIcon
            variant="subtle" size="sm"
            className="sr-header-ico sr-hide-sm"
            aria-label="命令面板"
            onClick={() => window.dispatchEvent(new CustomEvent(OPEN_COMMAND_PALETTE_EVENT))}
          >
            <IconSearch size={15} />
          </ActionIcon>
        </Tooltip>
        {/* 全局刷新（真实刷新：广播 sr-refresh 事件） */}
        <Tooltip label="刷新当前页面数据">
          <ActionIcon
            variant="subtle" size="sm"
            className="sr-header-ico"
            aria-label="刷新当前页面数据"
            onClick={handleGlobalRefresh}
          >
            {refreshing ? <Loader size={14} /> : <IconRefresh size={15} />}
          </ActionIcon>
        </Tooltip>
        {/* 单一任务入口（含运行中徽标） */}
        <JobBar />
        {/* 主题切换 */}
        <Tooltip label={theme === 'dark' ? '切换到白天模式' : '切换到夜间模式'}>
          <ActionIcon
            variant="subtle" size="sm"
            className="sr-header-ico"
            aria-label={theme === 'dark' ? '切换到白天模式' : '切换到夜间模式'}
            onClick={toggleTheme}
          >
            {theme === 'dark' ? <IconSun size={15} /> : <IconMoon size={15} />}
          </ActionIcon>
        </Tooltip>
        {/* 设置（<768 由底部导航提供，顶栏隐藏以节省空间） */}
        <Tooltip label="系统设置">
          <ActionIcon
            variant="subtle" size="sm"
            className="sr-header-ico sr-hide-sm"
            aria-label="系统设置"
            onClick={() => navigate('/settings')}
          >
            <IconSettings size={15} />
          </ActionIcon>
        </Tooltip>
      </AppShell.Header>
      <AppShell.Main className="sr-main">
        {versionMismatch && (
          <div
            role="alert"
            data-testid="sr-version-mismatch"
            style={{
              padding: '6px 12px', fontSize: 'var(--sr-font-sm)',
              background: 'var(--sr-warning-bg, color-mix(in srgb, var(--sr-warning) 15%, transparent))',
              color: 'var(--sr-warning)', borderBottom: '1px solid var(--sr-border)',
              textAlign: 'center',
            }}
          >
            前端构建落后于后端代码,页面可能异常。请稍后刷新;若持续出现请联系管理员重建前端(dist)。
          </div>
        )}
        <div className="sr-content">
          {/* 页面级错误边界：单页崩溃不影响布局；路由切换时自动重置 */}
          {/* T-55 key 改造：/stocks/:code 用稳定 key（切股不重挂，由工作台内多 tab 保活机制承接）；
              其余路由保持 pathname key，路由变化仍重挂，原行为不变 */}
          <ErrorBoundary key={routeKey}>
            <div key={routeKey}>
              <Outlet />
            </div>
          </ErrorBoundary>
        </div>
      </AppShell.Main>
      <Drawer.Root
        opened={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        position="left"
        size="var(--sr-nav-drawer-w)"
        className="sr-nav-drawer"
      >
        <Drawer.Overlay />
        <Drawer.Content className="sr-drawer-content">
          <Drawer.Body className="sr-drawer-body">
            {/* 头部：品牌 + 右上角显式关闭按钮（withCloseButton=false 无内建关闭，按钮样式见 layout.css） */}
            <div className="sr-drawer-head">
              <div className="sr-sider-brand">
                <BrandLogo />
              </div>
              <ActionIcon
                variant="subtle" size="sm"
                className="sr-drawer-close"
                aria-label="关闭菜单"
                onClick={() => setDrawerOpen(false)}
              >
                <IconX size={16} />
              </ActionIcon>
            </div>
            {renderNavMenu(false)}
          </Drawer.Body>
        </Drawer.Content>
      </Drawer.Root>
      <HermesWidget />
      <CommandPalette />
      <WatchlistWidget />
      {viewport === 'mobile' && <BottomNav />}
      {/* Debug 三态挂载：off 零成本；visual 挂 UI 版（未就绪回退内置面板）；headless 不渲染任何 UI */}
      {debugMode === 'visual' && (
        debugUi
          ? (
            <>
              <debugUi.Panel />
              <debugUi.Overlay />
            </>
          )
          : (
            <div className="sr-debug-panel" role="status" aria-live="polite">
              <span className="sr-debug-panel-title">Debug 布局检测中</span>
              <span className="sr-debug-panel-sub">红色=紧贴边缘 · Ctrl+Shift+D 关闭</span>
            </div>
          )
      )}
    </AppShell>
  )
}
