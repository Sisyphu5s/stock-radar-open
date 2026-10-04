> ⚠️ 本文档为 **antd 时代设计文档(已过时)**。2026-08-14 起,设计事实源 = `frontend/DESIGN/` 设计书(01~08 章);UI 底座已迁移 Mantine 9,令牌体系由 `src/theme/tokens.ts` 生成。本文保留作历史演进记录,与设计书冲突处以设计书为准。
>
> (此指引由 T-48 文档同步加入)

# DESIGN.md — 设计哲学 / 设计原则 / 组件目录

> 本文件是 UI 与整体架构的**设计事实源**（单一来源）。改动设计级行为前先读本文与
> 历史设计决策已从当前公共规范中移除。

## 1. 设计哲学

### 1.1 动态优先（Dynamic First）
一切布局以「浏览器页面大小是预算」为前提：宽度、高度、间距、字号优先用相对手段表达——
百分比、`clamp()`、`Flex`/`Space`、24 栅格（`Row/Col`）、`Splitter` 可调分隔。
**只有 ≤36px 的小元素**（图标、徽标、按钮高度、控制件）允许硬编码少量固定尺寸，
且应尽量收敛到令牌（`--sr-ctl-h` 等）。硬编码宽高的布局断言（如"面板必须 320px"）视为设计缺陷。

### 1.2 响应式单一事实源
- **页面级断点**：antd Grid 断点 `480/576/768/992/1200/1600/1920`，用 `Row/Col` 响应式 props
  与 `Grid.useBreakpoint()`（`useViewport` 内部即基于它派生 `expanded/rail/drawer/mobile`）。
- **容器内**：换行用 `Flex wrap`、间距用 `gap`（可直接传 `var(--sr-pad-*)` 令牌）。
- 曾经自研的「宽度预算算法」（容器查询 `adaptive.css` + `useWidthBudget`）**已废弃并删除，禁止复活**。

### 1.3 令牌驱动（Token Driven）
视觉一致性由 `--sr-*` CSS 变量单点定义（`src/index.css`：`:root` 亮色 + `body[data-theme='dark']` 暗色覆盖），
antd 通过 ConfigProvider `theme`（`defaultAlgorithm` / `darkAlgorithm` + 组件 token）与之一致：
- 颜色：`--sr-text-1/2/3`（文字层级）、`--sr-card-bg/--sr-block-bg/--sr-bg`、`--sr-border`、
  `--sr-accent`、涨跌 `--sr-up`（红涨）/ `--sr-down`（绿跌）、`--sr-star`、`--sr-warning`、`--sr-hover-bg`
- 字号：`--sr-font-meta/xs/sm/page/title/head`（10–14px 刻度）+ `--sr-font-stat`（20px，页面级统计数字档，见 §1.5）
- 间距：`--sr-pad-xs/sm/md/lg/xl/2xl`（4/6/8/10/12/16px）+ `--sr-card-pad`、`--sr-gap-row`
- 控件/图形：`--sr-ctl-h`、`--sr-radius-card/tag/ctl`、`--sr-chart-*`
- 遮罩/动效：`--sr-overlay`/`--sr-overlay-soft`（模态 scrim 与投影弱化）、`--sr-dur-fast`（0.15s）/`--sr-dur-norm`（0.2s）（transition 两档）
- 页面内**禁止硬编码主题敏感色**；图表 canvas（ECharts 统计图与 lightweight-charts K 线）不解析 CSS 变量 → 用 `isDark` 切换的 JS 常量。

### 1.4 官方优先（Official First）
能用成熟库就不手写：
- **antd v6 核心**：Layout/Sider/Grid/Flex/Space/Splitter/Card/Statistic/Form/Table(+virtual)/
  Tabs/Segmented/Switch/Tag.CheckableTag/Popover/Drawer/Descriptions/Badge/Tooltip/Empty/Result…
- **官方生态**：`@ant-design/icons`、`@ant-design/x`（AI 对话：Bubble/Sender/Welcome/Prompts）、
  `clsx`（类名拼接）、`katex`（LaTeX 公式）、`cmdk`（命令面板）
