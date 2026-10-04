import { create } from 'zustand'

/**
 * K 线图应用内全屏状态（T-117）：会话级视图状态，不持久化。
 * 全屏 = 隐藏壳层 chrome（侧栏/顶栏/底导，MainLayout 加 .sr-kline-fs 类）+ 工作台只渲染 KlinePanel；
 * 图表保持单实例（容器自然放大，状态零丢失），ESC / 工具栏按钮退出。
 */
interface KlineFullscreenState {
  on: boolean
  enter: () => void
  exit: () => void
}

export const useKlineFullscreen = create<KlineFullscreenState>((set) => ({
  on: false,
  enter: () => set({ on: true }),
  exit: () => set({ on: false }),
}))
