/**
 * 设计令牌单一事实源(T-36)
 * ------------------------------------------------------------
 * 分层:
 *   primitives  — 原始色板(仅语义层引用,页面禁止直接引用)
 *   semantic    — 语义令牌(页面/组件唯一合法入口,亮/暗双主题)
 *   shadow      — 投影令牌(卡片/浮层,双主题)
 *   spacing     — 间距刻度 pad + 语义 gap
 *   radius/dur/size/font — 圆角/动效/控件与图表高/字号刻度
 *   layout      — 壳层几何(高宽/安全区/派生变量)
 * 信号类别色/板块色随 03-design-system §2.1 迁入本源,
 * 由 signalColor(code)/sectorColor(code) 出口取用。
 *
 * 值策略:从 index.css 原样搬入(先搬运不改值,视觉零变化);
 * 唯一修订:动效两档按 DESIGN §5 收敛为 120ms/200ms(原 0.15s/0.2s,
 * 设计文档 §5 明确 fast=120ms、norm=200ms,0.15s 为历史遗留档)。
 * CSS 变量由 scripts/frontend/gen-tokens.mjs 生成到 src/theme/tokens.css
 * (键名与既有 --sr-* 完全一致),本文件不再手工同步 CSS。
 */

/* ================= 1. primitives:原始色板(页面禁直用) ================= */

export const primitives = {
  /** 中性面(亮/暗):T-47 按 03-design-system §2.1 修订(暗色 #0d1117 深灰蓝防疲劳;
   *  亮色 bg/blockBg/siderBg 微调;text3 两主题为达 WCAG AA 4.5:1 微调——
   *  03 表原文 亮 #7a838d / 暗 #6e7b88 对主要背景均 <4.5:1(实测 3.3~4.4:1),
   *  与 03 决策 4「全部语义色对满足 AA」自相矛盾,以断言达标为准,偏差见 T-47 汇报) */
  neutral: {
    bgLight: '#f5f6f8',      // 页面底(浅中性灰)
    bgDark: '#0d1117',       // 页面底(深灰蓝,防疲劳)
    cardLight: '#ffffff',    // 表面 L1
    cardDark: '#161b22',     // 表面 L1
    blockLight: '#eef0f3',   // 表面 L2(内嵌块/悬浮项)
    blockDark: '#1c2128',    // 表面 L2
    hoverLight: '#e6e9ee',   // hover 底(== surface3 亮)
    hoverDark: '#252b33',    // hover 底(== surface3 暗)
    siderLight: '#eceef2',   // 侧栏底
    siderDark: '#10151c',    // 侧栏底
    text1Light: '#14181f', text1Dark: '#e6edf3',
    text2Light: '#57606a', text2Dark: '#9aa7b5',
    text3Light: '#5f6a76', text3Dark: '#8b98a9',
  },
  /** accent 命令蓝阶梯 */
  accent: {
    main: { light: '#1668dc', dark: '#4c9aff' },
    deep: { light: '#0e4daf', dark: '#0d2a52' }, // 深一号(强调实底,白字 7.8:1)
  },
  /** 状态语义色(与涨跌色分离) */
  status: {
    warning: { light: '#b45309', dark: '#f0a93a' },
    success: { light: '#15803d', dark: '#4ade80' },
    error: { light: '#dc2626', dark: '#f87171' },
    up: { light: '#d62025', dark: '#ff6b6b' },     // A股红涨
    down: { light: '#067a4f', dark: '#2fdd8f' },   // A股绿跌
    star: { light: '#b08500', dark: '#e3b341' },   // 关注星标(T-47 修订暗色)
  },
  /** 遮罩 scrim(slate-900 系,双主题通用) */
  overlay: { light: 'rgba(15, 23, 42, 0.45)', dark: 'rgba(15, 23, 42, 0.45)' },
  overlaySoft: { light: 'rgba(15, 23, 42, 0.28)', dark: 'rgba(15, 23, 42, 0.28)' },
  /** 细边框 */
  border: { light: 'rgba(16, 24, 40, 0.10)', dark: 'rgba(148, 163, 184, 0.14)' },
  /** 图表网格线 */
  chartGrid: { light: 'rgba(120, 140, 160, 0.18)', dark: 'rgba(148, 163, 184, 0.18)' },
} as const

/* ================= 2. semantic:语义令牌(唯一合法入口) ================= */

export type SemanticKey =
  | 'bg' | 'cardBg' | 'blockBg' | 'siderBg'
  | 'text1' | 'text2' | 'text3'
  | 'border' | 'accent' | 'accentDeep' | 'onAccent'
  | 'warning' | 'success' | 'error' | 'up' | 'down' | 'star'
  | 'hoverBg' | 'chartGrid' | 'overlay' | 'overlaySoft'
  | 'surface0' | 'surface1' | 'surface2' | 'surface3'

