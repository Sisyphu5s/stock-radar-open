import {
  ActionIcon,
  Button,
  Group,
  NumberInput,
  SegmentedControl,
  Select,
  Text,
  TextInput,
  Tooltip,
} from '@mantine/core'
import { IconParentheses, IconPlayerPlay, IconPlus, IconTrash } from '@tabler/icons-react'
import { useCallback, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  isScreenerGroup,
  runScreener,
  SCREENER_FIELD_OPTIONS,
  SCREENER_LOGIC_OPTIONS,
  SCREENER_OP_OPTIONS,
  SCREENER_UNIVERSE_OPTIONS,
} from '../../../api/screener'
import type {
  ScreenerCondition,
  ScreenerConditionGroup,
  ScreenerConditionNode,
  ScreenerConditionTree,
  ScreenerField,
  ScreenerLogic,
  ScreenerOp,
  ScreenerResultRow,
  ScreenerRunPayload,
  ScreenerUniverse,
} from '../../../api/screener'
import DataTable from '../../../components/ui/DataTable'
import type { SrColumn } from '../../../components/ui/tableTypes'
import EmptyState from '../../../components/ui/EmptyState'
import PageHeader from '../../../components/ui/PageHeader'
import PageShell from '../../../components/ui/PageShell'
import StatStrip from '../../../components/ui/StatStrip'
import { useQueryState } from '../../../data/queryBase'
import { errMsg, fmtNum, fmtTime, pctColor } from '../../../utils/format'
import { useScreenerSchemes } from './useScreenerSchemes'
import type { ScreenerSchemeFilters } from './useScreenerSchemes'

/** 数值列排序：null（无数据）恒排最后（升序时 -Infinity） */
const numSorter = (pick: (r: ScreenerResultRow) => number | null) =>
  (a: ScreenerResultRow, b: ScreenerResultRow) =>
    (pick(a) ?? Number.NEGATIVE_INFINITY) - (pick(b) ?? Number.NEGATIVE_INFINITY)

/** 数值单元格：null → '—'；否则按 digits 千分位格式化 */
const fmtCell = (v: number | null, digits = 2): string =>
  v == null ? '—' : fmtNum(v, digits)

// ===== T-75 条件树编辑：path = 自根分组起的子节点索引链（[] = 根分组） =====
// 不可变更新/删除/插入，递归支持任意深嵌套（UI 提供一层分组，数据结构支持任意深）。

/** 默认新叶子条件 */
const defaultLeaf = (): ScreenerCondition => ({ field: 'pct_change', op: 'gt', value: 0 })
/** 默认新分组（and + 一个默认叶子，避免空组） */
const defaultGroup = (): ScreenerConditionGroup => ({ logic: 'and', children: [defaultLeaf()] })

type NodeUpdater = (path: number[], updater: (n: ScreenerConditionNode) => ScreenerConditionNode) => void
type NodeRemover = (path: number[]) => void
type NodeInserter = (path: number[], child: ScreenerConditionNode) => void

/** 不可变更新：path 定位节点替换为 updater 结果（path 为空 = 根分组自身） */
function updateNodeAtPath(
  node: ScreenerConditionNode,
  path: number[],
  updater: (n: ScreenerConditionNode) => ScreenerConditionNode,
): ScreenerConditionNode {
  if (path.length === 0) return updater(node)
  if (!isScreenerGroup(node)) return node
  const [head, ...rest] = path
  const children = node.children.map((c, i) => (i === head ? updateNodeAtPath(c, rest, updater) : c))
  return { ...node, children }
}

/**
 * 不可变删除：移除 path 定位的子节点。若删除后所在分组变空且非根，
 * 返回 null 让父级连带删除该分组（「组可删除连带子项」+ 空组兜底）；根分组保持非空由 UI 守卫。
 */
function removeNodeAtPath(
  node: ScreenerConditionNode,
  path: number[],
  isRoot: boolean,
): ScreenerConditionNode | null {
  if (path.length === 0) return null
  if (!isScreenerGroup(node)) return node
  const [head, ...rest] = path
  const children: ScreenerConditionNode[] = []
  for (let i = 0; i < node.children.length; i++) {
    if (i === head) {
      if (rest.length === 0) continue // 删除该子节点
      const sub = removeNodeAtPath(node.children[i], rest, false)
      if (sub !== null) children.push(sub)
    } else {
      children.push(node.children[i])
    }
  }
  if (children.length === 0 && !isRoot) return null
  return { ...node, children }
}

