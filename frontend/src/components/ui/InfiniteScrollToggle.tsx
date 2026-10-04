import { Switch, Tooltip } from '@mantine/core'
import clsx from 'clsx'
import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'

export interface InfiniteScrollToggleProps {
  /** 持久化键（localStorage）；提供后开关状态跨会话保留 */
  storageKey?: string
  /** 受控值（提供后为受控模式） */
  checked?: boolean
  /** 受控/非受控变更回调 */
  onChange?: (checked: boolean) => void
  /** 非受控默认值 */
  defaultChecked?: boolean
  /** 兼容保留（不再渲染）：标签固定为「无限滚动」，旧调用传入的 label 被忽略 */
  label?: ReactNode
  /** 悬停提示 */
  tooltip?: ReactNode
  size?: 'small' | 'middle'
  className?: string
}

function readStored(key: string | undefined, fallback: boolean): boolean {
  if (!key) return fallback
  try {
    const saved = localStorage.getItem(key)
    if (saved !== null) return saved === '1' || saved === 'true'
  } catch {
    /* localStorage 不可用时静默降级 */
  }
  return fallback
}

/**
 * 无限滚动开关：分页 / 无限滚动两种列表模式的切换（原生 Switch，role=switch / aria-checked）。
 * 固定标签「无限滚动」+ Switch，宽高固定（不随开关状态改变尺寸），不附带图标与
 * checkedChildren/unCheckedChildren 文本。
 * 开启（无限滚动模式）时页面调用者应隐藏分页并挂载 InfiniteScrollSentinel；关闭时恢复分页。
 * 支持持久化 storageKey、受控/非受控、tooltip 与 size/className。
 * 持久化语义与 useLocalStorage 等价（兼容旧存储格式 0/1，保持手写读写不改行为）。
 */
export function InfiniteScrollToggle({
  storageKey,
  checked,
  onChange,
  defaultChecked = false,
  tooltip,
  size = 'small',
  className,
}: InfiniteScrollToggleProps) {
  const controlled = checked !== undefined
  const [internal, setInternal] = useState<boolean>(() => readStored(storageKey, defaultChecked))
  const on = controlled ? checked : internal

  // 持久化：无论受控与否都在值变化时写入 storageKey
  useEffect(() => {
    if (!storageKey) return
    try {
      localStorage.setItem(storageKey, on ? '1' : '0')
    } catch {
      /* ignore */
    }
  }, [on, storageKey])

  const toggle = (next: boolean) => {
    if (!controlled) setInternal(next)
    onChange?.(next)
  }

  const control = (
    <span className={clsx('sr-infinite-toggle', className)}>
      <span className="sr-infinite-toggle-label">无限滚动</span>
      <Switch
        size={size === 'middle' ? 'md' : 'xs'}
        checked={on}
        onChange={(e) => toggle(e.currentTarget.checked)}
        aria-label="无限滚动"
      />
    </span>
  )
  return tooltip ? <Tooltip label={tooltip}>{control}</Tooltip> : control
}

/**
 * 页面侧受控开关的便捷 hook：读/写同一 localStorage key。
 * 用法：const [inf, setInf] = useInfiniteScrollEnabled('sr-wl-infinite')
 *       <InfiniteScrollToggle checked={inf} onChange={setInf} storageKey="sr-wl-infinite" />
 *       {inf ? <InfiniteScrollSentinel …/> : <Pagination …/>}
 */
export function useInfiniteScrollEnabled(storageKey: string, fallback = false): [boolean, (next: boolean) => void] {
  const [on, setOn] = useState<boolean>(() => readStored(storageKey, fallback))
  const set = (next: boolean) => {
    setOn(next)
    if (!storageKey) return
    try {
      localStorage.setItem(storageKey, next ? '1' : '0')
    } catch {
      /* ignore */
    }
  }
  return [on, set]
}

export default InfiniteScrollToggle
