import { Button, Flex, Grid, GridCol, Group, Modal, NumberInput, Select, Text, TextInput } from '@mantine/core'
import { useForm } from '@mantine/form'
import { IconDatabase, IconPlayerPlay } from '@tabler/icons-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { JobDetail } from '../../api/client'
import { createExperiment, createFactor } from '../../api/client'
import { useDatasets } from '../../data/jobs'
import { useJobFlow } from '../../hooks/useJobFlow'
import { useJobEvents } from '../../hooks/useJobEvents'
import { useEchartsLifecycle } from '../../hooks/useEchartsLifecycle'
import { useThemeStore } from '../../stores/useAppStore'
import { usePersistentState } from '../../utils/stateMemory'
import { chartDataZoom, chartTheme, chartYAxis } from '../../utils/echartsTheme'
import { echarts } from '../../utils/echartsSetup'
import { fmtNum, fmtPct, fmtRatio } from '../../utils/format'
import { CardShell, DatasetSelector, MetricStat, PageShell, PageState, ResearchSplit, StatStrip, TaskProgress } from '../../components/ui'
import FormRow from '../../components/ui/FormRow'
import MonitorPanel from './shared/MonitorPanel'
import ResearchFlowBar from './shared/ResearchFlowBar'
import { toast } from './shared/toast'

/** NN 训练结果契约（后端 neural_train 任务固定返回，见任务卡）：meta 为 Record<string, unknown>，页面局部断言 */
interface NNMeta {
  train_loss: number[]
  val_loss: number[]
  train_ic: number | null
  val_ic: number | null
  epochs: number
  /** T-57：T-56 起 model_kind=arch（mlp/lstm/transformer），放宽为 string */
  model_kind: string
  checkpoint: string | null
  /** 训练完成自动落库 nn_models 后的回填 ID；旧任务无此字段（可选） */
  model_id?: number | null
  /** T-57 权重/注意力可视化：mlp=末层权重 / lstm=末层 LSTM 权重(Wx/Wh) / transformer=注意力(最后一层,均值)；无产出或计算失败为 null */
  viz?: { kind: string; matrix: number[][]; rowLabels?: string[]; colLabels?: string[] } | null
  /** T-58 DQN（rl_train 任务，model_kind="dqn"）：训练 episode 总数 / 每 episode 平均奖励 / 评估指标 */
  episodes?: number
  train_rewards?: number[]
  eval?: {
    equity?: number[]
    total_return?: number
    sharpe?: number
    max_drawdown?: number
    trades?: number
    avg_pos?: number
  }
}

/** 训练页 job 消费形状：TaskProgress 要求 job 带索引签名（TaskJob），JobDetail 接口无索引签名，页面局部扩展 */
type NNFlowJob = JobDetail & { [key: string]: unknown }

/** 表单持久化状态（'nn:state'，sessionStorage 恢复） */
interface NnFormState {
  datasetId?: number
  /** T-57：架构（mlp/lstm/transformer），后端 ARCH_REGISTRY 白名单；T-58 加 dqn（rl_train 任务） */
  arch: string
  l1: number
  l2: number
  activation: string
  lr: number
  epochs: number
  batchSize: number
  horizon: number
  seed: number
  /** 序列架构超参（lstm/transformer 提交时组 arch_hparams，键与后端注册表对齐） */
  hidden: number
  numLayers: number
  seqLen: number
  dims: number
  numHeads: number
  /** T-58 DQN 超参（arch='dqn' 提交 rl_train；与后端 rl_train handler 缺省对齐：50/0.99/0.001） */
  episodes: number
  gamma: number
  cost: number
}

const NN_DEFAULTS: NnFormState = {
  datasetId: undefined,
  arch: 'mlp',
  l1: 32,
  l2: 16,
  activation: 'relu',
  lr: 0.01,
  epochs: 50,
  batchSize: 256,
  horizon: 5,
  seed: 42,
  hidden: 32,
  numLayers: 1,
  seqLen: 20,
  dims: 32,
  numHeads: 4,
  episodes: 50,
  gamma: 0.99,
  cost: 0.001,
}

/** 各字段清空兜底默认值（原 antd v ?? default 语义；validate 兜空值） */
const FIELD_FALLBACK: Record<Exclude<keyof NnFormState, 'datasetId'>, number | string> = {
  arch: 'mlp',
  l1: 32,
  l2: 16,
  activation: 'relu',
  lr: 0.01,
  epochs: 50,
  batchSize: 256,
  horizon: 5,
  seed: 42,
  hidden: 32,
  numLayers: 1,
  seqLen: 20,
  dims: 32,
  numHeads: 4,
  episodes: 50,
  gamma: 0.99,
  cost: 0.001,
}

const ACTIVATION_OPTIONS = [
  { value: 'relu', label: 'relu' },
  { value: 'tanh', label: 'tanh' },
  { value: 'sigmoid', label: 'sigmoid' },
]

/** T-57 架构选项/中文名：与后端 ARCH_REGISTRY 键对齐（mlp 恒可用；lstm/transformer 需 MLX）；
 *  T-58 dqn 走 rl_train 任务（experiments 白名单注册），依赖 MLX */
const ARCH_OPTIONS = [
  { value: 'mlp', label: 'MLP 全连接' },
  { value: 'lstm', label: 'LSTM 循环' },
  { value: 'transformer', label: 'Transformer 注意力' },
  { value: 'dqn', label: 'DQN 强化学习(个股仓位)' },
]
const ARCH_LABELS: Record<string, string> = {
  mlp: 'MLP 全连接',
  lstm: 'LSTM 循环',
  transformer: 'Transformer 注意力',
  dqn: 'DQN 强化学习(个股仓位)',
}

/** T-57 可视化 tab 标题：由后端 viz.kind 驱动（mlp=末层权重 / lstm=末层 LSTM 权重 / transformer=注意力均值） */
const VIZ_TITLES: Record<string, string> = {
  weights: '末层权重',
  lstm_weights: '末层 LSTM 权重(Wx/Wh)',
  attention: '注意力(最后一层,均值)',
}

const numOr = (v: number | string, fb: number): number =>
  (typeof v === 'string' && v.trim() === '') ? fb : Number(v)

/** IC 值色调：正 up / 负 down（涨跌色令牌随主题） */
const icTone = (v: number | null | undefined): 'up' | 'down' | 'plain' =>
  v == null ? 'plain' : v > 0 ? 'up' : v < 0 ? 'down' : 'plain'

/** 网络结构可视化（SVG viewBox 560×180 等比缩放）：块级抽象，不画节点/连线（C1） */
const DIAGRAM_W = 560
const DIAGRAM_H = 180
const DIAGRAM_PAD = 16

/** 内联 SVG 箭头（指向右端终点 (x2, cy)） */
function ArrowHead({ x, y, color = 'var(--sr-border)' }: { x: number; y: number; color?: string }) {
  return <path d={`M ${x} ${y} l -7 -4 v 8 z`} fill={color} />
}

/**
 * MLP 层架构示意（C1：列式全连接改层间架构）：输入(5 特征) → 隐藏层×N（每层单元数）→ 输出(1)。
 * 块级抽象（同 TransformerDiagram 粒度）：每层一个矩形块 + 层间粗箭头，不画节点/连线。
 * 隐藏层数量由调用点固定（l1/l2 两个），块宽按可用宽度均分恒放得下。
 */
