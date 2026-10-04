/* ============================================================
   DebugPanel 可拖拽工作台(右下角固定,z-index 2000)
   - Tab1 检查器:选中元素坐标/BoxModel 数值表/定位链/CSS 摘要/断点关系
   - Tab2 测量:测量列表(逐条删除)+ 模式提示
   - Tab3 碰撞:runChecks 结果,每条点击高亮对应元素(describe 反查)
   - 底部:贴边计数(sr-debug-count)+ Ctrl+Shift+D 关闭提示
   - 拖拽:简单 pointer 会话(不引 useFloatGeometry,避免依赖交叉)
   - 状态与 DebugOverlay 共享 useDebugOverlay 单例 store
   - 挂载契约:mode!=='visual' 时返回 null,组件自身随 'sr-debug-mode' 显隐
   ============================================================ */

import { useEffect, useMemo, useRef, useState } from 'react'
import type { PointerEvent as RPointerEvent } from 'react'
import { findElByDescribe, useDebugOverlay } from '../../utils/debug/debugOverlay'
import type { CollisionItem, Point } from '../../utils/debug/debugOverlay'
import { BREAKPOINTS, boxModelOf, describe, offsetChainOf, rectOf, relRectOf, runChecks } from '../../utils/debug/debugCore'
import type { BoxModel } from '../../utils/debug/debugTypes'
import './debug-panel.css'

type Tab = 'inspect' | 'measure' | 'collision'

const TAB_LABELS: Record<Tab, string> = { inspect: '检查器', measure: '测量', collision: '碰撞' }
const KIND_LABELS: Record<CollisionItem['kind'], string> = {
  fixed: '互撞',
  'overflow-v': '视口',
  'overflow-c': '容器',
  breakpoint: '断点',
}
const BM_LAYERS = ['margin', 'border', 'padding', 'content'] as const
const DIR_NAME: Record<'left' | 'right' | 'top' | 'bottom', string> = { left: '左', right: '右', top: '上', bottom: '下' }
const DRAG_MARGIN = 8

const clamp = (v: number, min: number, max: number): number => Math.min(max, Math.max(min, v))