- **不引入**：`@ant-design/pro-components`（peer 仅 antd ^4/^5，不兼容 v6）、`dockview`（已移除，
  可调分隔统一 `Splitter`）、`@ant-design/plots`（已卸载，迷你走势用内联 SVG）、`naive-ui`（Vue）、
  Tailwind（双轨成本高于收益，utility 需求用少量 `sr-` 工具类承担）
- 手写组件必须是「薄壳」：只承载令牌/布局行为/跨页一致性，内容渲染一律 antd。

### 1.5 信息密度与克制
- 克制的平面视觉：无嵌套卡片堆砌、无渐变堆砌、无大圆角（面板半径走令牌）。
- 功能栏按「功能分组」组织（时间/筛选/视图/操作），删除冗余说明性小字；筛选入口收敛为单一位置。
- 统计数字统一（页面级 20px 一档；面板内 ≤16px），tabular-nums 对齐。

### 1.6 可访问性与对比度
- 正文 ≥ 4.5:1（WCAG AA）、大号/粗体 ≥ 3:1；暗色 `--sr-accent: #4c9aff`（与 `src/index.css` 代码一致）。
- 图标按钮必须有 `aria-label`；纯装饰 span 可点的改 `role="button" + tabIndex + Enter/Space`；
  移动端触控目标 ≥ 40px（小控件至少 28–32px）。

### 1.7 数据时间契约
- 后端返回 **naive ISO 字符串**（Asia/Shanghai 市场时区，无时区后缀）。
- 前端 `formatSignalTime` **直接按字段解析字符串**，禁止 `new Date` 按浏览器本地时区解析（跨时区会偏）。
- 显示精度随周期：分钟 `MM-DD HH:mm`（跨年补年）/ 日 `MM-DD` / 周月 `YYYY-MM-DD`。
- 信号时点由 `bar_market_time` 契约保证（日/周/月=触发 bar 交易日 15:00；分钟=bar 精确时分、秒微秒清零）。
- **双时间语义**（消除误导）：`triggered_at`=bar 标签时点（去重/排序/筛选唯一依据）；`as_of`=数据实际截止时刻（盘中日线扫描=判定时刻，否则=15:00，可空）。
- **周期语义状态机**（信号流时点展示单一事实源）：`resolveSignalMoment(period, t, asOf, nowMs?)`（utils/time.ts）输出 `{kind, label, hint, intraday, sortEpoch, title}`——kind=时段语义（intraday 盘中/today_closed 今日收盘/yesterday/older/future 未来异常），label=周期原生精度绝对时刻（分钟今日仅 HH:mm、跨日补 MM-DD、跨年补 YYYY-；日 MM-DD；周月 YYYY-MM-DD），hint=上下文词（盘中/今天/今天收盘/昨天/本周/本月）。分钟与日线由周期语义天然区分，未来 bar 标签盘中判「盘中」、收盘后判「future」。**跨年补年判断统一用上海年**（nowMs+8h 的 UTC 年份，formatSignalTime/formatSignalMinute 第 3 参 nowMs 可冻结测试；禁浏览器本地年——负时区在跨年窗口会误判）。边界断言见 `scripts/time-moment-check.mjs`（19 用例，含上海 1/1 跨年窗口）。`GET /signals/scan/status` 提供最后扫描新鲜度（日线扫描语义）。

### 1.8 性能与包体优先
- 大数据列表：桌面 antd Table `virtual`（分页态；无限态保持哨兵）；`expandable+virtual` 是 antd 已知弱项，避免组合。
- 渲染稳定性：`ctx`/列定义 `useMemo` 稳定引用、行组件 `memo`、拖拽/ResizeObserver 用 rAF 合并、`KlineChart` 重绘依赖字符串化。
- 包体：K 线走 lightweight-charts（体积远小于 echarts）；echarts 仅统计图按需注册
  （`utils/echartsSetup.ts`）；迷你走势内联 SVG；路由级 `lazy`。
- 本地缓存：持久化键统一 `sr-` 前缀；可聚合状态用**单键 JSON map**（如关注已读 `sr-wl-seen-id`）；
  频繁变更的持久化（如只看/排除 dir）**防抖写入**（400–500ms）。

### 1.9 通用构建哲学（机制 > 特判）

