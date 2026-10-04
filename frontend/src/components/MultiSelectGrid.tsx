import { Combobox, InputBase, Pill, TextInput, useCombobox } from '@mantine/core'
import { IconSearch, IconX } from '@tabler/icons-react'
import clsx from 'clsx'
import { Fragment, useEffect, useMemo, useState } from 'react'
import { useElementSize } from '../hooks/useElementSize'
import { ANTD_TO_CSS } from '../utils/antdColorCompat'

export interface MsGridOption {
  value: string
  label: string
  color?: string
  /** 可选分组名：提供后下拉面板按组展示（组标题 + 组内选项），搜索过滤保留 */
  group?: string
}

interface MultiSelectGridProps {
  value: string[]
  onChange: (v: string[]) => void
  options: MsGridOption[]
  placeholder?: string
  maxTagCount?: number | 'responsive'
  width?: number
  /** 自适应宽度：min 120 / max 320、占满剩余空间（配合宽度预算容器使用） */
  flexible?: boolean
  /** a11y：字段标签 id，透传触发控件 aria-labelledby（React.AriaAttributes 原生支持） */
  ariaLabelledBy?: string
}
/** antd 预设色名 → CSS 色值：SignalTag 走 Badge 色名无需此映射；grid/popover 圆点直接
 *  取 CSS background，preset 名中 volcano/geekblue 非 CSS 合法色名，需显式映射（固定语义色，非主题敏感）。
 *  映射单一事实源为 utils/antdColorCompat.ts 的 ANTD_TO_CSS（P1-45），此处仅引用派生，禁止本地重复字面量 */
const PRESET_CSS = ANTD_TO_CSS
export const msgridDotColor = (c?: string) => {
  if (!c || c === 'default') return 'var(--sr-text-3)'
  return PRESET_CSS[c] ?? c
}

/**
 * 多选下拉网格：基于 Mantine Combobox（受控多选，T-44 由 antd Select mode=multiple + popupRender 迁移）。
 * - 触发控件 = InputBase(button) + Pill 已选标签（移除按钮逐项删除；maxTagCount 数字 = 前 N + 溢出数）；
 * - 下拉面板为自绘 CSS grid 点选（.sr-msgrid-* 全局类，结构/交互与原 antd 实现一致）；
 * - 键盘可用：面板容器 role=listbox（aria-multiselectable），每项为原生 button
 *   （role=option / aria-selected），Tab 聚焦 + Enter/Space 切换
 * - 触控目标：选项最小高度 40px，满足移动端触摸要求
 */
