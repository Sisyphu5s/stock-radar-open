import { Button, Checkbox, Flex, Group, Loader, ScrollArea, Select, Text } from '@mantine/core'
import { IconPlayerPlay, IconRocket } from '@tabler/icons-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import { getFactors } from '../../../api/client'
import type { FactorInfo, PanelRebuildProgress } from '../../../api/client'
import { COMBINE_METHOD_LABEL, COMBINE_METHOD_OPTIONS, runCombine } from '../../../api/portfolio'
import type { CombineMethod, CombineResult } from '../../../api/portfolio'
import { useDatasets } from '../../../data/jobs'
import { useDatasetInit } from '../../../hooks/useDatasetInit'
import { useEchartsLifecycle } from '../../../hooks/useEchartsLifecycle'
import { useThemeStore } from '../../../stores/useAppStore'
import { chartDataZoom, chartTheme, chartYAxis } from '../../../utils/echartsTheme'
import { echarts } from '../../../utils/echartsSetup'
import { errMsg, fmtPct, fmtRatio, fmtStab } from '../../../utils/format'
import { DataTable, DatasetSelector, EmptyState, FormulaText, MetricStat, PageShell, StatStrip, TaskProgress } from '../../../components/ui'
import ResearchSplit from '../../../components/ui/ResearchSplit'
import { toast } from '../shared/toast'
import '../validation/run-layout.css'

/** MetricStat 网格单元（与 EvaluationResults 一致） */
const statCell: CSSProperties = { flex: '1 1 140px', minWidth: 0 }

/** 因子选择滚动列表最大高度（px） */
const FACTOR_LIST_MAX_H = 280

/**
 * 因子合成（T-11）：第 8 个工作台 Tab。
 * 左栏配置：因子库多选 + 合成方法 + 数据集；右栏结果：合成 IC 指标 / 权重 / IC 序列 / 表达式 / 信号快照。
 * /alpha/combine 热缓存为同步 API；冷缓存 miss → 202 受理，内部自动轮询
 * panel_build（C11b），页面仅保持 running/loading 态，无需感知任务细节。
 */
