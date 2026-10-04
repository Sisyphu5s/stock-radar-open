import { Badge, Button, Tabs, Text, Tooltip } from '@mantine/core'
import { useEffect, useMemo, useRef } from 'react'
import DataTable from '../../../../../components/ui/DataTable'
import type { SrColumn } from '../../../../../components/ui/tableTypes'
import type { CombinedTuneCandidate, CombinedTuneResponse, IndicatorSpec, TuneCandidate, TuneResponse } from '../../../../../api/client'
import { fmtParamValues, TUNE_LABEL, useWorkbench } from '../../context'
import { fmtPct, fmtRatio, fmtStab } from '../../../../../utils/format'
import { useEchartsLifecycle } from '../../../../../hooks/useEchartsLifecycle'
import { echarts } from '../../../../../utils/echartsSetup'
import { chartTheme, chartYAxis } from '../../../../../utils/echartsTheme'
import { useThemeStore } from '../../../../../stores/useAppStore'
import { antdToMantine } from '../../tagColor'
import { ALGO_LABEL, fmtNamed } from './constants'
import { useTuneResults } from './useTuneResults'

/** IC 列：进度条 + 数值（对比总表与候选表共用；null 显示 —） */
function IcCell({ v }: { v: number | null | undefined }) {
  if (v == null) return <span style={{ fontSize: 'var(--sr-font-xs)', textAlign: 'right' }}>—</span>
  const pct = Math.min(Math.abs(v) * 1000, 100)
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sr-pad-sm)' }}>
      <div style={{ flex: 1, height: 8, background: 'var(--sr-hover-bg)', borderRadius: 'var(--sr-radius-tag)', overflow: 'hidden' }}>
        <div style={{ width: pct + '%', height: '100%', background: v >= 0 ? 'var(--sr-up)' : 'var(--sr-down)', opacity: 0.7 }} />
      </div>
      <span style={{ fontSize: 'var(--sr-font-xs)', textAlign: 'right' }}>{v.toFixed(3)}</span>
    </div>
  )
}

/** 结果底部目标说明行：目标描述 + 评估窗口说明（IC 语义）+ 非 grid 算法评估覆盖度标注 */
function TuneTargetNote({ resp }: { resp: TuneResponse | CombinedTuneResponse | undefined }) {
  const { tuneTarget } = useWorkbench()
  if (!resp) return null
  return (
    <Text span size="xs" className="sr-tune-target-note" style={{ color: 'var(--sr-text-3)' }}>
      目标: {resp.targets?.[tuneTarget] ?? tuneTarget}（评估窗口：未来{resp.horizon}日收益，IC 衡量指标对该窗口收益的预测能力）
      {resp.algorithm && resp.algorithm !== 'grid' && (
        <>　·　<Badge variant="light" color={antdToMantine('default')} radius="var(--sr-radius-tag)" size="sm">{ALGO_LABEL[resp.algorithm] ?? resp.algorithm}（评估 {resp.evaluated ?? 0}/{resp.candidates_total ?? 0} 组）</Badge></>
      )}
    </Text>
  )
}

/** 单指标调优结果 IC-参数折线图（x=单参指标主参数值 / 多参指标候选序号，y=IC，当前最优高亮）。
 *  canvas 不解析 CSS var()：颜色走 chartTheme JS 常量，随 useThemeStore 亮暗切换。 */