export const semantic: Record<SemanticKey, { light: string; dark: string }> = {
  bg:        { light: primitives.neutral.bgLight,      dark: primitives.neutral.bgDark },
  cardBg:    { light: primitives.neutral.cardLight,    dark: primitives.neutral.cardDark },
  blockBg:   { light: primitives.neutral.blockLight,   dark: primitives.neutral.blockDark },
  siderBg:   { light: primitives.neutral.siderLight,   dark: primitives.neutral.siderDark },
  text1:     { light: primitives.neutral.text1Light,   dark: primitives.neutral.text1Dark },
  text2:     { light: primitives.neutral.text2Light,   dark: primitives.neutral.text2Dark },
  text3:     { light: primitives.neutral.text3Light,   dark: primitives.neutral.text3Dark },
  border:    { ...primitives.border },
  accent:    { ...primitives.accent.main },
  accentDeep:{ ...primitives.accent.deep },
  onAccent:  { light: '#ffffff', dark: '#0c0f13' },   // accent/涨跌实底上的前景
  warning:   { ...primitives.status.warning },
  success:   { ...primitives.status.success },
  error:     { ...primitives.status.error },
  up:        { ...primitives.status.up },
  down:      { ...primitives.status.down },
  star:      { ...primitives.status.star },
  hoverBg:   { light: primitives.neutral.hoverLight,  dark: primitives.neutral.hoverDark },
  chartGrid: { ...primitives.chartGrid },
  overlay:   { ...primitives.overlay },
  overlaySoft:{ ...primitives.overlaySoft },
  /* 表面色阶(暗色层级方案,DESIGN §2.2):surface0 页底 → 3 hover/弹层;
     现状值与对应语义同值(surface3=hoverBg),避免引入第二套值造成漂移 */
  surface0:  { light: primitives.neutral.bgLight,     dark: primitives.neutral.bgDark },
  surface1:  { light: primitives.neutral.cardLight,   dark: primitives.neutral.cardDark },
  surface2:  { light: primitives.neutral.blockLight,  dark: primitives.neutral.blockDark },
  surface3:  { light: primitives.neutral.hoverLight,  dark: primitives.neutral.hoverDark },
}

/* ================= 3. shadow:投影(卡片/浮层) ================= */

export const shadow = {
  card: { light: '0 1px 2px rgba(16, 24, 40, 0.05), 0 0 0 1px rgba(16, 24, 40, 0.04)',
          dark: '0 1px 2px rgba(16, 24, 40, 0.05), 0 0 0 1px rgba(16, 24, 40, 0.04)' },
  cardHover: { light: '0 2px 6px rgba(16, 24, 40, 0.08), 0 0 0 1px rgba(16, 24, 40, 0.05)',
               dark: '0 2px 6px rgba(0, 0, 0, 0.4), 0 0 0 1px rgba(148, 163, 184, 0.12)' },
  float: { light: '0 4px 20px rgba(0, 0, 0, 0.18)',
           dark: '0 4px 20px rgba(0, 0, 0, 0.18)' },
} as const

/* ================= 4. spacing:间距刻度 + 语义 gap ================= */

/** 间距刻度(comfort),--sr-pad-*;compact 整表缩放见 padCompact */
export const pad = {
  xs: '4px', sm: '6px', md: '8px', lg: '10px', xl: '12px', '2xl': '16px', '3xl': '24px',
} as const

/** 间距刻度(compact):2/4/6/8/10/12/16(整表缩放,DESIGN §7) */
export const padCompact = {
  xs: '2px', sm: '4px', md: '6px', lg: '8px', xl: '10px', '2xl': '12px', '3xl': '16px',
} as const

/** 语义 gap(comfort) */
export const gap = { page: '16px', card: '12px', row: '8px', ctl: '8px' } as const

/** 语义 gap(compact) */
export const gapCompact = { page: '12px', card: '8px', row: '6px', ctl: '6px' } as const

/* ================= 5. radius / dur / size / font ================= */

/** 圆角(comfort):卡片 8 / 控件 6 / 标签 4(DESIGN §4.2 修订 6→8,与 4px 标签拉开两级差) */
export const radius = { card: '8px', ctl: '6px', tag: '4px' } as const

/** 圆角(compact):降一档 6/4/4(DESIGN §7) */
export const radiusCompact = { card: '6px', ctl: '4px', tag: '4px' } as const