function MlpDiagram({ layers }: { layers: number[] }) {
  const W = DIAGRAM_W
  const H = DIAGRAM_H
  const pad = DIAGRAM_PAD
  const cy = H / 2
  const bh = 44
  const valid = (v: number): number | null => (Number.isFinite(v) && v > 0 ? Math.floor(v) : null)
  const blocks = [
    { label: '输入层', sub: '5 特征' },
    ...layers.map((s, i) => ({ label: `隐藏层 ${i + 1}`, sub: `${valid(s) ?? '—'} 单元` })),
    { label: '输出层', sub: '1 输出' },
  ]
  const gap = 14
  // 块宽按可用宽度均分（调用点固定 2 个隐藏层 → 共 4 块，恒在 viewBox 内）
  const bw = (W - 2 * pad - gap * (blocks.length - 1)) / blocks.length
  let cursor = pad
  const cols = blocks.map((b) => {
    const cx = cursor + bw / 2
    cursor += bw + gap
    return { ...b, cx }
  })

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid meet"
      role="img"
      aria-label="MLP 全连接网络结构"
      style={{ width: '100%', display: 'block' }}
    >
      {/* 输入箭头 */}
      <line x1={6} y1={cy} x2={cols[0].cx - bw / 2} y2={cy} stroke="var(--sr-border)" strokeWidth={1} />
      <ArrowHead x={cols[0].cx - bw / 2} y={cy} />
      <text x={6} y={cy - 10} style={{ fill: 'var(--sr-text-3)', fontSize: 'var(--sr-font-xs)' }}>输入</text>
      {cols.map((b, i) => {
        const left = b.cx - bw / 2
        const right = b.cx + bw / 2
        return (
          <g key={b.label}>
            <rect x={left} y={cy - bh / 2} width={bw} height={bh} rx={8}
              fill="var(--sr-block-bg)" stroke="var(--sr-border)" strokeWidth={1} />
            <text x={b.cx} y={cy - 2} textAnchor="middle"
              style={{ fill: 'var(--sr-text-2)', fontSize: 'var(--sr-font-xs)' }}>{b.label}</text>
            {b.sub && (
              <text x={b.cx} y={cy + 13} textAnchor="middle"
                style={{ fill: 'var(--sr-text-3)', fontSize: 'var(--sr-font-xs)' }}>{b.sub}</text>
            )}
            {i < cols.length - 1 ? (
              <>
                {/* 层间粗箭头（架构流向） */}
                <line x1={right} y1={cy} x2={cols[i + 1].cx - bw / 2} y2={cy} stroke="var(--sr-accent)" strokeWidth={2} opacity={0.8} />
                <ArrowHead x={cols[i + 1].cx - bw / 2} y={cy} color="var(--sr-accent)" />
              </>
            ) : (
              <>
                <line x1={right} y1={cy} x2={W - 6} y2={cy} stroke="var(--sr-border)" strokeWidth={1} />
                <ArrowHead x={W - 6} y={cy} />
                <text x={W - 10} y={cy - 10} textAnchor="end"
                  style={{ fill: 'var(--sr-text-3)', fontSize: 'var(--sr-font-xs)' }}>输出</text>
              </>
            )}
          </g>
        )
      })}
    </svg>
  )
}

/**
 * LSTM 层架构示意（C1：横向时间步单元改层间架构）：输入序列 → LSTM 层×N（隐藏 h）→ 输出层。
 * 块级抽象（同 TransformerDiagram 粒度）：LSTM 块上沿画回环小箭头表达自循环记忆语义。
 * 隐藏单元数 h 由训练超参决定（NetworkDiagram 未接收 hidden 值，标注为通用 h）。
 */
function LstmDiagram({ seqLen, numLayers }: { seqLen: number; numLayers: number }) {
  const W = DIAGRAM_W
  const H = DIAGRAM_H
  const pad = DIAGRAM_PAD
  const cy = H / 2
  const bh = 44
  const n = Math.max(1, Math.floor(numLayers) || 1)
  const blocks = [
    { label: '输入序列', sub: `seq_len=${Math.floor(seqLen) || 1}`, w: 116 },
    { label: 'LSTM 层', sub: `${n} 层 · 隐藏 h`, w: 148 },
    { label: '输出层', sub: '回归头', w: 84 },
  ]
  const gap = 14
  let cursor = pad
  const cols = blocks.map((b) => {
    const cx = cursor + b.w / 2
    cursor += b.w + gap
    return { ...b, cx }
  })

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid meet"
      role="img"
      aria-label="LSTM 循环网络结构"
      style={{ width: '100%', display: 'block' }}
    >
      {/* 输入箭头 */}
      <line x1={6} y1={cy} x2={cols[0].cx - cols[0].w / 2} y2={cy} stroke="var(--sr-border)" strokeWidth={1} />
      <ArrowHead x={cols[0].cx - cols[0].w / 2} y={cy} />
      <text x={6} y={cy - 10} style={{ fill: 'var(--sr-text-3)', fontSize: 'var(--sr-font-xs)' }}>输入</text>
      {cols.map((b, i) => {
        const left = b.cx - b.w / 2
        const right = b.cx + b.w / 2
        const isLstm = i === 1
        return (
          <g key={b.label}>
            {/* LSTM 自循环回环：右端出、弧顶回左端（输出反馈回输入，记忆语义） */}
            {isLstm && (
              <>
                <path d={`M ${right - 8} ${cy - bh / 2 - 3} Q ${b.cx} ${cy - bh / 2 - 18} ${left + 6} ${cy - bh / 2 - 3}`}
                  fill="none" stroke="var(--sr-accent)" strokeWidth={1.2} opacity={0.8} />
                {/* 回环箭头（指向左端，即反馈方向） */}
                <path d={`M ${left + 6} ${cy - bh / 2 - 3} l -5 -3 v 6 z`} fill="var(--sr-accent)" />
              </>
            )}
            <rect x={left} y={cy - bh / 2} width={b.w} height={bh} rx={8}
              fill="var(--sr-block-bg)" stroke={isLstm ? 'var(--sr-accent)' : 'var(--sr-border)'} strokeWidth={isLstm ? 1.4 : 1} />
            <text x={b.cx} y={cy - 2} textAnchor="middle"
              style={{ fill: 'var(--sr-text-2)', fontSize: 'var(--sr-font-xs)' }}>{b.label}</text>
            {b.sub && (
              <text x={b.cx} y={cy + 13} textAnchor="middle"
                style={{ fill: 'var(--sr-text-3)', fontSize: 'var(--sr-font-xs)' }}>{b.sub}</text>
            )}
            {i < cols.length - 1 ? (
              <>
                {/* 层间粗箭头（架构流向） */}
                <line x1={right} y1={cy} x2={cols[i + 1].cx - cols[i + 1].w / 2} y2={cy} stroke="var(--sr-accent)" strokeWidth={2} opacity={0.8} />
                <ArrowHead x={cols[i + 1].cx - cols[i + 1].w / 2} y={cy} color="var(--sr-accent)" />
              </>
            ) : (
              <>
                <line x1={right} y1={cy} x2={W - 6} y2={cy} stroke="var(--sr-border)" strokeWidth={1} />
                <ArrowHead x={W - 6} y={cy} />
                <text x={W - 10} y={cy - 10} textAnchor="end"
                  style={{ fill: 'var(--sr-text-3)', fontSize: 'var(--sr-font-xs)' }}>输出</text>
              </>
            )}
          </g>
        )
      })}
      {/* 语义说明 */}
      <text x={W / 2} y={H - 6} textAnchor="middle"
        style={{ fill: 'var(--sr-text-3)', fontSize: 'var(--sr-font-xs)' }}>
        LSTM 隐藏状态 h 沿时间步传递（单元自循环记忆）· 输入按 seq_len 滑动窗口取特征
      </text>
    </svg>
  )
}

/**
 * Transformer 块级示意：输入 → 输入嵌入（d_model）→ 多头注意力块 ×n_layers → 前馈 FFN → 输出。
 * 块级抽象（不画节点/注意力头细节），符合 "块级示意" 交付粒度。
 */
