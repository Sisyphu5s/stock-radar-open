import type { ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { bottomNavItems } from '../../layouts/menu'

interface NavItem {
  key: string
  label: string
  icon: ReactNode
}

/** 底部导航项（<768 全屏导航）：由 layouts/menu.tsx 的 flattenMenuLeaves() 派生（信号第一，4 项恒等） */
const ITEMS: NavItem[] = bottomNavItems().map((l) => ({ key: l.key, label: l.label, icon: l.icon }))

/** 底部导航栏：仅 <768 显示；点击跳转，当前项高亮，底部适配安全区 */
export default function BottomNav() {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const active = ITEMS.find((it) => pathname === it.key || pathname.startsWith(it.key + '/'))
  return (
    <nav className="sr-bottom-nav" aria-label="底部导航">
      {ITEMS.map((it) => (
        <button
          key={it.key}
          type="button"
          className={'sr-bottom-item' + (active?.key === it.key ? ' sr-bottom-item-active' : '')}
          onClick={() => navigate(it.key)}
        >
          <span className="sr-bottom-icon">{it.icon}</span>
          <span className="sr-bottom-label">{it.label}</span>
        </button>
      ))}
    </nav>
  )
}
