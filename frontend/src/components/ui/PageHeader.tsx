import type { CSSProperties, ReactNode } from 'react'

interface Props {
  /** 页级操作区；无操作区时整个组件不渲染 */
  extra?: ReactNode
  /** extra 的别名 */
  actions?: ReactNode
  style?: CSSProperties
}

/**
 * 页级紧凑操作区：仅在有 extra/actions 时输出，顶栏路径作为所有页面唯一可见名称。
 * 页面名由 MainLayout 顶栏语义 h1 承担，标题类参数已全部移除（无调用方传入）。
 */
function PageHeader({ extra, actions, style }: Props) {
  const head = extra ?? actions
  if (!head) return null
  // extra 直接靠左紧凑排布（原 .sr-page-head-flex 占位已移除：无标题时左侧留白会把
  // 操作区推得过远，现不再渲染空 flex 占位）。
  return (
    <div className="sr-page-head" style={style}>
      {head}
    </div>
  )
}

export default PageHeader
