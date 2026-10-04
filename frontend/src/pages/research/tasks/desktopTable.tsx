import { Badge, Checkbox, Group, Progress, Text, Tooltip } from '@mantine/core'
import { useCallback, useMemo } from 'react'
import DataTable from '../../../components/ui/DataTable'
import type { SrColumn } from '../../../components/ui/tableTypes'
import FormulaCode from '../../../components/FormulaCode'
import TaskProgress from '../../../components/ui/TaskProgress'
import { formatFullTime } from '../../../utils/time'
import { useElementSize } from '../../../hooks/useElementSize'
import type { TaskRow } from './constants'
import { TYPE_LABELS, taskExpr } from './constants'
import type { SortKey } from './TasksFilters'
import { SummaryCell } from './summary'
import TaskStatusTag from './taskStatusTag'
import TaskActions from './taskActions'

interface DesktopTableProps {
  rows: TaskRow[]
  total: number
  page: number
  pageSize: number
  loading: boolean
  /** 无限滚动开启时隐藏分页（服务端增量由 Tasks.tsx 的哨兵驱动）；同时禁用批量选择 */
  infinite?: boolean
  /** 与父页 PageShell fill 同源（fillActive = view==='jobs' && !inf.on）：驱动 DataTable grow，
   *  单一事实源避免 fill/grow 条件双份漂移；缺省回退 !infinite（旧行为，独立复用安全） */
  fillActive?: boolean
  onPageChange: (p: number) => void
  onOpen: (r: TaskRow) => void
  onPause: (id: number) => void
  onResume: (id: number) => void
  onCancel: (id: number) => void
  onDelete: (id: number) => void
  onRerun: (id: number) => void
  /** 批量选择（仅分页态）：选中 id 集 + 变更回调（选择仅限当前页，翻页/筛选即清空） */
  selectedIds: number[]
  onSelectionChange: (ids: number[]) => void
  isArchived: (id: number) => boolean
  onArchive: (id: number) => void
  onUnarchive: (id: number) => void
  datasetName: (id?: number | null) => string
  /** 前端排序状态（Tasks.tsx 持有并持久化）：经受控 sortOrder 激活 DataTable 排序作用范围提示（M3 §3.1）；
   *  排序本身仍由 Tasks.tsx 前端执行（后端无 order_by），此处不加列 sorter、不引入表头点击排序 */
  sortBy: SortKey
  sortOrder: 'asc' | 'desc'
}

/** 容器实测宽首帧兜底(px,T-64):useElementSize 挂载首帧宽为 0,此间列宽按该值计算
 *  (与关注页 DEFAULT_TOTAL=1000 语义一致),实测到位后自动以容器宽为准 */
const DEFAULT_TOTAL = 1000

/** 响应式列宽：随容器实测宽线性缩放，min/max 双端钳制（虚拟表格要求数值像素宽，故不用 CSS clamp 字符串）。
 *  T-64:分母从 window.innerWidth(vw) 换为容器实测宽 boxSize.width——布局宽度一律容器实测(M4)，
 *  首帧实测为 0 时由调用方传入兜底宽 */
const colW = (boxW: number, min: number, frac: number, max: number) => Math.round(Math.min(max, Math.max(min, boxW * frac)))

/** paused 专用进度卡：黄色警示圆点 + 静态强调色进度条 + 协作式说明 tooltip（宽度容器化：
 *  minWidth 100% 撑满列宽——列宽已由 colW(容器实测比例)决定，不再用 vw 视口钳制） */
function PausedProgress({ job }: { job: TaskRow }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-gap-row)', minWidth: '100%' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sr-pad-md)' }}>
        <span aria-hidden style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--sr-warning)', flex: 'none' }} />
        <Text span style={{ fontSize: 'var(--sr-font-sm)', fontWeight: 600 }}>{TYPE_LABELS[job.job_type] ?? job.job_type}</Text>
        <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>#{job.id}</Text>
        <div style={{ flex: 1 }} />
        <TaskStatusTag job={job} />
      </div>
      <Progress value={Math.round(job.progress ?? 0)} size="xs" color="var(--sr-accent)" />
    </div>
  )
}

