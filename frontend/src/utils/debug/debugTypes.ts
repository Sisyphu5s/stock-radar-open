/**
 * Debug 模式纯函数层类型定义（单一事实源）。
 * 供 debugCore / debugLayout / fe-dbg-ui / fe-dbg-mount 消费，签名变更需同步契约。
 */

/** Debug 模式：off=关闭 / visual=可视化检测（样式+observer）/ headless=仅跑检查不注入 */
export type DebugMode = 'off' | 'visual' | 'headless'

/** 归一矩形：left/top 左上角，right/bottom 右下角，width/height 由 getBoundingClientRect 口径 */
export interface Rect {
  left: number
  top: number
  right: number
  bottom: number
  width: number
  height: number
}

/** 平面角点（如 transform 后 quad 四角） */
export interface QuadPoint {
  x: number
  y: number
}

/** 单层盒真实几何：corners=四角（左上/右上/右下/左下），rect=四角包围矩形（轴对齐视图） */
export interface BoxQuad {
  corners: [QuadPoint, QuadPoint, QuadPoint, QuadPoint]
  rect: Rect
}

/** CSS 盒模型四层（含 transform 真实几何；transformed=任一层非轴对齐） */
export interface BoxModel {
  margin: BoxQuad
  border: BoxQuad
  padding: BoxQuad
  content: BoxQuad
  transformed: boolean
}

/** 两元素碰撞对；a/b 为 describe() 选择器描述，areaPct=交叠面积/较小者面积 */
export interface CollisionPair {
  a: string
  b: string
  overlap: Rect
  areaPct: number
}

/** 视口/容器溢出：dir 方向超出 px 像素 */
export interface OverflowInfo {
  el: string
  dir: 'left' | 'right' | 'top' | 'bottom'
  px: number
}

/** 元素边界与某 antd 断点相交 */
export interface BreakpointCross {
  el: string
  bp: number
}

/** 定位元素（fixed/absolute）描述 */
export interface PositionedInfo {
  el: string
  tag: string
  cls: string
}

/** runChecks 聚合报告 */
export interface DebugReport {
  collisions: CollisionPair[]
  overflows: OverflowInfo[]
  flushCount: number
  breakpointCrosses: BreakpointCross[]
  positioned: PositionedInfo[]
}
