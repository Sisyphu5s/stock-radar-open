import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Command } from 'cmdk'
import { notifications } from '@mantine/notifications'
import {
  IconAdjustmentsHorizontal, IconMoon, IconPlus, IconRefresh, IconScan, IconSun,
} from '@tabler/icons-react'
import { flattenMenuLeaves, menuGroups } from '../layouts/menu'
import { useThemeStore, useDensityStore } from '../stores/useAppStore'
import { triggerSignalScan } from '../api/client'
import { errMsg } from '../utils/format'
import { useStockSearch } from '../hooks/useStockSearch'
import './CommandPalette.css'

/** 命令面板打开事件名：MainLayout 顶栏入口按钮 dispatch 此事件，本组件监听打开
 *  （极简状态桥，不引入新依赖；Cmd+K 直接切换的既有机制不受影响） */
export const OPEN_COMMAND_PALETTE_EVENT = 'sr-open-command-palette'

/** 动作反馈：统一 @mantine/notifications（与页面层同源，色/时长走既有契约） */
const notifySuccess = (m: string) => notifications.show({ message: m, color: 'teal', autoClose: 2000 })
const notifyWarning = (m: string) => notifications.show({ message: m, color: 'yellow', autoClose: 3200 })

interface StockItem {
  code: string
  name: string
}