/** 不可变插入：向 path 定位的分组末尾追加子节点（path 为空 = 根分组） */
function insertChildAtPath(
  node: ScreenerConditionNode,
  path: number[],
  child: ScreenerConditionNode,
): ScreenerConditionNode {
  if (!isScreenerGroup(node)) return node
  if (path.length === 0) return { ...node, children: [...node.children, child] }
  const [head, ...rest] = path
  const children = node.children.map((c, i) => (i === head ? insertChildAtPath(c, rest, child) : c))
  return { ...node, children }
}

/** 叶子条件行：字段 / 操作符 / 阈值 + 删除 */
function LeafRow({ leaf, path, canRemove, updateAt, removeAt }: {
  leaf: ScreenerCondition
  path: number[]
  canRemove: boolean
  updateAt: NodeUpdater
  removeAt: NodeRemover
}) {
  return (
    // T-131 窄屏溢出修复:nowrap 固定总宽 ~394px 必溢出 320/375px——改 wrap,
    // 字段保宽不变,窄容器自动折行到下一行
    <Group gap={8} wrap="wrap">
      <Select
        size="xs"
        style={{ width: 128, flexShrink: 0 }}
        data={SCREENER_FIELD_OPTIONS}
        value={leaf.field}
        onChange={(v) => { if (v) updateAt(path, (n) => ({ ...n, field: v as ScreenerField })) }}
        aria-label={`条件 ${path.join('.')} 字段`}
      />
      <Select
        size="xs"
        style={{ width: 72, flexShrink: 0 }}
        data={SCREENER_OP_OPTIONS}
        value={leaf.op}
        onChange={(v) => { if (v) updateAt(path, (n) => ({ ...n, op: v as ScreenerOp })) }}
        aria-label={`条件 ${path.join('.')} 操作符`}
      />
      <NumberInput
        size="xs"
        style={{ width: 140, flexShrink: 0 }}
        value={leaf.value}
        onChange={(v) => updateAt(path, (n) => ({ ...n, value: typeof v === 'number' ? v : (v === '' ? 0 : Number(v)) }))}
        allowDecimal
        aria-label={`条件 ${path.join('.')} 阈值`}
      />
      <Tooltip label="删除条件">
        <ActionIcon
          size={30}
          variant="subtle"
          color="red"
          disabled={!canRemove}
          onClick={() => removeAt(path)}
          aria-label="删除条件"
        >
          <IconTrash size={14} />
        </ActionIcon>
      </Tooltip>
    </Group>
  )
}

