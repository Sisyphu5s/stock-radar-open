import { Badge, Code, Group, Text } from '@mantine/core'
import FormulaCode from '../../../components/FormulaCode'
import DataTable from '../../../components/ui/DataTable'
import DataList from '../shared/DataList'
import { fmtNum, fmtPct, fmtRatio, fmtStab, fmtWinRate } from '../../../utils/format'
import type {
  Alpha101ScorePayload, BacktestPayload, BTModesResult, BTMode, DatasetBuildPayload, EvaluatePayload,
  FactorResult, FactorTunePayload, GpRunPayload, JobDetail, MarketScanPayload, PaperJobPayload,
  WalkForwardPayload,
} from '../../../api/client'
import type { TaskRow } from './constants'

/** 模拟盘信号结果行（与 PaperTrading sr-paper-stat 数据源一致） */
interface PaperSignalRow {
  signal: string
  name?: string
  triggers?: number
  win_rate?: number
  avg_r5?: number
  avg_r10?: number
  last_trigger?: string | null
}

/** 列表行摘要：仅 done 任务展示，按类型渲染关键指标 */
export function SummaryCell({ job }: { job: TaskRow }) {
  if (job.status !== 'done') return <Text span c="dimmed">—</Text>
  const s = job.summary
  if (!s) return <Text span c="dimmed">—</Text>
  const Item = ({ label, val }: { label: string; val: string }) => (
    <span style={{ fontSize: 'var(--sr-font-sm)' }}>{label} {val}</span>
  )
  switch (job.job_type) {
    case 'backtest':
    case 'backtest_opt': { // 组合优化回测与回测同摘要（后端 _job_summary 同源）
      const modeLabel: Record<string, string> = { long_short: '多空对冲', long: '仅多头', short: '仅空头', quantile: '分层' }
      return (
        <Group gap={4}>
          {s.mode != null && <Badge variant="light" color="cyan" style={{ marginInlineEnd: 0 }}>{modeLabel[String(s.mode)] ?? (s.mode as string | null)}</Badge>}
          <Item label="年化" val={fmtPct(s.annual_return as number)} />
          <Item label="夏普" val={fmtRatio(s.sharpe as number, 2)} />
          <Item label="回撤" val={fmtPct(s.max_drawdown as number)} />
          <Item label="胜率" val={fmtStab(s.win_rate as number, 0)} />
        </Group>
      )
    }
    case 'evaluate':
      return (
        <Group gap={4}>
          <Item label="IC" val={fmtRatio(s.ic as number)} />
          <Item label="rankIC" val={fmtRatio(s.rank_ic as number)} />
          <Item label="多空年化" val={fmtPct(s.long_short_annual as number)} />
          <Item label="稳定性" val={fmtStab(s.stability as number)} />
          <Item label="OOS IC" val={fmtRatio(s.oos_ic as number)} />
        </Group>
      )
    case 'gp_run':
      return (
        <Group gap={4}>
          {s.count != null && <Item label="因子数" val={String(s.count)} />}
          <Item label="最大训练IC" val={fmtRatio(s.max_train_ic as number)} />
        </Group>
      )
    case 'alpha101_score':
      return (
        <Group gap={4}>
          {s.count != null && <Item label="评分因子" val={String(s.count)} />}
          <Item label="最高分" val={fmtRatio(s.max_score as number, 2)} />
        </Group>
      )
    case 'factor_tune':
      return (
        <Group gap={4}>
          {s.best_value != null && <Item label="最优参数" val={String(s.best_value)} />}
          <Item label="训练IC" val={fmtRatio(s.best_ic as number)} />
          <Item label="OOS IC" val={fmtRatio(s.oos_ic as number)} />
        </Group>
      )
    case 'dataset_build':
      return (
        <Group gap={4}>
          {s.stock_count != null && <Item label="股票" val={`${String(s.stock_count)} 只`} />}
          {s.row_count != null && <Item label="行数" val={fmtRatio(s.row_count as number, 0)} />}
        </Group>
      )
    case 'rl_train': // DQN 训练摘要（后端 _job_summary 产出）
      return (
        <Group gap={4}>
          {s.episodes != null && <Item label="轮次" val={String(s.episodes)} />}
          {s.avg_train_reward != null && <Item label="平均奖励" val={fmtRatio(s.avg_train_reward as number, 3)} />}
          {s.sharpe != null && <Item label="夏普" val={fmtRatio(s.sharpe as number, 2)} />}
          {s.total_return != null && <Item label="总收益" val={fmtPct(s.total_return as number)} />}
        </Group>
      )
    case 'paper_experiment': {
      // 列表项无完整 result，直接读列表项已提供的 summary（后端聚合字段）：
      // {signal_count, total_triggers, avg_win_rate}（avg_win_rate 为 0~1 小数或 null）
      const signalCount = Number(s?.signal_count ?? 0)
      const totalTriggers = Number(s?.total_triggers ?? 0)
      const avgWin = s?.avg_win_rate != null ? Number(s.avg_win_rate) : NaN
      if (signalCount <= 0 && totalTriggers <= 0) return <Text span c="dimmed">模拟盘实验</Text>
      return (
        <Group gap={4}>
          <Item label="信号" val={`${signalCount} 个`} />
          <Item label="触发" val={`${totalTriggers} 次`} />
          <Item label="胜率" val={fmtWinRate(avgWin)} />
        </Group>
      )
    }
    default:
      return <Text span c="dimmed">—</Text>
  }
}