export default function Combine() {
  const isDark = useThemeStore((s) => s.theme) === 'dark'
  const datasets = useDatasets() ?? []
  const [selectedDs, setSelectedDs] = useState<number | undefined>()

  // 因子库列表（多选来源）
  const [factors, setFactors] = useState<FactorInfo[]>([])
  const [factorsLoading, setFactorsLoading] = useState(true)
  const [factorsError, setFactorsError] = useState<string | null>(null)
  const [selectedIds, setSelectedIds] = useState<number[]>([])

  const [method, setMethod] = useState<CombineMethod>('score')
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<CombineResult | null>(null)
  const [saving, setSaving] = useState(false)
  // 面板冷缓存重建进度（C11b）：后端 202 受理 → panel_build 轮询进度
  const [rebuild, setRebuild] = useState<PanelRebuildProgress | null>(null)

  const icChartRef = useRef<HTMLDivElement>(null)
  const icChart = useRef<echarts.ECharts | null>(null)
  useEchartsLifecycle(icChartRef, icChart)

  const loadFactors = useCallback(async () => {
    setFactorsLoading(true)
    setFactorsError(null)
    try {
      setFactors(await getFactors())
    } catch (e) {
      setFactorsError(errMsg(e))
    } finally {
      setFactorsLoading(false)
    }
  }, [])

  useEffect(() => { loadFactors() }, [loadFactors])

  // 数据集来自共享层（useDatasets）：首次到达时解析默认选择（上次选择 ?? 首个）
  useDatasetInit(datasets, undefined, (v) => setSelectedDs(v ?? undefined))

  const run = async () => {
    if (!selectedIds.length) { toast.warning('请至少选择一个因子'); return }
    if (!selectedDs) { toast.warning('请选择数据集'); return }
    setRunning(true)
    setRebuild(null)
    try {
      // 冷缓存 202 → 内部轮询 panel_build（1s/120s）→ 完成后重取热数据（C11b）
      setResult(await runCombine({
        factor_ids: selectedIds,
        dataset_id: selectedDs,
        method,
        save_to_library: false,
      }, (job) => setRebuild(job)))
    } catch (e) {
      toast.error('合成失败: ' + errMsg(e))
    } finally {
      setRunning(false)
      setRebuild(null)
    }
  }

  // 入因子库：复用 /alpha/combine 的 save_to_library（后端返回 factor.duplicate 判定重复）
  const saveToLibrary = async () => {
    if (!result || !selectedDs) return
    setSaving(true)
    setRebuild(null)
    try {
      const res = await runCombine({
        factor_ids: selectedIds,
        dataset_id: selectedDs,
        method,
        save_to_library: true,
      }, (job) => setRebuild(job))
      if (res.factor?.duplicate) {
        toast.warning(`合成表达式已存在于因子库：因子「${res.factor.name}」#${res.factor.id}`)
      } else {
        toast.success(`合成因子「${res.factor?.name ?? ''}」已入因子库`)
      }
    } catch (e) {
      toast.error('入因子库失败: ' + errMsg(e))
    } finally {
      setSaving(false)
      setRebuild(null)
    }
  }

  // IC 序列图（echarts 折线，横轴用索引即可；isDark 进依赖数组切换色板）
  useEffect(() => {
    if (!result?.ic_series?.length || !icChartRef.current) return
    if (!icChart.current) icChart.current = echarts.init(icChartRef.current)
    const t = chartTheme(isDark)
    const dates = result.ic_series.map((_, i) => String(i))
    icChart.current.setOption({
      animation: false,
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, valueFormatter: (v: number) => fmtRatio(v, 4) },
      grid: { left: 45, right: 16, top: 24, bottom: 24 },
      dataZoom: chartDataZoom(isDark),
      xAxis: { type: 'category', data: dates, axisLabel: { fontSize: 10, hideOverlap: true } },
      yAxis: chartYAxis(isDark),
      series: [{
        name: 'IC', type: 'line',
        data: result.ic_series.map((v, i) => [dates[i], v]),
        showSymbol: false,
        lineStyle: { width: 1.4, color: t.accent },
        areaStyle: { color: t.accentAreaWeak },
        markLine: {
          silent: true, symbol: 'none',
          lineStyle: { type: 'dashed', color: t.up },
          data: [{ yAxis: 0 }],
          label: { show: false },
        },
      }],
    }, true)
  }, [result, isDark])

  // 卸载时释放图表实例
  useEffect(() => () => {
    if (icChart.current && !icChart.current.isDisposed()) icChart.current.dispose()
    if (icChartRef.current) {
      const i = echarts.getInstanceByDom(icChartRef.current)
      if (i && !i.isDisposed()) i.dispose()
    }
    icChart.current = null
  }, [])

  const factorList = (
    <>
      <Checkbox.Group value={selectedIds} onChange={setSelectedIds}>
        <ScrollArea.Autosize mah={FACTOR_LIST_MAX_H} type="auto">
          <Flex direction="column" gap={6}>
            {factors.map((f) => (
              <Checkbox
                key={f.id}
                value={f.id}
                label={(
                  <span style={{ display: 'block', maxWidth: 300 }}>
                    <Text span fw={500} style={{ fontSize: 'var(--sr-font-sm)' }}>{f.name}</Text>
                    <Text span c="dimmed" truncate style={{ fontSize: 'var(--sr-font-xs)', maxWidth: 280 }}>{f.expression}</Text>
                  </span>
                )}
              />
            ))}
          </Flex>
        </ScrollArea.Autosize>
      </Checkbox.Group>
      <Text style={{ fontSize: 'var(--sr-font-xs)', color: 'var(--sr-text-3)', marginTop: 'var(--sr-pad-sm)' }}>
        已选 {selectedIds.length} 个因子
      </Text>
    </>
  )

  return (
    <PageShell>
      {/* L0 结论条（自拟，05 无规格）：组合因子数 = 选中因子 selectedIds.length；合成 IC 来自 runCombine 结果
          result.ic（等权口径），未运行无数据 → 骨架；不新增数据层，仅消费现有状态 */}
      <StatStrip
        loading={result == null}
        style={{ marginBottom: 'var(--sr-gap-row)' }}
        items={[
          { key: 'count', label: '组合因子数', value: selectedIds.length, sub: '参与合成' },
          {
            key: 'ic',
            label: '合成 IC',
            value: result != null ? fmtRatio(result.ic) : null,
            tone: result?.ic != null && result.ic > 0 ? 'up' : result?.ic != null && result.ic < 0 ? 'down' : 'plain',
            sub: result != null ? 'RankIC' : undefined,
          },
        ]}
      />
      <ResearchSplit
        config={
          <>
            {/* 左栏配置 */}
            <div className="sr-run-panel">
              <div className="sr-run-panel-title">因子选择</div>
              {factorsError ? (
                <EmptyState text={`因子库加载失败：${factorsError}`} onRetry={loadFactors} />
              ) : factorsLoading ? (
                <Flex justify="center" py="var(--sr-pad-xl)"><Loader size="sm" /></Flex>
              ) : factors.length ? factorList : (
                <EmptyState description="因子库为空，请先在因子挖掘/评估页创建因子" />
              )}
            </div>

            <div className="sr-run-panel">
              <div className="sr-run-panel-title">合成方法</div>
              <Select
                className="sr-ctl-h"
                data={COMBINE_METHOD_OPTIONS}
                value={method}
                onChange={(v) => { if (v != null) setMethod(v as CombineMethod) }}
              />
            </div>

            <div className="sr-run-panel">
              <div className="sr-run-panel-title">数据集</div>
              <DatasetSelector
                datasets={datasets}
                value={selectedDs}
                onChange={setSelectedDs}
                width="100%"
                placeholder="选择数据集"
              />
            </div>

            <Button
              fullWidth
              size="sm"
              variant="filled"
              leftSection={<IconPlayerPlay size={14} />}
              loading={running}
              onClick={run}
            >
              运行合成
            </Button>
          </>
        }
      >
        {/* 右栏结果 */}
        {rebuild && (
          <div className="sr-run-panel" style={{ marginBottom: 'var(--sr-pad-md)' }}>
            <TaskProgress job={rebuild} typeLabel={{ panel_build: '面板缓存重建' }} />
          </div>
        )}
        {result ? (
          <>
            <div className="sr-run-panel">
              <div className="sr-run-panel-title">
                <IconRocket style={{ color: 'var(--sr-accent)' }} />
                入因子库
              </div>
              <Flex direction="column" gap="var(--sr-pad-lg)">
                <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)', margin: 0 }}>
                  将合成表达式保存为草稿因子（名称自动生成，指标随记录 IC 与正占比）。
                </Text>
                <div>
                  <Button
                    variant="filled"
                    leftSection={<IconRocket size={14} />}
                    loading={saving}
                    onClick={saveToLibrary}
                  >
                    入因子库
                  </Button>
                </div>
              </Flex>
            </div>

            <Flex wrap="wrap" gap="var(--sr-pad-md)">
              <div style={statCell}>
                <MetricStat
                  label="合成 IC"
                  value={fmtRatio(result.ic)}
                  tone={result.ic != null && result.ic > 0 ? 'up' : result.ic != null && result.ic < 0 ? 'down' : 'plain'}
                />
              </div>
              <div style={statCell}>
                <MetricStat label="IC 正占比" value={fmtStab(result.ic_positive_ratio)} sub="IC>0 占比" />
              </div>
              <div style={statCell}>
                <MetricStat label="因子数" value={selectedIds.length} sub="参与合成" />
              </div>
            </Flex>

            <div className="sr-run-panel">
              <div className="sr-run-panel-title">合成表达式</div>
              <FormulaText expr={result.expression} block />
            </div>

            <div className="sr-run-panel">
              <div className="sr-run-panel-title">权重（{COMBINE_METHOD_LABEL[result.method] ?? result.method}）</div>
              <Flex direction="column" gap="var(--sr-pad-sm)">
                {result.weights.map((w) => {
                  const reverse = w.weight < 0
                  return (
                    <Group key={w.name} justify="space-between" wrap="nowrap" gap="var(--sr-pad-sm)">
                      <Text truncate title={w.name} style={{ fontSize: 'var(--sr-font-sm)', maxWidth: '65%' }}>{w.name}</Text>
                      <Group gap={6} wrap="nowrap">
                        {reverse && (
                          <Text span style={{
                            fontSize: 'var(--sr-font-xs)', color: 'var(--sr-text-2)',
                            border: '1px solid var(--sr-border)', borderRadius: 999, padding: '0 6px',
                          }}>
                            反向
                          </Text>
                        )}
                        <Text span fw={600} style={{ fontSize: 'var(--sr-font-sm)', fontVariantNumeric: 'tabular-nums' }}>
                          {fmtPct(Math.abs(w.weight))}
                        </Text>
                      </Group>
                    </Group>
                  )
                })}
              </Flex>
            </div>

            <div className="sr-run-panel">
              <div className="sr-run-panel-title">IC 时间序列</div>
              {result.ic_series?.length ? (
                <div ref={icChartRef} role="img" aria-label="合成信号 IC 时间序列图" className="sr-chart-box-sm" />
              ) : (
                <EmptyState description="暂无 IC 序列数据" />
              )}
            </div>

            <div className="sr-run-panel">
              <div className="sr-run-panel-title">合成信号（前 20 只）</div>
              <DataTable
                rowKey="code"
                dataSource={result.combined_signal.slice(0, 20)}
                pagination={false}
                columns={[
                  { title: '代码', dataIndex: 'code' },
                  { title: '信号值', dataIndex: 'value', align: 'right', render: (v: number) => fmtRatio(v, 4) },
                ]}
                fillWidth
              />
            </div>
          </>
        ) : (
          <div className="sr-run-panel">
            <EmptyState description="选择因子与数据集后运行合成，指标 / 权重 / IC 序列在此展示" />
          </div>
        )}
      </ResearchSplit>
    </PageShell>
  )
}
