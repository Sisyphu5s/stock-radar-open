import { useState } from 'react'
import KlineChart from '../../../components/KlineChart'
import type { DrawingTool } from '../../../components/kline/klineDrawing'
import KlineToolbar from './KlineToolbar'
import { useWorkbench } from './context'

/**
 * K 线面板：顶部稳定工具条 + 图表（flex 占满剩余空间）。
 * 高度由父级布局（Splitter / 固定高度容器）决定，图表内部自适应宽度。
 */
export default function KlinePanel() {
  const c = useWorkbench()
  // T-134:K 线遮罩只随 K 线数据加载(klineLoading)——面板加载(基本面/新闻/财务)不遮图表
  const { klineData, klineLoading, errKline, period, overlays, effectiveParams, onRetry, toggleOverlay, errInd, retryIndicators, klineRef } = c
  // 布局重置信号：工具栏「重置布局」→ KlineChart 恢复 pane 默认高度
  const [resetSignal, setResetSignal] = useState(0)
  // T-14 画线接线：工具激活态 + 数量（KlineToolbar 与 KlineChart 的兄弟桥接；清除全部经 klineRef 直达引擎）
  const [drawingTool, setDrawingTool] = useState<DrawingTool>('none')
  const [drawingCount, setDrawingCount] = useState(0)

  return (
    <div style={{ height: '100%', minHeight: 0, minWidth: 0, display: 'flex', flexDirection: 'column', padding: '6px 10px 8px' }}>
      <KlineToolbar
        onResetLayout={() => setResetSignal((s) => s + 1)}
        drawingTool={drawingTool}
        onDrawingToolChange={setDrawingTool}
        onClearDrawings={() => klineRef.current?.clearDrawings()}
        drawingCount={drawingCount}
      />
      <div style={{ flex: 1, minHeight: 0, minWidth: 0 }}>
        <KlineChart
          ref={klineRef}
          data={klineData}
          loading={klineLoading}
          error={errKline}
          period={period}
          overlays={overlays}
          effectiveParams={effectiveParams}
          onRetry={onRetry}
          onToggleOverlay={toggleOverlay}
          layoutResetSignal={resetSignal}
          indicatorError={errInd}
          onRetryIndicators={retryIndicators}
          drawingTool={drawingTool}
          onDrawingChange={setDrawingCount}
          code={c.code}
          onPeriodShortcut={c.setPeriod}
        />
      </div>
    </div>
  )
}
