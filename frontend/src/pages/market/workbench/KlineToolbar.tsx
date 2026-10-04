import { Badge, Button, Divider, Popover, SegmentedControl, Tooltip } from '@mantine/core'
import { openConfirmModal } from '@mantine/modals'
import {
  IconAdjustments, IconArrowUpRight, IconArrowsVertical, IconBaselineDensityMedium, IconChartDots3,
  IconDotsVertical, IconEraser, IconFlask, IconMaximize, IconMinimize, IconMinus, IconPencil,
  IconRuler, IconSquare, IconTrendingUp,
} from '@tabler/icons-react'
import { useState, type ReactNode } from 'react'
import IconTextButton from '../../../components/ui/IconTextButton'
import { useViewport } from '../../../app/useViewport'
import type { DrawingTool } from '../../../components/kline/klineDrawing'
import { IndicatorMenuButton, MAIN_GROUPS, SUB_GROUPS } from './IndicatorStrip'
import { fmtParamValues, TUNE_LABEL, useWorkbench } from './context'
import { PERIODS } from '../../../utils/periods'
import { useKlineFullscreen } from '../../../stores/klineFullscreen'
import { antdToMantine } from './tagColor'
import './KlineToolbar.css'

/** 生效参数 Tag 内联上限：超出一行时折叠为「+N」Tag（tooltip 展开全部），不把工具栏撑成 2-3 行 */
const MAX_INLINE_TAGS = 3

/**
 * 稳定工具条：周期 Segmented + 布局重置 + 应用内全屏 + 主图/副图指标分组菜单 + 调优入口（点击打开调优面板）+ 画线工具组 + 生效指标参数。
 * 同类连续：周期/布局/全屏 ‖ 指标配置（主图/副图） ‖ 操作（调优/画线）；生效参数信息类恒内联尾部。
 * flex-wrap 自动换行（窄屏折行，不横向滚动）；生效参数恒内联展示（窄屏折行，无聚合形态）。
 */