export default function MultiSelectGrid({
  value, onChange, options, placeholder = '请选择', maxTagCount = 'responsive', width, flexible, ariaLabelledBy,
}: MultiSelectGridProps) {
  const [q, setQ] = useState('')
  const combobox = useCombobox({
    onDropdownClose: () => combobox.resetSelectedOption(),
  })
  // 下拉面板宽度随触发控件宽度 + 视口动态调整：340px 上限、240px 下限、再按视口收敛；
  // window resize 订阅（T-27）：wrapSize 不变而视口变化时仍重算（如侧栏收起/展开）
  const { ref: wrapRef, size: wrapSize } = useElementSize<HTMLDivElement>()
  const [winW, setWinW] = useState(() => window.innerWidth)
  useEffect(() => {
    const onResize = () => setWinW(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  const popupW = useMemo(() => {
    const w = wrapSize.width
    const vw = winW
    return Math.round(Math.min(340, Math.max(240, Math.min(w, vw - 24))))
  }, [wrapSize, winW])
  const sel = useMemo(() => new Set(value), [value])
  const kw = q.trim().toLowerCase()
  const visible = useMemo(() =>
    options.filter((o) => !kw || o.label.toLowerCase().includes(kw) || o.value.toLowerCase().includes(kw)),
    [options, kw])
  // 分组模式：任一选项带 group 时启用（组顺序 = 选项出现顺序；组内保持原顺序，搜索过滤保留）
  const grouped = options.some((o) => o.group)
  const visibleGroups = useMemo(() => {
    if (!grouped) return null
    const m = new Map<string, MsGridOption[]>()
    for (const o of visible) {
      const g = o.group ?? '其他'
      const arr = m.get(g) ?? []
      arr.push(o)
      m.set(g, arr)
    }
    return [...m.entries()]
  }, [visible, grouped])

  const toggle = (v: string) => {
    const next = sel.has(v) ? value.filter((x) => x !== v) : [...value, v]
    onChange(next)
  }
  const removeTag = (v: string) => onChange(value.filter((x) => x !== v))

  // 已选标签展示：数字 maxTagCount = 前 N 个 + 「+溢出数」；'responsive' = 全部 Pill 换行展示
  const overflow = maxTagCount === 'responsive' ? 0 : value.length - maxTagCount
  const shown = maxTagCount === 'responsive' ? value : value.slice(0, maxTagCount)

  const renderItem = (o: MsGridOption) => {
    const active = sel.has(o.value)
    return (
      <button
        key={o.value}
        type="button"
        role="option"
        aria-selected={active}
        className={clsx('sr-msgrid-item', active && 'sr-msgrid-item-active')}
        onClick={() => toggle(o.value)}
        style={{ minHeight: 40 }}
      >
        <span className="sr-msgrid-dot" style={{ background: msgridDotColor(o.color) }} />
        <span className="sr-msgrid-label" title={o.label}>{o.label}</span>
      </button>
    )
  }

  const panel = (
    <div className="sr-msgrid" style={{ width: popupW }} onClick={(e) => e.stopPropagation()}>
      <div className="sr-msgrid-search">
        <TextInput size="xs" placeholder="搜索选项"
          value={q} onChange={(e) => setQ(e.currentTarget.value)}
          leftSection={<IconSearch size={14} />}
          rightSection={q ? (
            <button type="button" aria-label="清空搜索" onClick={() => setQ('')}
              style={{ border: 'none', background: 'none', cursor: 'pointer', display: 'inline-flex', alignItems: 'center', color: 'var(--sr-text-3)' }}>
              <IconX size={12} />
            </button>
          ) : null}
          rightSectionPointerEvents="auto"
        />
      </div>
      {visible.length === 0 ? (
        <div className="sr-msgrid-empty">无匹配选项</div>
      ) : grouped ? (
        <div className="sr-msgrid-grid sr-msgrid-grid-grouped" role="listbox" aria-multiselectable="true" aria-label={placeholder}>
          {visibleGroups!.map(([g, items]) => (
            <Fragment key={g}>
              <div className="sr-msgrid-group-title" role="presentation">
                <span className="sr-msgrid-dot" style={{ background: msgridDotColor(items[0]?.color) }} />
                <span className="sr-msgrid-group-name">{g}</span>
              </div>
              <div className="sr-msgrid-group-grid">{items.map(renderItem)}</div>
            </Fragment>
          ))}
        </div>
      ) : (
        <div className="sr-msgrid-grid" role="listbox" aria-multiselectable="true" aria-label={placeholder}>
          {visible.map(renderItem)}
        </div>
      )}
    </div>
  )

  // 触发控件宽度自适应：flexible = 弹性占满剩余（clamp 120~320，flex-basis 随容器百分比）；
  // 固定 width = 精确宽度（调用方契约）；其余（移动单元格等）= 占满容器。
  // 下拉面板宽度由 wrapRef 实测驱动。
  const wrapStyle: React.CSSProperties = flexible
    ? { flex: '1 1 clamp(120px, 15%, 320px)', minWidth: 120, maxWidth: 320 }
    : width != null
      ? { width, flexShrink: 0 }
      : { width: '100%', minWidth: 'clamp(120px, 16%, 240px)' }

  return (
    <div ref={wrapRef} style={wrapStyle}>
      <Combobox
        store={combobox}
        withinPortal
        position="bottom-start"
        width={popupW}
        onOptionSubmit={() => combobox.closeDropdown()}
      >
        <Combobox.Target>
          <InputBase
            component="button"
            type="button"
            size="xs"
            pointer
            style={{ width: '100%' }}
            onClick={() => combobox.toggleDropdown()}
            rightSection={<Combobox.Chevron />}
            rightSectionPointerEvents="none"
            aria-labelledby={ariaLabelledBy}
          >
            {value.length > 0 ? (
              <Pill.Group>
                {shown.map((v) => {
                  const o = options.find((x) => x.value === v)
                  // span 包裹：Pill 移除按钮的 click 冒泡会触发 trigger toggleDropdown，在此拦截
                  return (
                    <span key={v} style={{ display: 'inline-flex' }}
                      onClick={(e) => e.stopPropagation()} onMouseDown={(e) => e.stopPropagation()}>
                      <Pill withRemoveButton onRemove={() => removeTag(v)}>
                        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
                          <span className="sr-msgrid-dot" style={{ background: msgridDotColor(o?.color) }} />
                          {o?.label ?? v}
                        </span>
                      </Pill>
                    </span>
                  )
                })}
                {overflow > 0 && <Pill>+{overflow}</Pill>}
              </Pill.Group>
            ) : (
              <span style={{ color: 'var(--sr-text-3)' }}>{placeholder}</span>
            )}
          </InputBase>
        </Combobox.Target>
        <Combobox.Dropdown>{panel}</Combobox.Dropdown>
      </Combobox>
    </div>
  )
}