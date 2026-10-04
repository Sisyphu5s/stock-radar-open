import { Drawer, Splitter } from '@mantine/core'
import type { SplitterPaneSize } from '@mantine/hooks'
import clsx from 'clsx'
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { WorkbenchCtx } from './workbench/context'
import KlinePanel from './workbench/KlinePanel'
import InspectorPanel from './workbench/InspectorPanel'
import { SignalTimelineView } from './workbench/TimelinePanel'
import { TuneSection } from './workbench/sections'
import { useWorkbenchData } from './workbench/useWorkbenchData'
import { loadHSplit, loadSplit, persistHSplit, persistSplit } from './workbench/persist'
import { useElementSize } from '../../hooks/useElementSize'
import { RESEARCH_SPLIT_STACK_AT } from '../../components/ui/ResearchSplit'
import StockHeader from './stock/StockHeader'
import WorkbenchTabs from './workbench/stock/WorkbenchTabs'
import { useWorkbenchTabs } from '../../stores/workbenchTabs'
import { useKlineFullscreen } from '../../stores/klineFullscreen'
import { HSPLIT_DEFAULT, SPLIT_DEFAULT } from './workbench/persist'
import { toSplitNumbers } from '../../utils/splitPersist'
import '../../styles/workbench.css'

/** 无 URL 参数(旧路径 /market/workbench 兜底)且无任何 tab 时的默认股票 */
const DEFAULT_CODE = '600519.SH'

/**
 * 单只股票的保活工作台实例(T-55)：code 固定来自 props，不随 URL 变化重挂；
 * active=false 时父容器 display:none，但组件保持挂载——周期/指标显隐/时间线滚动/
 * 分割比例等面板状态不丢；非活跃时 useWorkbenchData 各池 enabled=false 停止订阅轮询。
 */
