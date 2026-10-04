import { useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'

/**
 * 全站唯一的 ResizeObserver 封装（禁止在组件内手写 new ResizeObserver；
 * 例外：需要动态重观察目标 / 自定义回调的复杂场景，见 HScroll.tsx 注释）。
 *
 * - ref 挂在目标元素上，size 随容器尺寸变化更新（初始 0）；
 * - 尺寸未变化时不 setState（引用稳定），避免 resize 触发无谓重渲染；
 * - 卸载时自动 disconnect。
 */
export function useElementSize<T extends HTMLElement>(): {
  ref: RefObject<T | null>
  size: { width: number; height: number }
} {
  const ref = useRef<T | null>(null)
  const [size, setSize] = useState({ width: 0, height: 0 })

  useEffect(() => {
    const el = ref.current
    if (!el) return
    const update = () => {
      const w = el.clientWidth
      const h = el.clientHeight
      setSize((prev) => (prev.width === w && prev.height === h ? prev : { width: w, height: h }))
    }
    update()
    const ro = new ResizeObserver(update)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  return { ref, size }
}