function TransformerDiagram({ dims, numHeads, numLayers }: { dims: number; numHeads: number; numLayers: number }) {
  const W = DIAGRAM_W
  const H = DIAGRAM_H
  const pad = DIAGRAM_PAD
  const cy = H / 2
  const bh = 44
  const blocks = [
    { label: '输入嵌入', sub: `d_model=${dims}`, w: 96 },
    { label: '多头注意力', sub: `${numHeads} 头 × ${numLayers} 层`, w: 132 },
    { label: '前馈 FFN', sub: '', w: 84 },
    { label: '输出', sub: '回归头', w: 60 },
  ]
  const gap = 14
  let cursor = pad
  const cols = blocks.map((b) => {
    const cx = cursor + b.w / 2
    cursor += b.w + gap
    return { ...b, cx }
  })

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid meet"
      role="img"
      aria-label="Transformer 编码器结构"
      style={{ width: '100%', display: 'block' }}
    >
      {/* 输入箭头 */}
      <line x1={6} y1={cy} x2={cols[0].cx - cols[0].w / 2} y2={cy} stroke="var(--sr-border)" strokeWidth={1} />
      <ArrowHead x={cols[0].cx - cols[0].w / 2} y={cy} />
      <text x={6} y={cy - 10} style={{ fill: 'var(--sr-text-3)', fontSize: 'var(--sr-font-xs)' }}>输入</text>
      {cols.map((b, i) => {
        const left = b.cx - b.w / 2
        const right = b.cx + b.w / 2
        const attention = i === 1
        return (
          <g key={b.label}>
            <rect x={left} y={cy - bh / 2} width={b.w} height={bh} rx={8}
              fill="var(--sr-block-bg)" stroke={attention ? 'var(--sr-accent)' : 'var(--sr-border)'} strokeWidth={attention ? 1.4 : 1} />
            <text x={b.cx} y={cy - 2} textAnchor="middle"
              style={{ fill: 'var(--sr-text-2)', fontSize: 'var(--sr-font-xs)' }}>{b.label}</text>
            {b.sub && (
              <text x={b.cx} y={cy + 13} textAnchor="middle"
                style={{ fill: 'var(--sr-text-3)', fontSize: 'var(--sr-font-xs)' }}>{b.sub}</text>
            )}
            {i < cols.length - 1 ? (
              <>
                <line x1={right} y1={cy} x2={cols[i + 1].cx - cols[i + 1].w / 2} y2={cy} stroke="var(--sr-border)" strokeWidth={1} />
                <ArrowHead x={cols[i + 1].cx - cols[i + 1].w / 2} y={cy} />
              </>
            ) : (
              <>
                <line x1={right} y1={cy} x2={W - 6} y2={cy} stroke="var(--sr-border)" strokeWidth={1} />
                <ArrowHead x={W - 6} y={cy} />
                <text x={W - 10} y={cy - 10} textAnchor="end"
                  style={{ fill: 'var(--sr-text-3)', fontSize: 'var(--sr-font-xs)' }}>输出</text>
              </>
            )}
          </g>
        )
      })}
    </svg>
  )
}

/**
 * T-58 DQN Q 网络块级示意：输入窗口（seq_len×5 特征）→ 展平 → MLP 隐藏层 → 3 个 Q 输出。
 * 块级抽象（同 TransformerDiagram 粒度）：不画节点，底部注明动作三档与奖励语义。
 */
function DqnDiagram({ seqLen }: { seqLen: number }) {
  const W = DIAGRAM_W
  const H = DIAGRAM_H
  const pad = DIAGRAM_PAD
  const cy = H / 2
  const bh = 44
  const blocks = [
    { label: '输入窗口', sub: `seq_len×5 特征`, w: 104 },
    { label: '展平', sub: 'Flatten', w: 68 },
    { label: 'MLP 隐藏层', sub: '', w: 96 },
    { label: 'Q 输出', sub: '3 动作', w: 76 },
  ]
  const gap = 14
  let cursor = pad
  const cols = blocks.map((b) => {
    const cx = cursor + b.w / 2
    cursor += b.w + gap
    return { ...b, cx }
  })

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="xMidYMid meet"
      role="img"
      aria-label="DQN Q 网络结构"
      style={{ width: '100%', display: 'block' }}
    >
      {/* 输入箭头 */}
      <line x1={6} y1={cy} x2={cols[0].cx - cols[0].w / 2} y2={cy} stroke="var(--sr-border)" strokeWidth={1} />
      <ArrowHead x={cols[0].cx - cols[0].w / 2} y={cy} />
      <text x={6} y={cy - 10} style={{ fill: 'var(--sr-text-3)', fontSize: 'var(--sr-font-xs)' }}>输入</text>
      {cols.map((b, i) => {
        const left = b.cx - b.w / 2
        const right = b.cx + b.w / 2
        const q = i === cols.length - 1
        return (
          <g key={b.label}>
            <rect x={left} y={cy - bh / 2} width={b.w} height={bh} rx={8}
              fill="var(--sr-block-bg)" stroke={q ? 'var(--sr-accent)' : 'var(--sr-border)'} strokeWidth={q ? 1.4 : 1} />
            <text x={b.cx} y={cy - 2} textAnchor="middle"
              style={{ fill: 'var(--sr-text-2)', fontSize: 'var(--sr-font-xs)' }}>{b.label}</text>
            {b.sub && (
              <text x={b.cx} y={cy + 13} textAnchor="middle"
                style={{ fill: 'var(--sr-text-3)', fontSize: 'var(--sr-font-xs)' }}>{b.sub}</text>
            )}
            {i < cols.length - 1 ? (
              <>
                <line x1={right} y1={cy} x2={cols[i + 1].cx - cols[i + 1].w / 2} y2={cy} stroke="var(--sr-border)" strokeWidth={1} />
                <ArrowHead x={cols[i + 1].cx - cols[i + 1].w / 2} y={cy} />
              </>
            ) : (
              <>
                <line x1={right} y1={cy} x2={W - 6} y2={cy} stroke="var(--sr-border)" strokeWidth={1} />
                <ArrowHead x={W - 6} y={cy} />
                <text x={W - 10} y={cy - 10} textAnchor="end"
                  style={{ fill: 'var(--sr-text-3)', fontSize: 'var(--sr-font-xs)' }}>输出</text>
              </>
            )}
          </g>
        )
      })}
      <text x={W / 2} y={H - 6} textAnchor="middle"
        style={{ fill: 'var(--sr-text-3)', fontSize: 'var(--sr-font-xs)' }}>
        动作: 0 空仓 / 1 半仓 / 2 满仓 · 奖励=收益−成本 · seq_len={Math.floor(seqLen) || 1}
      </text>
    </svg>
  )
}

/** T-57 按 arch 分发网络结构示意（C1 后全为块级层架构：mlp=层框 / lstm=层框+自循环 / transformer=块级编码器）；
 *  T-58 dqn=Q 网络块级示意（输入窗口→展平→MLP→3 动作 Q 输出） */
function NetworkDiagram({ arch, layers, seqLen, dims, numHeads, numLayers }: {
  arch: string
  layers: number[]
  seqLen: number
  dims: number
  numHeads: number
  numLayers: number
}) {
  if (arch === 'lstm') return <LstmDiagram seqLen={seqLen} numLayers={numLayers} />
  if (arch === 'transformer') return <TransformerDiagram dims={dims} numHeads={numHeads} numLayers={numLayers} />
  if (arch === 'dqn') return <DqnDiagram seqLen={seqLen} />
  return <MlpDiagram layers={layers} />
}

