# UI 模板体系（共享模板 + 特殊模板）

> 状态：**规范性公共组件语义与目录表**
> 生效：2026-08-15；可用组件目录以 `src/components/ui/index.ts` 为准，精确 TypeScript 参数以各组件导出的 Props 类型为可执行事实源。
> 本表固定组件用途、默认语义、兼容边界和禁止事项；参数摘要不得与源码冲突，变更必须同批同步。
> 设计令牌的唯一事实源是 `src/theme/tokens.ts`，`src/theme/tokens.css` 为生成物；本表不重复定义 token 值。

## 一、设计令牌（tokens.ts 单一事实源）

| 令牌 | 值(亮/暗) | 用途 |
|---|---|---|
| `--sr-radius-card` / `--sr-card-radius` | 8px 主键 / 兼容别名 | 卡片圆角；新代码使用主键 |
| `--sr-card-shadow` | token 源生成的卡片阴影；hover 用 `--sr-card-hover-shadow` | 卡片 |
| `--sr-gap-page/card/row/ctl` | 16/12/8/8 | 页面/卡片/行/控件间距 |
| `--sr-font-page/sm/xs/meta` | 13/12/11/10（compact 各降一档） | 正文/说明/次要/微型 |
| `--sr-header-h` / `--sr-content-pad` / `--sr-page-fill-h` | 48px / 16px / 由 `tokens.ts` 的 layout/derived 生成 | 满高页；派生式同时扣除底导与安全区，不在文档复制第二份公式 |
| 涨跌色 | 亮 `#d62025/#067a4f`,暗 `#ff6b6b/#2fdd8f` | CSS 与 ECharts(echartsTheme)同一来源 |

禁止:半档字号(12.5/10.5/11.5)、魔法数高度(calc(100vh-108px))、页面内联覆盖 .sr-page/.sr-toolbar 基础属性。

## 二、共享模板组件（src/components/ui/）

