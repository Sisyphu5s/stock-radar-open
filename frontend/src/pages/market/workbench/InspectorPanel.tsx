import { Button } from '@mantine/core'
import { IconChartBar, IconChartCandle, IconChartPie, IconShieldCheck, IconWorld } from '@tabler/icons-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { CapitalSection, FinancialSection, FundamentalSection, NewsSection, RiskSection } from './sections'
import './InspectorPanel.css'

/** 分析区五个 section 的锚点导航配置（单一事实源：锚点按钮渲染 + 高亮监听 + 滚动目标共用）。
    风险置顶为第一项（T-66 用户裁决：删综合概览） */
const SECTIONS: { key: string; label: string; icon: ReactNode }[] = [
  { key: 'risk', label: '风险', icon: <IconShieldCheck size={14} /> },
  { key: 'fund', label: '基本面', icon: <IconChartBar size={14} /> },
  { key: 'fin', label: '财务', icon: <IconChartPie size={14} /> },
  { key: 'capital', label: '资金', icon: <IconChartCandle size={14} /> },
  { key: 'news', label: '新闻', icon: <IconWorld size={14} /> },
]
/** key → section 锚点 id（scrollIntoView 目标） */
const SECTION_ID: Record<string, string> = Object.fromEntries(SECTIONS.map((s) => [s.key, `inspector-${s.key}`]))
/** IO rootMargin 顶部固定安全值：锚点条实际高（--sr-wb-anchor-h，CSS 单一事实源：亮 37 / 暗 33）+ 冗余 ≈ 56px；
    落点偏移与高亮判定一致性由 CSS scroll-margin-top: calc(var(--sr-wb-anchor-h) + 8px) 联动（InspectorPanel.css） */
const IO_ROOT_MARGIN = '-56px 0px -55% 0px'

/**
 * 分析 inspector（K 线下方区域，Splitter Panel2）：单页滚动流。
 * 五个 section（风险/基本面/财务/资金/新闻）纵向连续渲染于单一滚动容器，
 * 顶部 sticky 锚点条：点击 scrollIntoView 平滑滚动，滚动中 IntersectionObserver 高亮当前 section。
 * 风险置顶为第一项（T-66 删综合概览）；细分 section 保留独立接口与入口；
 * 参数调优为 K 线图上方 Drawer 入口，不占用面板。
 */
export default function InspectorPanel() {
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const [active, setActive] = useState(SECTIONS[0].key)

  const scrollTo = useCallback((key: string) => {
    setActive(key)
    scrollerRef.current
      ?.querySelector(`#${SECTION_ID[key]}`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [])

  // 滚动高亮：IntersectionObserver 监听五个 section，锚点条下方「顶部区域」内最靠上的 section 为当前
  useEffect(() => {
    const scroller = scrollerRef.current
    if (!scroller || typeof IntersectionObserver === 'undefined') return
    const secs = SECTIONS
      .map(({ key }) => ({ key, el: scroller.querySelector<HTMLElement>(`#${SECTION_ID[key]}`) }))
      .filter((x): x is { key: string; el: HTMLElement } => x.el != null)
    if (!secs.length) return
    const io = new IntersectionObserver(
      (entries) => {
        const visible = entries.filter((e) => e.isIntersecting && e.target instanceof HTMLElement)
        if (!visible.length) return
        // 取 top 最小（最接近滚动容器顶部）的 section（visible 已保证非空）
        const cur = visible.reduce<IntersectionObserverEntry>((min, e) =>
          e.boundingClientRect.top < min.boundingClientRect.top ? e : min, visible[0]!)
        const key = (cur.target as HTMLElement).dataset.key
        if (key) setActive(key)
      },
      { root: scroller, rootMargin: IO_ROOT_MARGIN, threshold: 0 },
    )
    secs.forEach(({ el }) => io.observe(el))
    return () => io.disconnect()
  }, [])

  // 滚到底兜底：末节（新闻）内容不足一屏时 IO 判不出 intersecting，显式高亮最后一项
  useEffect(() => {
    const scroller = scrollerRef.current
    if (!scroller) return
    const onScroll = () => {
      if (scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 4) {
        setActive(SECTIONS[SECTIONS.length - 1].key)
      }
    }
    scroller.addEventListener('scroll', onScroll, { passive: true })
    return () => scroller.removeEventListener('scroll', onScroll)
  }, [])

  return (
    <div
      ref={scrollerRef}
      className="sr-wb-inspector"
      style={{ height: '100%', minHeight: 0, minWidth: 0, display: 'flex', flexDirection: 'column', overflowY: 'auto', padding: '8px 10px' }}
    >
      <nav className="sr-wb-anchor" aria-label="分析区导航">
        {SECTIONS.map(({ key, label, icon }) => (
          <Button
            key={key}
            size="xs"
            leftSection={icon}
            variant={active === key ? 'filled' : 'subtle'}
            className="sr-wb-anchor-btn"
            onClick={() => scrollTo(key)}
          >
            {label}
          </Button>
        ))}
      </nav>
      <section id="inspector-risk" className="sr-wb-sec" data-key="risk">
        <RiskSection />
      </section>
      <section id="inspector-fund" className="sr-wb-sec" data-key="fund">
        <FundamentalSection />
      </section>
      <section id="inspector-fin" className="sr-wb-sec" data-key="fin">
        <FinancialSection />
      </section>
      <section id="inspector-capital" className="sr-wb-sec" data-key="capital">
        <CapitalSection />
      </section>
      <section id="inspector-news" className="sr-wb-sec" data-key="news">
        <NewsSection />
      </section>
    </div>
  )
}
