/* 复合模板统一出口(templates/):T1 页头表格块 / T4 设置区块 / T5 工作台面板。
 * 契约见 docs/DESIGN-CONTRACT.md §3.3 与 UI-TEMPLATES.md §三(审计检⑥强制入表)。
 * 本层允许组合 antd 原子与 ui/ 薄壳;页面层只从 ui/ 与 templates/ 组合调用。 */
export { SettingBlock } from './settings'
export type { SettingBlockProps } from './settings'
export { DockPanel } from './dock'
export type { DockPanelProps } from './dock'
export { PageTableBlock } from './page-table'
export type { PageTableBlockProps } from './page-table'
