import { lazy, Suspense, useEffect, useState } from 'react'
import type { ComponentType, ReactNode } from 'react'
import { Tabs } from '@mantine/core'
import { useLocation, useNavigate, useParams } from 'react-router-dom'
import RouteFallback from '../../app/RouteFallback'
import './workbench.css'
/* 研究域共享样式（扁平面板 .sr-run-panel 等）：壳恒挂载，保证任意 tab 冷启动样式完整（修跨 chunk 依赖） */
import './shared/research-shared.css'

/** 路由级 code splitting：八个研究页独立 chunk，切 tab 时加载 */
const load = (f: () => Promise<{ default: ComponentType }>) => lazy(f)
const DatasetsPage = load(() => import('./Datasets'))
const FactorMining = load(() => import('./FactorMining'))
const FactorEvaluation = load(() => import('./FactorEvaluation'))
const Backtest = load(() => import('./Backtest'))
const FactorLibrary = load(() => import('./FactorLibrary'))
const Alpha101 = load(() => import('./Alpha101'))
const NeuralStudy = load(() => import('./NeuralStudy'))
const Combine = load(() => import('./combine/Combine'))

/** 工作台流程顺序：数据集 → 因子挖掘 → 神经网络 → 因子评估 → 回测 → 因子库 → Alpha101 库 → 因子合成 */
const STEPS = [
  { key: 'datasets', label: '数据集' },
  { key: 'discovery', label: '因子挖掘' },
  { key: 'neural', label: '神经网络' },
  { key: 'evaluation', label: '因子评估' },
  { key: 'backtests', label: '回测' },
  { key: 'factors', label: '因子库' },
  { key: 'alpha101', label: 'Alpha101 库' },
  { key: 'combine', label: '因子合成' },
] as const

export type ResearchTabKey = (typeof STEPS)[number]['key']

const isTabKey = (v: string | null | undefined): v is ResearchTabKey =>
  !!v && STEPS.some((s) => s.key === v)

/** 从 URL query 读取合法 tab（旧 ?tab= 兼容；缺省/非法返回 null） */
function tabFromSearch(search: string): ResearchTabKey | null {
  const v = new URLSearchParams(search).get('tab')
  return isTabKey(v) ? v : null
}

/** 最近访问 tab（sessionStorage 单一事实源）：底导「研究」/侧栏「因子研究」落点 */
const LAST_TAB_KEY = 'sr-research-last-tab'
const readLastTab = (): ResearchTabKey | null => {
  try {
    const v = sessionStorage.getItem(LAST_TAB_KEY)
    return isTabKey(v) ? v : null
  } catch { return null }
}
const writeLastTab = (tab: ResearchTabKey) => {
  try { sessionStorage.setItem(LAST_TAB_KEY, tab) } catch { /* ignore */ }
}

const l = (node: ReactNode) => (
  <Suspense fallback={<RouteFallback />}>
    {node}
  </Suspense>
)

/**
 * 因子工作台：统一研究流程页。
 * 七步 Tabs 是唯一导航（子页面内嵌 ResearchFlowBar 仅保留上下文芯片，见 stepsHidden）。
 * tab 状态：/research/:tab 路径参数为单一事实源（可寻址）；旧 ?tab= query 兼容迁移；
 * 无路径参数直访（/research，菜单/底导入口）按 最近访问 tab（sessionStorage）落位并规范化为路径。
 * 注意：evaluation/backtests 子页面自写 URL 参数只动 search，路径 tab 不受影响，无需旧「仅 URL 显式 tab 才响应」守卫。
 * Mantine Tabs 仅承担导航头；面板区由 visited set + display 控制——访问过的 tab 保持挂载，
 * 切 tab 不丢运行中任务的监控上下文（useJobFlow 轮询 / useJobEvents SSE 订阅 / 表单态等内存态）。
 */
export default function FactorWorkbench() {
  const navigate = useNavigate()
  const location = useLocation()
  const { tab: tabParam } = useParams<{ tab?: string }>()
  const [tab, setTab] = useState<ResearchTabKey>(() => {
    if (isTabKey(tabParam)) return tabParam
    const q = tabFromSearch(location.search)
    if (q) return q
    return readLastTab() ?? 'datasets'
  })
  // 访问过的 tab 保持挂载（初始 = 当前 tab；切 tab 时 add；永不卸载已挂载面板）
  const [visited, setVisited] = useState<Set<ResearchTabKey>>(() => new Set([tab]))

  // 路径参数变化（顶栏 JobBar 深链 / 浏览器前进后退）→ 同步 tab
  useEffect(() => {
    if (isTabKey(tabParam) && tabParam !== tab) setTab(tabParam)
  }, [tabParam]) // eslint-disable-line react-hooks/exhaustive-deps

  // 落位即记录最近访问 tab（含 URL 直访/深链/前进后退）：底导「研究」下次直访落点
  useEffect(() => {
    writeLastTab(tab)
  }, [tab])

  // 无路径参数（/research 直访）：规范化为 /research/:tab（保留其余 query，删除旧 ?tab=）
  useEffect(() => {
    if (isTabKey(tabParam)) return
    const sp = new URLSearchParams(location.search)
    sp.delete('tab')
    navigate({ pathname: `/research/${tab}`, search: sp.toString() }, { replace: true })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const goTab = (key: ResearchTabKey) => {
    setTab(key)
    setVisited((prev) => new Set(prev).add(key))
    const sp = new URLSearchParams(location.search)
    sp.delete('tab')
    navigate({ pathname: `/research/${key}`, search: sp.toString() })
  }

  return (
    <div className="sr-wb">
      <Tabs
        value={tab}
        className="sr-wb-tabs"
        /* 只做导航头：不设 Tabs.Panel（Mantine Panel 失活即卸载），面板区由 .sr-wb-panels 保持挂载 */
        onChange={(k) => { if (k != null) goTab(k as ResearchTabKey) }}
        classNames={{ list: 'sr-wb-tabs-list' }}
      >
        <Tabs.List>
          {STEPS.map((s) => (
            <Tabs.Tab key={s.key} value={s.key}>{s.label}</Tabs.Tab>
          ))}
        </Tabs.List>
      </Tabs>
      <div className="sr-wb-panels">
        {STEPS.filter((s) => visited.has(s.key)).map((s) => (
          <div key={s.key} className="sr-wb-tab" style={{ display: tab === s.key ? undefined : 'none' }}>
            {s.key === 'datasets' ? l(<DatasetsPage />)
              : s.key === 'discovery' ? l(<FactorMining />)
                : s.key === 'neural' ? l(<NeuralStudy />)
                  : s.key === 'evaluation' ? l(<FactorEvaluation />)
                    : s.key === 'backtests' ? l(<Backtest />)
                      : s.key === 'factors' ? l(<FactorLibrary />)
                        : s.key === 'combine' ? l(<Combine />)
                          : l(<Alpha101 />)}
          </div>
        ))}
      </div>
    </div>
  )
}