| 组件 | Props | 替代 |
|---|---|---|
| `PageShell` | `{ fill?, header?, children, className?, style? }` | 页面根容器(普通滚动 / `.sr-page-fill` 满高页) |
| `PageHeader` | `{ extra?, actions?, style? }` | 紧凑页级操作区；页面名由顶栏语义 h1 承担，无操作时不渲染 |
| `Toolbar` | `{ children, compact?, scrollable?, sticky?, tail?, className?, style? }` | 所有 `sr-toolbar + 内联覆盖` |
| `DataTable` | 核心：`{ columns, dataSource, rowKey }`；布局/状态：`loading? pagination? scrollX? scrollY? fillWidth? grow? sticky? virtual? virtualRowEstimate? emptyText? expandable?`；行为：`sortScope? sortScopeUnit? onChange? onRow? rowClassName? locale? paginationRef? className?` | 统一表头、斑马纹、分页、排序范围、展开、横滚和虚拟行；`pagination.total` 优先作总页数口径（服务端分页场景）；`fillWidth` 的 min 列宽和并入列 `minWidth`（窄屏防挤压）；新列优先中性 `SrColumn`，antd 形状只作兼容 |
| `FormRow` | `{ label, children, required?, width?, className?, style? }` | 104 处"标签+控件"行 4 变体(2026-08 §3.7 解禁重建) |
| `EmptyState` | `{ text?, description?, onRetry?, padding? }` | workbench 私有 EmptyState + KlineChart 私有错误态 |
| `CardShell` | `{ title, children, icon?, extra?, className? }` | 统一卡片标题、图标、附加操作与内容结构 |
| `CardState` | `{ loading?, error?, empty?, emptyDesc?, onRetry?, loadingText?, minHeight?, style?, children }` | **兼容桥**：内部转 `PageState`；新代码直接使用 `PageState` |
| `IconTextButton` | `{ icon, text, tooltip?, onClick?, type?, danger?, loading?, disabled?, size?, className?, buttonClassName?, ariaLabel? }` | 图标+文字/纯图标按钮；纯图标时必须提供 `ariaLabel` |
| `MetricStat` | `{ label, value, tone?, sub?, icon?, active?, onClick?, color? }` | 指标卡;与 StatCard 归并(2026-08),color 优先于 tone |
| `SignalTag` | `{ code, labelOf?, size? }` | 信号 Tag+dot ×3 处 |
| `StatusTag` | `{ status, kind?: 'job'\|'signal'\|'publish' }`（默认 job） | 任务/信号/发布状态映射统一出口 |
| `SignalMomentCell` | `{ period, triggeredAt, asOf?, discoveredAt?, extraTip?, compact? }` | 信号时点双时单元格(主行信号 bar 时刻+相对词 / 副行扫描时刻+延迟 / tooltip 双时间),全站共用,走 resolveSignalMoment 状态机 |
| `TaskProgress` | `{ job, typeLabel?, extra? }` | 任务进度卡 ×7 处 |
| `DatasetSelector` | `{ datasets, value?, onChange?, width?, placeholder?, loading?, disabled? }` | 数据集选择 + `saveLastDataset`；加载/空集时显式禁用 |
| `FormulaText` | `{ tex?, expr?, block?, size? }` | Latex/FormulaCode 回退模式 ×4 处 |
| `TriStateGroup` | `{ value, onChange, options, className? }` | 逐项色 ✓/× 方向按钮组(antd Segmented 不支持逐项色) |
| `HScroll` | `{ children, className?, step? }` | hover/触摸显按钮的横滚容器 |
| `SkeletonBlock` | `{ variant?, rows? }` | 骨架屏薄壳(antd Skeleton 封装) |
| `InfiniteScrollToggle` | `{ storageKey?, checked?, onChange?, defaultChecked?, tooltip?, size?, className? }` + `useInfiniteScrollEnabled(storageKey, fallback?)` | 分页/无限滚动开关与持久化同源；旧 `label` 仅兼容、不再渲染 |
| `InfiniteScrollSentinel` | 继承 `UseInfiniteScrollOptions`，另含 `{ loadingText?, doneText?, errorText?, loadMoreText?, rootMargin?, threshold?, fillMaxBatches?, className?, style? }` | 触底加载、键盘后备按钮、错误重试、aria-live 和首批自动填满的统一出口 |
| `PageState` | `state?` 或散参数 `{ status?, data?, error?, stale?, emptyDesc?, refetch?, reset? }`，并含 `{ children, loadingText?, minHeight?, style? }` | `useAsyncState` 的 loading/error/empty/ready/stale 统一渲染；两种输入方式二选一，`state` 优先 |
| `ResearchSplit` | `{ config, children, minConfig?, maxConfig?, stackAt?, gap?, className? }` + 导出 `RESEARCH_SPLIT_STACK_AT` | 研究域分栏唯一通道(10 章 M3):容器实测宽动态分栏/堆叠,禁硬编码断点;配置列 sticky 自滚 |
| `StatStrip` | `{ items[{key,label,value,tone?,sub?,onClick?}], loading?, scrollable?, className?, style? }` | L0 结论条(页面首屏结论数字,stat 22px + tabular-nums,加载骨架) |
| `SegmentToolbar` | `{ options[{value,label}], value, onChange, extra?, block? }` | 同一路由多视图互斥切换；`block` 控制独立成行，`extra` 承载附加操作 |
| `ExprEditor` | `{ value, onChange, placeholder?, minHeight?, operators?, features? }` | CodeMirror 6 表达式编辑器(高亮/补全/括号匹配,懒加载) |

## 三、特殊模板（组合方式）

### T1 页头+工具条+卡片表格
MarketRadar / SignalTemplates / Alpha101 / Tasks / WatchlistPage
`PageShell > PageHeader + (MetricStat 筛选行|Toolbar) + (Card>DataTable | 自定义列表)`

### T2 表单+多行工具条+折叠结果区
Backtest / FactorEvaluation(镜像结构,同一模板)
`PageShell > PageHeader + Toolbar×N + Collapse(内容块,不套 Card) + MetricStat 行 + TaskProgress`

### T3 分步表单向导
FactorMining:`PageShell > PageHeader + StepCard(编号徽章) + DatasetSelector + DataTable`

### T4 设置表单(卡片组)
SystemSettings / DataConnections / LlmCopilot:
`PageShell > PageHeader + CardGroup(统一间距,禁嵌套 sr-page/手写 marginTop) + FormRow`

### T5 工作台面板(dock)
SignalCenter / StockWorkbench:
`PageShell(fill) > DockWorkspace;面板内统一 sr-dock-panel + Toolbar + sr-dock-scroll`

### T6 因子卡片流
FactorLibrary:`PageShell > PageHeader + Toolbar(状态筛选) + 因子 Card(统一头部:名称/状态 Tag/公式/按钮) + Collapse(版本表)`

## 四、排版纪律

1. 间距:页面级 16、卡片内 12、工具条 8,只用 token;列表行 6/8。
2. 字号:13/12/11/10 四档;数值用 `--sr-clamp-num`。
3. 嵌套深度:Collapse 内不得再套 Card;页面根最多 3 层(Shell>区块>内容)。
4. 状态:加载/空/错统一 `PageState`（遗留区域可经 CardState 兼容）；表格用 DataTable locale。
5. 禁止:同页两份同构组件、页面私有复制公共组件、内联覆盖体系类基础属性。
