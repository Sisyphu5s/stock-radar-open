import CardShell from '../../../components/ui/CardShell'
import DataList from '../shared/DataList'
import './discovery.css'

export interface FmConfigSnapshot {
  datasetName?: string
  features: string[]
  opSet: string[]
  algorithm: string
  backendMode: string
  backend: string
  popSize: number
  gens: number
  horizon: number
  target: string
  penaltyComplexity: boolean
}

const ALGO_LABEL: Record<string, string> = {
  gp: 'GP 符号回归（默认）',
  gplearn: 'gplearn（sklearn）',
  neural: 'neural（MLP 神经网络）',
  pysr: 'PySR（神经符号回归）',
}

const TARGET_LABEL: Record<string, string> = {
  ic: 'IC（预测方向）',
  ic_abs: '|IC|（方向无关）',
  icir: 'ICIR（稳健性）',
  ls_annual: '多空年化',
  composite: '综合评分',
}

/** 移动端只读配置快照：研究参数只读展示（antd Descriptions → DataList 薄壳） */
export default function ConfigReadonly({ cfg }: { cfg: FmConfigSnapshot }) {
  return (
    <CardShell title="研究配置（只读）" className="sr-disc-step-card">
      <DataList
        size="small"
        column={1}
        items={[
          { key: 'ds', label: '数据集', children: cfg.datasetName ?? '—' },
          {
            key: 'features', label: '特征字段',
            children: cfg.features.length ? cfg.features.join('、') : '—',
          },
          {
            key: 'ops', label: '算子集合',
            children: cfg.opSet.length ? cfg.opSet.join('、') : '—',
          },
          { key: 'algo', label: '回归算法', children: ALGO_LABEL[cfg.algorithm] ?? cfg.algorithm },
          {
            key: 'run', label: '运行参数',
            children: `${cfg.popSize} 种群 × ${cfg.gens} 代 · 预测 ${cfg.horizon} 日 · ${TARGET_LABEL[cfg.target] ?? cfg.target}`,
          },
          {
            key: 'backend', label: '计算模式',
            children: `${cfg.backendMode}（后端 ${cfg.backend}）${cfg.penaltyComplexity ? ' · 复杂度惩罚开' : ''}`,
          },
        ]}
      />
    </CardShell>
  )
}