/** T-57 权重/注意力热力图：matrix → heatmap series，visualMap 三档 accent/bg/up
 * 随主题令牌切换（同 EvaluationResults IC 条带图模式，canvas 用 JS 常量，isDark 进重绘依赖）。
 * tooltip 显示行列与值；attention(softmax) 值域 [0,1]，权重类对称 ±|max|。 */
function WeightHeatmap({ viz, isDark }: { viz: NonNullable<NNMeta['viz']>; isDark: boolean }) {
  const ref = useRef<HTMLDivElement>(null)
  const inst = useRef<echarts.ECharts | null>(null)
  useEchartsLifecycle(ref, inst)
  useEffect(() => () => {
    const el = ref.current ? echarts.getInstanceByDom(ref.current) : null
    if (el && !el.isDisposed()) el.dispose()
    inst.current = null
  }, [])

  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (!inst.current) inst.current = echarts.init(el)
    const t = chartTheme(isDark)
    const rows = viz.matrix.length
    const cols = viz.matrix[0]?.length ?? 0
    if (rows === 0 || cols === 0) return
    const data: [number, number, number][] = viz.matrix.flatMap((row, i) =>
      row.map((v, j) => [j, i, v] as [number, number, number]))
    const absMax = Math.max(0.001, ...data.map(([, , v]) => Math.abs(v)))
    const rowLabels = viz.rowLabels?.length === rows ? viz.rowLabels : Array.from({ length: rows }, (_, i) => String(i))
    const colLabels = viz.colLabels?.length === cols ? viz.colLabels : Array.from({ length: cols }, (_, j) => String(j))
    inst.current.setOption({
      animation: false,
      tooltip: {
        position: 'top',
        formatter: (p: any) => `行 ${p.data[1]} · 列 ${p.data[0]}<br/>值 ${p.data[2]}`,
      },
      grid: { left: 40, right: 16, top: 16, bottom: 56 },
      xAxis: {
        type: 'category', data: colLabels,
        axisLabel: { fontSize: 10, color: t.text3, interval: 0, rotate: cols > 12 ? 45 : 0 },
        splitArea: { show: true },
      },
      yAxis: {
        type: 'category', data: rowLabels, inverse: true,
        axisLabel: { fontSize: 10, color: t.text3 },
        splitArea: { show: true },
      },
      visualMap: {
        // attention 为 softmax 概率 [0,1]；权重类以 ±|max| 对称（蓝→白→红，accent/bg/up）
        min: viz.kind === 'attention' ? 0 : -absMax,
        max: viz.kind === 'attention' ? 1 : absMax,
        orient: 'horizontal', left: 'center', bottom: 0,
        itemWidth: 14, itemHeight: 120,
        textStyle: { fontSize: 10, color: t.text3 },
        inRange: { color: [t.accent, t.bg, t.up] },
      },
      series: [{
        type: 'heatmap', data,
        itemStyle: { borderColor: t.bg, borderWidth: 1 },
      }],
    }, true)
  }, [viz, isDark])

  return (
    <div ref={ref} role="img" aria-label="权重/注意力热力图"
      style={{ width: '100%', height: 'var(--sr-chart-h-md)' }} />
  )
}

/**
 * T-58 DQN 通用折线图（训练奖励曲线 / 评估净值曲线共用）：data 数值序列按序索引为 x 轴。
 * 生命周期/主题驱动重绘与损失曲线同模式（useEchartsLifecycle + isDark 进依赖）。
 */
function LineCurve({ data, title, isDark, color = 'accent', yName }: {
  data: number[] | null | undefined
  title: string
  isDark: boolean
  /** 曲线颜色令牌名（accent=强调 / up=涨），canvas 用 JS 常量，isDark 驱动 */
  color?: 'accent' | 'up'
  /** x 轴名称（如 episode） */
  yName?: string
}) {
  const ref = useRef<HTMLDivElement>(null)
  const inst = useRef<echarts.ECharts | null>(null)
  useEchartsLifecycle(ref, inst)
  useEffect(() => () => {
    const el = ref.current ? echarts.getInstanceByDom(ref.current) : null
    if (el && !el.isDisposed()) el.dispose()
    inst.current = null
  }, [])

  useEffect(() => {
    const el = ref.current
    if (!el || !data || data.length === 0) return
    if (!inst.current) inst.current = echarts.init(el)
    const t = chartTheme(isDark)
    const seriesColor = color === 'up' ? t.up : t.accent
    inst.current.setOption({
      animation: false,
      tooltip: { trigger: 'axis' },
      legend: { data: [title], textStyle: { color: t.text2, fontSize: 11 } },
      grid: { left: 50, right: 16, top: 24, bottom: 28 },
      dataZoom: chartDataZoom(isDark),
      xAxis: {
        type: 'category',
        data: data.map((_, i) => String(i + 1)),
        name: yName ?? '',
        axisLabel: { fontSize: 10, color: t.text3 },
      },
      yAxis: { ...chartYAxis(isDark) },
      series: [{
        name: title, type: 'line', data, showSymbol: false,
        lineStyle: { width: 2, color: seriesColor }, itemStyle: { color: seriesColor },
        areaStyle: color === 'up' ? { color: t.up, opacity: 0.08 } : undefined,
      }],
    }, true)
  }, [data, isDark, title, color, yName])

  return (
    <div ref={ref} role="img" aria-label={title}
      style={{ width: '100%', height: 'var(--sr-chart-h-md)' }} />
  )
}

/**
 * 神经网络因子训练：配置表单（自持持久化 useForm 'nn:state'）→ createExperiment('neural_train')
 * → useJobFlow 轮询 → 结果（损失曲线 echarts + 训练/验证 IC，PageState 三态收编）。
 * 布局同 FactorMining（配置 + 结果双栏，移动端单栏堆叠）。
 */
