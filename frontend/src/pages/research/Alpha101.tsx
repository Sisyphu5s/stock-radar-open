import { Badge, Button, Code, Divider, Drawer, Grid, GridCol, Group, Modal, Progress, Select, Stack, Text, Tooltip } from '@mantine/core'
import { IconBolt, IconDatabase, IconRefresh } from '@tabler/icons-react'
import { useEffect, useMemo, useState } from 'react'
import { api, evaluateAlpha101, getAlpha101, scoreAlpha101 } from '../../api/client'
import type { Alpha101ScoreItem, Alpha101ScorePayload, JobDetail, PanelRebuildProgress } from '../../api/client'
import { useDatasets } from '../../data/jobs'
import { useJobFlow } from '../../hooks/useJobFlow'
import LatexFormula from '../../components/LatexFormula'
import { CardShell, DataTable, DatasetSelector, FormulaText, IconTextButton, InfiniteScrollToggle, PageHeader, PageShell, StatStrip, TaskProgress, Toolbar } from '../../components/ui'
import type { DataTableProps } from '../../components/ui'
import { PageTableBlock } from '../../templates/page-table'
import { fmtNum, fmtPct, fmtRatio, fmtStab } from '../../utils/format'
import { useDatasetInit } from '../../hooks/useDatasetInit'
import { useViewport } from '../../app/useViewport'
import { useDensityStore } from '../../stores/useAppStore'
import ResearchFlowBar from './shared/ResearchFlowBar'
import { useListInfinite, ListLoadMeta, ListInfiniteSentinel } from './shared/ListInfinite'
import DataList from './shared/DataList'
import { toast } from './shared/toast'

type AlphaItem = {
  id: number; name: string; category: string; formula: string; latex?: string; desc?: string
  formula_expr?: string; params_desc?: string; usage?: string
  evaluable?: boolean; skip_reason?: string
}

type ScoreItem = {
  id: number; name: string; ic_mean: number | null; stability: number | null; score: number | null
}

type AlphaDetail = {
  alpha: AlphaItem
  same_category: AlphaItem[]
}

type AlphaEvalMetric = {
  ic?: number | null
  stability?: number | null
  ic_positive_ratio?: number | null
  top_annual?: number | null
  long_short_annual?: number | null
  turnover?: number | null
  complexity?: number | null
}

type AlphaEvalDetail = {
  alpha: AlphaItem
  meta?: { stocks?: number; days?: number }
  train?: AlphaEvalMetric
  oos?: AlphaEvalMetric
}

type MetricRow = { key: string; label: string; train: string | number; oos: string | number }

const INF_KEY = 'sr-research-a101-inf'

/** 库评分达标阈值：评分 >= 该值判定因子表现达标（高亮为涨色） */
const MIN_SCORE_TO_RUN = 3
/** 操作列 dataIndex（仅作移动端隐藏过滤的列标识，无对应数据字段） */
const OP_COL_INDEX = 'operation'