export default function CommandPalette() {
  const navigate = useNavigate()
  const theme = useThemeStore((st) => st.theme)
  const toggleTheme = useThemeStore((st) => st.toggleTheme)
  const density = useDensityStore((st) => st.density)
  const setDensity = useDensityStore((st) => st.setDensity)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [stocks, setStocks] = useState<StockItem[]>([])
  const search = useStockSearch()

  // 股票跳转：输入即搜（300ms 防抖 + seq 竞态守卫，统一走 useStockSearch）。
  // 保留旧结果直到新结果到达：不在 query 变化时清空，避免键入过程列表闪烁
  useEffect(() => {
    search.onQuery(query)
  }, [query, search.onQuery])

  // hook 结果同步为本地渲染结构（code/name）
  useEffect(() => {
    setStocks(search.options.map((o) => ({ code: o.value, name: o.label })))
  }, [search.options])

  // 全局 Cmd/Ctrl+K 守卫：输入聚焦 / IME 组合输入时忽略
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'k' && e.key !== 'K') return
      if (!(e.metaKey || e.ctrlKey)) return
      if (e.isComposing) return
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      e.preventDefault()
      setOpen((o) => !o)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // 顶栏入口按钮桥：MainLayout dispatch OPEN_COMMAND_PALETTE_EVENT 时打开（打开语义，非切换）
  useEffect(() => {
    const onOpen = () => setOpen(true)
    window.addEventListener(OPEN_COMMAND_PALETTE_EVENT, onOpen)
    return () => window.removeEventListener(OPEN_COMMAND_PALETTE_EVENT, onOpen)
  }, [])

  // 页面组（T-46）：三域全量路由 = flattenMenuLeaves（信号/关注/模拟盘/研究/任务/设置/状态），按域分组展示
  const pageGroups = useMemo(
    () => menuGroups.map((g) => ({
      title: g.title,
      items: g.items
        .map((m) => ({ key: m.key, label: m.label, icon: m.icon }))
        .filter((it) => flattenMenuLeaves().some((l) => l.key === it.key)),
    })),
    [],
  )

  const close = () => setOpen(false)

  const toggleDensity = () => {
    const next = density === 'compact' ? 'comfort' : 'compact'
    setDensity(next)
    notifySuccess(`已切换为${next === 'compact' ? '紧凑' : '舒适'}密度`)
  }

  const refreshAll = () => {
    window.dispatchEvent(new CustomEvent('sr-refresh'))
    notifySuccess('已刷新当前页面数据')
  }

  const newPaperProject = () => {
    navigate('/paper?new=1')
    notifySuccess('已打开新建模拟盘项目向导')
  }

  /** 发起信号扫描（目标周期默认日线）：真实调用后端触发；成功后进信号中心并触发一次页面刷新。
   *  SignalCenter 的扫描完成刷新由其页内任务跟踪承载，此处只负责「发起」+ 反馈。 */
  const runScan = () => {
    triggerSignalScan('daily')
      .then(() => {
        navigate('/signals')
        window.dispatchEvent(new CustomEvent('sr-refresh'))
        notifySuccess('日线信号扫描已发起')
      })
      .catch((e) => {
        navigate('/signals')
        notifyWarning('扫描发起失败：' + errMsg(e))
      })
  }

  return (
    <>
      <Command.Dialog
        open={open}
        onOpenChange={setOpen}
        overlayClassName=""
        contentClassName="sr-cmdk-content"
        label="命令面板"
      >
        <div className="sr-cmdk-input-wrap">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden>
            <circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1.6" />
            <path d="M11 11l3.5 3.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
          <Command.Input
            value={query}
            onValueChange={setQuery}
            placeholder="搜索页面 / 股票 / 命令…"
            autoFocus
          />
        </div>
        <Command.List>
          {pageGroups.map((g) => (
            <Command.Group key={g.title} heading={g.title}>
              {g.items.map((it) => (
                <Command.Item
                  key={it.key}
                  value={it.key}
                  keywords={[it.label]}
                  onSelect={() => { close(); navigate(it.key) }}
                >
                  <span className="sr-cmdk-ico">{it.icon}</span>
                  <span className="sr-cmdk-lbl">{it.label}</span>
                </Command.Item>
              ))}
            </Command.Group>
          ))}
          <Command.Group heading="快捷动作">
            <Command.Item
              value="act:refresh"
              keywords={['全局刷新', '刷新', 'refresh']}
              onSelect={() => { refreshAll(); close() }}
            >
              <span className="sr-cmdk-ico"><IconRefresh size={14} aria-hidden /></span>
              <span className="sr-cmdk-lbl">全局刷新</span>
            </Command.Item>
            <Command.Item
              value="act:theme"
              keywords={['切换主题', '主题', '白天', '夜间', '深色', '浅色']}
              onSelect={() => {
                const next = theme === 'dark' ? 'light' : 'dark'
                toggleTheme()
                notifySuccess(`已切换为${next === 'dark' ? '夜间' : '白天'}模式`)
                close()
              }}
            >
              <span className="sr-cmdk-ico">{theme === 'dark' ? <IconSun size={14} aria-hidden /> : <IconMoon size={14} aria-hidden />}</span>
              <span className="sr-cmdk-lbl">{theme === 'dark' ? '切换到白天模式' : '切换到夜间模式'}</span>
            </Command.Item>
            <Command.Item
              value="act:density"
              keywords={['切换密度', '密度', '紧凑', '舒适', 'density']}
              onSelect={() => { toggleDensity(); close() }}
            >
              <span className="sr-cmdk-ico"><IconAdjustmentsHorizontal size={14} aria-hidden /></span>
              <span className="sr-cmdk-lbl">{density === 'compact' ? '切换到舒适密度' : '切换到紧凑密度'}</span>
            </Command.Item>
            <Command.Item
              value="act:paper-new"
              keywords={['新建', '模拟盘', '项目', 'paper']}
              onSelect={() => { newPaperProject(); close() }}
            >
              <span className="sr-cmdk-ico"><IconPlus size={14} aria-hidden /></span>
              <span className="sr-cmdk-lbl">新建模拟盘项目</span>
            </Command.Item>
            <Command.Item
              value="act:scan"
              keywords={['扫描', '信号扫描', '发起', '日线', 'scan']}
              onSelect={() => { runScan(); close() }}
            >
              <span className="sr-cmdk-ico"><IconScan size={14} aria-hidden /></span>
              <span className="sr-cmdk-lbl">发起信号扫描（日线）</span>
            </Command.Item>
          </Command.Group>
          {query.trim() !== '' && (
            <Command.Group heading="股票跳转">
              {stocks.map((s) => (
                <Command.Item
                  key={s.code}
                  value={`stock:${s.code}`}
                  keywords={[s.name]}
                  onSelect={() => { close(); navigate(`/stocks/${encodeURIComponent(s.code)}`) }}
                >
                  <span className="sr-cmdk-lbl">{s.name}</span>
                  <span className="sr-cmdk-parent">{s.code}</span>
                </Command.Item>
              ))}
            </Command.Group>
          )}
          <Command.Empty>未找到匹配项</Command.Empty>
        </Command.List>
        <div className="sr-cmdk-footer">
          <span>↑↓ 选择</span>
          <span>Enter 确认</span>
          <span style={{ marginLeft: 'auto' }}>Esc 关闭</span>
        </div>
      </Command.Dialog>
    </>
  )
}