/** 详情抽屉结果区：按类型渲染完整 result（仅 done 任务）；job 来自 getJob 详情（JobDetail） */
export function ResultContent({ job }: { job: JobDetail }) {
  if (job.status !== 'done' || !job.result) return null
  // result 结构随 job_type 变化：按判别收窄为对应载荷（不再 as any）
  switch (job.job_type) {
    case 'gp_run': {
      const results = (job.result as GpRunPayload).results ?? []
      return (
        <div>
          <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>因子结果（Top {results.length}）</Text>
          <DataTable rowKey={(r: FactorResult) => r.expression} pagination={false} sticky={false}
            dataSource={results.slice(0, 10)}
            columns={[
              { title: '表达式', dataIndex: 'expression', ellipsis: true, minWidth: 120 },
              { title: '复杂度', dataIndex: 'complexity', align: 'center' },
              { title: 'Train IC', dataIndex: 'train_ic', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v: number) => fmtRatio(v) },
              { title: 'Val IC', dataIndex: 'val_ic', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v: number) => fmtRatio(v) },
            ]} />
        </div>
      )
    }
    case 'evaluate': {
      const r = (job.result as EvaluatePayload).result ?? ({} as EvaluatePayload['result'])
      const oos = (job.result as EvaluatePayload).oos ?? ({} as EvaluatePayload['oos'])
      return (
        <DataList column={2} bordered items={[
          { key: 'ic', label: 'IC', children: fmtRatio(r?.ic) },
          { key: 'rank', label: 'RankIC', children: fmtRatio(r?.rank_ic) },
          { key: 'ls', label: '多空年化', children: fmtPct(r?.long_short_annual) },
          { key: 'st', label: '稳定性', children: fmtStab(r?.stability) },
          { key: 'oic', label: 'OOS IC', children: fmtRatio(oos?.ic) },
          { key: 'ost', label: 'OOS 稳定性', children: fmtStab(oos?.stability) },
        ]} />
      )
    }
    case 'backtest':
    case 'backtest_opt': { // 组合优化回测载荷与回测同源（BTResult，后端复用）
      const bt = (job.result as BacktestPayload).backtest ?? ({} as BTModesResult)
      const modes = bt.modes ?? []
      return (
        <div>
          {modes.map((m: BTMode) => {
            const mm = m.metrics ?? {}
            return (
              <div key={m.mode} style={{ marginBottom: 'var(--sr-gap-row)' }}>
                <Badge variant="light" color="cyan" style={{ marginBottom: 'var(--sr-pad-sm)' }}>{m.mode}</Badge>
                <DataList column={4} bordered items={[
                  { key: 'a', label: '年化', children: fmtPct(mm.annual_return) },
                  { key: 's', label: '夏普', children: fmtRatio(mm.sharpe, 2) },
                  { key: 'd', label: '回撤', children: fmtPct(mm.max_drawdown) },
                  { key: 'w', label: '胜率', children: fmtStab(mm.win_rate, 0) },
                ]} />
              </div>
            )
          })}
          {(bt.quantiles ?? []).length > 0 && (
            <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
              分层回测 {bt.quantiles?.length} 个分位已生成（见回测页叠加图）
            </Text>
          )}
        </div>
      )
    }
    case 'walk_forward': {
      // Walk-forward 滚动验证载荷：分窗 OOS IC + 全 OOS 摘要
      const wf = (job.result as WalkForwardPayload).walk_forward
      if (!wf) return null
      return (
        <div>
          <DataList column={2} bordered items={[
            { key: 'n', label: '窗口数', children: String(wf.n_windows) },
            { key: 'horizon', label: '周期', children: `${wf.horizon} 日` },
            { key: 'mean', label: 'OOS 平均 IC', children: fmtRatio(wf.summary.mean_ic) },
            { key: 'std', label: 'OOS IC 波动', children: fmtRatio(wf.summary.ic_std) },
            { key: 'pos', label: 'IC 为正占比', children: fmtStab(wf.summary.ic_positive_ratio) },
            { key: 'days', label: 'OOS 天数', children: String(wf.summary.n_days) },
          ]} />
          <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)', display: 'block', marginTop: 'var(--sr-pad-sm)' }}>
            分窗明细见评估页（深链 ?job=）
          </Text>
        </div>
      )
    }
    case 'rl_train': {
      // DQN 训练结果在 result.meta（handlers/rl_train 组装，client 无专用载荷类型，页面局部断言）
      const meta = (job.result as { meta?: { episodes?: number; avg_train_reward?: number; eval?: { sharpe?: number; total_return?: number } } }).meta
      if (!meta) return null
      return (
        <DataList column={2} bordered items={[
          { key: 'ep', label: '训练轮次', children: meta.episodes != null ? String(meta.episodes) : '—' },
          { key: 'rw', label: '平均奖励', children: meta.avg_train_reward != null ? fmtRatio(meta.avg_train_reward, 3) : '—' },
          { key: 'sh', label: '夏普', children: meta.eval?.sharpe != null ? fmtRatio(meta.eval.sharpe, 2) : '—' },
          { key: 'tr', label: '总收益', children: meta.eval?.total_return != null ? fmtPct(meta.eval.total_return) : '—' },
        ]} />
      )
    }
    case 'market_scan': {
      const scan = (job.result as MarketScanPayload).scan
      if (!scan) return null
      return (
        <DataList column={2} bordered items={[
          { key: 'scanned', label: '扫描股票', children: String(scan.scanned ?? 0) },
          { key: 'events', label: '新信号', children: String(scan.events ?? 0) },
          { key: 'source', label: '结果来源', children: String(scan.source ?? '—') },
          { key: 'elapsed', label: '耗时', children: scan.elapsed != null ? `${scan.elapsed.toFixed(1)}s` : '—' },
        ]} />
      )
    }
    case 'alpha101_score': {
      const results = (job.result as Alpha101ScorePayload).results ?? []
      return (
        <div>
          <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>评分结果（Top {results.length}，已按库评分排序）</Text>
          <DataTable rowKey="id" pagination={{ pageSize: 8 }} scrollX={false} sticky={false}
            dataSource={results.slice(0, 20)}
            columns={[
              { title: '#', dataIndex: 'id', align: 'center' },
              { title: '因子', dataIndex: 'name', ellipsis: true, minWidth: 100 },
              { title: 'IC', dataIndex: 'ic_mean', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v: number) => fmtRatio(v) },
              { title: '稳定性', dataIndex: 'stability', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v: number) => fmtStab(v) },
              { title: '库评分', dataIndex: 'score', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v: number) => v != null ? v.toFixed(2) : '—' },
            ]} />
        </div>
      )
    }
    case 'factor_tune': {
      const tune = job.result as FactorTunePayload
      const grid = tune.grid ?? []
      return (
        <div>
          <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>网格搜索结果（按目标排序）</Text>
          <DataTable rowKey="param_value" pagination={false} sticky={false}
            dataSource={grid.slice(0, 12)}
            columns={[
              { title: '参数值', dataIndex: 'param_value', align: 'right', className: 'sr-num-col', minWidth: 70 },
              { title: 'Train IC', dataIndex: 'train_ic', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v: number) => fmtRatio(v) },
              { title: 'ICIR', dataIndex: 'icir', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v: number) => fmtRatio(v, 2) },
              { title: '多空年化', dataIndex: 'ls_annual', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v: number) => fmtPct(v) },
              { title: '稳定性', dataIndex: 'stability', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v: number) => fmtStab(v) },
              { title: 'OOS IC', dataIndex: 'oos_ic', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v: number) => fmtRatio(v) },
            ]} />
          {tune.best_expression && (
            <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-sm)', display: 'block', marginTop: 'var(--sr-pad-sm)' }}>
              最优表达式: <Code><FormulaCode expr={tune.best_expression} /></Code>
            </Text>
          )}
        </div>
      )
    }
    case 'dataset_build': {
      const ds = (job.result as DatasetBuildPayload).dataset ?? {}
      return (
        <DataList column={2} bordered items={[
          { key: 'name', label: '数据集', children: ds.name ?? '—' },
          { key: 'stocks', label: '股票数', children: ds.stock_count ?? '—' },
          { key: 'rows', label: '行数', children: ds.row_count ?? '—' },
          { key: 'range', label: '区间', children: `${ds.start_date ?? '—'} ~ ${ds.end_date ?? '—'}` },
        ]} />
      )
    }
    case 'paper_experiment': {
      const rows = ((job.result as PaperJobPayload).results ?? []) as PaperSignalRow[]
      return (
        <div>
          <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>信号统计（{rows.length} 个信号）</Text>
          <DataTable
            rowKey={(r: PaperSignalRow) => r.signal}
            pagination={false}
            sticky={false}
            dataSource={rows}
            columns={[
              { title: '信号', dataIndex: 'signal', ellipsis: true, minWidth: 100, render: (_: string, r: PaperSignalRow) => r.name ?? r.signal },
              { title: '触发', dataIndex: 'triggers', align: 'right', className: 'sr-num-col', minWidth: 60, render: (v: number) => fmtNum(v, 0) },
              { title: '胜率', dataIndex: 'win_rate', align: 'right', className: 'sr-num-col', minWidth: 70, render: (v: number) => fmtWinRate(v) },
              { title: '平均5日收益', dataIndex: 'avg_r5', align: 'right', className: 'sr-num-col', minWidth: 90, render: (v: number) => fmtPct(v, 2) },
              { title: '平均10日收益', dataIndex: 'avg_r10', align: 'right', className: 'sr-num-col', minWidth: 90, render: (v: number) => fmtPct(v, 2) },
            ]} />
        </div>
      )
    }
    default:
      return null
  }
}