export default function DebugPanel() {
  const { state, actions } = useDebugOverlay()
  const [tab, setTab] = useState<Tab>('inspect')
  const [collapsed, setCollapsed] = useState(false)
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null)
  const [flushCount, setFlushCount] = useState(0)
  const panelRef = useRef<HTMLDivElement | null>(null)
  const dragRef = useRef<{ pointerId: number; sx: number; sy: number; baseX: number; baseY: number; moved: boolean } | null>(null)

  // 贴边计数:监听 debugLayout observer 广播的 sr-debug-count
  useEffect(() => {
    const onCount = (e: Event): void => {
      setFlushCount((e as CustomEvent<{ count: number }>).detail?.count ?? 0)
    }
    window.addEventListener('sr-debug-count', onCount)
    return () => window.removeEventListener('sr-debug-count', onCount)
  }, [])

  // 简单 pointer 拖拽(头部手柄);live 钳制在视口内
  const onHeaderPointerDown = (e: RPointerEvent<HTMLDivElement>): void => {
    if (e.button !== 0) return
    if (dragRef.current) return
    e.preventDefault()
    const el = panelRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    dragRef.current = { pointerId: e.pointerId, sx: e.clientX, sy: e.clientY, baseX: r.left, baseY: r.top, moved: false }

    const onMove = (ev: PointerEvent): void => {
      const d = dragRef.current
      if (!d || d.pointerId !== ev.pointerId || !panelRef.current) return
      const dx = ev.clientX - d.sx
      const dy = ev.clientY - d.sy
      if (!d.moved && Math.hypot(dx, dy) < 4) return
      d.moved = true
      const w = panelRef.current.offsetWidth
      const h = panelRef.current.offsetHeight
      setPos({
        x: clamp(d.baseX + dx, DRAG_MARGIN, window.innerWidth - w - DRAG_MARGIN),
        y: clamp(d.baseY + dy, DRAG_MARGIN, window.innerHeight - h - DRAG_MARGIN),
      })
    }
    const onUp = (ev: PointerEvent): void => {
      const d = dragRef.current
      if (!d || d.pointerId !== ev.pointerId) return
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onUp)
      dragRef.current = null
      if (!d.moved || !panelRef.current) return
      const w = panelRef.current.offsetWidth
      const h = panelRef.current.offsetHeight
      setPos({
        x: clamp(d.baseX + (ev.clientX - d.sx), DRAG_MARGIN, window.innerWidth - w - DRAG_MARGIN),
        y: clamp(d.baseY + (ev.clientY - d.sy), DRAG_MARGIN, window.innerHeight - h - DRAG_MARGIN),
      })
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onUp)
  }

  // 关闭:广播 sr-debug-mode off,本组件随事件关闭(mount 侧需监听同步自身开关态)
  const close = (): void => {
    window.dispatchEvent(new CustomEvent('sr-debug-mode', { detail: { mode: 'off' } }))
  }

  // Tab1 检查器数据(仅在选中元素变化时重算)
  const inspect = useMemo(() => {
    const el = state.selectedEl
    if (!el) return null
    const cs = getComputedStyle(el)
    const r = rectOf(el)
    const rel = relRectOf(el)
    const op = rel.offsetParent
    const bm = boxModelOf(el)
    const chain = offsetChainOf(el)
    const crossed = BREAKPOINTS.filter((bp: number) => r.left < bp && bp < r.right)
    return {
      el, cs, r, bm, chain, crossed, vw: window.innerWidth,
      rel: {
        left: op ? Math.round(r.left - op.left) : Math.round(r.left),
        top: op ? Math.round(r.top - op.top) : Math.round(r.top),
      },
    }
  }, [state.selectedEl])

  // Tab3 碰撞检查:runChecks 返回 DebugReport(collisions/overflows/breakpointCrosses)
  const run = (): void => {
    const res = runChecks()
    const items: CollisionItem[] = []
    res.collisions.forEach((c, i) => items.push({
      id: `f${i}`,
      kind: 'fixed',
      desc: c.a,
      detail: `× ${c.b} · 重叠 ${Math.round(c.overlap.width)}×${Math.round(c.overlap.height)} · ${Math.round(c.areaPct * 100)}%`,
      overlap: c.overlap,
      areaPct: c.areaPct,
    }))
    res.overflows.forEach((o, i) => items.push({ id: `v${i}`, kind: 'overflow-v', desc: o.el, detail: `视口${DIR_NAME[o.dir]}溢出 ${o.px}px` }))
    res.breakpointCrosses.forEach((b, i) => items.push({ id: `b${i}`, kind: 'breakpoint', desc: b.el, detail: `跨越断点 ${b.bp}px` }))
    actions.setCollisionItems(items)
  }
  useEffect(() => {
    if (tab === 'collision') run()
  }, [tab])

  const onRowClick = (it: CollisionItem): void => {
    actions.setHighlight(findElByDescribe(it.desc))
  }

  if (state.mode !== 'visual') return null

  return (
    <div
      ref={panelRef}
      className="sr-dbg-panel"
      style={pos ? { left: pos.x, top: pos.y, right: 'auto', bottom: 'auto' } : undefined}
    >
      <div className="sr-dbg-header" onPointerDown={onHeaderPointerDown} title="拖拽移动">
        <span className="sr-dbg-title">Debug 工作台</span>
        <span className="sr-dbg-grip" aria-hidden>≡</span>
        <div className="sr-dbg-actions">
          <button type="button" onClick={() => setCollapsed((c) => !c)} title={collapsed ? '展开' : '折叠'}>
            {collapsed ? '+' : '−'}
          </button>
          <button type="button" onClick={close} title="关闭(Ctrl+Shift+D)">×</button>
        </div>
      </div>
      {!collapsed && (
        <>
          <div className="sr-dbg-tabs" role="tablist">
            {(Object.keys(TAB_LABELS) as Tab[]).map((t) => (
              <button
                key={t}
                type="button"
                role="tab"
                aria-selected={tab === t}
                className={tab === t ? 'sr-dbg-tab-btn is-active' : 'sr-dbg-tab-btn'}
                onClick={() => setTab(t)}
              >
                {TAB_LABELS[t]}
              </button>
            ))}
          </div>
          <div className="sr-dbg-body">
            {tab === 'inspect' && (inspect ? <InspectView data={inspect} /> : <div className="sr-dbg-empty">点击页面元素检查</div>)}
            {tab === 'measure' && (
              <MeasureView
                measureMode={state.measureMode}
                pending={state.pendingPoint != null}
                measures={state.measures}
                onToggle={() => actions.toggleMeasureMode()}
                onRemove={actions.removeMeasure}
                onClear={actions.clearMeasures}
              />
            )}
            {tab === 'collision' && (
              <CollisionView
                items={state.collisionItems}
                onRun={run}
                onRowClick={onRowClick}
                onClear={() => actions.clearCollision()}
              />
            )}
          </div>
          <div className="sr-dbg-footer">
            <span>{flushCount} 贴边</span>
            <span>Ctrl+Shift+D 关闭</span>
          </div>
        </>
      )}
    </div>
  )
}

