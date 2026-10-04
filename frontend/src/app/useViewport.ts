import { useMediaQuery } from '@mantine/hooks'

export type ViewportMode = 'expanded' | 'rail' | 'drawer' | 'mobile'

/**
 * 响应式断点（基于 @mantine/hooks useMediaQuery，断点 768/992/1200 与旧 antd Grid 同值）：
 * xl(>=1200) 展开侧栏 · lg(992–1199) 图标轨 · md(768–991) 抽屉 · <768 底部导航 + 抽屉。
 *
 * 对外签名不变（返回 ViewportMode，18 处调用方零改动）：
 * 第三参 { getInitialValueInEffect: false }（Mantine 9）首帧直接 matchMedia 同步读真实档位，
 * 消除默认首帧 initialValue(false)→'mobile' 后 effect 翻转的必翻转一次（B2：翻转会使
 * StockWorkbench 桌面/窄屏两套 JSX 整树重挂，其内部 section Tab 状态复位）。纯 SPA 无
 * SSR/水合，同步读无服务端初值不一致问题。
 */
export function useViewport(): ViewportMode {
  const isXl = useMediaQuery('(min-width: 1200px)', false, { getInitialValueInEffect: false })
  const isLg = useMediaQuery('(min-width: 992px) and (max-width: 1199px)', false, { getInitialValueInEffect: false })
  const isMd = useMediaQuery('(min-width: 768px) and (max-width: 991px)', false, { getInitialValueInEffect: false })
  if (isXl) return 'expanded'
  if (isLg) return 'rail'
  if (isMd) return 'drawer'
  return 'mobile'
}
