import { create } from 'zustand'

/**
 * 工作台多股 tab 保活 store(T-55):
 * tabs 数组 = 打开顺序(新开置末,超上限按最旧淘汰);点击已有 tab 走 setActive 只切激活
 * 不重排(浏览器式,2026-08-15 用户裁决——点击后 tab 条不跳位);open 仅由 URL 驱动
 * (路由 code 变化/深链/新开)调用。
 * 持久化到 localStorage(sr-wb-tabs: {tabs, activeCode}),刷新/离开页面后恢复最近打开。
 * 渲染侧:StockWorkbench 为每个 tab 渲染保活实例(display:none 非活跃),组件不随 URL 重挂。
 */

/** 工作台同时打开的股票上限(用户裁决:上限 5) */
export const MAX_WB_TABS = 5

/** 持久化键(与 workbench/persist.ts 同风格,待登记 storageKeys 迁移清单) */
const TABS_KEY = 'sr-wb-tabs'

export interface WorkbenchTab {
  code: string
  /** 股票名称:行情接口返回后惰性回填(setTabName);未就绪时 undefined,tab 仅展示 code */
  name?: string
}

interface PersistShape {
  tabs: WorkbenchTab[]
  activeCode: string | null
}

interface WorkbenchTabsState {
  tabs: WorkbenchTab[]
  activeCode: string | null
  /** 打开(URL 路由 code 变化 / 新开):不存在则加入(超上限淘汰最旧),总是激活并置为最近访问 */
  open: (code: string, name?: string) => void
  /** 关闭:若关闭的是当前激活 tab,切到相邻(右侧优先);全部关闭后 activeCode=null */
  close: (code: string) => void
  /** 仅切换激活(不改 LRU 顺序);code 不在 tabs 中时忽略 */
  setActive: (code: string) => void
  /** 标记最近访问:把该 tab 移到数组末尾(LRU 淘汰顺序更新),不改激活 */
  touch: (code: string) => void
  /** 行情返回后回填名称(只改 name,不动顺序/激活) */
  setTabName: (code: string, name: string) => void
}

function loadTabs(): PersistShape {
  try {
    const raw = localStorage.getItem(TABS_KEY)
    if (raw) {
      const parsed = JSON.parse(raw) as unknown
      if (parsed && typeof parsed === 'object') {
        const tabs = Array.isArray((parsed as PersistShape).tabs)
          ? (parsed as PersistShape).tabs
            .filter((t): t is WorkbenchTab => !!t && typeof t.code === 'string')
            .slice(-MAX_WB_TABS)
          : []
        const activeCode = typeof (parsed as PersistShape).activeCode === 'string'
          && tabs.some((t) => t.code === (parsed as PersistShape).activeCode)
          ? (parsed as PersistShape).activeCode
          : null
        return { tabs, activeCode }
      }
    }
  } catch { /* localStorage 不可用/损坏时回退空 */ }
  return { tabs: [], activeCode: null }
}

const initial = loadTabs()

function persist(state: PersistShape) {
  try { localStorage.setItem(TABS_KEY, JSON.stringify(state)) } catch { /* ignore */ }
}

export const useWorkbenchTabs = create<WorkbenchTabsState>((set) => ({
  tabs: initial.tabs,
  activeCode: initial.activeCode,
  open: (code, name) => set((st) => {
    const prev = st.tabs.find((t) => t.code === code)
    const others = st.tabs.filter((t) => t.code !== code)
    // 新开时超上限按 LRU 淘汰最旧(首位);已存在则不淘汰
    const tabs = prev
      ? [...others, { code, name: prev.name ?? name }]
      : [...others.slice(-(MAX_WB_TABS - 1)), { code, name }]
    const out = { tabs, activeCode: code }
    persist(out)
    return out
  }),
  close: (code) => set((st) => {
    const idx = st.tabs.findIndex((t) => t.code === code)
    if (idx === -1) return st
    const tabs = st.tabs.filter((t) => t.code !== code)
    let activeCode = st.activeCode
    if (activeCode === code) {
      // 关闭的是激活 tab:切相邻(右侧优先:原 idx 右侧左移顶位;idx 为末位则取末位=左侧)
      activeCode = tabs.length ? tabs[Math.min(idx, tabs.length - 1)].code : null
    }
    const out = { tabs, activeCode }
    persist(out)
    return out
  }),
  setActive: (code) => set((st) => {
    if (!st.tabs.some((t) => t.code === code)) return st
    const out = { ...st, activeCode: code }
    persist(out)
    return out
  }),
  touch: (code) => set((st) => {
    const prev = st.tabs.find((t) => t.code === code)
    if (!prev) return st
    const tabs = [...st.tabs.filter((t) => t.code !== code), prev]
    const out = { ...st, tabs }
    persist(out)
    return out
  }),
  setTabName: (code, name) => set((st) => {
    const t = st.tabs.find((x) => x.code === code)
    if (!t || t.name === name) return st
    const tabs = st.tabs.map((x) => (x.code === code ? { ...x, name } : x))
    const out = { ...st, tabs }
    persist(out)
    return out
  }),
}))