> 用法：设计时按「设计」组取舍；写码时按「实现」组自检；提交前过「质量/演进」；
> 评审时按「协作」交流。违反任何一条，都应在提交信息或注释里给出理由。
> 本组为通用沉淀，项目硬规则速查见 §2。

#### 一、设计

0. 本质优先（第一性原理）：从问题的本质与基本事实出发推理；
   惯例、现有实现、类比只是参考，不是依据。
1. 机制服务于场景：每个需求都长在通用机制上，场景是机制的实例；
   新增需求 = 扩展机制的参数与入口，而不是旁路分支。
2. 简单是可靠的前提：能砍则砍，复杂度要么本质，要么自找。
3. 为今天设计，为明天留口：只实现今天的，扩展用接口留口，不为猜测做地基。
4. 组合优于继承：小部件 + 接口搭积木，用浅组合代替深继承。
5. 约定优于配置：默认合理，特例才配置；每份配置都是多记一件事。
6. 单一事实源：事实只存一处，派生由源头计算。
7. 信息隐藏：接口暴露意图，实现是私事；调用者只依赖契约，不依赖内部。

#### 二、实现

8. 单一职责：一个函数一件事，一个模块一个理由变化。
9. 拒绝复制：逻辑出现两处是巧合，三处是必然的 bug。
10. 命名即文档：好名字让代码自解释，坏名字让注释说谎。
11. 最小惊讶：接口行为符合直觉，调用者不必读源码才敢用。
12. 数据先行：能用数据/配置表达的逻辑交给数据，扩展点用注册表承载。
13. 快速失败：错误尽早暴露，带足定位上下文（什么/在哪/输入是什么），不吞不延后。
14. 防御适度：信任边界外才防御，内部契约交给类型和测试。
15. 错误是值：可预期失败走显式结果，异常只留给意外。
16. 领域实践优先：方案采用对应领域的最佳实践与成熟模式
   （官方 skill/标准库/行业惯例）；专业知识与代码事实一样需验证来源。

#### 三、质量

17. 测试锁定行为：先写失败测试再实现，重构才有底气。
18. 可测试性是信号：难测 = 设计问题，修设计而非绕过。
19. 性能最后调：先正确后测量，优化以数据为准，不以猜测为准。

#### 四、演进

20. 每步可运行：提交永远处于可用态，回滚成本与步长成正比。
21. 童子军规则：离开时比来时干净，顺手能做的不做就是债；重构以测试为护栏。
22. 大改优先重构：大范围修改先评估重设计，不在坏结构上打补丁；
   修补只用于小问题或过渡期（过渡必须记账）。
23. 接口是承诺：公共接口谨慎变更，实现可以重写。

#### 五、协作

24. 评审找分歧：价值在理解一致，错字交给工具。
25. 陌生人维护：可读性不是风格，是工程。
26. 留下痕迹：决策与取舍写进提交和文档。

> 贯穿暗线：代码的第一读者是六个月的你，不是编译器。

## 2. 设计原则（硬规则速查）

1. 布局必须相对化；≤36px 小元素才可硬编码。
2. 响应式只用 antd 断点 + Flex wrap；禁止复活容器查询预算体系。
3. 颜色/字号/间距必须走令牌，禁止页面内硬编码主题色。
4. 能直接用 antd 官方组件就不用自研；新组件先查 §3 目录是否已有薄壳。
5. 表单一律 antd `Form`；统计数字 antd `Statistic`；可调分隔 antd `Splitter`。
6. 新增/修改 API 字段必须同步 TS 类型（`tsc -b` 兜底）。
7. 改设计级行为（交互语义/布局范式/导航结构）前先向用户确认。

## 3. 组件目录

### 3.1 antd 官方直连（无需封装，直接用）
布局 Layout/Sider/Header/Content · Row/Col · Flex · Space · Splitter · Divider
数据 Card（variant/size/extra）· Statistic · Descriptions · Table(+virtual) · Tag.CheckableTag · Badge · Tabs · Segmented
表单 Form · Input/InputNumber · Select · Switch · Radio · DatePicker · AutoComplete
反馈 Popover · Tooltip · Drawer · Modal · Spin · Empty · Result · Pagination · Skeleton · message/notification