export default function KlineToolbar({
  showTune = true, onResetLayout, drawingTool, onDrawingToolChange, onClearDrawings, drawingCount = 0,
}: {
  showTune?: boolean
  onResetLayout?: () => void
  /** T-14 画线：当前激活工具（受控，'none'=未激活；KlinePanel 持有状态桥接 KlineChart） */
  drawingTool: DrawingTool
  onDrawingToolChange: (t: DrawingTool) => void
  /** T-14 画线：清除当前股票全部画线 */
  onClearDrawings: () => void
  /** T-14 画线：当前股票画线数量（0 时清除全部按钮禁用） */
  drawingCount?: number
}) {
  const c = useWorkbench()
  const {
    period, setPeriod, overlays, toggleOverlay, effectiveParams, tunedInds,
    tuneOpen, setTuneOpen, clearTune,
  } = c

  // T-14/T-117 画线工具组配置（数据驱动）：type → 图标/文案/tooltip（菜单内激活项再次点击 = 取消激活）
  const DRAW_TOOLS: { type: Exclude<DrawingTool, 'none'>; icon: ReactNode; text: string; tooltip: string }[] = [
    { type: 'trend', icon: <IconTrendingUp size={14} />, text: '趋势线', tooltip: '趋势线：点击两点画一条线段' },
    { type: 'horizontal', icon: <IconMinus size={14} />, text: '水平线', tooltip: '水平线：点击一点画一条水平线' },
    { type: 'vertical', icon: <IconDotsVertical size={14} />, text: '垂直线', tooltip: '垂直线：点击一点画一条竖直直线（该时点纵贯线）' },
    { type: 'ray', icon: <IconArrowUpRight size={14} />, text: '射线', tooltip: '射线：点击两点，从第一点沿第二点方向无限延伸' },
    { type: 'channel', icon: <IconBaselineDensityMedium size={14} />, text: '平行通道', tooltip: '平行通道：点击两点画两条平行线，沿通道方向无限延伸' },
    { type: 'fib', icon: <IconChartDots3 size={14} />, text: '斐波那契', tooltip: '斐波那契回撤：点击两点画比例回撤带' },
    { type: 'measure', icon: <IconRuler size={14} />, text: '测量', tooltip: '测量：点击两点显示价差与百分比' },
    { type: 'rectangle', icon: <IconSquare size={14} />, text: '矩形', tooltip: '矩形：点击两个对角点画矩形' },
  ]
  // T-68 菜单化：画线 5 按钮平铺 → 单个「画线」按钮 + Popover 菜单（消解工具栏拥挤）；
  // 按钮图标跟随激活工具、激活态 accent 高亮；菜单含 4 工具 + 清除全部 + 已画 N 条计数
  const [drawMenuOpen, setDrawMenuOpen] = useState(false)
  const activeTool = DRAW_TOOLS.find((t) => t.type === drawingTool)
  const pickTool = (t: DrawingTool) => {
    onDrawingToolChange(drawingTool === t ? 'none' : t)
    setDrawMenuOpen(false)
  }

  // T-80:移动端紧凑工具条——周期条一行 + 「工具」Popover(主图/副图/调优/画线/重置/参数收纳),
  // 替代桌面 5 按钮 flex-wrap 折行(390 宽折 4 行 158px,把 K 线图表容器挤到 2px 高)
  const viewport = useViewport()
  const isMobile = viewport === 'mobile'
  const [mobileToolsOpen, setMobileToolsOpen] = useState(false)
  // T-117 K 线应用内全屏：会话级 store，桌面按钮/移动端「工具」菜单共用
  const klineFs = useKlineFullscreen((s) => s.on)
  const fsToggle = () => useKlineFullscreen.getState()[klineFs ? 'exit' : 'enter']()
  if (isMobile) {
    return (
      <div className="sr-stk-toolbar">
        <span className="sr-ctl-h sr-stk-period">
          <SegmentedControl
            size="xs"
            value={period}
            onChange={setPeriod}
            data={PERIODS.map((p) => ({ value: p.k, label: p.l }))}
          />
        </span>
        <Popover opened={mobileToolsOpen} onChange={setMobileToolsOpen} position="bottom-start">
          <Popover.Target>
            {/* Mantine 9 受控 Popover 需在 Target 手动 onClick 切换(库仅对非受控自动注入 toggle) */}
            <Button size="xs" leftSection={<IconAdjustments size={14} />} aria-label="图表工具菜单" onClick={() => setMobileToolsOpen((o) => !o)}>工具</Button>
          </Popover.Target>
          <Popover.Dropdown>
            <div className="sr-stk-mobile-tools" role="group" aria-label="图表工具">
              <div className="sr-stk-mobile-tools-row">
                <IndicatorMenuButton label="主图" main groups={MAIN_GROUPS} overlays={overlays} onToggle={toggleOverlay} effectiveParams={effectiveParams} tunedInds={tunedInds} />
                <IndicatorMenuButton label="副图" groups={SUB_GROUPS} overlays={overlays} onToggle={toggleOverlay} effectiveParams={effectiveParams} tunedInds={tunedInds} />
              </div>
              {onResetLayout && (
                <IconTextButton
                  icon={<IconArrowsVertical size={14} />}
                  text="重置布局"
                  tooltip="恢复图表各面板默认高度"
                  onClick={onResetLayout}
                />
              )}
              <IconTextButton
                icon={klineFs ? <IconMinimize size={14} /> : <IconMaximize size={14} />}
                text={klineFs ? '退出全屏' : '全屏'}
                tooltip={klineFs ? '退出全屏（Esc）' : '全屏聚焦 K 线（Esc 退出）'}
                onClick={() => { fsToggle(); setMobileToolsOpen(false) }}
              />
              {showTune && (
                <IconTextButton
                  icon={<IconFlask size={14} />}
                  text="指标调优"
                  tooltip="打开指标参数调优面板"
                  onClick={() => { setTuneOpen(true); setMobileToolsOpen(false) }}
                />
              )}
              <div className="sr-stk-mobile-draw">
                <div className="sr-stk-mobile-tools-title">画线工具</div>
                <div className="sr-stk-mobile-draw-grid">
                  {DRAW_TOOLS.map((t) => (
                    <button
                      key={t.type}
                      type="button"
                      className={'sr-stk-mobile-draw-item' + (drawingTool === t.type ? ' sr-stk-mobile-draw-on' : '')}
                      onClick={() => pickTool(t.type)}
                    >
                      {t.text}
                    </button>
                  ))}
                  <button
                    type="button"
                    className={'sr-stk-mobile-draw-item' + (drawingCount === 0 ? ' sr-stk-draw-menu-disabled' : '')}
                    onClick={() => { if (drawingCount > 0) { onClearDrawings(); setMobileToolsOpen(false) } }}
                  >
                    清除全部
                  </button>
                </div>
              </div>
              {tunedInds.length > 0 && (
                <div className="sr-stk-mobile-tuned">
                  <div className="sr-stk-mobile-tools-title">生效参数</div>
                  {tunedInds.map((k) => (
                    <Badge key={k} variant="light" color={antdToMantine('blue')} radius="var(--sr-radius-tag)" size="sm">
                      {TUNE_LABEL[k] ?? k.toUpperCase()} = {fmtParamValues(effectiveParams[k])}
                    </Badge>
                  ))}
                  <Button
                    size="xs" variant="subtle" color="red" aria-label="清除本图表调优"
                    onClick={() => openConfirmModal({
                      title: '清除本图表调优',
                      children: '仅移除当前股票的手动参数覆盖（不影响全局默认）',
                      labels: { confirm: '清除', cancel: '取消' },
                      confirmProps: { color: 'red' },
                      onConfirm: () => clearTune('manual'),
                    })}
                  >
                    清除本图表调优
                  </Button>
                </div>
              )}
            </div>
          </Popover.Dropdown>
        </Popover>
      </div>
    )
  }

  return (
    <div className="sr-stk-toolbar">
      {/* 周期选择（视图类）：与按钮高度对齐（--sr-ctl-h 统一控件高度，外层 span 承载） */}
      <span className="sr-ctl-h sr-stk-period">
        <SegmentedControl
          size="xs"
          value={period}
          onChange={setPeriod}
          data={PERIODS.map((p) => ({ value: p.k, label: p.l }))}
        />
      </span>
      <Divider orientation="vertical" />
      {/* 视图类：布局重置（恢复 pane 默认高度，副图拖动/折叠后一键还原）+ 应用内全屏（T-117，聚焦 K 线操作） */}
      {onResetLayout && (
        <>
          <span className="sr-stk-reset">
            <IconTextButton
              icon={<IconArrowsVertical size={14} />}
              text="重置布局"
              tooltip="恢复图表各面板默认高度"
              buttonClassName="sr-stk-reset-btn"
              onClick={onResetLayout}
            />
          </span>
          <Divider orientation="vertical" />
        </>
      )}
      <span className="sr-stk-fs">
        <IconTextButton
          icon={klineFs ? <IconMinimize size={14} /> : <IconMaximize size={14} />}
          text={klineFs ? '退出全屏' : '全屏'}
          tooltip={klineFs ? '退出全屏（Esc）' : '全屏聚焦 K 线，主图/副图/画线/调优均在（Esc 退出）'}
          className={klineFs ? 'sr-stk-fs-open' : undefined}
          buttonClassName="sr-stk-fs-btn"
          onClick={fsToggle}
        />
      </span>
      <Divider orientation="vertical" />
      {/* 指标配置类连续：主图 / 副图 */}
      <IndicatorMenuButton label="主图" main groups={MAIN_GROUPS} overlays={overlays} onToggle={toggleOverlay} effectiveParams={effectiveParams} tunedInds={tunedInds} />
      <IndicatorMenuButton label="副图" groups={SUB_GROUPS} overlays={overlays} onToggle={toggleOverlay} effectiveParams={effectiveParams} tunedInds={tunedInds} />
      <Divider orientation="vertical" />
      {/* 操作类：调优（固定文案「指标调优」不随指标名变化；面板打开时高亮） */}
      {showTune && (
        <span className="sr-stk-tune">
          <IconTextButton
            icon={<IconFlask size={14} />}
            text="指标调优"
            tooltip="打开指标参数调优面板"
            className={tuneOpen ? 'sr-stk-tune-open' : undefined}
            buttonClassName="sr-stk-tune-btn"
            onClick={() => setTuneOpen(true)}
          />
        </span>
      )}
      {/* T-14/T-68 画线工具（操作类尾组：调优之后）：单个「画线」按钮 + Popover 菜单——
          按钮图标跟随激活工具、激活态 accent 高亮；菜单内 4 工具（激活项再次点击取消）+ 清除全部 + 已画 N 条计数；
          点线/拖拽/删除（Del/Backspace/Esc）/右键菜单/样式面板交互在图表内（klineDrawing 引擎）；清除全部经 KlinePanel ref 直达引擎 */}
      <Divider orientation="vertical" />
      <div className="sr-stk-draw" role="group" aria-label="画线工具">
        <Popover
          opened={drawMenuOpen}
          onChange={setDrawMenuOpen}
          position="bottom-start"
        >
          <Popover.Target>
            {/* Mantine 9 受控 Popover 需在 Target 手动 onClick 切换(库仅对非受控自动注入 toggle) */}
            <Button
              size="xs"
              leftSection={activeTool?.icon ?? <IconPencil size={14} />}
              className={drawingTool !== 'none' ? 'sr-stk-draw-active' : undefined}
              aria-label={`画线工具${drawingTool !== 'none' ? `（当前 ${activeTool?.text}，点击取消）` : ''}`}
              onClick={() => setDrawMenuOpen((o) => !o)}
            >
              <span>{activeTool?.text ?? '画线'}{drawingCount > 0 ? ` · ${drawingCount}` : ''}</span>
            </Button>
          </Popover.Target>
          <Popover.Dropdown>
            <div className="sr-stk-draw-menu" role="group" aria-label="画线工具选择">
              <div className="sr-stk-draw-menu-title">
                画线工具
                <span className="sr-stk-draw-menu-count">已画 {drawingCount} 条</span>
              </div>
              {DRAW_TOOLS.map((t) => (
                <div
                  key={t.type}
                  className={'sr-stk-draw-menu-item' + (drawingTool === t.type ? ' sr-stk-draw-menu-on' : '')}
                  role="radio"
                  aria-checked={drawingTool === t.type}
                  tabIndex={0}
                  title={t.tooltip}
                  onClick={() => pickTool(t.type)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      pickTool(t.type)
                    }
                  }}
                >
                  <span className="sr-stk-draw-menu-icon">{t.icon}</span>
                  <span className="sr-stk-draw-menu-label">{t.text}</span>
                </div>
              ))}
              <div className="sr-stk-draw-menu-sep" />
              <div
                className={'sr-stk-draw-menu-item' + (drawingCount === 0 ? ' sr-stk-draw-menu-disabled' : '')}
                role="button"
                tabIndex={0}
                title={drawingCount > 0 ? '清除当前股票的全部画线' : '当前没有画线'}
                onClick={() => {
                  if (drawingCount === 0) return
                  onClearDrawings()
                  setDrawMenuOpen(false)
                }}
                onKeyDown={(e) => {
                  if ((e.key === 'Enter' || e.key === ' ') && drawingCount > 0) {
                    e.preventDefault()
                    onClearDrawings()
                    setDrawMenuOpen(false)
                  }
                }}
              >
                <span className="sr-stk-draw-menu-icon"><IconEraser size={14} /></span>
                <span className="sr-stk-draw-menu-label">清除全部</span>
              </div>
            </div>
          </Popover.Dropdown>
        </Popover>
      </div>
      {/* 信息类：生效参数（尾部）+ 一键清除（仅当前图表手动覆盖） */}
      {tunedInds.length > 0 && (
        <div className="sr-tune-inline" role="group" aria-label="生效参数">
          {tunedInds.slice(0, MAX_INLINE_TAGS).map((k) => (
            <Badge key={k} variant="light" color={antdToMantine('blue')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-inline-tag">
              {TUNE_LABEL[k] ?? k.toUpperCase()} = {fmtParamValues(effectiveParams[k])}
            </Badge>
          ))}
          {tunedInds.length > MAX_INLINE_TAGS && (
            <Tooltip
              label={
                <div className="sr-tune-inline-more-tip">
                  {tunedInds.map((k) => (
                    <div key={k}>{TUNE_LABEL[k] ?? k.toUpperCase()} = {fmtParamValues(effectiveParams[k])}</div>
                  ))}
                </div>
              }
            >
              <Badge variant="light" color={antdToMantine('blue')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-inline-more sr-tune-inline-tag">+{tunedInds.length - MAX_INLINE_TAGS}</Badge>
            </Tooltip>
          )}
          <Button
            size="xs"
            variant="subtle"
            color="red"
            className="sr-tune-inline-clear"
            aria-label="清除本图表调优"
            onClick={() => openConfirmModal({
              title: '清除本图表调优',
              children: '仅移除当前股票的手动参数覆盖（不影响全局默认）',
              labels: { confirm: '清除', cancel: '取消' },
              confirmProps: { color: 'red' },
              onConfirm: () => clearTune('manual'),
            })}
          >
            清除本图表调优
          </Button>
        </div>
      )}
    </div>
  )
}
