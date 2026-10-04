/**
 * Mantine 主题桥(T-37)
 * ------------------------------------------------------------
 * antd → Mantine 双库并存过渡期的脚手架基建,只做两件事:
 *
 * 1. buildMantineTheme(isDark, density)
 *    —— 从 tokens.ts(单一事实源)派生 MantineThemeOverride,
 *      让 Mantine 组件的默认观感对齐设计系统(03-design-system §1.2 三端同步)。
 *
 * 2. MantineStyleProvider
 *    —— 订阅 useThemeStore / useDensityStore,渲染 <MantineProvider forceColorScheme>。
 *
 * 单一主题事实源仍是 sr-theme → body[data-theme] 驱动 --sr-* CSS 变量(未变);
 * Mantine 的颜色方案由同一份 zustand store 派生,不新增事实源。
 * antd 侧 ConfigProvider 原样保留(卸载波 T-45 再做)。
 *
 * 映射对照(值全部来自 tokens.ts,禁止在此引入第二套字面量):
 *   - colors.primary ← semantic.accent,主色置 shade 6,0/9 用 Mantine darken/lighten 展开
 *   - colors.dark    ← 暗色表面色阶(0 文字 → 9 页底),组件默认背景/文字随语义走
 *   - radius/spacing/fontSizes/shadows ← tokens 对应刻度(密度由调用方决定档位)
 *   - white/black    ← 亮色 cardBg / text1(Mantine 亮色组件默认背景与文字)
 *
 * 本文件只做主题派生与 Provider 装配,不迁移任何组件(T-38+ 职责)。
 */

import { MantineProvider, darken, lighten } from '@mantine/core'
import type { MantineColorsTuple, MantineThemeOverride } from '@mantine/core'
import type { ReactNode } from 'react'
import { useDensityStore, useThemeStore } from '../stores/useAppStore'
import type { DensityMode } from '../stores/useAppStore'
import {
  fontScale, fontScaleCompact, pad, padCompact, radius, radiusCompact,
  semantic, shadow,
} from './tokens'

/** 与 antd token 同款字族(main.tsx ConfigProvider 的 fontFamily),避免双库渲染字面漂移 */
const FONT_STACK =
  "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', sans-serif"

/**
 * 由主色展开 10 档色阶(Mantine 约定:0 最浅 → 9 最深,主色置 shade 6):
 *   i<6  逐档向白混合,越浅提亮越多
 *   i=6  主色本身(primaryShade 指向)
 *   i>6  逐档向黑混合
 */
function makeScale(main: string): MantineColorsTuple {
  const scale: string[] = []
  for (let i = 0; i < 10; i++) {
    if (i < 6) scale.push(lighten(main, ((5 - i) / 5) * 0.85))
    else if (i === 6) scale.push(main)
    else scale.push(darken(main, ((i - 6) / 3) * 0.45))
  }
  return scale as unknown as MantineColorsTuple
}

/**
 * 暗色中性阶:Mantine 暗色主题的组件背景/文字默认引用 colors.dark
 * (body=dark-7、text=dark-0、default 按钮=dark-6、placeholder=dark-3),
 * 逐档对齐 tokens 暗色语义(0 最浅 → 9 最深,严格单调递减):
 *   0=text1 / 2=text2 / 3=text3(placeholder)/ 5=hover / 6=block / 7=card(body)/ 9=bg
 * 1/4/8 为过渡插值(非语义档,仅补单调)。
 */
function makeDarkScale(): MantineColorsTuple {
  return [
    semantic.text1.dark,    // 0 主文字
    '#cbd4de',              // 1 插值
    semantic.text2.dark,    // 2 次文字
    semantic.text3.dark,    // 3 弱文字/placeholder
    '#4d5864',              // 4 插值
    semantic.hoverBg.dark,  // 5 hover
    semantic.blockBg.dark,  // 6 块面
    semantic.cardBg.dark,   // 7 卡片(=Mantine body/Paper)
    '#10151c',              // 8 插值
    semantic.bg.dark,       // 9 页底(最深)
  ] as MantineColorsTuple
}