function IcParamChart({ spec, results, best }: { spec: IndicatorSpec; results: TuneCandidate[]; best: TuneCandidate | null }) {
  const isDark = useThemeStore((s) => s.theme) === 'dark'
  const chartRef = useRef<HTMLDivElement>(null)
  const chartInst = useRef<echarts.ECharts | null>(null)
  useEchartsLifecycle(chartRef, chartInst)
  useEffect(() => () => {
    if (chartInst.current && !chartInst.current.isDisposed()) chartInst.current.dispose()
    chartInst.current = null
  }, [])
  const points = useMemo(() => {
    const single = spec.params.length === 1
    const out: { x: string; ic: number; best: boolean }[] = []
    results.forEach((r, i) => {
      if (r.ic == null) return
      let x = String(i + 1)
      if (single) {
        const k = spec.params[0].key
        const v = r.params[k]
        if (v != null && Number.isFinite(Number(v))) x = String(v)
      }
      out.push({ x, ic: r.ic, best: best != null && JSON.stringify(r.params) === JSON.stringify(best.params) })
    })
    return { single, points: out }
  }, [spec, results, best])
  useEffect(() => {
    if (points.points.length === 0) {
      if (chartInst.current && !chartInst.current.isDisposed()) chartInst.current.dispose()
      chartInst.current = null
      return
    }
    if (!chartRef.current) return
    if (!chartInst.current) chartInst.current = echarts.init(chartRef.current)
    const t = chartTheme(isDark)
    chartInst.current.setOption({
      animation: false,
      tooltip: { trigger: 'axis', valueFormatter: (v: number) => fmtRatio(v, 3) },
      grid: { left: 42, right: 12, top: 18, bottom: 22 },
      xAxis: { type: 'category', data: points.points.map((p) => p.x), axisLabel: { fontSize: 10, color: t.text3 } },
      yAxis: chartYAxis(isDark),
      series: [{
        name: 'IC', type: 'line',
        data: points.points.map((p) => ({
          value: p.ic,
          symbolSize: p.best ? 9 : 4,
          itemStyle: p.best ? { color: t.accent, borderColor: t.accent } : undefined,
        })),
        showSymbol: true,
        lineStyle: { width: 1.2, color: t.accent, opacity: 0.85 },
        itemStyle: { color: t.accent },
        markLine: {
          silent: true, symbol: 'none',
          lineStyle: { type: 'dashed', color: t.text3, opacity: 0.5 },
          data: [{ yAxis: 0 }], label: { show: false },
        },
      }],
    }, true)
  }, [points, isDark])
  if (points.points.length === 0) return null
  return (
    <div className="sr-tune-chart">
      <div className="sr-tune-chart-title">
        IC-参数折线（{points.single ? '主参数值' : '候选序号'}，高亮=当前最优）
      </div>
      <div ref={chartRef} className="sr-tune-chart-canvas" />
    </div>
  )
}

/** 调优结果区：对比总表 + 各指标 Tab + IcParamChart + 错误/重试 + 整体模式结果 + last 标记。
 *  两模式结果并存不互斥：hasIndividual/hasCombined 分别标记，切换模式保留并提示查看另一模式。 */