### 3.2 官方生态组件
| 包 | 用途 |
|---|---|
| `@ant-design/icons` | 全站图标 |
| `@ant-design/x` | Copilot 对话内容（Bubble.List/Sender/Welcome/Prompts）；定位壳自持 |
| `clsx` | 类名拼接 |
| `katex` | LaTeX 公式渲染（因子表达式，`LatexFormula`） |
| `lightweight-charts` | K 线主图/副图渲染（数据层契约在 `utils/klineSeries.ts` 统一 re-export，不直接依赖包路径） |
| `echarts/core + echartsSetup.ts` | 统计图按需注册（Line/Bar + Grid/Tooltip/Legend/DataZoom/MarkLine/Graphic/AxisPointer/VisualMap + CanvasRenderer） |

### 3.3 薄壳基板（`src/components/ui/`，只承载令牌与布局行为）
| 组件 | 职责 |
|---|---|
| `Toolbar` | 功能栏壳：Flex wrap + 令牌 gap；不输出任何预算类 |
| `PageShell` | 页面壳：满高 flex + 内边距（仅 header/children 两种用法） |
| `PageHeader` | 页级紧凑操作区：仅在有 extra/actions 时输出，页面名由顶栏语义 h1 承担 |
| `DataTable` | antd Table 直连 + small + 表头 12px + 斑马纹 + scrollX 可关 + useTablePagination 分页/无限归一 |
| `FormRow` | 标签+控件行薄壳：flex 行布局，label 定宽 + required 星号，间距令牌 |
| `EmptyState` | 统一空态/错误态（Empty / Result + 重试） |
| `CardShell` | antd Card 薄壳（title/extra 透传） |
| `IconTextButton` | antd Button + icon + 可选 Tooltip 薄壳 |
| `TriStateGroup` | **极特殊**：逐项颜色的 ✓/× 方向按钮组（antd Segmented 不支持逐项色与 1/0/-1 语义）；信号中心排除弹层消费 |
| `HScroll` | **极特殊**：hover/触摸显按钮的横滚容器（antd 无等价） |
| `SkeletonBlock` | 全站骨架薄壳（antd Skeleton 封装） |
| `InfiniteScrollToggle` / `InfiniteScrollSentinel` | 无限滚动开关与触底哨兵（行为层） |
| `MetricStat` | 指标卡（Statistic + 涨跌令牌上色，20px 数值） |
| `CardState` / `SignalTag` / `StatusTag` / `TaskProgress` / `DatasetSelector` / `FormulaText` | 领域小件（视觉薄壳） |
| `SignalMomentCell` | 信号时点双时单元格（全站共用）：主行「信号」=bar 标签原生精度时刻 + 相对上下文词（盘中/今天收盘/昨天/本周/本月），副行「扫描」=发现时刻 + 发现延迟，tooltip=as_of 数据截止完整时间 ∪ bar 完整时间 ∪ 相对词 ∪ extraTip；时点语义一律走 `resolveSignalMoment` 状态机，不新写相对时间/格式分支；排序/筛选仍按 `as_of ?? triggered_at`（展示层单一事实源） |
| `PageState` | 三态统一渲染壳（useAsyncState 归一化：loading/error/empty/ready + stale 降饱和提示；CardState 为其兼容别名） |
| `StatStrip` | L0 结论条（页面首屏结论数字条：label 11px + value stat 22px tabular-nums + sub，可点击项 role=button，loading 数字位骨架） |
| `ExprEditor` | CodeMirror 6 表达式编辑器薄壳（算子/特征高亮+补全+括号匹配，懒加载 chunk，宿主 FactorExprPanel） |
| `SegmentToolbar` | 分段工具条（SegmentedControl 薄壳：视图/周期切换统一形态，tail 尾随动作，scrollable 横滚） |
| `ResearchSplit` | 研究域分栏唯一通道（10 章 M3）：容器实测宽动态分栏/堆叠，配置列 sticky 自滚 |

> 说明：`utils/signals.ts` 的 `groupOf/ageTagColor`（今日/近3日/近7日/更早）仍保留——
> **watchlist（关注页）用作文情时点新鲜度标签**；信号中心已移除该相对标签（改精确时点+周期精度），
> 两处语义不同，勿互相迁移。