/** 按主题取语义值(亮/暗),密度由调用方传入对应刻度 */
export function buildMantineTheme(isDark: boolean, density: DensityMode): MantineThemeOverride {
  const compact = density === 'compact'
  const s = compact ? padCompact : pad
  const r = compact ? radiusCompact : radius
  const f = compact ? fontScaleCompact : fontScale
  const accent = semantic.accent[isDark ? 'dark' : 'light']
  const scheme = isDark ? 'dark' : 'light'

  return {
    primaryColor: 'primary',
    // 主色统一置 shade 6,亮暗各用自己的 accent(亮 #1668dc / 暗 #4c9aff)
    primaryShade: { light: 6, dark: 6 },
    autoContrast: true, // 主色底上的前景自动取白/黑,与 onAccent 语义一致
    colors: {
      primary: makeScale(accent),
      dark: makeDarkScale(),
    },
    // Mantine 亮色组件默认背景(Paper)= 卡片白;亮色默认文字 = text1
    white: semantic.cardBg.light,
    black: semantic.text1.light,
    fontFamily: FONT_STACK,
    headings: {
      fontFamily: FONT_STACK,
      fontWeight: '600', // 设计书 §3.2:标题 600
      sizes: {
        h1: { fontSize: f.display, lineHeight: '1.4' },
        h2: { fontSize: f.stat, lineHeight: '1.4' },
        h3: { fontSize: f.head, lineHeight: '1.4' },
        h4: { fontSize: f.page, lineHeight: '1.4' },
        h5: { fontSize: f.page, lineHeight: '1.4' },
        h6: { fontSize: f.page, lineHeight: '1.4' },
      },
    },
    // 字号刻度:md 对齐设计正文 13px(body 默认字号即随它)
    fontSizes: {
      xs: f.xs,
      sm: f.sm,
      md: f.page,
      lg: f.head,
      xl: f.stat,
      meta: f.meta,
      title: f.title,
      display: f.display,
    },
    // 行高:正文 1.6 / 表格 1.5 / 标题 1.4(设计书 §3.1)
    lineHeights: { xs: '1.4', sm: '1.5', md: '1.6', lg: '1.6', xl: '1.6' },
    fontWeights: { regular: '400', medium: '500', semibold: '600', bold: '700' },
    // 间距刻度:xs-xl 对齐 Mantine 5 档,2xl/3xl 扩展档对应设计刻度
    spacing: {
      xs: s.xs,
      sm: s.sm,
      md: s.md,
      lg: s.lg,
      xl: s.xl,
      '2xl': s['2xl'],
      '3xl': s['3xl'],
    },
    // 圆角:tag → xs,ctl → sm(默认档),card → md/lg/xl
    radius: { xs: r.tag, sm: r.ctl, md: r.card, lg: r.card, xl: r.card },
    defaultRadius: r.ctl,
    // 阴影:轻/中档取卡片阴影,lg/xl 对齐设计书"弹层 float 软投影"
    shadows: {
      xs: shadow.card[scheme],
      sm: shadow.card[scheme],
      md: shadow.cardHover[scheme],
      lg: shadow.float[scheme],
      xl: shadow.float[scheme],
    },
    respectReducedMotion: true, // 设计书 §5.4:prefers-reduced-motion 关闭动效
    cursorType: 'pointer',
  }
}

/**
 * Mantine 样式 Provider 桥:订阅主题/密度 store,渲染 MantineProvider。
 * 颜色方案与 tokens 派生的主题均由 store 单一事实源驱动。
 */
export function MantineStyleProvider({ children }: { children: ReactNode }) {
  const theme = useThemeStore((s) => s.theme)
  const density = useDensityStore((s) => s.density)
  return (
    <MantineProvider
      forceColorScheme={theme}
      theme={buildMantineTheme(theme === 'dark', density)}
    >
      {children}
    </MantineProvider>
  )
}
