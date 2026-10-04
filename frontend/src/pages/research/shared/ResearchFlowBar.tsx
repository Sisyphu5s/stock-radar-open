import { Button } from '@mantine/core'
import {
  IconChartLine, IconCheck, IconDatabase, IconFileSearch,
  IconFlask, IconFunction, IconRocket,
} from '@tabler/icons-react'
import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import './research-shared.css'

/** 流程步骤键（与路由一一对应；neural 为工作台管道中间步，不占流程条导航位） */
export type ResearchStepKey = 'datasets' | 'discovery' | 'neural' | 'evaluation' | 'backtests' | 'factors'
/** 发现页双视图：GP 符号回归 / Alpha101 库 */
export type DiscoveryView = 'gp' | 'alpha101'

export interface FlowBarContext {
  dataset?: { id?: number; name?: string } | null
  expression?: string | null
  job?: { id?: number; status?: string } | null
  /** 任务胶囊点击（默认跳转任务管理） */
  onJob?: () => void
}

interface ResearchFlowBarProps extends FlowBarContext {
  current: ResearchStepKey
  /** 发现页当前视图（current==='discovery' 时渲染 GP/Alpha101 切换） */
  discoveryView?: DiscoveryView
  /** 切换发现视图回调；缺省时按路径跳转 */
  onDiscoveryView?: (v: DiscoveryView) => void
  /** 隐藏步骤条（工作台内由 Tabs 承担步骤导航）：只保留上下文芯片与右侧内容；current 逻辑不变 */
  stepsHidden?: boolean
}

const STEPS: { key: ResearchStepKey; title: string; path: string }[] = [
  { key: 'datasets', title: '数据集', path: '/research/datasets' },
  { key: 'discovery', title: '发现', path: '/research/discovery' },
  { key: 'evaluation', title: '评估', path: '/research/evaluation' },
  { key: 'backtests', title: '回测', path: '/research/backtests' },
  { key: 'factors', title: '因子库', path: '/research/factors' },
]

const STEP_ORDER: Record<ResearchStepKey, number> = {
  datasets: 0, discovery: 1, neural: 2, evaluation: 3, backtests: 4, factors: 5,
}

const jobBadge = (status?: string): 'success' | 'error' | 'processing' | undefined =>
  status === 'done' ? 'success'
    : status === 'failed' ? 'error'
      : status === 'running' || status === 'pending' ? 'processing'
        : undefined

/**
 * 研究域统一可点击流程条：数据集 → 发现(GP/Alpha101) → 评估 → 回测 → 因子库。
 * 顶部展示当前 dataset / expression / job 上下文（无教学说明）；所有研究主页面共用。
 * stepsHidden：工作台内嵌时由 Tabs 承担步骤导航，本组件只渲染上下文芯片（紧凑单行）。
 */