function WorkbenchPane({ code, active, fullscreen }: { code: string; active: boolean; fullscreen: boolean }) {
  const ctx = useWorkbenchData(code, active)

  // T-117 全屏 Esc 退出：仅激活实例监听（多 tab 保活实例全挂载，防多份监听重复退出）；
  // 调优 Drawer 打开时 Esc 交给 Mantine 关抽屉（不误退全屏）——画线 Esc 由引擎 stopPropagation 优先消费
  useEffect(() => {
    if (!fullscreen || !active || ctx.tuneOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') useKlineFullscreen.getState().exit()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [fullscreen, active, ctx.tuneOpen])

  // 行情名称惰性回填 tab 条（行情接口返回前 tab 仅显示 code，返回后补短名）
  const setTabName = useWorkbenchTabs((s) => s.setTabName)
  const quoteName = ctx.quote?.name
  useEffect(() => {
    if (typeof quoteName === 'string' && quoteName) setTabName(code, quoteName)
  }, [quoteName, code, setTabName])

  // 横排/堆叠决策改容器实测宽动态（M3 机制契约，替代 useViewport 媒体查询）：
  // 测量 .sr-wb-layout（两种布局的直接父容器，宽不依赖内部分割比例；若测 .sr-wb-main，
  // 其宽随横向 Splitter 分割比例变化，可能造成分栏/堆叠判定震荡）。
  // 宽 ≥720 横向 Splitter（信号时间线 / K 线主区），<720 纵向堆叠；首帧无测量退分栏态（=旧 ≥992 行为）。
  const { ref: layoutRef, size: layoutSize } = useElementSize<HTMLDivElement>()
  const lastGoodWRef = useRef<number | null>(null)
  const measured = layoutSize.width
  if (measured > 0) lastGoodWRef.current = measured
  const effectiveW = lastGoodWRef.current
  const isDesktop = effectiveW == null || effectiveW >= RESEARCH_SPLIT_STACK_AT

  // 垂直分割（K线 / 下方分析）：onSizeChange 以 rAF 合并 setState（拖拽高频回调只落一次状态），
  // onResizeEnd 冲刷挂起的 rAF 并持久化到 localStorage（D4）
  const [splitSizes, setSplitSizes] = useState<number[]>(loadSplit)
  const splitRafRef = useRef<number | null>(null)
  // Mantine 回调 sizes 单位为声明单位（number=百分比），归一为 number[] 再落状态
  // （防御走共享 toSplitNumbers：仅 number/'%' 串，防 px 串被 parseFloat 污染）
  const toNumbers = (sizes: SplitterPaneSize[]): number[] => toSplitNumbers(sizes)
  const onSplitResize = useCallback((sizes: SplitterPaneSize[]) => {
    if (splitRafRef.current != null) return
    splitRafRef.current = requestAnimationFrame(() => {
      splitRafRef.current = null
      setSplitSizes(toNumbers(sizes))
    })
  }, [])
  const onSplitResizeEnd = useCallback((_handle: number, sizes: SplitterPaneSize[]) => {
    if (splitRafRef.current != null) { cancelAnimationFrame(splitRafRef.current); splitRafRef.current = null }
    const nums = toNumbers(sizes)
    setSplitSizes(nums)
    persistSplit(nums)
  }, [])
  useEffect(() => () => {
    if (splitRafRef.current != null) { cancelAnimationFrame(splitRafRef.current); splitRafRef.current = null }
  }, [])

  // 水平分割（信号时间线 / K 线主区）：与 vertical 同一套 rAF 合并 + 结束后持久化模式
  const [hSplit, setHSplit] = useState<number[]>(loadHSplit)
  const hSplitRafRef = useRef<number | null>(null)
  const onHSplitResize = useCallback((sizes: SplitterPaneSize[]) => {
    if (hSplitRafRef.current != null) return
    hSplitRafRef.current = requestAnimationFrame(() => {
      hSplitRafRef.current = null
      setHSplit(toNumbers(sizes))
    })
  }, [])
  const onHSplitResizeEnd = useCallback((_handle: number, sizes: SplitterPaneSize[]) => {
    if (hSplitRafRef.current != null) { cancelAnimationFrame(hSplitRafRef.current); hSplitRafRef.current = null }
    const nums = toNumbers(sizes)
    setHSplit(nums)
    persistHSplit(nums)
  }, [])
  useEffect(() => () => {
    if (hSplitRafRef.current != null) { cancelAnimationFrame(hSplitRafRef.current); hSplitRafRef.current = null }
  }, [])

  // K 线主区（桌面 Splitter Panel2 与窄屏堆叠块共用）：
  // 内部用 Splitter vertical 分隔 K线图 与 下方分析。
  // pane 类名区分（sr-wb-kline-pane/sr-wb-insp-pane）：全屏时 CSS 隐藏 Inspector 并让 K 线 pane
  // flex 占满——KlinePanel 保持同一树位置，图表单实例零重挂，状态（周期/副图槽位/画线/hover）零丢失
  const klineArea = (
    <div className="sr-wb-main">
      <Splitter
        orientation="vertical"
        className="sr-wb-splitter"
        sizes={splitSizes}
        onSizeChange={onSplitResize}
        onResizeEnd={onSplitResizeEnd}
      >
        {/* min/max 相对值：随容器高度（clamp(480px,70vh,640px)）等比收缩，小屏不溢出。
            defaultSize = 真默认常量（双击 handle 重置目标；sizes 受控承载持久化值，
            二者分离——绑定持久化值会使双击重置变为 no-op） */}
        <Splitter.Pane className="sr-wb-pane sr-wb-kline-pane" defaultSize={SPLIT_DEFAULT[0]} min={28} max={82}>
          <KlinePanel />
        </Splitter.Pane>
        <Splitter.Pane className="sr-wb-pane sr-wb-insp-pane" defaultSize={SPLIT_DEFAULT[1]} min={20} max={62}>
          <InspectorPanel />
        </Splitter.Pane>
      </Splitter>
    </div>
  )

  return (
    <div className={'sr-wb-pane-body' + (fullscreen ? ' sr-wb-fs' : '')}>
      <WorkbenchCtx.Provider value={ctx}>
        {/* T-117 应用内全屏：不换树——StockHeader/时间线/Inspector 经 .sr-wb-fs CSS 隐藏，
            KlinePanel 仍在原 Splitter 槽位（单图表实例放大，状态零丢失）；
            调优 Drawer 保留（全屏下参数调整不缺席） */}
        <StockHeader />
        {/* Mantine Splitter 布局：桌面（lg+）horizontal 左 信号时间线 / 右 K线分析主区，可拖拽、比例持久化；
            窄屏（<992）条件渲染为纵向堆叠（K 线主区在前、信号时间线在后，stock.css order 兜底） */}
        <div
          ref={layoutRef}
          className={clsx('sr-wb-layout', isDesktop ? 'sr-wb-layout-split' : 'sr-wb-layout-stack')}
        >
          {isDesktop ? (
            <Splitter
              orientation="horizontal"
              className="sr-wb-splitter sr-wb-hsplit"
              sizes={hSplit}
              onSizeChange={onHSplitResize}
              onResizeEnd={onHSplitResizeEnd}
            >
              {/* min 相对值（20%≈240px@1200、40%≈480px@1200）：随容器宽度等比收缩，小屏不横向溢出；max 45% 限制时间线最大占比。
                  defaultSize = 真默认常量（双击 handle 重置目标） */}
              <Splitter.Pane className="sr-wb-pane sr-wb-tl-pane" defaultSize={HSPLIT_DEFAULT[0]} min={20} max={45}>
                <div className="sr-wb-side">
                  <SignalTimelineView />
                </div>
              </Splitter.Pane>
              <Splitter.Pane className="sr-wb-pane sr-wb-main-pane" defaultSize={HSPLIT_DEFAULT[1]} min={40}>
                {klineArea}
              </Splitter.Pane>
            </Splitter>
          ) : (
            <>
              <div className="sr-wb-col-main">{klineArea}</div>
              <div className="sr-wb-col-side">
                <div className="sr-wb-side">
                  <SignalTimelineView />
                </div>
              </div>
            </>
          )}
        </div>
        {/* 参数调优：K 线上方的醒目入口 → 右侧 Drawer（不遮挡 K 线，边调边看；
            TuneSection 顶部自带「只影响图表、不影响信号扫描」边界说明） */}
        <Drawer
          opened={ctx.tuneOpen}
          onClose={() => ctx.setTuneOpen(false)}
          title="指标参数调优"
          position="right"
          size="min(640px, calc(100vw - 48px))"
        >
          <TuneSection />
        </Drawer>
      </WorkbenchCtx.Provider>
    </div>
  )
}

/**
 * 个股工作台多 tab 保活壳（T-55）：
 * - 顶部 tab 条（最近访问 LRU，上限 5，可关闭）
 * - 为 tabs 中每个 code 渲染一个保活实例（非活跃 display:none，不重挂 → 状态不丢）
 * - URL（useParams :code / 旧路径 ?code=）作为 open() 触发源：路由 code 变化 → store.open + setActive，
 *   store active 变化（切 tab / 关闭）→ navigate 同步 URL；全部关闭 → 跳 /signals
 */
export default function StockWorkbench() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const { code: pathCode } = useParams<{ code: string }>()
  // 兼容 /stocks/:code（新路由）与 /market/workbench?code=（旧路径）；?code= 优先
  const urlCode = params.get('code') ?? pathCode ?? undefined

  // T-117 K 线全屏：全局 Esc 退出（画线引擎已对「被消费的 Esc」stopPropagation，
  // 画线取消优先于全屏退出，互不误伤）；全屏时隐藏 tab 条（聚焦图表）
  const klineFs = useKlineFullscreen((s) => s.on)

  const tabs = useWorkbenchTabs((s) => s.tabs)
  const activeCode = useWorkbenchTabs((s) => s.activeCode)
  const open = useWorkbenchTabs((s) => s.open)

  // 区分「深链空态首次进入(应打开)」与「关闭全部(应离开)」：本会话实例内从未有 tab 才算空态；
  // 曾有过 tab 后清空 = 关闭全部,不再用旧 URL 重开,交给 store→URL effect 跳 /signals。
  // 按渲染同步刷新(ref 幂等写),供下方 URL→store effect 读取,不依赖 effect 时序。
  const hadTabsRef = useRef(false)
  if (tabs.length > 0 || activeCode != null) hadTabsRef.current = true

  // URL → store：仅响应「外部」URL 变化（深链 / 前进后退 / 他页 navigate）。
  // 关键：不把 activeCode/tabs.length 放进依赖、activeCode 用 getState() 读最新值——
  // 否则点标签页(open 改 activeCode)会触发本 effect 把 store 反向拉回 URL，与下方
  // store→URL effect 互相踩脚形成无限乒乓(T-55 疯狂切换根因)。store 自身导航产生的
  // URL 变化在此与 store 已一致 → 不动作，天然不回环。
  useEffect(() => {
    const active = useWorkbenchTabs.getState().activeCode
    if (!urlCode) {
      // 旧路径(/market/workbench)无 code 参数:空态兜底默认股;有持久化 tab 时交给 store→URL effect 恢复
      if (!hadTabsRef.current) open(DEFAULT_CODE)
      return
    }
    // 深链空态(从未有 tab):打开 URL 对应股票
    if (!hadTabsRef.current) { open(urlCode); return }
    // 常规:URL 与激活不一致才 open(关闭全部后 activeCode=null 不重开)
    if (active != null && urlCode !== active) open(urlCode)
  }, [urlCode, open])

  // store → URL：active 变化（切 tab / 关闭激活 tab / 空态兜底）时同步导航，URL 为纯投影。
  // 只响应 activeCode（不依赖 urlCode，读 urlCode 走 ref 防 stale closure）：
  // 若依赖 urlCode，外部导航到新 code 时本 effect 会反向 navigate 回旧 code，同样形成乒乓。
  // 全部关闭（activeCode=null）→ 回信号中心。
  // 关键：activeCode 用 getState() 读最新值——首次挂载时 URL→store 的 open() 已同步写 store，
  // 若读组件闭包里的 activeCode(初值 null)会误判「无激活」navigate 到 /signals，把首开股票
  // 弹回信号中心(深链空态)。与上方 URL→store effect 同一模式。
  const urlCodeRef = useRef(urlCode)
  urlCodeRef.current = urlCode
  useEffect(() => {
    const active = useWorkbenchTabs.getState().activeCode
    const cur = urlCodeRef.current
    if (!active) {
      if (cur) navigate('/signals', { replace: true })
      return
    }
    if (active !== cur) navigate(`/stocks/${active}`, { replace: true })
  }, [activeCode, navigate])

  return (
    <div className="sr-page sr-page-fill" style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      {!klineFs && <WorkbenchTabs />}
      {/* 保活容器：非活跃实例 display:none 但不卸载，状态全保留 */}
      <div className="sr-wb-panes">
        {tabs.map((t) => (
          <div
            key={t.code}
            className="sr-wb-pane-holder"
            style={activeCode === t.code ? undefined : { display: 'none' }}
          >
            <WorkbenchPane code={t.code} active={activeCode === t.code} fullscreen={klineFs} />
          </div>
        ))}
      </div>
    </div>
  )
}