export default function Alpha101() {
  const viewport = useViewport()
  const isMobile = viewport === 'mobile'
  const density = useDensityStore((s) => s.density)
  const ctlSize = density === 'compact' ? 'small' : 'middle'
  const [items, setItems] = useState<AlphaItem[]>([])
  const [allItems, setAllItems] = useState<AlphaItem[]>([])
  const [categories, setCategories] = useState<string[]>([])
  const [category, setCategory] = useState<string>()
  const datasets = (useDatasets() ?? []) as { id: number; name: string }[]
  const [dsId, setDsId] = useState<number>()
  const [scores, setScores] = useState<Map<number, ScoreItem>>(new Map())
  const [importing, setImporting] = useState(false)
  const [detail, setDetail] = useState<AlphaEvalDetail | null>(null)
  const [drawerItem, setDrawerItem] = useState<AlphaItem | null>(null)
  const [drawerDetail, setDrawerDetail] = useState<AlphaDetail | null>(null)
  const [loading, setLoading] = useState(false)
  // 单因子评估 loading：记录正在评估的因子 id（防重复点击，按钮/名称链显示 loading）
  const [evaluating, setEvaluating] = useState<number | null>(null)
  // 单因子评估冷缓存重建进度（C11b）：后端 202 受理 → panel_build 轮询进度
  const [evalRebuild, setEvalRebuild] = useState<PanelRebuildProgress | null>(null)
  // P2-16:魔法数字命名——无限滚动每批 20 条、评分 TopN 导入/推荐条数
  const INF_PAGE_SIZE = 20
  const TOP_N = 5
  const inf = useListInfinite(items.length, INF_KEY, INF_PAGE_SIZE)

  // 全库评分任务 → useJob 共享轮询（替代 waitForJob 页面局部轮询）：结果从 job.result 读取
  const { job: scoreJob, polling: scorePolling, submit: submitScore } = useJobFlow<JobDetail>({
    submit: async () => {
      // 全库评分 = 后台任务（alpha101_score，逐因子进度）：scoreAlpha101 已提交任务，
      // limit 传 undefined 评全库：分类筛选下传 items.length 只评当前分类条目数，评分与表格错位
      const id = dsId
      if (id == null) throw new Error('请先选择数据集')
      const r = await scoreAlpha101(id)
      toast.success(`全库评分任务 #${r.job_id} 已提交（每个因子独立计分）`)
      return { job_id: r.job_id }
    },
    makeOptimistic: (id) => ({ id, status: 'pending', progress: 0, result: null, job_type: 'alpha101_score' }),
    onDone: (j) => {
      const res = (j.result ?? {}) as Alpha101ScorePayload
      setScores(new Map((res?.results ?? []).map((s: Alpha101ScoreItem) => [s.id, s])))
      toast.success(`库评分完成: ${res?.meta?.scored ?? 0} 个因子`)
    },
    onFailed: (j) => { toast.error('评分失败: ' + (j.error ?? '评分任务失败')) },
  })

  // 分类筛选加载：cancelled 标志防竞态（快速切分类时旧响应后到不得覆盖新列表）
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    getAlpha101(category)
      .then((r) => {
        if (cancelled) return
        setItems(r.data)
        setCategories(r.categories)
      })
      .catch(() => { /* 忽略加载失败 */ })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [category])

  useEffect(() => {
    getAlpha101().then((r) => setAllItems(r.data)).catch(() => {})
  }, [])

  // 数据集来自共享层（useDatasets）：首次到达时解析默认选择（仅未选择时）
  useDatasetInit(datasets, undefined, (v) => { if (!dsId) setDsId(v) })

  const runScore = async () => {
    if (!dsId) { toast.warning('请先选择数据集'); return }
    try {
      await submitScore()
    } catch (e: any) {
      toast.error('评分失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
    }
  }

  const runEval = async (a: AlphaItem) => {
    if (a.evaluable === false) { toast.warning(a.skip_reason || '该因子暂不可评估'); return }
    if (!dsId) { toast.warning('请先选择数据集'); return }
    setEvaluating(a.id)
    setEvalRebuild(null)
    try {
      // 冷缓存 202 → 内部轮询 panel_build（1s/120s）→ 完成后重取热数据（C11b）
      const r = await evaluateAlpha101(a.id, dsId, 5, (job) => setEvalRebuild(job))
      setDetail(r)
    } catch (e: any) {
      toast.error('评估失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
    } finally {
      setEvaluating(null)
      setEvalRebuild(null)
    }
  }

  const openDetail = async (a: AlphaItem) => {
    setDrawerItem(a)
    try {
      const r = (await api.get(`/alpha/alpha101/${a.id}`)).data as AlphaDetail
      setDrawerDetail(r)
    } catch {
      setDrawerDetail(null)
    }
  }

  const closeDetail = () => {
    setDrawerItem(null)
    setDrawerDetail(null)
  }

  const importToLib = async (a: AlphaItem): Promise<'ok' | 'dup' | 'err'> => {
    if (!dsId) { toast.warning('请先选择数据集'); return 'err' }
    try {
      const r = (await api.post(`/alpha/alpha101/${a.id}/to-library`, { dataset_id: dsId })).data
      return r.ok ? 'ok' : 'dup'
    } catch {
      return 'err'
    }
  }

  const importSingle = async (a: AlphaItem) => {
    const r = await importToLib(a)
    if (r === 'ok') {
      toast.success(`「${a.name}」已加入因子库，可前往 因子库 页面查看与回测（/research/factors）`)
    } else if (r === 'dup') {
      toast.info('该因子已存在于因子库，无需重复导入')
    } else {
      toast.error('加入因子库失败，请重试')
    }
  }

  const importTopN = async () => {
    if (!dsId) { toast.warning('请先选择数据集'); return }
    const ranked = items
      .map((it) => ({ it, s: scores.get(it.id) }))
      .filter((x) => x.s?.score != null)
      .sort((a, b) => (b.s!.score as number) - (a.s!.score as number))
      .slice(0, TOP_N)
    if (!ranked.length) { toast.warning('请先运行「全库评分」，再导入评分最高的因子'); return }
    setImporting(true)
    let ok = 0, dup = 0, err = 0
    for (const { it } of ranked) {
      const r = await importToLib(it)
      if (r === 'ok') ok += 1
      else if (r === 'dup') dup += 1
      else err += 1
    }
    setImporting(false)
    toast.success(`导入完成: 成功 ${ok} 个，已存在跳过 ${dup} 个${err ? `，失败 ${err} 个` : ''}`)
  }

  const catStats = useMemo(() => {
    const m = new Map<string, { count: number; scores: number[] }>()
    for (const it of allItems) {
      const e = m.get(it.category) ?? { count: 0, scores: [] }
      e.count += 1
      const s = scores.get(it.id)?.score
      if (s != null) e.scores.push(s)
      m.set(it.category, e)
    }
    return [...m.entries()].map(([cat, e]) => ({
      cat,
      count: e.count,
      avg: e.scores.length ? e.scores.reduce((x, y) => x + y, 0) / e.scores.length : null,
    }))
  }, [allItems, scores])

  const recs = useMemo(() => {
    const same = drawerDetail?.same_category ?? []
    return same
      .filter((x) => x.id !== drawerItem?.id)
      .sort((a, b) => (scores.get(b.id)?.score ?? -Infinity) - (scores.get(a.id)?.score ?? -Infinity))
      .slice(0, TOP_N)
  }, [drawerDetail, drawerItem, scores])

  // columns 引用稳定化（DataTable 已 memo）：deps 覆盖闭包内可变引用——scores（评分 Map）与 dsId（runEval/importSingle 依赖的数据集），
  // runEval/importSingle/msg 等本身引用 dsId 且无其他可变依赖，随 dsId 变化重建即可
  const baseColumns = useMemo<DataTableProps<AlphaItem>['columns']>(() => [
    { title: 'ID', dataIndex: 'id', align: 'center', minWidth: 60 },
    {
      title: '因子', dataIndex: 'name', ellipsis: true, minWidth: 100,
      render: (v, row) => (
        <Button variant="transparent" size="xs" style={{ padding: 0, height: 'auto' }}
          loading={evaluating === row.id}
          onClick={(e) => { e.stopPropagation(); runEval(row) }}>{v}</Button>
      ),
    },
    { title: '类别', dataIndex: 'category', render: (v) => <Badge variant="light" color="gray">{v}</Badge> },
    {
      title: '公式', dataIndex: 'formula', ellipsis: true, minWidth: 120,
      render: (_, row) => <FormulaText tex={row.latex} expr={row.formula} size="small" />,
    },
    {
      title: '说明', dataIndex: 'desc', ellipsis: true, minWidth: 100,
      render: (v: string) => (v
        ? <Tooltip label={v}><Text span style={{ fontSize: 'var(--sr-font-sm)' }}>{v}</Text></Tooltip>
        : '—'),
    },
    {
      title: '库评分', align: 'right', className: 'sr-num-col',
      sorter: (a, b) => (scores.get(a.id)?.score ?? -Infinity) - (scores.get(b.id)?.score ?? -Infinity),
      render: (_, row) => {
        const s = scores.get(row.id)
        if (!s) return <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>未评分</Text>
        if (s.score === null) return <Text span c="dimmed">—</Text>
        return <Text fw={600} style={{ color: s.score >= MIN_SCORE_TO_RUN ? 'var(--sr-up)' : 'var(--sr-text-1)' }}>{fmtNum(s.score, 2)}</Text>
      },
    },
    {
      title: 'IC 均值', align: 'right', className: 'sr-num-col',
      sorter: (a, b) => (scores.get(a.id)?.ic_mean ?? -Infinity) - (scores.get(b.id)?.ic_mean ?? -Infinity),
      render: (_, row) => fmtRatio(scores.get(row.id)?.ic_mean),
    },
    {
      title: '稳定性', align: 'right', className: 'sr-num-col',
      sorter: (a, b) => (scores.get(a.id)?.stability ?? -Infinity) - (scores.get(b.id)?.stability ?? -Infinity),
      render: (_, row) => fmtStab(scores.get(row.id)?.stability),
    },
    {
      title: '操作', dataIndex: OP_COL_INDEX, align: 'center',
      render: (_, row) => (
        <Group gap={4}>
          <Button size="xs" leftSection={<IconDatabase size={14} />} onClick={(e) => { e.stopPropagation(); importSingle(row) }}>入库</Button>
          {row.evaluable === false
            ? <Tooltip label={row.skip_reason}>
                <span><Button size="xs" variant="outline" leftSection={<IconBolt size={14} />} disabled>评估</Button></span>
              </Tooltip>
            : <Button size="xs" variant="outline" leftSection={<IconBolt size={14} />} loading={evaluating === row.id}
                onClick={(e) => { e.stopPropagation(); runEval(row) }}>评估</Button>}
        </Group>
      ),
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [scores, dsId, evaluating])

  const columns: DataTableProps<AlphaItem>['columns'] = isMobile
    ? baseColumns.filter((c) => !('children' in c) && c.dataIndex !== OP_COL_INDEX)
    : baseColumns

  const detailRows: MetricRow[] = [
    { key: 'ic', label: 'IC', train: fmtRatio(detail?.train?.ic), oos: fmtRatio(detail?.oos?.ic) },
    { key: 'stability', label: '稳定性', train: fmtStab(detail?.train?.stability), oos: fmtStab(detail?.oos?.stability) },
    { key: 'ic_positive_ratio', label: 'IC 为正占比', train: fmtStab(detail?.train?.ic_positive_ratio), oos: fmtStab(detail?.oos?.ic_positive_ratio) },
    { key: 'top_annual', label: '多头年化', train: fmtPct(detail?.train?.top_annual), oos: fmtPct(detail?.oos?.top_annual) },
    { key: 'long_short_annual', label: '多空年化', train: fmtPct(detail?.train?.long_short_annual), oos: fmtPct(detail?.oos?.long_short_annual) },
    { key: 'turnover', label: '换手率', train: fmtRatio(detail?.train?.turnover, 3), oos: fmtRatio(detail?.oos?.turnover, 3) },
    { key: 'complexity', label: '复杂度', train: detail?.train?.complexity ?? '—', oos: detail?.oos?.complexity ?? '—' },
  ]

  // metricColumns 纯静态：useMemo 稳定引用（DataTable 已 memo，避免每次渲染重建列定义）
  const metricColumns = useMemo<DataTableProps<MetricRow>['columns']>(
    () => [
      { title: '指标', dataIndex: 'label', align: 'left' },
      { title: '训练段', dataIndex: 'train', align: 'right', className: 'sr-num-col', minWidth: 70 },
      { title: '样本外', dataIndex: 'oos', align: 'right', className: 'sr-num-col', minWidth: 70 },
    ],
    [],
  )

  const cur = drawerDetail?.alpha ?? drawerItem

  // L0 结论条（05 §5.5 Alpha101）：因子总数 = 全库 allItems.length；可用 = evaluable !== false（跳过原因项不可评估）；
  // 待验证 = 可用但未评分（scores 中无评分记录或 score 为 null，与「库评分」列「未评分」口径一致）。
  // 口径以代码现状为准：全库列表 getAlpha101() 无参拉取，加载失败时维持骨架不显示假数字。
  const usableCount = useMemo(
    () => allItems.filter((a) => a.evaluable !== false).length,
    [allItems],
  )
  const pendingCount = useMemo(
    () => allItems.filter((a) => a.evaluable !== false && (scores.get(a.id)?.score ?? null) == null).length,
    [allItems, scores],
  )

  return (
    <PageShell fill={!inf.on} className="sr-sticky-tools">
      <PageHeader
        extra={
          !isMobile && (
            <Group gap={8}>
              <IconTextButton size={ctlSize} icon={<IconDatabase size={16} />} text="导入 TopN 到因子库" loading={importing} onClick={importTopN} />
              <IconTextButton size={ctlSize} icon={<IconRefresh size={16} />} text="全库评分" loading={scorePolling} onClick={runScore} type="primary" />
            </Group>
          )
        }
      />
      <ResearchFlowBar
        current="discovery"
        discoveryView="alpha101"
        dataset={dsId != null ? datasets.find((d) => d.id === dsId) ?? null : null}
        stepsHidden
      />
      {/* L0 结论条（05 §5.5 Alpha101）：101 因子 · 可用 · 待验证；全库列表未到时骨架 */}
      <StatStrip
        loading={allItems.length === 0}
        scrollable={isMobile}
        style={{ marginBottom: 'var(--sr-gap-row)' }}
        items={[
          { key: 'total', label: '101 因子', value: allItems.length },
          { key: 'usable', label: '可用', value: usableCount },
          { key: 'pending', label: '待验证', value: pendingCount, sub: '未评分' },
        ]}
      />
      <Toolbar sticky tail={<InfiniteScrollToggle checked={inf.on} onChange={inf.setOn} storageKey={INF_KEY} />}>
        <Select
          className="sr-ctl-h" placeholder="类别筛选" clearable searchable
          value={category}
          onChange={(v) => setCategory(v ?? undefined)}
          data={categories.map((c) => ({ value: c, label: c }))}
          styles={{ input: { height: 'var(--sr-ctl-h)', minHeight: 'var(--sr-ctl-h)' } }}
        />
        <DatasetSelector
          datasets={datasets}
          value={dsId}
          placeholder="数据集"
          onChange={(v) => {
            setDsId(v)
            // 切换数据集即清空旧评分/旧评估：评分结果与数据集强绑定，避免跨数据集误读
            setScores(new Map())
            setDetail(null)
          }}
        />
      </Toolbar>
      {scorePolling && (
        <Progress value={Math.round(scoreJob?.progress ?? 0)} size="xs" style={{ marginBottom: 'var(--sr-pad-md)' }} striped animated />
      )}
      {evalRebuild && (
        <div style={{ marginBottom: 'var(--sr-pad-md)' }}>
          <TaskProgress job={evalRebuild} typeLabel={{ panel_build: '面板缓存重建' }} />
        </div>
      )}
      <CardShell title="类别统计">
        <Grid gap={12}>
          {catStats.map((s) => (
            // T-63 断点对齐：lg(1200) 不在准许断点清单（480/576/768/992/1200/1600/1920 为 useViewport 四档，
            // Grid 取 992 档 md:3 即可在 992+ 保持 8 列/行；rail 档容器更窄，可接受——注释说明密度语义不变）
            <GridCol key={s.cat} span={{ base: 6, sm: 4, md: 3 }}>
              <Stack gap={0}>
                <Group gap={8}>
                  <Text fw={600}>{s.cat}</Text>
                  <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>{s.count} 个因子</Text>
                </Group>
                <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
                  平均评分: {s.avg === null ? '—' : fmtNum(s.avg, 2)}
                </Text>
              </Stack>
            </GridCol>
          ))}
        </Grid>
      </CardShell>
      {/* T-52 满高:PageTableBlock 卡参与纵向弹性(fill 分页态),DataTable grow 撑满;flow 态(无限滚动)不生效 */}
      <PageTableBlock title="Alpha101 因子列表" className="sr-fill-flex">
        <DataTable
          rowKey="id" loading={loading}
          columns={columns} dataSource={inf.on ? items.slice(0, inf.shown) : items}
          pagination={inf.on ? false : { pageSize: 15, showSizeChanger: false }}
          sticky={!inf.on}
          grow={!inf.on}
          fillWidth
          onRow={(row) => ({ onClick: () => openDetail(row), style: { cursor: 'pointer' } })}
        />
        <ListLoadMeta on={inf.on} shown={inf.shown} total={inf.total} />
        <ListInfiniteSentinel on={inf.on} hasMore={inf.hasMore} loadMore={inf.loadMore} resetKey={category} />
      </PageTableBlock>

      <Drawer
        opened={!!cur} onClose={closeDetail} size="min(560px, 92vw)" position="right"
        title={cur ? `${cur.name} 详情` : ''}
      >
        {cur && (
          <Stack gap={12}>
            <DataList column={1} bordered size="small" items={[
              { key: 'id', label: 'ID', children: cur.id },
              { key: 'name', label: '名称', children: cur.name },
              { key: 'category', label: '类别', children: <Badge variant="light" color="gray">{cur.category}</Badge> },
              { key: 'params', label: '参数说明', children: cur.params_desc || '—' },
              { key: 'usage', label: '使用方向', children: cur.usage || '—' },
            ]} />
            <div>
              <Text fw={600}>公式</Text>
              <div style={{ marginTop: 'var(--sr-pad-md)' }}>
                {cur.latex
                  ? <LatexFormula tex={cur.latex} block />
                  : <Code style={{ fontSize: 'var(--sr-font-title)', display: 'block', padding: 'var(--sr-pad-sm) 14px', borderRadius: 'var(--sr-radius-ctl)', background: 'var(--sr-hover-bg)' }}>{cur.formula_expr ?? cur.formula}</Code>}
              </div>
              <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)', marginTop: 'var(--sr-pad-md)', marginBottom: 0, display: 'block' }}>
                <Code>{cur.formula_expr ?? cur.formula}</Code>
              </Text>
            </div>
            <div>
              <Text fw={600}>说明</Text>
              <Text style={{ fontSize: 'var(--sr-font-title)', marginTop: 'var(--sr-pad-xs)', marginBottom: 0 }}>
                {cur.desc || '—'}
              </Text>
            </div>
            {!isMobile && (
              <Group>
                {cur.evaluable === false
                  ? <Tooltip label={cur.skip_reason}>
                      <span><Button variant="filled" leftSection={<IconBolt size={14} />} disabled>评估</Button></span>
                    </Tooltip>
                  : <Button variant="filled" leftSection={<IconBolt size={14} />} loading={evaluating === cur.id} onClick={() => runEval(cur)}>评估</Button>}
                <Button variant="default" leftSection={<IconDatabase size={14} />} onClick={() => importSingle(cur)}>加入因子库</Button>
              </Group>
            )}
            {recs.length > 0 && (
              <>
                <Divider />
                <Text fw={600}>同类别推荐（按评分 Top5）</Text>
                {recs.map((r) => {
                  const s = scores.get(r.id)
                  return (
                    <div
                      key={r.id}
                      role="button"
                      tabIndex={0}
                      style={{ cursor: 'pointer', padding: 'var(--sr-pad-xs) var(--sr-pad-md)', display: 'flex', justifyContent: 'space-between', gap: 'var(--sr-pad-md)', alignItems: 'center' }}
                      onClick={() => openDetail(r)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDetail(r) }
                      }}
                    >
                      <Text style={{ fontSize: 'var(--sr-font-sm)' }}>{r.name}</Text>
                      <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
                        {s?.score != null ? fmtNum(s.score, 2) : '未评分'}
                      </Text>
                    </div>
                  )
                })}
              </>
            )}
          </Stack>
        )}
      </Drawer>

      <Modal
        opened={!!detail} onClose={() => setDetail(null)} size="min(720px, 94vw)"
        title={detail ? `${detail.alpha.name} 评估 · ${detail.meta?.stocks ?? '?'} 只股票 / ${detail.meta?.days ?? '?'} 日` : ''}
      >
        {detail && (
          <Stack gap={12}>
            <LatexFormula tex={detail.alpha.latex} />
            <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
              {detail.alpha.formula}
            </Text>
            <DataTable rowKey="key" pagination={false} scrollX={false} sticky={false}
              columns={metricColumns} dataSource={detailRows} />
          </Stack>
        )}
      </Modal>
    </PageShell>
  )
}