export function TuneResultsPanel() {
  const c = useWorkbench()
  const {
    tuneMode, setTuneMode, tuneResults, tuneErrors, retryTune, applyBest,
    specs, tuneScope, globalParams,
  } = c
  const {
    activeKey, setActiveKey, resultKeys, resettingKey, resetGlobal,
    combo, comboErr, hasIndividual, hasCombined, hasAny,
    isApplied, bestOf, firstRes, comboGain,
  } = useTuneResults()

  // R5 清理：渲染期 JSON.stringify 收敛为预计算（行 key + best 匹配键，useMemo 随数据重建）
  type Keyed<T> = T & { _rowKey: string }
  const comboKeyed = useMemo(() => {
    if (!combo) return null
    const bestKey = combo.best ? JSON.stringify(combo.best.params_by_indicator) : null
    return {
      rows: combo.results.map((r) => ({ ...r, _rowKey: JSON.stringify(r.params_by_indicator) })),
      bestKey,
    }
  }, [combo])
  const keyedSingles = useMemo(() => {
    const m = new Map<string, { rows: Keyed<TuneCandidate>[]; bestKey: string | null }>()
    for (const [k, res] of Object.entries(tuneResults)) {
      if (!res || 'combined' in res) continue
      const bestKey = res.best ? JSON.stringify(res.best.params) : null
      m.set(k, {
        rows: res.results.map((r) => ({ ...r, _rowKey: r.param ?? JSON.stringify(r.params ?? {}) })),
        bestKey,
      })
    }
    return m
  }, [tuneResults])

  // 对比总表（individual）：行点击切指标 Tab，行尾应用按钮不冒泡
  const overviewCols: SrColumn<{ key: string }>[] = [
    {
      title: '指标', key: 'ind', width: 150,
      render: (_v: unknown, r: { key: string }) => (
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
          {TUNE_LABEL[r.key] ?? r.key.toUpperCase()}
          {isApplied(r.key) && <Badge variant="light" color={antdToMantine('green')} radius="var(--sr-radius-tag)" size="sm">已应用 ✓</Badge>}
        </span>
      ),
    },
    {
      title: '最优参数', key: 'params', width: 170, ellipsis: true,
      render: (_v: unknown, r: { key: string }) => {
        const res = tuneResults[r.key]
        if (!res || 'combined' in res || !res.best) return '—'
        return fmtNamed(res.spec, res.best.params)
      },
    },
    {
      title: 'IC', key: 'ic', align: 'right', width: 76,
      render: (_v: unknown, r: { key: string }) => fmtRatio(bestOf(r.key)?.ic, 3),
    },
    {
      title: 'ICIR', key: 'icir', align: 'right', minWidth: 76,
      render: (_v: unknown, r: { key: string }) => fmtRatio(bestOf(r.key)?.icir, 2),
    },
    {
      title: '夏普', key: 'sharpe', align: 'right', minWidth: 76,
      render: (_v: unknown, r: { key: string }) => fmtRatio(bestOf(r.key)?.sharpe, 2),
    },
    {
      title: '多空', key: 'ls_annual', align: 'right', minWidth: 76,
      render: (_v: unknown, r: { key: string }) => fmtPct(bestOf(r.key)?.ls_annual, 1),
    },
    {
      title: '胜率', key: 'win_rate', align: 'right', minWidth: 76,
      render: (_v: unknown, r: { key: string }) => fmtPct(bestOf(r.key)?.win_rate, 0),
    },
    {
      title: '回撤', key: 'max_drawdown', align: 'right', minWidth: 76,
      render: (_v: unknown, r: { key: string }) => fmtPct(bestOf(r.key)?.max_drawdown, 1),
    },
    {
      title: '综合', key: 'composite', align: 'right', minWidth: 76,
      render: (_v: unknown, r: { key: string }) => fmtRatio(bestOf(r.key)?.composite, 3),
    },
    {
      title: '', key: 'apply', width: 60, align: 'right',
      render: (_v: unknown, r: { key: string }) => (
        <Button
          size="xs"
          variant="transparent"
          disabled={bestOf(r.key) == null}
          onClick={(e) => { e.stopPropagation(); applyBest(r.key) }}
          aria-label={`应用${TUNE_LABEL[r.key] ?? r.key.toUpperCase()}最优参数`}
        >
          应用
        </Button>
      ),
    },
  ]

  return (
    <div className="sr-tune-results">
      {!hasAny ? (
        <Text span size="xs" className="sr-tune-target-note" style={{ color: 'var(--sr-text-3)' }}>
          {tuneMode === 'combined' ? '请先运行整体调优' : '请先运行调优'}
        </Text>
      ) : tuneMode === 'combined' ? (
        combo ? (
          <div>
            {combo.best && (
              <div className="sr-tune-best">
                <span className="sr-tune-best-label">整体最优组合</span>
                {Object.entries(combo.best.params_by_indicator).map(([ind, params]) => (
                  <Badge key={ind} variant="light" color={antdToMantine('green')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">{specs[ind]?.name ?? ind} = {fmtParamValues(params)}</Badge>
                ))}
                <Badge variant="light" color={antdToMantine('blue')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">IC {fmtRatio(combo.best.ic, 3)}</Badge>
                <Badge variant="light" color={antdToMantine('geekblue')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">ICIR {fmtRatio(combo.best.icir, 2)}</Badge>
                <Badge variant="light" color={antdToMantine('default')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">胜率 {fmtPct(combo.best.win_rate, 0)}</Badge>
                <Badge variant="light" color={antdToMantine('green')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">多空 {fmtPct(combo.best.ls_annual, 1)}</Badge>
                <Badge variant="light" color={antdToMantine('default')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">夏普 {fmtRatio(combo.best.sharpe, 2)}</Badge>
                {comboGain}
                <div className="sr-tune-best-actions">
                  <Button size="xs" variant="filled" onClick={() => applyBest('__combined__')} aria-label="应用整体最优参数">
                    {tuneScope === 'global' ? '保存为全局默认' : '应用到当前图表'}
                  </Button>
                </div>
              </div>
            )}
            <TuneTargetNote resp={combo} />
            <div className="sr-tune-table-wrap">
              <DataTable
                fillWidth
                rowKey={(r: Keyed<CombinedTuneCandidate>) => r._rowKey}
                pagination={{ pageSize: 10, showSizeChanger: false }}
                columns={[
                  {
                    title: '参数', key: 'params', width: 240, fixed: 'left', ellipsis: true,
                    render: (_v: unknown, r: Keyed<CombinedTuneCandidate>) => {
                      const isBest = comboKeyed?.bestKey != null && r._rowKey === comboKeyed.bestKey
                      const txt = Object.entries(r.params_by_indicator ?? {}).map(([ind, params]) => `${specs[ind]?.name ?? ind}=${fmtParamValues(params)}`).join('  ')
                      return isBest ? <Badge variant="light" color={antdToMantine('green')} radius="var(--sr-radius-tag)" size="sm">{txt}</Badge> : txt
                    },
                  },
                  {
                    title: 'IC', dataIndex: 'ic', align: 'right', width: 130,
                    render: (v: number | null) => <IcCell v={v} />,
                  },
                  { title: 'RankIC', dataIndex: 'rank_ic', align: 'right', minWidth: 70, render: (v: number) => fmtRatio(v, 3) },
                  { title: 'ICIR', dataIndex: 'icir', align: 'right', minWidth: 70, render: (v: number) => fmtRatio(v, 2) },
                  { title: '稳定性', dataIndex: 'stability', align: 'right', minWidth: 70, render: (v: number) => fmtStab(v, 1) },
                  { title: '胜率', dataIndex: 'win_rate', align: 'right', minWidth: 70, render: (v: number) => fmtPct(v, 0) },
                  { title: '多空', dataIndex: 'ls_annual', align: 'right', minWidth: 70, render: (v: number) => fmtPct(v, 1) },
                  { title: '夏普', dataIndex: 'sharpe', align: 'right', minWidth: 70, render: (v: number) => fmtRatio(v, 2) },
                  { title: '回撤', dataIndex: 'max_drawdown', align: 'right', minWidth: 70, render: (v: number) => fmtPct(v, 1) },
                  { title: '综合', dataIndex: 'composite', align: 'right', minWidth: 70, render: (v: number) => fmtRatio(v, 3) },
                ]}
                dataSource={comboKeyed?.rows ?? []}
                rowClassName={(r: Keyed<CombinedTuneCandidate>) => (comboKeyed?.bestKey != null && r._rowKey === comboKeyed.bestKey ? 'sr-tune-row-best' : '')}
              />
            </div>
          </div>
        ) : comboErr ? (
          <div className="sr-tune-err" role="alert">
            <Text span size="sm" style={{ color: 'var(--sr-error)' }}>{comboErr}</Text>
            <div className="sr-tune-best-actions">
              <Button size="xs" variant="default" onClick={() => retryTune('__combined__')} aria-label="重试整体调优">重试</Button>
            </div>
          </div>
        ) : hasIndividual ? (
          <div className="sr-tune-last">
            <Badge variant="light" color={antdToMantine('default')} radius="var(--sr-radius-tag)" size="sm">上次结果（逐个模式）</Badge>
            <Button size="xs" variant="default" onClick={() => setTuneMode('individual')} aria-label="查看逐个模式结果">查看逐个结果</Button>
          </div>
        ) : (
          <Text span size="xs" className="sr-tune-target-note" style={{ color: 'var(--sr-text-3)' }}>请先运行整体调优</Text>
        )
      ) : hasCombined && !hasIndividual ? (
        <div className="sr-tune-last">
          <Badge variant="light" color={antdToMantine('default')} radius="var(--sr-radius-tag)" size="sm">上次结果（整体模式）</Badge>
          <Button size="xs" variant="default" onClick={() => setTuneMode('combined')} aria-label="查看整体模式结果">查看整体结果</Button>
        </div>
      ) : (
        <>
          {hasCombined && (
            <div className="sr-tune-last">
              <Badge variant="light" color={antdToMantine('default')} radius="var(--sr-radius-tag)" size="sm">上次结果（整体模式）</Badge>
              <Button size="xs" variant="default" onClick={() => setTuneMode('combined')} aria-label="查看整体模式结果">查看整体结果</Button>
            </div>
          )}
          <Tabs
            value={activeKey}
            onChange={(v) => { if (v) setActiveKey(v) }}
            variant="pills"
          >
            <Tabs.List>
              <Tabs.Tab value="__overview__">对比总表</Tabs.Tab>
              {resultKeys.map((k) => (
                <Tabs.Tab key={k} value={k}>{TUNE_LABEL[k] ?? k.toUpperCase()}</Tabs.Tab>
              ))}
            </Tabs.List>
            <Tabs.Panel value="__overview__">
              <div>
                <TuneTargetNote resp={firstRes} />
                <div className="sr-tune-table-wrap">
                  <DataTable
                    fillWidth
                    rowKey="key"
                    pagination={false}
                    columns={overviewCols}
                    dataSource={resultKeys.map((k) => ({ key: k }))}
                    onRow={(r) => ({ onClick: () => setActiveKey(r.key), style: { cursor: 'pointer' } })}
                  />
                </div>
              </div>
            </Tabs.Panel>
            {resultKeys.map((k) => {
              const res = tuneResults[k]
              const err = tuneErrors[k]
              const single = res && !('combined' in res) ? res : undefined
              const ks = single ? keyedSingles.get(k) : undefined
              return (
                <Tabs.Panel key={k} value={k}>
                  {err && !single ? (
                    <div className="sr-tune-err" role="alert">
                      <Text span size="sm" style={{ color: 'var(--sr-error)' }}>{err}</Text>
                      <div className="sr-tune-best-actions">
                        <Button size="xs" variant="default" onClick={() => retryTune(k)} aria-label={`重试${TUNE_LABEL[k] ?? k.toUpperCase()}调优`}>重试</Button>
                      </div>
                    </div>
                  ) : single ? (
                    <>
                      {single.best && (
                        <div className="sr-tune-best">
                          <span className="sr-tune-best-label">最优参数</span>
                          <Badge variant="light" color={antdToMantine('green')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">{fmtNamed(single.spec, single.best.params)}</Badge>
                          {isApplied(k) && <Badge variant="light" color={antdToMantine('green')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">已应用 ✓</Badge>}
                          <Badge variant="light" color={antdToMantine('blue')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">IC {fmtRatio(single.best.ic, 3)}</Badge>
                          <Badge variant="light" color={antdToMantine('purple')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">RankIC {fmtRatio(single.best.rank_ic, 3)}</Badge>
                          <Badge variant="light" color={antdToMantine('geekblue')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">ICIR {fmtRatio(single.best.icir, 2)}</Badge>
                          <Badge variant="light" color={antdToMantine('default')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">稳定性 {fmtStab(single.best.stability, 1)}</Badge>
                          <Badge variant="light" color={antdToMantine('default')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">胜率 {fmtPct(single.best.win_rate, 0)}</Badge>
                          <Badge variant="light" color={antdToMantine('green')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">多空 {fmtPct(single.best.ls_annual, 1)}</Badge>
                          <Badge variant="light" color={antdToMantine('default')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">夏普 {fmtRatio(single.best.sharpe, 2)}</Badge>
                          <Badge variant="light" color={antdToMantine('default')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">回撤 {fmtPct(single.best.max_drawdown, 1)}</Badge>
                          <div className="sr-tune-best-actions">
                            <Button size="xs" variant="filled" onClick={() => applyBest(k)} aria-label="应用最优参数">
                              {tuneScope === 'global' ? '保存为全局默认' : '应用到当前图表'}
                            </Button>
                            {globalParams[k] && (
                              <Button size="xs" variant="default" color="red" loading={resettingKey === k} onClick={() => void resetGlobal(k)}>重置全局</Button>
                            )}
                          </div>
                        </div>
                      )}
                      <TuneTargetNote resp={single} />
                      <IcParamChart spec={single.spec} results={single.results} best={single.best} />
                      <div className="sr-tune-table-wrap">
                        <DataTable
                          fillWidth
                          rowKey={(r: Keyed<TuneCandidate>) => r._rowKey}
                          pagination={{ pageSize: 10, showSizeChanger: false }}
                          columns={[
                            {
                              // fixed 列必须有数值 width；200px + ellipsis（antd 自动 Tooltip）兜底超长参数文本
                              title: '参数', dataIndex: 'param', width: 200, fixed: 'left', ellipsis: true,
                              render: (_v: unknown, r: Keyed<TuneCandidate>) => {
                                const isBest = ks?.bestKey != null && r._rowKey === ks.bestKey
                                const txt = fmtNamed(single.spec, r.params)
                                return isBest ? <Badge variant="light" color={antdToMantine('green')} radius="var(--sr-radius-tag)" size="sm">{txt}</Badge> : txt
                              },
                            },
                            {
                              // IC 进度条 + 数值，固定宽 130 保持
                              title: 'IC', dataIndex: 'ic', align: 'right', width: 130,
                              render: (v: number | null) => <IcCell v={v} />,
                            },
                            { title: 'RankIC', dataIndex: 'rank_ic', align: 'right', minWidth: 70, render: (v: number) => fmtRatio(v, 3) },
                            { title: 'ICIR', dataIndex: 'icir', align: 'right', minWidth: 70, render: (v: number) => fmtRatio(v, 2) },
                            { title: '稳定性', dataIndex: 'stability', align: 'right', minWidth: 70, render: (v: number) => fmtStab(v, 1) },
                            { title: '胜率', dataIndex: 'win_rate', align: 'right', minWidth: 70, render: (v: number) => fmtPct(v, 0) },
                            { title: 'LS年化', dataIndex: 'ls_annual', align: 'right', minWidth: 70, render: (v: number) => fmtPct(v, 1) },
                            { title: '夏普', dataIndex: 'sharpe', align: 'right', minWidth: 70, render: (v: number) => fmtRatio(v, 2) },
                            { title: '回撤', dataIndex: 'max_drawdown', align: 'right', minWidth: 70, render: (v: number) => fmtPct(v, 1) },
                            { title: '综合', dataIndex: 'composite', align: 'right', minWidth: 70, render: (v: number) => fmtRatio(v, 3) },
                            {
                              // P2-7：当前值列加说明（列头为 ReactNode，可直挂 Tooltip，零契约改动）——
                              // last_value = 按该组参数计算出的指标最新值（最后一根 K 线的指标输出）
                              title: (
                                <Tooltip label="按该组参数计算出的指标最新值（最后一根 K 线的指标输出），可直观对比参数对指标当前输出的影响">
                                  <span>当前值</span>
                                </Tooltip>
                              ),
                              dataIndex: 'last_value', align: 'right', minWidth: 70,
                            },
                          ]}
                          dataSource={ks?.rows ?? []}
                        />
                      </div>
                    </>
                  ) : null}
                </Tabs.Panel>
              )
            })}
          </Tabs>
        </>
      )}
    </div>
  )
}
