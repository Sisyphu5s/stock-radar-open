import { create } from 'zustand'
import { STORAGE_KEYS } from '../utils/storageKeys'

export type ThemeMode = 'light' | 'dark'

const THEME_KEY = STORAGE_KEYS.theme

interface ThemeState {
  theme: ThemeMode
  toggleTheme: () => void
}

/** 主题初始化(T-47 暗色默认,02 §2.3/03 §2):localStorage 用户偏好 → 'dark' 兜底。
 *  不再读 prefers-color-scheme(盯盘场景暗色为默认与基准,亮色为同构镜像);
 *  已存 sr-theme 的用户不受影响(键保留)。index.html 预置脚本与本站逻辑同构,防白闪。 */
function initTheme(): ThemeMode {
  try {
    const saved = localStorage.getItem(THEME_KEY)
    if (saved === 'light' || saved === 'dark') return saved
  } catch { /* ignore */ }
  return 'dark'
}

export const useThemeStore = create<ThemeState>((set) => ({
  theme: initTheme(),
  toggleTheme: () => set((st) => {
    const next = st.theme === 'light' ? 'dark' : 'light'
    try { localStorage.setItem(THEME_KEY, next) } catch { /* ignore */ }
    return { theme: next }
  }),
}))

export type DensityMode = 'compact' | 'comfort'

const DENSITY_KEY = STORAGE_KEYS.density

interface DensityState {
  density: DensityMode
  setDensity: (d: DensityMode) => void
}

function initDensity(): DensityMode {
  try {
    const saved = localStorage.getItem(DENSITY_KEY)
    if (saved === 'compact' || saved === 'comfort') return saved
  } catch { /* ignore */ }
  return 'comfort'
}

export const useDensityStore = create<DensityState>((set) => ({
  density: initDensity(),
  setDensity: (density) => {
    try { localStorage.setItem(DENSITY_KEY, density) } catch { /* ignore */ }
    set({ density })
  },
}))