export default function ResearchFlowBar({
  current, discoveryView, onDiscoveryView, dataset, expression, job, onJob, stepsHidden,
}: ResearchFlowBarProps) {
  const navigate = useNavigate()
  const curIdx = STEP_ORDER[current]
  // 下一跳目标:管道顺序中第一个位于当前步骤之后的导航位(neural 为工作台中间步 order=2,自然落到评估;末站 factors 无后续 → 不渲染)
  const nextStep = STEPS.find((s) => STEP_ORDER[s.key] > curIdx)
  const view = discoveryView ?? 'gp'

  const goDiscovery = (v: DiscoveryView) => {
    if (onDiscoveryView) { onDiscoveryView(v); return }
    navigate(v === 'alpha101' ? '/research/alpha101' : '/research/discovery')
  }

  // 推进按钮:小尺寸 default 变体(令牌色/边框),与步骤条胶囊按钮视觉区分;文案即下一跳目标
  const nextBtn = nextStep ? (
    <Button
      size="xs"
      variant="default"
      title={`前往 ${nextStep.title}`}
      onClick={() => navigate(nextStep.path)}
    >
      下一步:{nextStep.title}
    </Button>
  ) : null

  // stepsHidden（仅上下文芯片模式）且无任何 ctx 内容可渲染、也无下一跳目标时，整条不渲染，避免页面顶部出现空条占位
  const hasCtx = dataset?.name != null || (expression != null && expression !== '') || job?.id != null
  if (stepsHidden && !hasCtx && !nextStep) return null

  return (
    <div className={`sr-flow${stepsHidden ? ' sr-flow-ctx-only' : ''}`} aria-label="研究流程">
      {!stepsHidden && (
        <div className="sr-flow-steps">
        {STEPS.map((s, i) => (
          <span key={s.key} style={{ display: 'inline-flex', alignItems: 'center', minWidth: 0 }}>
            {i > 0 && <span className="sr-flow-arrow" aria-hidden>›</span>}
            <button
              type="button"
              className={`sr-flow-step${s.key === current ? ' is-active' : i < curIdx ? ' is-past' : ''}`}
              aria-current={s.key === current ? 'step' : undefined}
              onClick={() => navigate(s.path)}
            >
              <StepIcon step={s.key} />
              {s.title}
              {s.key === current && (
                <span style={{ display: 'inline-flex', alignItems: 'center' }}><IconCheck size={12} /></span>
              )}
            </button>
            {s.key === 'discovery' && current === 'discovery' && (
              <span className="sr-flow-switch">
                <Button
                  size="xs" variant={view === 'gp' ? 'filled' : 'default'}
                  onClick={(e) => { e.stopPropagation(); goDiscovery('gp') }}
                >
                  GP
                </Button>
                <Button
                  size="xs" variant={view === 'alpha101' ? 'filled' : 'default'}
                  onClick={(e) => { e.stopPropagation(); goDiscovery('alpha101') }}
                >
                  Alpha101
                </Button>
              </span>
            )}
            {/* 当前步骤右侧的「下一步」推进按钮（跨 Tab 导航；末站不渲染） */}
            {s.key === current && nextBtn}
          </span>
        ))}
        </div>
      )}

      <div className="sr-flow-ctx">
        {dataset?.name != null && (
          <FlowChip
            icon={<IconDatabase />} label="数据集"
            value={dataset.name}
            title={`数据集: ${dataset.name}`}
            onClick={() => navigate('/research/datasets')}
          />
        )}
        {expression != null && expression !== '' && (
          <FlowChip
            icon={<IconFunction />} label="表达式"
            value={expression}
            title={`表达式: ${expression}`}
          />
        )}
        {job?.id != null && (
          <FlowChip
            icon={<IconFlask />} label="任务"
            value={`#${job.id} ${job.status ?? ''}`}
            badge={jobBadge(job.status)}
            onClick={onJob ?? (() => navigate('/tasks'))}
          />
        )}
        {/* stepsHidden（工作台内 Tabs 承担导航）：推进按钮渲染于上下文芯片区右侧（若有下一跳目标） */}
        {stepsHidden && nextBtn}
      </div>
    </div>
  )
}

function StepIcon({ step }: { step: ResearchStepKey }) {
  const icon: ReactNode =
    step === 'datasets' ? <IconDatabase />
      : step === 'discovery' ? <IconFlask />
        : step === 'evaluation' ? <IconFileSearch />
          : step === 'backtests' ? <IconChartLine />
            : <IconRocket />
  return <span aria-hidden style={{ display: 'inline-flex', fontSize: 11 }}>{icon}</span>
}

function FlowChip({
  icon, label, value, title, badge, onClick,
}: {
  icon: ReactNode; label: string; value: string; title?: string; badge?: string; onClick?: () => void
}) {
  // 状态点颜色与 StatusTag/JOB_STATUS_META 同源（--sr-* 语义令牌），不借涨跌色（--sr-up/--sr-down 回归行情用途）
  const dot = badge === 'success' ? 'var(--sr-success)'
    : badge === 'error' ? 'var(--sr-error)'
      : badge === 'processing' ? 'var(--sr-accent)'
        : undefined
  const cls = ['sr-flow-chip']
  if (onClick) cls.push('sr-flow-chip-btn')
  return (
    <span
      className={cls.join(' ')}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      title={title}
      onClick={onClick}
      onKeyDown={(e) => {
        if (onClick && (e.key === 'Enter' || e.key === ' ')) {
          e.preventDefault()
          onClick()
        }
      }}
    >
      <span aria-hidden style={{ display: 'inline-flex', color: 'var(--sr-text-3)', flexShrink: 0 }}>{icon}</span>
      <span className="sr-flow-chip-label">{label}</span>
      {dot && <span className="sr-signal-dot" style={{ color: dot }} />}
      <span className="sr-flow-chip-value">{value}</span>
    </span>
  )
}