### 3.4 领域组件（业务逻辑，保持单体）
`KlineChart`（lightweight-charts 领域层，高度由父级 Splitter 受控）· `StockWorkbench`/`SignalCenter`（页面编排 +
ctx useMemo）· `StockSearch` · `MultiSelectGrid`（多选网格下拉）·
`JobBar` · `HermesWidget` · `WatchlistWidget`/`WatchlistLauncher`/`WatchlistFloat`（全局关注提醒：launcher 常驻入口 + 可拖动/缩放/收起的浮窗，挂 MainLayout）· `CommandPalette`（cmdk）·
copilot 域：`Launcher/CopilotWindow/ChatPanel`（@ant-design/x 内容 + 自持定位壳 `copilotGeometry`/`useCopilotPlacement`）
其余领域件：`BrandLogo` · `ErrorBoundary`（全局错误边界）· `LatexFormula`/`FormulaCode`（katex 渲染/代码回退）·
`layout/BottomNav`（移动端底部导航，单源常量）· `watchlist/Sparkline`（内联 SVG 迷你走势）
research/shared：`JobResultPlaceholder`（任务三态占位：failed/pending/empty）· `DiscoveredSubLine`（信号发现时刻副行，SignalCenter/MobileSignalList 共用）· `ListInfinite`（无限滚动列表壳，research 各页复用）

### 3.5 hooks / 工具 / 数据层
`useViewport`（`src/app/useViewport.ts`，useBreakpoint 派生 4 档）· `useInfiniteScroll`（哨兵+findScrollRoot）· `useTablePagination` ·
`usePersistentState`/`stateMemory`（utils/）· `debugLayout`（开发工具，Ctrl+Shift+D）· `formatSignalTime`/`resolveSignalMoment`（时点契约，utils/time.ts）·
`useWatchlistAlerts`（全局关注提醒：notifications 增量 + 事件 id 已读水位）· `useFloatPlacement`（浮窗/launcher 拖动缩放状态机：pointer + rAF + 归一化持久化 + 边缘吸附，copilot 定位范式复用）

hooks/ 其余（业务 hook，均有调用方）：`useCopilotProvider`（Copilot 路由上下文）· `useDatasetInit` · `useEchartsLifecycle` · `useElementSize` ·
`useJobEvents`（任务 SSE 流，终态自动关闭）· `useJobFlow`（任务提交→轮询→终态收尾）· `useJobRestore`（?job= 恢复任务视图，useJobFlow 同域）·
`useLatexPreview` · `useResearchUrlSync`（?job= 恢复）· `useStockSearch`

数据层 `src/data/`（查询池实例，统一 `createQueryPool` 惰性淘汰 + 非交易降频 + visibility 停表）：
`market.ts`（行情/事件/关注池）· `kline.ts`（K 线池）· `indicators.ts`（指标 + risk 池，单股订阅；批量快照场景不走池）·
`jobs.ts`（任务池，终态停表）· `scanStatus.ts`（扫描状态池，触发扫描后 `invalidateScanStatus()` 立即重拉）· `query.ts`（池机制本体）

stores/：`useAppStore.ts`（主题 sr-theme/密度）· `copilotStore.ts`（会话/消息，持久化 `sr-copilot-*`）
utils/：`format.ts` · `signals.ts`（groupOf 新鲜度标签 + `scoreColorScheme` 评分色单一事实源）· `klineSeries.ts`（K 线数据契约，lightweight-charts 统一 re-export）· `klineSubConfig.ts` · `periods.ts` · `jobs.ts` · `time.ts`（含 `latestEventOf` 事件末条取用）· `stateMemory.ts` · `echartsSetup/echartsTheme.ts` · `useLastDataset.ts` · `routeTitles.ts`（路由→标题映射）

### 3.6 后端服务结构
`app/api/`：market · signals · stocks · indicators · paper（模拟盘）· todos · alpha · factors · datasets ·
experiments · copilot/hermes · system（health 端点在 main.py，含 native_available）

