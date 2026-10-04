import { useRef } from 'react'
import type { ReactNode } from 'react'
import { useElementSize } from '../../hooks/useElementSize'

/** 研究域分栏唯一通道(M3 布局机制契约,frontend/DESIGN/10-layout-contracts.md §1 M3):
 * 分栏/堆叠决策由容器实测宽驱动,严禁媒体查询硬编码断点;全站禁另写第二份断点常量。 */
export const RESEARCH_SPLIT_STACK_AT = 720

interface ResearchSplitProps {
  /** 配置列(分栏时居左 sticky 自滚;堆叠时在上) */
  config: ReactNode
  /** 结果列(分栏时居右自适应;堆叠时在下) */
  children: ReactNode
  /** 配置列最小宽(px) */
  minConfig?: number
  /** 配置列最大宽(px) */
  maxConfig?: number
  /** 容器实测宽低于此值纵向堆叠(px) */
  stackAt?: number
  /** 列间距(同时作为列内子块间距) */
  gap?: string
  className?: string
}

/**
 * ResearchSplit:研究域「配置 | 结果」分栏唯一通道。
 *
 * - 内部 useElementSize 测自身容器宽 → <stackAt 纵向堆叠(配置在上) / ≥stackAt 双栏
 *   `minmax(minConfig,maxConfig) minmax(0,1fr)`,配置列 sticky 自滚(语义承自
 *   run-layout.css .sr-run-config,sticky 只存在于分栏态)。
 * - 首帧兜底(lastGoodWRef 式,同 M1 基准 SignalStreamPanel):实测宽 ≤0(首帧/临时隐藏)
 *   延用上次有效宽;首个有效值前(undefined)默认分栏,与 992+ 视口旧行为一致,避免首帧堆叠闪动。
 */
export default function ResearchSplit({
  config,
  children,
  minConfig = 300,
  maxConfig = 460,
  stackAt = RESEARCH_SPLIT_STACK_AT,
  gap = 'var(--sr-pad-xl)',
  className,
}: ResearchSplitProps) {
  const { ref, size } = useElementSize<HTMLDivElement>()
  const lastGoodWRef = useRef<number | null>(null)
  const measured = size.width
  if (measured > 0) lastGoodWRef.current = measured
  const effectiveW = lastGoodWRef.current
  const split = effectiveW == null || effectiveW >= stackAt

  return (
    // data-testid="rsplit"(T-78 G8 运行时断言锚点:窄视口堆叠 / 宽视口并排)
    <div ref={ref} className={className} style={{ width: '100%' }} data-testid="rsplit">
      {split ? (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: `minmax(${minConfig}px, ${maxConfig}px) minmax(0, 1fr)`,
            gap,
            alignItems: 'start',
          }}
        >
          {/* 配置列:sticky 自滚(承自 .sr-run-config 桌面语义) */}
          <div
            style={{
              position: 'sticky',
              top: 0,
              maxHeight: 'var(--sr-page-fill-h)',
              overflowY: 'auto',
              overscrollBehavior: 'contain',
              display: 'flex',
              flexDirection: 'column',
              gap,
              minWidth: 0,
            }}
          >
            {config}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap, minWidth: 0 }}>{children}</div>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap, minWidth: 0 }}>{config}</div>
          <div style={{ minWidth: 0 }}>{children}</div>
        </div>
      )}
    </div>
  )
}