export default function NeuralStudy() {
  const isDark = useThemeStore((s) => s.theme) === 'dark'
  const datasets = useDatasets()
  const [stored, setStored] = usePersistentState<NnFormState>('nn:state', NN_DEFAULTS)
  // 自持持久化 useForm：initialValues 取 sessionStorage 快照；变更即 setStored 持久化（键 'nn:state' 不变）
  // T-57：合并 NN_DEFAULTS——旧会话快照缺 arch/序列超参等新键，以默认值兜底
  const form = useForm<NnFormState>({
    initialValues: { ...NN_DEFAULTS, ...stored },
    validate: {
      datasetId: (v) => (v != null ? null : '必选'),
      arch: (v) => (v ? null : '必选'),
      l1: (v) => (v != null && v >= 1 && v <= 1024 ? null : '1~1024'),
      l2: (v) => (v != null && v >= 1 && v <= 1024 ? null : '1~1024'),
      activation: (v) => (v ? null : '必选'),
      lr: (v) => (v != null && v >= 0.0001 && v <= 1 ? null : '0.0001~1'),
      epochs: (v) => (v != null && v >= 1 && v <= 500 ? null : '1~500'),
      batchSize: (v) => (v != null && v >= 1 && v <= 8192 ? null : '1~8192'),
      horizon: (v) => (v != null && v >= 1 && v <= 60 ? null : '1~60'),
      seed: (v) => (v != null && v >= 0 ? null : '≥0'),
      // 序列架构超参（与后端 _resolve_arch_hparams 校验对齐；隐藏字段恒有默认值，validate 恒过）
      hidden: (v) => (v != null && v >= 1 && v <= 1024 ? null : '1~1024'),
      numLayers: (v) => (v != null && v >= 1 && v <= 16 ? null : '1~16'),
      seqLen: (v) => (v != null && v >= 2 && v <= 512 ? null : '2~512'),
      dims: (v) => (v != null && v >= 2 && v <= 512 && v % 2 === 0 ? null : '2~512 偶数'),
      numHeads: (v) => (v != null && v >= 1 && v <= 64 ? null : '1~64'),
      // T-58 DQN 超参（与后端 rl_train handler 校验对齐；隐藏字段恒有默认值，validate 恒过）
      episodes: (v) => (v != null && v >= 1 && v <= 500 ? null : '1~500'),
      gamma: (v) => (v != null && v >= 0 && v <= 1 ? null : '0~1'),
      cost: (v) => (v != null && v >= 0 && v <= 0.1 ? null : '0~0.1'),
    },
  })
  // 变更即持久化（sessionStorage 'nn:state'）
  useEffect(() => { setStored(form.values) }, [form.values]) // eslint-disable-line react-hooks/exhaustive-deps

  // T-58：本次提交的 job_type（dqn→rl_train / 其他→neural_train），makeOptimistic 乐观占位按实际类型
  const jobTypeRef = useRef<'neural_train' | 'rl_train'>('neural_train')

  const { job, polling, submit } = useJobFlow<NNFlowJob>({
    submit: async () => {
      const v = form.values
      // T-58：dqn 分流——rl_train 任务（experiments 白名单注册），参数键与后端 handler 对齐；
      // hidden/batch_size/capacity/target_sync_steps 走后端缺省
      if (v.arch === 'dqn') {
        jobTypeRef.current = 'rl_train'
        const r = await createExperiment('rl_train', {
          dataset_id: v.datasetId,
          seq_len: v.seqLen,
          episodes: v.episodes,
          lr: v.lr,
          gamma: v.gamma,
          cost: v.cost,
          seed: v.seed,
        })
        toast.success(`训练任务 #${r.job_id} 已提交`)
        return r
      }
      // T-57：arch 分发——mlp 保持旧提交参数（layers:[l1,l2] 不变）；
      // lstm/transformer 提交 arch_hparams（键与后端 ARCH_REGISTRY default_hparams 对齐）
      jobTypeRef.current = 'neural_train'
      const params: Record<string, unknown> = {
        dataset_id: v.datasetId,
        activation: v.activation,
        lr: v.lr,
        epochs: v.epochs,
        batch_size: v.batchSize,
        seed: v.seed,
        horizon: v.horizon,
        arch: v.arch,
      }
      if (v.arch === 'mlp') {
        params.layers = [v.l1, v.l2]
      } else if (v.arch === 'lstm') {
        params.arch_hparams = { hidden: v.hidden, num_layers: v.numLayers, seq_len: v.seqLen }
      } else {
        params.arch_hparams = { dims: v.dims, num_heads: v.numHeads, num_layers: v.numLayers, seq_len: v.seqLen }
      }
      const r = await createExperiment('neural_train', params)
      toast.success(`训练任务 #${r.job_id} 已提交`)
      return r
    },
    makeOptimistic: (id) => ({ id, status: 'pending', progress: 0, result: null, job_type: jobTypeRef.current }),
    onDone: () => toast.success('训练完成'),
    onFailed: (j) => toast.error('训练失败: ' + (j.error ?? '')),
  })

  // 任务实时事件订阅（SSE /experiments/{job_id}/events）；未提交时 job 为 null → 不连接
  const { events } = useJobEvents(job?.id)

  const run = async () => {
    if (!form.values.datasetId) { toast.warning('请先选择数据集'); return }
    try {
      await submit()
    } catch (e: any) {
      toast.error('提交训练失败: ' + (e?.message ?? e))
    }
  }

  // 结果 meta：仅 done 且有 result 时解析；useMemo 稳定引用，避免轮询渲染引发图表重绘
  const meta = useMemo(() => {
    if (job?.status !== 'done' || !job?.result?.meta) return undefined
    return job.result.meta as unknown as NNMeta
  }, [job])

  // phase 可选字段：job 类型已含（JobListItem.phase?），直接读取
  const jobPhase = job?.phase

  // ===== T-57 实时损失增量（SSE progress 事件 payload 扩展 epoch/train_loss/val_loss） =====
  // progress 事件环形缓冲有上限（前端 200 条），按 epoch 合并累积可存活上限截断与断线重连；
  // 新任务（job id 变化）重置；done 后以 meta 全量校准（lossSeries 优先取 meta）。
  interface LossPoint { epoch: number; train: number; val: number | null }
  interface LossSeries { epochs: number[]; train: number[]; val: (number | null)[] }
  // SSE 事件为服务端原始 JSON，JobEvent 类型未含扩展键——页面局部断言（同 NNFlowJob 模式）
  const lossEventKeys = (ev: unknown): LossPoint | null => {
    const x = ev as { type?: string; epoch?: unknown; train_loss?: unknown; val_loss?: unknown }
    if (x.type !== 'progress' || typeof x.epoch !== 'number' || typeof x.train_loss !== 'number') return null
    return { epoch: x.epoch, train: x.train_loss, val: typeof x.val_loss === 'number' ? x.val_loss : null }
  }
  const [liveLoss, setLiveLoss] = useState<LossSeries | null>(null)
  const jobIdRef = useRef<number | null>(null)
  useEffect(() => {
    const jid = job?.id
    if (jid != null && jobIdRef.current !== jid) {
      jobIdRef.current = jid
      setLiveLoss(null) // 新任务：清空上一任务的流式曲线
    }
    if (!events?.length) return
    const points: LossPoint[] = []
    for (const ev of events) {
      const p = lossEventKeys(ev)
      if (p) points.push(p)
    }
    if (!points.length) return
    setLiveLoss((prev) => {
      const map = new Map<number, { t: number; v: number | null }>()
      if (prev) prev.epochs.forEach((e, i) => map.set(e, { t: prev.train[i], v: prev.val[i] }))
      for (const p of points) map.set(p.epoch, { t: p.train, v: p.val })
      const sorted = [...map.entries()].sort((a, b) => a[0] - b[0])
      return {
        epochs: sorted.map(([e]) => e),
        train: sorted.map(([, v]) => v.t),
        val: sorted.map(([, v]) => v.v),
      }
    })
  }, [events, job?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  // ===== 入库为 nn 因子 =====
  const [factorOpen, setFactorOpen] = useState(false)
  const [factorName, setFactorName] = useState('')
  const [savingFactor, setSavingFactor] = useState(false)

  // 默认因子名：NN-{数据集名或id}-{layers}-{epochs}ep
  const datasetLabel = useMemo(() => {
    const d = (datasets ?? []).find((x) => x.id === form.values.datasetId)
    return d?.name ?? (form.values.datasetId != null ? `ds${form.values.datasetId}` : 'ds')
  }, [datasets, form.values.datasetId])

  const openFactorModal = () => {
    // T-57：默认因子名按 arch 取——mlp 沿用 l1xl2，序列架构用 arch 键
    const archShort = form.values.arch === 'mlp'
      ? `${form.values.l1}x${form.values.l2}`
      : form.values.arch
    setFactorName(`NN-${datasetLabel}-${archShort}-${form.values.epochs}ep`)
    setFactorOpen(true)
  }

  const saveFactor = async () => {
    const name = factorName.trim()
    if (!name) { toast.warning('请输入因子名称'); return }
    if (meta == null || meta.model_id == null) { toast.error('训练结果缺少模型 ID，无法入库'); return }
    setSavingFactor(true)
    try {
      const archLabel = ARCH_LABELS[form.values.arch] ?? form.values.arch
      // T-58：dqn 的 archDetail 用 RL 超参（与提交参数同源），避免落入序列架构 JSON 分支
      const archDetail = form.values.arch === 'mlp'
        ? `${form.values.l1}x${form.values.l2}`
        : form.values.arch === 'dqn'
          ? JSON.stringify({ seq_len: form.values.seqLen, episodes: form.values.episodes, lr: form.values.lr, gamma: form.values.gamma, cost: form.values.cost })
          : JSON.stringify(form.values.arch === 'lstm'
            ? { hidden: form.values.hidden, num_layers: form.values.numLayers, seq_len: form.values.seqLen }
            : { dims: form.values.dims, num_heads: form.values.numHeads, num_layers: form.values.numLayers, seq_len: form.values.seqLen })
      await createFactor({
        name,
        kind: 'nn',
        model_id: meta.model_id,
        description: `${archLabel} ${archDetail} ${form.values.activation} horizon=${form.values.horizon}`,
      })
      setFactorOpen(false)
      toast.success(`因子「${name}」已入库`)
    } catch (e: any) {
      toast.error('入库失败: ' + (e?.response?.data?.detail ?? e?.message ?? String(e)))
    } finally {
      setSavingFactor(false)
    }
  }

  // ===== 损失曲线（echarts Line 双线，isDark 驱动 canvas 颜色） =====
  // T-57 数据源双轨：done 后 meta 全量（权威）；训练中取流式增量（lossSeries 归一为同构形状）
  const chartRef = useRef<HTMLDivElement>(null)
  const chartInst = useRef<echarts.ECharts | null>(null)
  useEchartsLifecycle(chartRef, chartInst)

  useEffect(() => () => {
    const inst = chartRef.current ? echarts.getInstanceByDom(chartRef.current) : null
    if (inst && !inst.isDisposed()) inst.dispose()
    chartInst.current = null
  }, [])

  const lossSeries = useMemo<LossSeries | null>(() => {
    // T-58：dqn meta 无 train_loss/val_loss（承载 train_rewards），损失曲线仅非 dqn 时取 meta 全量
    if (meta && meta.model_kind !== 'dqn') {
      const n = Math.max(meta.train_loss.length, meta.val_loss.length)
      return {
        epochs: Array.from({ length: n }, (_, i) => i + 1),
        train: meta.train_loss,
        val: meta.val_loss,
      }
    }
    return liveLoss && liveLoss.epochs.length > 0 ? liveLoss : null
  }, [meta, liveLoss])

  useEffect(() => {
    const el = chartRef.current
    if (!el || !lossSeries) return
    // 实时分支↔结果分支切换会卸载/重挂 chart 容器：实例已脱离 DOM 时重建
    const inst = chartInst.current
    const attached = inst != null && el.contains(inst.getDom())
    if (!attached) {
      if (inst) inst.dispose()
      chartInst.current = echarts.getInstanceByDom(el) ?? echarts.init(el)
    }
    const t = chartTheme(isDark)
    const { epochs, train, val } = lossSeries
    chartInst.current!.setOption({
      animation: false,
      tooltip: { trigger: 'axis' },
      legend: { data: ['训练损失', '验证损失'], textStyle: { color: t.text2, fontSize: 11 } },
      grid: { left: 50, right: 16, top: 24, bottom: 28 },
      dataZoom: chartDataZoom(isDark),
      xAxis: {
        type: 'category',
        data: epochs.map(String),
        name: 'epoch',
        axisLabel: { fontSize: 10, color: t.text3 },
      },
      yAxis: { ...chartYAxis(isDark) },
      series: [
        {
          name: '训练损失', type: 'line', data: train, showSymbol: false,
          lineStyle: { width: 2, color: t.accent }, itemStyle: { color: t.accent },
        },
        {
          name: '验证损失', type: 'line', data: val, showSymbol: false,
          lineStyle: { width: 2, color: t.amber }, itemStyle: { color: t.amber },
        },
      ],
    }, true)
  }, [lossSeries, isDark])

  // ===== 渲染 =====
  const jobStatus = job?.status

  // T-57 实时曲线分支：任务未终态且已有流式损失增量 → 直接渲染曲线（PageState 加载骨架让位）
  const showLive = !meta && job != null && job.status !== 'done' && liveLoss != null && liveLoss.epochs.length > 0

  const configCard = (
    <CardShell title="网络配置">
      <Grid gap={12}>
        <GridCol span={24}>
          <FormRow label="数据集">
            <DatasetSelector
              datasets={(datasets ?? []).map((d) => ({ id: d.id, name: d.name }))}
              value={form.values.datasetId}
              onChange={(v) => form.setFieldValue('datasetId', v)}
              width="100%"
              placeholder={datasets ? (datasets.length ? '选择数据集' : '暂无数据集') : '加载中…'}
              loading={!datasets}
              disabled={!!datasets && !datasets.length}
            />
          </FormRow>
        </GridCol>
        {/* T-57：架构选择——mlp 保留 l1/l2 字段（旧行为）；序列架构切换为超参表单 */}
        <GridCol span={24}>
          <FormRow label="架构">
            <Select data={ARCH_OPTIONS} value={form.values.arch}
              onChange={(v) => {
                if (v == null) return
                form.setFieldValue('arch', v)
                // 切换时重置为该架构注册表默认超参（键与后端 default_hparams 对齐）
                if (v === 'lstm') {
                  form.setFieldValue('hidden', 32)
                  form.setFieldValue('numLayers', 1)
                  form.setFieldValue('seqLen', 20)
                } else if (v === 'transformer') {
                  form.setFieldValue('dims', 32)
                  form.setFieldValue('numHeads', 4)
                  form.setFieldValue('numLayers', 2)
                  form.setFieldValue('seqLen', 20)
                } else if (v === 'dqn') {
                  // T-58：dqn 默认与后端 rl_train handler 缺省对齐（20/50/1e-3/0.99/0.001）
                  form.setFieldValue('seqLen', 20)
                  form.setFieldValue('episodes', 50)
                  form.setFieldValue('lr', 0.001)
                  form.setFieldValue('gamma', 0.99)
                  form.setFieldValue('cost', 0.001)
                }
              }} />
          </FormRow>
          {form.values.arch !== 'mlp' && (
            <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
              {form.values.arch === 'dqn'
                ? 'DQN 强化学习依赖后端 MLX 支持；环境不可用时任务将失败并展示原因'
                : '序列架构（lstm/transformer）依赖后端 MLX 支持；环境不可用时任务将失败并展示原因'}
            </Text>
          )}
        </GridCol>
        {form.values.arch === 'mlp' ? (
          <>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="隐藏层">
                <NumberInput min={1} max={1024}
                  value={form.values.l1}
                  onChange={(v) => form.setFieldValue('l1', numOr(v, FIELD_FALLBACK.l1 as number))} />
              </FormRow>
            </GridCol>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="隐藏层2">
                <NumberInput min={1} max={1024}
                  value={form.values.l2}
                  onChange={(v) => form.setFieldValue('l2', numOr(v, FIELD_FALLBACK.l2 as number))} />
              </FormRow>
            </GridCol>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="激活函数">
                <Select data={ACTIVATION_OPTIONS} value={form.values.activation}
                  onChange={(v) => { if (v != null) form.setFieldValue('activation', v) }} />
              </FormRow>
            </GridCol>
          </>
        ) : form.values.arch === 'lstm' ? (
          <>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="hidden_size">
                <NumberInput min={1} max={1024}
                  value={form.values.hidden}
                  onChange={(v) => form.setFieldValue('hidden', numOr(v, FIELD_FALLBACK.hidden as number))} />
              </FormRow>
            </GridCol>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="num_layers">
                <NumberInput min={1} max={16}
                  value={form.values.numLayers}
                  onChange={(v) => form.setFieldValue('numLayers', numOr(v, FIELD_FALLBACK.numLayers as number))} />
              </FormRow>
            </GridCol>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="seq_len">
                <NumberInput min={2} max={512}
                  value={form.values.seqLen}
                  onChange={(v) => form.setFieldValue('seqLen', numOr(v, FIELD_FALLBACK.seqLen as number))} />
              </FormRow>
            </GridCol>
          </>
        ) : form.values.arch === 'dqn' ? (
          /* T-58：DQN 超参（键与后端 rl_train handler 对齐；epochs/batch_size/horizon 不适用于 RL 训练，隐藏） */
          <>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="seq_len">
                <NumberInput min={2} max={512}
                  value={form.values.seqLen}
                  onChange={(v) => form.setFieldValue('seqLen', numOr(v, FIELD_FALLBACK.seqLen as number))} />
              </FormRow>
            </GridCol>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="episodes">
                <NumberInput min={1} max={500}
                  value={form.values.episodes}
                  onChange={(v) => form.setFieldValue('episodes', numOr(v, FIELD_FALLBACK.episodes as number))} />
              </FormRow>
            </GridCol>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="lr">
                <NumberInput min={0.0001} max={1} step={0.001} decimalScale={4}
                  value={form.values.lr}
                  onChange={(v) => form.setFieldValue('lr', numOr(v, FIELD_FALLBACK.lr as number))} />
              </FormRow>
            </GridCol>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="gamma">
                <NumberInput min={0} max={1} step={0.01} decimalScale={2}
                  value={form.values.gamma}
                  onChange={(v) => form.setFieldValue('gamma', numOr(v, FIELD_FALLBACK.gamma as number))} />
              </FormRow>
            </GridCol>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="cost">
                <NumberInput min={0} max={0.1} step={0.0001} decimalScale={4}
                  value={form.values.cost}
                  onChange={(v) => form.setFieldValue('cost', numOr(v, FIELD_FALLBACK.cost as number))} />
              </FormRow>
            </GridCol>
          </>
        ) : (
          <>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="d_model">
                <NumberInput min={2} max={512} step={2}
                  value={form.values.dims}
                  onChange={(v) => form.setFieldValue('dims', numOr(v, FIELD_FALLBACK.dims as number))} />
              </FormRow>
            </GridCol>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="n_heads">
                <NumberInput min={1} max={64}
                  value={form.values.numHeads}
                  onChange={(v) => form.setFieldValue('numHeads', numOr(v, FIELD_FALLBACK.numHeads as number))} />
              </FormRow>
            </GridCol>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="n_layers">
                <NumberInput min={1} max={16}
                  value={form.values.numLayers}
                  onChange={(v) => form.setFieldValue('numLayers', numOr(v, FIELD_FALLBACK.numLayers as number))} />
              </FormRow>
            </GridCol>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="seq_len">
                <NumberInput min={2} max={512}
                  value={form.values.seqLen}
                  onChange={(v) => form.setFieldValue('seqLen', numOr(v, FIELD_FALLBACK.seqLen as number))} />
              </FormRow>
            </GridCol>
          </>
        )}
        {/* T-58：dqn 的 lr/episodes 已含于 dqn 表单；epochs/batch_size/horizon 为监督训练口径，RL 场景隐藏 */}
        {form.values.arch !== 'dqn' && (
          <>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="学习率">
                <NumberInput min={0.0001} max={1} step={0.001} decimalScale={4}
                  value={form.values.lr}
                  onChange={(v) => form.setFieldValue('lr', numOr(v, FIELD_FALLBACK.lr as number))} />
              </FormRow>
            </GridCol>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="epochs">
                <NumberInput min={1} max={500}
                  value={form.values.epochs}
                  onChange={(v) => form.setFieldValue('epochs', numOr(v, FIELD_FALLBACK.epochs as number))} />
              </FormRow>
            </GridCol>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="batch_size">
                <NumberInput min={1} max={8192}
                  value={form.values.batchSize}
                  onChange={(v) => form.setFieldValue('batchSize', numOr(v, FIELD_FALLBACK.batchSize as number))} />
              </FormRow>
            </GridCol>
            <GridCol span={{ base: 24, sm: 12 }}>
              <FormRow label="horizon">
                <NumberInput min={1} max={60} suffix="日"
                  value={form.values.horizon}
                  onChange={(v) => form.setFieldValue('horizon', numOr(v, FIELD_FALLBACK.horizon as number))} />
              </FormRow>
            </GridCol>
          </>
        )}
        <GridCol span={{ base: 24, sm: 12 }}>
          <FormRow label="seed">
            <NumberInput min={0}
              value={form.values.seed}
              onChange={(v) => form.setFieldValue('seed', numOr(v, FIELD_FALLBACK.seed as number))} />
          </FormRow>
        </GridCol>
      </Grid>
      <div role="group" aria-label="网络结构预览"
        style={{ marginTop: 'var(--sr-pad-md)', padding: 'var(--sr-pad-sm)',
          border: '1px solid var(--sr-border)', borderRadius: 8 }}>
        {/* T-57：按 arch 分发拓扑（C1 后统一块级层架构：mlp/lstm=层框，transformer/dqn=块级） */}
        <NetworkDiagram
          arch={form.values.arch}
          layers={[form.values.l1, form.values.l2]}
          seqLen={form.values.seqLen}
          dims={form.values.dims}
          numHeads={form.values.numHeads}
          numLayers={form.values.numLayers}
        />
      </div>
    </CardShell>
  )

  const runCard = (
    <CardShell title="训练">
      <Flex direction="column" gap="var(--sr-pad-sm)" style={{ width: '100%' }}>
        <Button variant="filled" leftSection={<IconPlayerPlay size={16} />} fullWidth
          loading={polling} disabled={!form.values.datasetId} onClick={run}>
          {jobStatus === 'running' || jobStatus === 'pending' ? '训练中…' : '开始训练'}
        </Button>
        {job && (
          <Flex direction="column" gap="var(--sr-pad-sm)" style={{ width: '100%' }}>
            <TaskProgress job={job} typeLabel={{ neural_train: '神经网络训练', rl_train: '强化学习训练' }} />
            <MonitorPanel job={job} events={events} />
            {jobPhase && (
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
                阶段: {jobPhase}
              </Text>
            )}
            {job.status === 'failed' && (
              <Text c="red" style={{ fontSize: 'var(--sr-font-sm)', whiteSpace: 'pre-wrap' }}>
                {job.error}
              </Text>
            )}
          </Flex>
        )}
      </Flex>
    </CardShell>
  )

  const icSummary = meta ? (
    meta.model_kind === 'dqn' ? (
      /* T-58：DQN 评估指标（meta.eval；总收益涨跌着色，最大回撤负值落跌色） */
      <Flex wrap="wrap" gap="var(--sr-pad-md)">
        <div style={{ flex: '1 1 140px', minWidth: 0 }}>
          <MetricStat label="总收益" value={meta.eval?.total_return != null ? fmtPct(meta.eval.total_return) : '—'} tone={icTone(meta.eval?.total_return)} />
        </div>
        <div style={{ flex: '1 1 140px', minWidth: 0 }}>
          <MetricStat label="夏普" value={meta.eval?.sharpe != null ? fmtRatio(meta.eval.sharpe, 2) : '—'} />
        </div>
        <div style={{ flex: '1 1 140px', minWidth: 0 }}>
          <MetricStat label="最大回撤" value={meta.eval?.max_drawdown != null ? fmtPct(meta.eval.max_drawdown) : '—'} tone={icTone(meta.eval?.max_drawdown)} />
        </div>
        <div style={{ flex: '1 1 140px', minWidth: 0 }}>
          <MetricStat label="交易次数" value={meta.eval?.trades != null ? fmtNum(meta.eval.trades) : '—'} />
        </div>
      </Flex>
    ) : (
      <Flex wrap="wrap" gap="var(--sr-pad-md)">
        <div style={{ flex: '1 1 140px', minWidth: 0 }}>
          <MetricStat label="训练 IC" value={meta.train_ic != null ? fmtRatio(meta.train_ic) : '—'} tone={icTone(meta.train_ic)} />
        </div>
        <div style={{ flex: '1 1 140px', minWidth: 0 }}>
          <MetricStat label="验证 IC" value={meta.val_ic != null ? fmtRatio(meta.val_ic) : '—'} tone={icTone(meta.val_ic)} />
        </div>
        <div style={{ flex: '1 1 140px', minWidth: 0 }}>
          <MetricStat label="epochs" value={meta.epochs ?? '—'} />
        </div>
        <div style={{ flex: '1 1 140px', minWidth: 0 }}>
          <MetricStat label="模型" value={ARCH_LABELS[meta.model_kind] ?? meta.model_kind ?? '—'} sub={meta.checkpoint ? 'checkpoint 已保存' : undefined} />
        </div>
      </Flex>
    )
  ) : null

  return (
    <PageShell>
      {/* 流程条（stepsHidden：工作台内 Tabs 承担导航，此处仅上下文芯片；T-46 管道化常驻） */}
      <ResearchFlowBar
        current="neural"
        dataset={form.values.datasetId != null && datasets
          ? datasets.find((d) => d.id === form.values.datasetId) ?? null
          : null}
        stepsHidden
      />
      {/* L0 结论条(05 §5.3):训练/验证 IC;任务未完成或无结果时数字位骨架(meta 仅 done 且含结果时解析)
          T-58: dqn(model_kind="dqn")结论位换 总收益/夏普 */}
      <StatStrip
        loading={!meta}
        style={{ marginBottom: 'var(--sr-gap-row)', flexShrink: 0 }}
        items={meta?.model_kind === 'dqn' ? [
          { key: 'total-return', label: '总收益', value: meta.eval?.total_return != null ? fmtPct(meta.eval.total_return) : '—', tone: icTone(meta.eval?.total_return) },
          { key: 'sharpe', label: '夏普', value: meta.eval?.sharpe != null ? fmtRatio(meta.eval.sharpe, 2) : '—', tone: 'plain' },
        ] : [
          { key: 'train-ic', label: '训练 IC', value: meta?.train_ic != null ? fmtRatio(meta.train_ic) : '—', tone: icTone(meta?.train_ic) },
          { key: 'val-ic', label: '验证 IC', value: meta?.val_ic != null ? fmtRatio(meta.val_ic) : '—', tone: icTone(meta?.val_ic) },
        ]}
      />
      <Modal
        opened={factorOpen}
        onClose={() => setFactorOpen(false)}
        title="入库为 nn 因子"
        size={380}
        keepMounted
      >
        <Text c="dimmed"
          style={{ fontSize: 'var(--sr-font-xs)', marginBottom: 'var(--sr-pad-sm)' }}>
          生成 kind=nn 因子，表达式由服务端自动生成
        </Text>
        <TextInput value={factorName} onChange={(e) => setFactorName(e.target.value)}
          maxLength={128} placeholder="因子名称"
          onKeyDown={(e) => { if (e.key === 'Enter') void saveFactor() }} />
        <Group justify="flex-end" gap="var(--sr-pad-sm)" mt="var(--sr-pad-lg)">
          <Button variant="default" onClick={() => setFactorOpen(false)}>取消</Button>
          <Button variant="filled" loading={savingFactor} onClick={() => void saveFactor()}>入库</Button>
        </Group>
      </Modal>
      <ResearchSplit gap="var(--sr-pad-md)" config={
        <Flex direction="column" gap="var(--sr-pad-md)">
          {configCard}
          {runCard}
        </Flex>
      }>
        <CardShell title="训练结果">
            {/* 结果区五态 → PageState（loading/failed/empty/ready）：无任务空态、失败错误态、
                运行中加载态、done 无 meta 空态、done 有 meta 内容态。
                T-57 实时分支：训练中且已收到流式损失增量时直接渲染曲线（不挡加载骨架） */}
            {showLive ? (
              <Flex direction="column" gap="var(--sr-pad-md)" style={{ width: '100%' }}>
                <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
                  训练中 · 实时损失（已上报 {liveLoss!.epochs.length} 个 epoch，完成前以流式数据为准）
                </Text>
                <div ref={chartRef} role="img" aria-label="神经网络训练实时损失曲线" style={{ width: '100%', height: 'var(--sr-chart-h-md)' }} />
              </Flex>
            ) : (
              <PageState<NNMeta>
                status={!job ? 'empty' : job.status === 'failed' ? 'error' : job.status !== 'done' ? 'loading' : meta ? 'ready' : 'empty'}
                data={meta}
                error={job?.status === 'failed' ? `训练失败：${job.error ?? ''}` : undefined}
                emptyDesc={!job
                  ? '提交训练任务后在此查看损失曲线与 IC'
                  : '任务已完成但未返回训练指标（meta 缺失）'}
                loadingText="训练中，进度见左侧任务卡…"
                minHeight="auto"
              >
                {(m) => (
                  <Flex direction="column" gap="var(--sr-pad-lg)" style={{ width: '100%' }}>
                    {icSummary}
                    {m.model_id != null && (
                      <Flex justify="flex-end">
                        <Button variant="default" leftSection={<IconDatabase size={14} />} onClick={openFactorModal}>入库为因子</Button>
                      </Flex>
                    )}
                    {/* T-67：结果区图表平铺——同一模型的多张图纵向排列、各带小节标题（删 Tabs 壳与受控状态；
                        平铺后 loss/viz、reward/equity 同时挂载，echarts 容器已有尺寸可直接 init，
                        无需惰性 init；标题文案沿用原 Tab 文案与 VIZ_TITLES） */}
                    {m.model_kind === 'dqn' ? (
                      <>
                        <div>
                          <Text fw={600} size="sm" style={{ color: 'var(--sr-text-1)', marginBottom: 'var(--sr-pad-sm)' }}>训练奖励</Text>
                          <LineCurve data={m.train_rewards} title="训练奖励" isDark={isDark} color="accent" yName="episode" />
                        </div>
                        <div>
                          <Text fw={600} size="sm" style={{ color: 'var(--sr-text-1)', marginBottom: 'var(--sr-pad-sm)' }}>评估净值</Text>
                          <LineCurve data={m.eval?.equity} title="评估净值" isDark={isDark} color="up" yName="步" />
                        </div>
                        {m.viz && (
                          <div>
                            <Text fw={600} size="sm" style={{ color: 'var(--sr-text-1)', marginBottom: 'var(--sr-pad-sm)' }}>{VIZ_TITLES[m.viz.kind] ?? '权重/注意力'}</Text>
                            <WeightHeatmap viz={m.viz} isDark={isDark} />
                          </div>
                        )}
                      </>
                    ) : (
                      <>
                        <div>
                          <Text fw={600} size="sm" style={{ color: 'var(--sr-text-1)', marginBottom: 'var(--sr-pad-sm)' }}>损失曲线</Text>
                          <div ref={chartRef} role="img" aria-label="神经网络训练损失曲线" style={{ width: '100%', height: 'var(--sr-chart-h-md)' }} />
                        </div>
                        {m.viz && (
                          <div>
                            <Text fw={600} size="sm" style={{ color: 'var(--sr-text-1)', marginBottom: 'var(--sr-pad-sm)' }}>{VIZ_TITLES[m.viz.kind] ?? '权重/注意力'}</Text>
                            <WeightHeatmap viz={m.viz} isDark={isDark} />
                          </div>
                        )}
                      </>
                    )}
                    {m.viz == null && (
                      <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
                        本次训练未产出权重可视化
                      </Text>
                    )}
                  </Flex>
                )}
              </PageState>
            )}
          </CardShell>
      </ResearchSplit>
    </PageShell>
  )
}