/** 动效两档(DESIGN §5):fast 悬停/按压反馈, norm 浮层进出/面板展开 */
export const dur = { fast: '120ms', norm: '200ms' } as const

/** 控件高 */
export const ctlH = { comfort: '32px', compact: '28px' } as const

/** 图表标准高 */
export const chartH = { sm: '240px', md: '300px', lg: '360px' } as const
export const chartHCompact = { sm: '200px', md: '260px', lg: '320px' } as const

/** 字号刻度(DESIGN §3.1):display 为新增档(首页级大结论,仅 L0 使用) */
export const fontScale = {
  meta: '10px', xs: '11px', sm: '12px', page: '13px', title: '13px',
  head: '14px', stat: '22px', display: '28px',
} as const

/** 字号刻度(compact):现状每档 -1px(meta 9px 为现状值,不设下限;stat/display 一并 -1) */
export const fontScaleCompact = {
  meta: '9px', xs: '10px', sm: '11px', page: '12px', title: '12px',
  head: '13px', stat: '21px', display: '27px',
} as const

/* ================= 6. layout:壳层几何(主题无关) ================= */

export const layout = {
  headerH: '48px',
  contentPad: '16px',
  siderW: '200px',
  navDrawerW: '220px',
  msgridW: '340px',
  bottombarH: '0px',
} as const

export const safeArea = {
  top: 'env(safe-area-inset-top, 0px)',
  bottom: 'env(safe-area-inset-bottom, 0px)',
  left: 'env(safe-area-inset-left, 0px)',
  right: 'env(safe-area-inset-right, 0px)',
} as const

/** 派生变量(引用其他令牌;var() 在声明处解析,主题依赖值需随主题域重声明) */
export const derived = {
  // 满高页可用高度:顶栏在 AppShell 配置里已含 safe-top(MainLayout header height),故此处一并扣除,
  // 否则 notch/刘海屏下 pageFillH 超算 safe-top 高度,页面底部溢出可视区(P1 列表未填满根因之一);
  // T-127:底部同时扣除 safe-bottom(Home Indicator 设备上满高页末段内容不再落到底导之下)
  pageFillH: 'calc(100dvh - var(--sr-header-h) - var(--sr-content-pad) * 2 - var(--sr-bottombar-h) - var(--sr-safe-top) - var(--sr-safe-bottom))',
  /** 斑马纹底(长表格偶数行):跟随 --sr-block-bg,暗色必须重声明 */
  zebraBg: 'color-mix(in srgb, var(--sr-block-bg) 55%, transparent)',
  /** 卡片体内边距别名:指向间距刻度 */
  cardPad: 'var(--sr-pad-xl) var(--sr-pad-2xl)',
  cardPadCompact: 'var(--sr-pad-md) var(--sr-pad-lg)',
  /** 圆角兼容双轨:--sr-radius-card 为主键,--sr-card-radius 对齐值(历史消费方) */
  radiusCard: 'var(--sr-radius-card)',
} as const

/* ================= 7. 信号类别色 / 板块色(03 §2.1 决策 2) ================= */
/**
 * 值为 antd 色名方言(历史遗留);Mantine Badge/Pill 渲染统一经
 * utils/antdColorCompat.ts 的 ANTD_TO_MANTINE 翻译(P1-45 收敛单一事实源)。
 * 本表保持语义键稳定,历史消费方 signalColor/sectorColor/TimelinePanel 反查直用。
 */

/** 信号分类 → antd Tag 颜色(与后端 catalog 的 category 取值一致) */
export const SIGNAL_CATEGORY_COLOR: Record<string, string> = {
  趋势: 'red',
  动量: 'volcano',
  反转: 'green',
  波动: 'purple',
  量能: 'orange',
  形态: 'cyan',
  位置: 'geekblue',
}

/** 交易所板块 → 筛选/标签颜色(沪主板/深主板/创业板/科创板/北交所/其他);
 *  值同为 antd 色名,经 utils/antdColorCompat.ts 翻译为 Mantine 色名(P1-45) */
export const SECTOR_COLOR: Record<string, string> = {
  沪主板: 'blue',
  深主板: 'cyan',
  创业板: 'green',
  科创板: 'purple',
  北交所: 'gold',
  其他: 'default',
}

/** 信号类别 → 标签色(未收录类别回落 'default') */
export function signalColor(code: string): string {
  return SIGNAL_CATEGORY_COLOR[code] ?? 'default'
}

/** 板块 → 标签色(未收录板块回落 'default') */
export function sectorColor(code: string): string {
  return SECTOR_COLOR[code] ?? 'default'
}