/* ---------------- Tab1 检查器 ---------------- */

interface InspectData {
  el: Element
  cs: CSSStyleDeclaration
  r: { left: number; top: number; width: number; height: number }
  rel: { left: number; top: number }
  bm: BoxModel
  chain: { el: string; offsetX: number; offsetY: number }[]
  crossed: number[]
  vw: number
}

function InspectView({ data }: { data: InspectData }) {
  const { el, cs, r, rel, bm, chain, crossed, vw } = data
  const flex = cs.display.includes('flex')
  const grid = cs.display.includes('grid')
  return (
    <>
      <div className="sr-dbg-el">{describe(el)}</div>
      <div className="sr-dbg-kv"><span>绝对</span><span>({Math.round(r.left)}, {Math.round(r.top)}) · {Math.round(r.width)}×{Math.round(r.height)}</span></div>
      <div className="sr-dbg-kv"><span>相对 offsetParent</span><span>({Math.round(rel.left)}, {Math.round(rel.top)})</span></div>
      <div className="sr-dbg-sec">Box Model{bm.transformed ? ' · 已变形' : ''}</div>
      <table className="sr-dbg-bm">
        <thead>
          <tr><th>层</th><th>左</th><th>上</th><th>宽</th><th>高</th></tr>
        </thead>
        <tbody>
          {BM_LAYERS.map((k) => (
            <tr key={k}>
              <td>{k}</td>
              <td>{Math.round(bm[k].rect.left)}</td>
              <td>{Math.round(bm[k].rect.top)}</td>
              <td>{Math.round(bm[k].rect.width)}</td>
              <td>{Math.round(bm[k].rect.height)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {bm.transformed && BM_LAYERS.map((k) => (
        <div key={k} className="sr-dbg-kv">
          <span>{k} 角点</span>
          <span>{bm[k].corners.map((c) => `(${Math.round(c.x)},${Math.round(c.y)})`).join(' ')}</span>
        </div>
      ))}
      <div className="sr-dbg-sec">定位链</div>
      <div className="sr-dbg-chain">
        {chain.length
          ? chain.map((c, i) => <span key={i}>{c.el} ({c.offsetX}, {c.offsetY})</span>)
          : <span className="sr-dbg-dim">无定位祖先</span>}
      </div>
      <div className="sr-dbg-sec">CSS 摘要</div>
      <div className="sr-dbg-kv"><span>display</span><span>{cs.display}</span></div>
      <div className="sr-dbg-kv"><span>position</span><span>{cs.position}</span></div>
      {flex && <div className="sr-dbg-kv"><span>flex 容器</span><span>{cs.flexDirection} · {cs.justifyContent} · {cs.alignItems}</span></div>}
      {grid && <div className="sr-dbg-kv"><span>grid</span><span>{cs.gridTemplateColumns || 'auto'}</span></div>}
      <div className="sr-dbg-kv"><span>overflow</span><span>{cs.overflowX} / {cs.overflowY}</span></div>
      <div className="sr-dbg-kv"><span>box-sizing</span><span>{cs.boxSizing}</span></div>
      <div className="sr-dbg-sec">断点关系</div>
      <div className="sr-dbg-kv"><span>视口</span><span>{vw}px</span></div>
      <div className="sr-dbg-kv"><span>跨越断点</span><span>{crossed.length ? crossed.join(', ') : '无(单段内)'}</span></div>
    </>
  )
}

/* ---------------- Tab2 测量 ---------------- */

interface MeasureViewProps {
  measureMode: boolean
  pending: boolean
  measures: { p1: Point; p2: Point }[]
  onToggle: () => void
  onRemove: (i: number) => void
  onClear: () => void
}

function MeasureView({ measureMode, pending, measures, onToggle, onRemove, onClear }: MeasureViewProps) {
  return (
    <>
      <div className="sr-dbg-tabtool">
        <span>模式: {measureMode ? '测量中' : '关闭'}</span>
        <button type="button" className="sr-dbg-del" onClick={onToggle}>
          {measureMode ? '退出测量' : '进入测量'}
        </button>
      </div>
      <div className="sr-dbg-hint">
        {measureMode
          ? (pending ? '已取第 1 点,再点取第 2 点(Esc 取消此点)' : '点击页面取第 1 点(M 键退出测量)')
          : 'M 键进入测量模式,点击两次取两点,继续追加'}
      </div>
      {measures.length === 0 && <div className="sr-dbg-empty">暂无测量</div>}
      <ul className="sr-dbg-list">
        {measures.map((m, i) => {
          const dx = Math.round(m.p2.x - m.p1.x)
          const dy = Math.round(m.p2.y - m.p1.y)
          return (
            <li key={i} className="sr-dbg-row" style={{ cursor: 'default' }}>
              <span className="sr-dbg-row-main">#{i + 1} dx {dx} · dy {dy} · {Math.round(Math.hypot(dx, dy))}px</span>
              <button type="button" className="sr-dbg-del" onClick={() => onRemove(i)}>删</button>
            </li>
          )
        })}
      </ul>
      {measures.length > 0 && (
        <button type="button" className="sr-dbg-del" style={{ alignSelf: 'flex-end' }} onClick={onClear}>清空</button>
      )}
    </>
  )
}

/* ---------------- Tab3 碰撞 ---------------- */

interface CollisionViewProps {
  items: CollisionItem[]
  onRun: () => void
  onRowClick: (it: CollisionItem) => void
  onClear: () => void
}

function CollisionView({ items, onRun, onRowClick, onClear }: CollisionViewProps) {
  return (
    <>
      <div className="sr-dbg-tabtool">
        <span>{items.length ? `共 ${items.length} 项,点击高亮` : '运行检查'}</span>
        <button type="button" className="sr-dbg-del" onClick={onRun}>重新检查</button>
      </div>
      {items.length === 0 && <div className="sr-dbg-empty">未发现布局问题</div>}
      <ul className="sr-dbg-list">
        {items.map((it) => (
          <li key={it.id} className="sr-dbg-row" onClick={() => onRowClick(it)} title="点击高亮对应元素">
            <span className="sr-dbg-row-kind">{KIND_LABELS[it.kind]}</span>
            <span className="sr-dbg-row-main">
              {it.desc}
              <span className="sr-dbg-row-detail">{it.detail}</span>
            </span>
          </li>
        ))}
      </ul>
      {items.length > 0 && (
        <button type="button" className="sr-dbg-del" style={{ alignSelf: 'flex-end' }} onClick={onClear}>清除高亮</button>
      )}
    </>
  )
}
