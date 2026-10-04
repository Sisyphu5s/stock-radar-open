// 指标调优（TuneSection）已拆分至 sections/tune/ 子目录：
// - constants.ts        调优目标分组/算法/参数说明常量与纯函数（TARGET_GROUPS/PARAM_HINTS/fmtNamed 等）
// - useTuneResults.ts   结果区 UI 状态与派生（activeKey/resultKeys/isApplied/comboGain 等）
// - ParamEditor.tsx     参数编辑器（ParamEditorBlock + IndicatorParamEditor）
// - ResultsPanel.tsx    结果区全部内容（对比总表/指标 Tab/IcParamChart/错误重试/整体结果）
// - ClearAll.tsx        头部「清除全部调优」三档清除（Popover 自绘）
// - index.tsx           TuneSection 主组件（仅编排）
// 此处 re-export 保持既有消费方 import 路径（sections.tsx 的 './sections/tune'）不变。
export { TuneSection } from './tune/index'