/**
 * 桌面语义表格：由 DataTable 基板衍生（scrollX/scrollY 数值驱动 virtual 虚拟滚动能力）。
 * - 列宽全部视口响应式（min/frac/max 钳制），不再有 100-220px 固定值；
 * - 非无限滚动（分页模式，默认）：开 virtual + scroll.y（高度由容器实测驱动，P1 填满），长列表 DOM 恒定；
 * - 无限滚动模式：关闭 virtual，表格随行数自然增高，页面级哨兵驱动增量加载
 *   （虚拟表格体内滚动会让哨兵常驻可视区、导致整表被一次拉满，故二选一）；
 * - 类型/状态/关键词筛选由服务端执行，行点击打开详情抽屉；
 * - 批量选择列仅分页态可用（DataTable 基板不透传 rowSelection，故手写 Checkbox 列：
 *   表头=全选当前页，行=单选，点击复选框不冒泡打开抽屉）；无限态禁用并标注。
 */
export default function DesktopTaskTable({
  rows, total, page, pageSize, loading, infinite = false,
  onPageChange, onOpen, onPause, onResume, onCancel, onDelete, onRerun,
  selectedIds, onSelectionChange, isArchived, onArchive, onUnarchive, datasetName,
  sortBy, sortOrder, fillActive,
}: DesktopTableProps) {
  // 容器宽度测量：colW 分母 = 容器实测宽（首帧 0 退 DEFAULT_TOTAL 兜底）；
  // 同时驱动 virtual 分页态 scroll.x（Math.max(totalW, 容器宽)），虚拟滚动高度已由 DataTable grow 接管
  const { ref: boxRef, size: boxSize } = useElementSize<HTMLDivElement>()
  const boxW = boxSize.width > 0 ? boxSize.width : DEFAULT_TOTAL
  const w = useCallback((min: number, frac: number, max: number) => colW(boxW, min, frac, max), [boxW])

  // 受控排序方向：'asc'|'desc' → DataTable sortOrder 字面量（仅当前排序键列传入，其余列不传）
  const sortDir = sortOrder === 'asc' ? 'ascend' : 'descend'

  // 全选当前页：rows 全部被选中 → 取消全选，否则选中当前页全部
  const allChecked = rows.length > 0 && rows.every((r) => selectedIds.includes(r.id))
  const someChecked = !allChecked && rows.some((r) => selectedIds.includes(r.id))
  const toggleAll = useCallback(() => {
    onSelectionChange(allChecked ? [] : rows.map((r) => r.id))
  }, [allChecked, rows, onSelectionChange])
  const toggleOne = useCallback((id: number) => {
    onSelectionChange(selectedIds.includes(id) ? selectedIds.filter((x) => x !== id) : [...selectedIds, id])
  }, [selectedIds, onSelectionChange])

  const columns = useMemo<SrColumn<TaskRow>[]>(() => [
    {
      key: 'select', width: 40, align: 'center',
      title: infinite
        ? (
          <Tooltip label="无限滚动模式不支持批量选择（仅分页模式可用）">
            <Checkbox disabled aria-label="批量选择不可用" />
          </Tooltip>
        )
        : (
          <Checkbox
            checked={allChecked}
            indeterminate={someChecked}
            onChange={toggleAll}
            aria-label="选择本页全部任务"
          />
        ),
      // 点击复选框 stopPropagation，避免冒泡到行点击打开详情抽屉
      render: (_: unknown, r: TaskRow) => (
        <span onClick={(e) => e.stopPropagation()}>
          <Checkbox
            checked={selectedIds.includes(r.id)}
            disabled={infinite}
            onChange={() => toggleOne(r.id)}
            aria-label={`选择任务 #${r.id}`}
          />
        </span>
      ),
    },
    { title: 'ID', dataIndex: 'id', width: w(56, 0.05, 80), align: 'center', render: (v: number) => `#${v}` },
    {
      title: '类型', dataIndex: 'job_type', width: w(110, 0.08, 150),
      sortOrder: sortBy === 'type' ? sortDir : undefined,
      render: (_: unknown, r: TaskRow) => (
        <Group gap={4}>
          <Badge variant="light" color="gray">{TYPE_LABELS[r.job_type] ?? r.job_type}</Badge>
          {isArchived(r.id) && <Badge variant="light" color="gray" style={{ marginInlineEnd: 0 }}>已归档</Badge>}
        </Group>
      ),
    },
    {
      title: '状态 / 进度', dataIndex: 'status', width: w(200, 0.16, 280),
      sortOrder: sortBy === 'status' ? sortDir : undefined,
      render: (_: unknown, r: TaskRow) => r.status === 'paused' ? <PausedProgress job={r} /> : (
        <TaskProgress
          job={{ id: r.id, job_type: r.job_type, status: r.status, progress: r.progress ?? 0 }}
          typeLabel={TYPE_LABELS}
          extra={<TaskStatusTag job={r} />}
        />
      ),
    },
    {
      title: '任务', dataIndex: 'expr', ellipsis: true, width: w(150, 0.14, 240),
      render: (_: unknown, r: TaskRow) => {
        const expr = taskExpr(r)
        return expr
          ? <Tooltip label={expr}><FormulaCode expr={expr} style={{ fontSize: 'var(--sr-font-sm)' }} /></Tooltip>
          : <Text span c="dimmed">—</Text>
      },
    },
    {
      title: '摘要', key: 'summary', width: w(200, 0.18, 300),
      render: (_: unknown, r: TaskRow) => <SummaryCell job={r} />,
    },
    {
      title: '数据集', dataIndex: 'dataset_id', ellipsis: true, width: w(100, 0.09, 160),
      render: (v: number | null | undefined) => datasetName(v) || (v != null ? `#${v}` : '—'),
    },
    {
      title: '创建时间', dataIndex: 'created_at', width: w(130, 0.11, 180),
      sortOrder: sortBy === 'time' ? sortDir : undefined,
      render: (v: string | null | undefined) => v ? formatFullTime(v) : '—',
    },
    {
      title: '操作', key: 'action', width: w(120, 0.1, 160), align: 'center',
      render: (_: unknown, r: TaskRow) => (
        <TaskActions
          job={r}
          archived={isArchived(r.id)}
          onOpen={() => onOpen(r)}
          onPause={onPause}
          onResume={onResume}
          onCancel={onCancel}
          onDelete={onDelete}
          onRerun={onRerun}
          onArchive={onArchive}
          onUnarchive={onUnarchive}
        />
      ),
    },
  ], [w, datasetName, onOpen, onPause, onResume, onCancel, onDelete, onRerun,
      selectedIds, allChecked, someChecked, toggleAll, toggleOne, isArchived, onArchive, onUnarchive,
      sortBy, sortDir, infinite])

  const totalW = useMemo(() => columns.reduce((s, c) => s + (typeof c.width === 'number' ? c.width : 0), 0), [columns])

  // ===== P2-69 DataTable memo：pagination/onRow 内联对象/箭头每次渲染重建，
  // 击穿 DataTable memo → 提为稳定引用（分页对象含内联 onChange/showTotal，一并 memo 化） =====
  const tablePagination = useMemo(() => (infinite ? false : {
    current: page,
    pageSize,
    total,
    showSizeChanger: false,
    showTotal: (t: number) => `共 ${t} 条`,
    onChange: (p: number) => onPageChange(p),
  }), [infinite, page, pageSize, total, onPageChange])

  const handleRow = useCallback((r: TaskRow) => ({ onClick: () => onOpen(r), style: { cursor: 'pointer' } }), [onOpen])

  // 虚拟滚动高度已由 DataTable grow 接管(T-52)：Tasks 页 fill 分页态下 wrap 纵向弹性，
  // 组件内部 ResizeObserver 实测 viewport 可视高自动回填 scrollY，分页条为兄弟节点自然占位
  // （原 viewH/pagRef 双目标手工测量逻辑已泛化入薄壳，此处不再需要）。
  const virtual = !infinite
  return (
    // boxRef 放最外层：基板在内部自行包裹 .sr-table-scroll（HScroll + ↔ 提示），宽度测量不受影响；
    // fill 分页态纵向弹性（flex:1 承接 DataTable grow）；无限滚动 flow 态保持原块级布局零回归
    <div ref={boxRef} style={{
      minWidth: 0,
      minHeight: 0,
      ...(infinite ? null : { flex: 1, display: 'flex', flexDirection: 'column' }),
    }}>
      <DataTable<TaskRow>
        className="sr-task-table"
        rowKey="id"
        loading={loading}
        columns={columns}
        dataSource={rows}
        pagination={tablePagination}
        virtual={virtual}
        // T-118:virtual 双表结构下表头独立 .sr-dt-vhead 吸顶,与行区绝对定位无冲突——
        // 分页态(virtual)与无限态(flow)统一保持吸顶,不再区分
        sticky
        // 分页态保持现状（数值 scrollX：列宽合计/容器宽取大，宽屏撑满容器）；
        // 无限态（flow）改 fillWidth(M1)：基板实测容器宽顶满，弃用 max-content
        scrollX={virtual ? Math.max(totalW, boxSize.width) : undefined}
        fillWidth={!virtual}
        grow={fillActive ?? !infinite}

        onRow={handleRow}
        emptyText="暂无任务"
        // 排序作用范围明示（M3 §3.1）：分页态='page'（排序作用于当前页 20 条），无限态='loaded'（已加载集合）；量词=条
        sortScope={infinite ? 'loaded' : 'page'}
        sortScopeUnit="条"
      />
    </div>
  )
}