`app/services/` 顶层：`cache.py`（TTLCache + 异步落盘，磁盘后备）· `common.py`（代码规范化/板块映射/周期常量/in_chunks）· `metrics.py`（绩效纯函数）
- `market/`：`quote.py`（三 provider 多源降级/冷却/恢复）· `scanner.py`（扫描调度 + 事件去重）· `kcache.py`（K 线 SQLite 缓存 + is_stale 新鲜度）·
  `sources.py`（源管理器）· `session.py`（交易时段，`session_spot_ttl` 快照 TTL 单一事实源）· `industry.py`（行业映射）· `hs300.py`（成分股三级降级）· `warmup.py`（启动预热）
- `signals/`：`engine.py`（信号判定引擎/时点契约）· `catalog.py`（信号目录，参数默认单一事实源）
- `indicators/`：`compute.py`（在线指标向量化计算）· `tune.py`（参数调优 SPECS）· `risk.py`（风险指标）
- `alpha/`：`operators.py`（RPN 编译/求值/算子注册表；特征叶子 FEATURES 14 个：7 个
  OHLCV 行情 + 6 个估值（pe/pb/ps/market_cap/float_cap/turnover，快照注入按交易日对齐、缺失 NaN）
  + 行业（industry 整数编码，0=未知））· `backend.py`（mlx/numpy 双后端）· `native_ops.py`（C 内核桥接）·
  `gp.py`（遗传规划）· `evaluate.py`（评估 + split_panel 切分单一实现）· `backtest.py`（回测 + 多空 + 分位）· `dataset.py`（面板构建/load_panel 特征子集）·
  `alpha101.py`（101 公式）· `nn.py`/`regressors.py`（神经网络）· `latex.py`（表达式渲染）
- `experiments/`：`queue.py`（任务队列：并发闸门/协作取消/终态 CAS，`RUNNERS` 注册表分派）· `events.py`（事件 ring + `_TERMINAL_TYPES` 单一事实源）
- `factors/`（发布状态机）· `paper/`（模拟盘）· `copilot/`（规则层 + 工具）
- 数据层：`app/models/__init__.py`（ORM 表 + 唯一约束）· `app/database.py` · `app/config.py`（Settings，调度/阈值单一事实源）

原生 C 内核（`backend/native/rolling_ops.c`，ctypes 加载，`native/build.sh` 双架构 universal dylib，
缺失/失败自动回退 numpy）：ts_sum/ts_max/ts_min/ts_rank/ts_skew/ts_kurt/ts_product/
ts_argmax/ts_argmin/decay_linear（与 Python 回退逐位一致，`tests/test_native_ops.py` 随机一致性兜底）。

### 3.7 明确不存在的（历史移除，勿重建）
`DockWorkspace/DockPanel/dockview`（去 dockview，用 Row/Col+Splitter）· `adaptive.css/.sr-cq`（预算算法）·
`useWidthBudget` · `StatBar/MetricCard/StockTabsLayout`（死组件已删）· `@ant-design/plots`（Sparkline 内联 SVG）·
`StockLink/WorkflowSteps` 等旧基板。

## 4. 布局体系（分工总表）

| 场景 | 采用 |
|---|---|
| 应用壳 | antd Layout：Sider（theme 随主题、breakpoint="lg"、collapsedWidth=64、trigger=null）+ Header（48px 令牌）+ Content |
| 页面级分栏/栅格 | `Row/Col`（响应式 span，窄屏 xs=24 堆叠或 Tabs 回退） |
| 可调分隔 | `Splitter`（`min/max` 相对值、`onResizeEnd` 持久化 `sr-*` 键、面板 fill） |
| 功能栏 | `Flex wrap`（gap 令牌）；按功能分组 + Divider |
| 卡片/统计 | `Card` / `Statistic` |
| 表格 | `Table` + `virtual`（分页态）+ 列宽收敛避免横滚（最后一列自适应撑满） |
| 悬浮件 | `Drawer`/`Popover`/`clamp()` 宽度 |

## 5. 验证与纪律速查

- 前端：`npx tsc -b`（禁 --noEmit）→ `vite build` → `smoke.mjs` → `ui-audit.mjs`（四视口×8 路由）
- 后端：`PYTHONPATH=. .venv/bin/python -m pytest -q`
- 原生：`cd backend/native && bash build.sh`（dylib 不入 git；start.sh 自动构建，失败仅告警回退 numpy）
- 协作：领地声明 / 并行 / 禁 git stash·reset / 报告对账（见贡献指南）