/** 条件分组（括号）：缩进 + 边框 + 括号徽标；组内逻辑切换；子节点递归；组可删除 */
function GroupEditor({ group, path, canRemove, updateAt, removeAt, insertChild }: {
  group: ScreenerConditionGroup
  path: number[]
  canRemove: boolean
  updateAt: NodeUpdater
  removeAt: NodeRemover
  insertChild: NodeInserter
}) {
  return (
    <div style={{
      border: '1px dashed var(--sr-border)',
      borderRadius: 6,
      padding: 8,
      marginLeft: 10,
      background: 'var(--sr-block-bg)',
      display: 'flex',
      flexDirection: 'column',
      gap: 8,
    }}>
      <Group gap={8} wrap="wrap">
        <Tooltip label="括号分组：组内条件按所选逻辑合并">
          <IconParentheses size={14} style={{ color: 'var(--sr-accent)', flexShrink: 0 }} aria-label="括号分组" />
        </Tooltip>
        <SegmentedControl
          size="xs"
          data={SCREENER_LOGIC_OPTIONS}
          value={group.logic}
          onChange={(v) => updateAt(path, (n) => ({ ...n, logic: v as ScreenerLogic }))}
          aria-label={`分组 ${path.join('.')} 逻辑`}
          style={{ height: 'var(--sr-ctl-h)' }}
          styles={{ label: { height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' } }}
        />
        <Tooltip label="删除分组（连带子条件）">
          <ActionIcon
            size={30}
            variant="subtle"
            color="red"
            disabled={!canRemove}
            onClick={() => removeAt(path)}
            aria-label="删除分组"
          >
            <IconTrash size={14} />
          </ActionIcon>
        </Tooltip>
      </Group>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {group.children.map((c, i) => {
          const childPath = [...path, i]
          const childCanRemove = group.children.length > 1
          return isScreenerGroup(c) ? (
            <GroupEditor key={i} group={c} path={childPath} canRemove={childCanRemove} updateAt={updateAt} removeAt={removeAt} insertChild={insertChild} />
          ) : (
            <LeafRow key={i} leaf={c} path={childPath} canRemove={childCanRemove} updateAt={updateAt} removeAt={removeAt} />
          )
        })}
      </div>
      <Group gap={8} wrap="wrap">
        <Button size="compact-xs" variant="light" leftSection={<IconPlus size={12} />} onClick={() => insertChild(path, defaultLeaf())}>
          添加条件
        </Button>
        <Button size="compact-xs" variant="light" leftSection={<IconParentheses size={12} />} onClick={() => insertChild(path, defaultGroup())}>
          添加分组
        </Button>
      </Group>
    </div>
  )
}

/**
 * 条件选股页（T-17 + T-75 条件树）：自定义条件组合查询快照。
 * - 条件树编辑器：叶子行（字段/操作符/阈值）+ 括号分组（组内 and/or 切换、增删）；
 *   顶层逻辑 = 根分组 logic；数据结构支持任意深嵌套（UI 提供一层分组）；
 * - 股票池：关注 / 沪深300 / 全市场；
 * - 「运行选股」→ POST /screener/run（普通请求，不轮询；同一条件重复运行强制重查）；
 * - 结果表点击行跳个股工作台；命名方案 localStorage 保存/应用（sr-screener-schemes，
 *   旧扁平方案读取时自动归一为树）。
 */
export default function ScreenerPage() {
  const navigate = useNavigate()

  const [universe, setUniverse] = useState<ScreenerUniverse>('all')
  const [tree, setTree] = useState<ScreenerConditionTree>({ logic: 'and', children: [defaultLeaf()] })
  // 运行参数：null = 未运行；变化（或 runSeq 递增）即触发查询
  const [payload, setPayload] = useState<ScreenerRunPayload | null>(null)
  const [runSeq, setRunSeq] = useState(0)

  // ---- 条件树编辑（不可变；path 递归定位） ----
  const updateAt = useCallback<NodeUpdater>((path, updater) => {
    setTree((prev) => updateNodeAtPath(prev, path, updater) as ScreenerConditionTree)
  }, [])
  const removeAt = useCallback<NodeRemover>((path) => {
    setTree((prev) => removeNodeAtPath(prev, path, true) as ScreenerConditionTree)
  }, [])
  const insertChild = useCallback<NodeInserter>((path, child) => {
    setTree((prev) => insertChildAtPath(prev, path, child) as ScreenerConditionTree)
  }, [])

  // ---- 运行：手动触发，普通请求不轮询（refetchInterval 不设；同条件重复点运行走 runSeq 强制重查） ----
  const run = useCallback((p: ScreenerRunPayload) => {
    setPayload({ ...p, conditions: { ...p.conditions, children: p.conditions.children.map((c) => ({ ...c })) } })
    setRunSeq((s) => s + 1)
  }, [])
  const resultState = useQueryState({
    queryKey: ['screener', 'run', payload ? `${JSON.stringify(payload)}#${runSeq}` : ''],
    queryFn: () => runScreener(payload as ScreenerRunPayload),
    enabled: payload != null,
    staleTime: 0,
  })

  const rows = resultState.value?.data ?? []
  const runError = resultState.error

  // ---- 命名方案（独立实现，参照 useSignalSchemes 模式）：完整条件现场保存/应用 ----
  const currentFilters = useMemo(
    () => ({ universe, conditions: tree }),
    [universe, tree],
  )
  const applyFilters = useCallback((f: ScreenerSchemeFilters) => {
    setUniverse(f.universe)
    setTree(f.conditions)
    // 方案应用即所见即所得：自动按方案条件运行一次
    run({ universe: f.universe, conditions: f.conditions })
  }, [run])
  const schemeStore = useScreenerSchemes({ currentFilters, applyFilters })
  const [schemeName, setSchemeName] = useState('')
  const saveCurrentScheme = useCallback(() => {
    if (!schemeName.trim()) return
    schemeStore.save(schemeName)
    setSchemeName('')
  }, [schemeName, schemeStore])

  // ---- 结果表 ----
  const columns = useMemo<SrColumn<ScreenerResultRow>[]>(() => [
    {
      key: 'code', title: '代码', dataIndex: 'code', width: 92,
      sorter: (a, b) => a.code.localeCompare(b.code),
      render: (_, r) => <Text size="sm">{r.code}</Text>,
    },
    {
      key: 'name', title: '名称', dataIndex: 'name', width: 112, ellipsis: true,
      render: (_, r) => <Text size="sm">{r.name}</Text>,
    },
    {
      key: 'price', title: '现价', dataIndex: 'price', width: 84, align: 'right',
      sorter: numSorter((r) => r.price),
      render: (v: number | null) => <Text size="sm">{fmtCell(v, 2)}</Text>,
    },
    {
      key: 'pct_change', title: '涨跌幅', dataIndex: 'pct_change', width: 92, align: 'right',
      sorter: numSorter((r) => r.pct_change),
      render: (v: number | null) => (
        <Text size="sm" style={{ color: pctColor(v) }}>{fmtCell(v, 2)}%</Text>
      ),
    },
    {
      key: 'turnover_rate', title: '换手率', dataIndex: 'turnover_rate', width: 92, align: 'right',
      sorter: numSorter((r) => r.turnover_rate),
      render: (v: number | null) => <Text size="sm">{fmtCell(v, 2)}%</Text>,
    },
    {
      key: 'volume_ratio', title: '量比', dataIndex: 'volume_ratio', width: 84, align: 'right',
      sorter: numSorter((r) => r.volume_ratio),
      render: (v: number | null) => <Text size="sm">{fmtCell(v, 2)}</Text>,
    },
    {
      key: 'pe', title: '市盈率', dataIndex: 'pe', width: 84, align: 'right',
      sorter: numSorter((r) => r.pe),
      render: (v: number | null) => <Text size="sm">{fmtCell(v, 2)}</Text>,
    },
    {
      key: 'pb', title: '市净率', dataIndex: 'pb', width: 84, align: 'right',
      sorter: numSorter((r) => r.pb),
      render: (v: number | null) => <Text size="sm">{fmtCell(v, 2)}</Text>,
    },
    {
      key: 'market_cap', title: '总市值(亿)', dataIndex: 'market_cap', width: 108, align: 'right',
      sorter: numSorter((r) => r.market_cap),
      render: (v: number | null) => <Text size="sm">{fmtCell(v, 0)}</Text>,
    },
  ], [])

  const res = resultState.value

  // ===== P2-69 DataTable memo：pagination/onRow 内联对象/箭头每次渲染重建，
  // 击穿 DataTable memo → 提为稳定引用（分页对象含内联 showTotal，一并 memo 化） =====
  const tablePagination = useMemo(() => ({
    pageSize: 20, hideOnSinglePage: true,
    showTotal: (t: number) => `共 ${t} 只`,
  }), [])
  const handleRow = useCallback((r: ScreenerResultRow) => ({ onClick: () => navigate(`/stocks/${r.code}`), style: { cursor: 'pointer' } }), [navigate])

  // ---- L0 结论条（M2 通用契约）：命中数 + 当前方案；未运行/加载中数字位骨架 ----
  const activeScheme = schemeStore.schemes.find((s) => s.id === schemeStore.activeId)
  const l0Items = [
    {
      key: 'hits',
      label: '命中',
      value: res ? `${res.count} 只` : '—',
      sub: res
        ? `数据源 ${res.source}${res.as_of ? ` · 截至 ${fmtTime(res.as_of)}` : ''}`
        : (payload == null ? '运行选股后展示命中结果' : undefined),
    },
    { key: 'scheme', label: '当前方案', value: activeScheme?.name ?? '未命名' },
  ]

  return (
    <PageShell
      fill
      header={(
        <PageHeader
          extra={(
            <Button
              size="compact-sm"
              leftSection={<IconPlayerPlay size={14} />}
              onClick={() => run({ universe, conditions: tree })}
              aria-label="运行选股"
            >
              运行选股
            </Button>
          )}
        />
      )}
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-gap-row)', minHeight: 0, height: '100%' }}>
        {/* ===== L0 结论条（M2：页面第一屏顶部，操作栏上方） ===== */}
        <StatStrip
          loading={payload == null || (!res && resultState.loading)}
          items={l0Items}
          style={{ flexShrink: 0 }}
        />
        {/* ===== 条件编辑区 ===== */}
        <div style={{ border: '1px solid var(--sr-border)', borderRadius: 8, padding: 12, background: 'var(--sr-card-bg)', flexShrink: 0 }}>
          <Group gap={16} wrap="wrap" align="center" justify="space-between">
            <Group gap={8} wrap="wrap">
              <Text span size="sm" style={{ color: 'var(--sr-text-2)' }}>股票池</Text>
              <SegmentedControl
                size="xs"
                data={SCREENER_UNIVERSE_OPTIONS}
                value={universe}
                onChange={(v) => setUniverse(v as ScreenerUniverse)}
                aria-label="股票池"
                style={{ height: 'var(--sr-ctl-h)' }}
                styles={{ label: { height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' } }}
              />
            </Group>
            <Group gap={8} wrap="wrap">
              <Text span size="sm" style={{ color: 'var(--sr-text-2)' }}>条件逻辑</Text>
              <SegmentedControl
                size="xs"
                data={SCREENER_LOGIC_OPTIONS}
                value={tree.logic}
                onChange={(v) => updateAt([], (n) => ({ ...n, logic: v as ScreenerLogic }))}
                aria-label="条件逻辑"
                style={{ height: 'var(--sr-ctl-h)' }}
                styles={{ label: { height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' } }}
              />
            </Group>
          </Group>

          <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
            {tree.children.map((c, i) => (
              isScreenerGroup(c) ? (
                <GroupEditor
                  key={i}
                  group={c}
                  path={[i]}
                  canRemove={tree.children.length > 1}
                  updateAt={updateAt}
                  removeAt={removeAt}
                  insertChild={insertChild}
                />
              ) : (
                <LeafRow
                  key={i}
                  leaf={c}
                  path={[i]}
                  canRemove={tree.children.length > 1}
                  updateAt={updateAt}
                  removeAt={removeAt}
                />
              )
            ))}
            <Group gap={8} wrap="wrap">
              <Button size="compact-xs" variant="light" leftSection={<IconPlus size={12} />} onClick={() => insertChild([], defaultLeaf())}>
                添加条件
              </Button>
              <Button size="compact-xs" variant="light" leftSection={<IconParentheses size={12} />} onClick={() => insertChild([], defaultGroup())}>
                添加分组
              </Button>
            </Group>
          </div>

          {/* ===== 命名方案栏（T-17 联动 T-13 模式，独立实现） ===== */}
          <div style={{ marginTop: 12, borderTop: '1px solid var(--sr-border)', paddingTop: 10, display: 'flex', flexDirection: 'column', gap: 8 }}>
            <Text span size="xs" style={{ color: 'var(--sr-text-3)' }}>方案</Text>
            <Group gap={8} wrap="wrap">
              <Select
                size="xs"
                style={{ width: 200, flexShrink: 0 }}
                placeholder={schemeStore.schemes.length ? '选择方案应用' : '暂无方案，先保存当前条件'}
                data={schemeStore.schemes.map((s) => ({
                  value: s.id,
                  label: s.id === schemeStore.activeId ? `${s.name} · 已应用` : s.name,
                }))}
                value={schemeStore.activeId}
                onChange={(v) => { if (v) schemeStore.apply(v) }}
                aria-label="选择选股方案"
              />
              <TextInput
                size="xs"
                placeholder="方案名"
                value={schemeName}
                onChange={(e) => setSchemeName(e.currentTarget.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') saveCurrentScheme() }}
                aria-label="方案名称"
                style={{ width: 140, flexShrink: 0 }}
              />
              <Button size="compact-xs" onClick={saveCurrentScheme} disabled={!schemeName.trim()}>
                保存方案
              </Button>
              {schemeStore.activeId != null && (
                <Tooltip label="删除当前方案">
                  <ActionIcon size="sm" variant="subtle" color="red" onClick={() => schemeStore.remove(schemeStore.activeId as string)} aria-label="删除当前方案">
                    <IconTrash size={14} />
                  </ActionIcon>
                </Tooltip>
              )}
            </Group>
          </div>
        </div>

        {/* ===== 结果区 ===== */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, minHeight: 0, flex: 1 }}>
          <div style={{ flex: 1, minHeight: 0, overflow: 'auto', border: '1px solid var(--sr-border)', borderRadius: 8, background: 'var(--sr-card-bg)' }}>
            {payload == null ? (
              <EmptyState
                description={(
                  <Text component="span" style={{ color: 'var(--sr-text-2)', fontSize: 'var(--sr-font-sm)' }}>
                    配置条件后点击「运行选股」，或应用已保存方案
                  </Text>
                )}
                padding="48px 0"
              />
            ) : runError ? (
              <EmptyState text={'选股失败: ' + errMsg(runError)} onRetry={() => run(payload)} />
            ) : (
              <DataTable
                columns={columns}
                dataSource={rows}
                rowKey="code"
                loading={resultState.loading}
                pagination={tablePagination}
                onRow={handleRow}
                fillWidth
              />
            )}
          </div>
        </div>
      </div>
    </PageShell>
  )
}
