import type { ReactNode } from 'react'
import {
  IconBell,
  IconCalendarClock,
  IconChartBar,
  IconFilter,
  IconFlask,
  IconLayoutDashboard,
  IconSettings,
  IconStar,
} from '@tabler/icons-react'

export interface MenuLeaf {
  key: string
  label: string
  icon?: ReactNode
  [d: `data-${string}`]: unknown
}

/** 菜单/底导图标统一尺寸：侧栏(展开 15~17px)、底导(17px)、命令面板(~13px)共用，取 16 折中 */
const MENU_ICON_SIZE = 16

/** 侧栏/抽屉/底部导航/命令面板共用的导航单一数据源（三域分组结构）。
 * 盯盘 = 行情/信号/组合；研究 = 因子流程 + 任务；系统 = 设置 + 状态。
 * 每项必须有 icon：992–1199 图标轨模式下菜单折叠为仅图标。
 * 系统设置/状态已并入系统组（T-46：不再固定渲染于侧栏/抽屉底部），
 * 侧栏底部仅保留折叠按钮。任务管理为唯一任务入口，与顶栏 JobBar（快速查看+操作）职责区分：
 * 侧栏=任务页导航，顶栏=任务快速操作。 */
export interface MenuGroup {
  /** 域 key（dom 分组标识） */
  key: string
  /** 域标题（展开态显示，图标轨/收起态隐藏） */
  title: string
  items: MenuLeaf[]
}

export const menuGroups: MenuGroup[] = [
  {
    key: 'watch',
    title: '盯盘',
    items: [
      { key: '/signals', icon: <IconBell size={MENU_ICON_SIZE} />, label: '信号中心' },
      { key: '/watchlist', icon: <IconStar size={MENU_ICON_SIZE} />, label: '我的关注' },
      { key: '/screener', icon: <IconFilter size={MENU_ICON_SIZE} />, label: '条件选股' },
      { key: '/paper', icon: <IconChartBar size={MENU_ICON_SIZE} />, label: '模拟盘' },
    ],
  },
  {
    key: 'research',
    title: '研究',
    items: [
      { key: '/research', icon: <IconFlask size={MENU_ICON_SIZE} />, label: '因子研究' },
      { key: '/tasks', icon: <IconCalendarClock size={MENU_ICON_SIZE} />, label: '任务管理' },
    ],
  },
  {
    key: 'system',
    title: '系统',
    items: [
      { key: '/settings', icon: <IconSettings size={MENU_ICON_SIZE} />, label: '系统设置' },
      { key: '/status', icon: <IconLayoutDashboard size={MENU_ICON_SIZE} />, label: '系统状态' },
    ],
  },
]

/** 全量扁平叶子（兼容既有消费方：MainLayout 选中态解析 / CommandPalette 页面列表）。
 * 引用方不得改写元素：写操作用 flattenMenuLeaves() 副本。 */
export const menuItems: MenuLeaf[] = menuGroups.flatMap((g) => g.items)

/** 菜单叶子项副本（MainLayout 选中态 / CommandPalette 页面列表共用；返回副本防调用方改写） */
export function flattenMenuLeaves(): MenuLeaf[] {
  return menuItems.map((m) => ({ ...m }))
}

/** 底部导航项（<768 全屏导航）：路由/图标从 flattenMenuLeaves() 派生，label 用短标签。
 * 底导 4 项恒等：信号 / 关注 / 研究 / 设置（系统状态不占底导位，深链可达）。 */
export function bottomNavItems(): MenuLeaf[] {
  const leaves = flattenMenuLeaves()
  const pick = (key: string): MenuLeaf | undefined => leaves.find((c) => c.key === key)
  const defs: [string, string][] = [
    ['/signals', '信号'],
    ['/watchlist', '关注'],
    ['/research', '研究'],
    ['/settings', '设置'],
  ]
  const nav: MenuLeaf[] = []
  for (const [key, label] of defs) {
    const leaf = pick(key)
    if (leaf) nav.push({ ...leaf, label })
  }
  return nav
}
