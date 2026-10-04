import { ActionIcon } from '@mantine/core'
import { IconChevronLeft, IconChevronRight } from '@tabler/icons-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'

interface HScrollProps {
  children: ReactNode
  className?: string
  /** 每次点击滚动步长(px);默认容器可视宽 60% */
  step?: number
}

/**
 * 找出子树中真正可横向滚动的元素:自身可滚优先,否则下钻最近的可滚后代
 * (如 DataTable 的 .sr-dt-viewport 自带 overflow-x:auto,属于后一种情况)。
 */
function findScrollTarget(el: HTMLElement | null): HTMLElement | null {
  if (!el) return null
  if (el.scrollWidth > el.clientWidth + 1) {
    const cs = getComputedStyle(el)
    if (cs.overflowX === 'auto' || cs.overflowX === 'scroll') return el
  }
  for (const child of Array.from(el.children)) {
    const t = findScrollTarget(child as HTMLElement)
    if (t) return t
  }
  return null
}

/**
 * 可横滚容器:隐藏滚动条 + 悬浮左右圆形按钮(自绘保留,内部按钮换 Mantine ActionIcon)。
 * - 可滚(canPrev/canNext)时才显示按钮(.sr-hscroll-has);
 * - 默认 opacity 0,容器 hover 或按钮 focus 时显现;触摸设备(hover:none)恒显示;
 * - 点击每次滚动 step(默认可视宽 60%)平滑滚动。
 */
export default function HScroll({ children, className, step }: HScrollProps) {
  const innerRef = useRef<HTMLDivElement | null>(null)
  const roRef = useRef<ResizeObserver | null>(null)
  const observedRef = useRef<HTMLElement | null>(null)
  const [target, setTarget] = useState<HTMLElement | null>(null)
  const [canPrev, setCanPrev] = useState(false)
  const [canNext, setCanNext] = useState(false)

  const syncFlags = useCallback((t: HTMLElement | null) => {
    if (!t) {
      setCanPrev(false)
      setCanNext(false)
      return
    }
    setCanPrev(t.scrollLeft > 2)
    setCanNext(t.scrollLeft + t.clientWidth < t.scrollWidth - 2)
  }, [])

  const update = useCallback(() => {
    const inner = innerRef.current
    if (!inner) return
    const t = findScrollTarget(inner)
    if (observedRef.current !== t && roRef.current) {
      if (observedRef.current) roRef.current.unobserve(observedRef.current)
      if (t) roRef.current.observe(t)
    }
    observedRef.current = t
    setTarget((prev) => (prev === t ? prev : t))
    syncFlags(t)
  }, [syncFlags])

  // 容器尺寸变化 / 初始挂载后重测。
  // 此处保留手写 ResizeObserver(全站唯一豁免):HScroll 需在回调里「取消观察旧目标并动态
  // 重新观察新目标」(findScrollTarget 结果变化时 roRef.unobserve/observe 切换),
  // 通用 useElementSize(hooks/useElementSize.ts)只监听固定 ref 的尺寸→state,无法承载该
  // 动态重观察语义,故不复用。
  useEffect(() => {
    const inner = innerRef.current
    if (!inner) return
    const ro = new ResizeObserver(() => update())
    roRef.current = ro
    ro.observe(inner)
    const raf = requestAnimationFrame(update)
    return () => { roRef.current = null; ro.disconnect(); cancelAnimationFrame(raf) }
  }, [update])

  // children 变化(数据/列宽变化)后重测。ReactNode 引用每次渲染都变化(调用方内联 JSX,
  // 如 DataTable 的 {viewport}),直接依赖 children 会在每次父渲染触发 rAF+DOM 测量,
  // 破环调用方 memo 收益(P2-79);改由 MutationObserver 观察 inner 子树 DOM 变化——
  // 仅真实内容变化(行/列/文本增删)才重测,与上方 ResizeObserver(容器尺寸)互补
  useEffect(() => {
    const inner = innerRef.current
    if (!inner) return
    let raf = 0
    const scheduleUpdate = () => {
      if (raf) return
      raf = requestAnimationFrame(() => { raf = 0; update() })
    }
    const mo = new MutationObserver(scheduleUpdate)
    mo.observe(inner, { childList: true, subtree: true, characterData: true })
    return () => { if (raf) cancelAnimationFrame(raf); mo.disconnect() }
  }, [update])

  // 目标变化时订阅其 scroll 事件
  useEffect(() => {
    if (!target) return
    const onScroll = () => syncFlags(target)
    onScroll()
    target.addEventListener('scroll', onScroll, { passive: true })
    return () => target.removeEventListener('scroll', onScroll)
  }, [target, syncFlags])

  const scrollByStep = useCallback((dir: 1 | -1) => {
    const t = target
    if (!t) return
    const px = Math.max(80, Math.round((step ?? t.clientWidth * 0.6) / 8) * 8)
    t.scrollBy({ left: dir * px, behavior: 'smooth' })
  }, [target, step])

  const hasScroll = canPrev || canNext
  const cls = 'sr-hscroll'
    + (hasScroll ? ' sr-hscroll-has' : '')
    + (className ? ` ${className}` : '')

  return (
    <div className={cls}>
      <div
        className="sr-hscroll-inner"
        ref={innerRef}
      >
        {children}
      </div>
      <ActionIcon
        className="sr-hscroll-btn sr-hscroll-prev"
        variant="transparent"
        aria-label="向左滚动"
        disabled={!canPrev}
        tabIndex={hasScroll ? 0 : -1}
        onClick={() => scrollByStep(-1)}
      >
        <IconChevronLeft size={14} stroke={2} />
      </ActionIcon>
      <ActionIcon
        className="sr-hscroll-btn sr-hscroll-next"
        variant="transparent"
        aria-label="向右滚动"
        disabled={!canNext}
        tabIndex={hasScroll ? 0 : -1}
        onClick={() => scrollByStep(1)}
      >
        <IconChevronRight size={14} stroke={2} />
      </ActionIcon>
    </div>
  )
}
