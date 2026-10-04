/**
 * ECharts 按需引入统一入口。
 *
 * 背景：整包 `import * as echarts from 'echarts'` 会把全部图表/组件打进去（约 1.1MB 共享 chunk）。
 * 这里只注册本项目实际用到的图表与组件 + CanvasRenderer，让打包器 tree-shake 掉其余部分。
 *
 * 用法：页面统一 `import { echarts } from 'utils/echartsSetup'`，再调用
 * `echarts.init / echarts.getInstanceByDom / echarts.ECharts（类型）`。
 *
 * 注册清单（逐一核对 workbench/sections/risk、RunDetail、BacktestResults、EvaluationResults、NeuralStudy；
 * K 线已由 lightweight-charts 接管，不再注册 K 线相关图表）：
 * - 图表：Line(净值/IC/进化曲线/均线)、Bar(风险分布直方图/成交量)、Heatmap(EvaluationResults IC 热力图)
 * - 组件：Grid(坐标系+分类/数值/对数轴)、Tooltip、Legend、DataZoom(inside+slider)、
 *   MarkLine(基准线/阈值线/金叉标记)、Graphic(副图文字标注)、AxisPointer(十字光标联动)、
 *   VisualMap(条带图色阶/热力图色阶)、Title(未直接用，保守注册)
 * - 渲染器：CanvasRenderer
 *
 * 注意：echarts 的静态工具（echarts.graphic / echarts.format / echarts.util）在
 * echarts/core 的类型声明中存在但非运行时入口，如需使用应从 'echarts/core' 单独导入。
 * 当前 5 个使用文件均未用到，如未来引入请按需补充。
 */
import * as echarts from 'echarts/core'
import {
  BarChart,
  HeatmapChart,
  LineChart,
} from 'echarts/charts'
import {
  AxisPointerComponent,
  DataZoomComponent,
  GraphicComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TitleComponent,
  TooltipComponent,
  VisualMapComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  BarChart,
  HeatmapChart,
  LineChart,
  AxisPointerComponent,
  DataZoomComponent,
  GraphicComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TitleComponent,
  TooltipComponent,
  VisualMapComponent,
  CanvasRenderer,
])

export { echarts }
