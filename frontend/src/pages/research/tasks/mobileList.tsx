import { Badge, Pagination, Progress, Text } from '@mantine/core'
import { useVirtualizer } from '@tanstack/react-virtual'
import { useRef } from 'react'
import type { TaskRow } from './constants'
import { TYPE_LABELS, taskExpr } from './constants'
import FormulaCode from '../../../components/FormulaCode'
import TaskStatusTag from './taskStatusTag'
import TaskActions from './taskActions'
import { SummaryCell } from './summary'
import { formatFullTime } from '../../../utils/time'

/** 任务卡片间距：与原 .sr-task-mcards 的 gap（--sr-gap-row）一致（虚拟化后由 item 底部 padding 承担） */
const CARD_GAP = 8
/** 任务卡片初始估算高度（px）：measureElement 挂载后立即按实测修正 */
const CARD_ESTIMATE = 160

interface MobileListProps {
  rows: TaskRow[]
  total: number
  page: number
  pageSize: number
  loading: boolean
  /** 无限滚动开启时隐藏分页与“共 N 条”页码提示，改为显示“已加载 X / 共 N” */
  infinite?: boolean
  onPageChange: (p: number) => void
  onOpen: (r: TaskRow) => void
  onPause: (id: number) => void
  onResume: (id: number) => void
  onCancel: (id: number) => void
  onDelete: (id: number) => void
  onRerun: (id: number) => void
  isArchived: (id: number) => boolean
  onArchive: (id: number) => void
  onUnarchive: (id: number) => void
  datasetName: (id?: number | null) => string
}

/** <768 专用任务列表：紧凑行卡 + 服务端分页（默认 20/页，无限滚动开启时隐藏），点击行卡打开右侧详情抽屉 */
export default function MobileTaskList({
  rows, total, page, pageSize, loading, infinite = false,
  onPageChange, onOpen, onPause, onResume, onCancel, onDelete, onRerun,
  isArchived, onArchive, onUnarchive, datasetName,
}: MobileListProps) {
  const cardsRef = useRef<HTMLDivElement | null>(null)
  // 虚拟化挂载到真实的滚动承载容器：
  // 从卡片容器自身开始向上找 overflow-y 为 auto/scroll 的祖先；若候选是“伪滚动容器”（高度随内容增长，
  // scrollHeight===clientHeight 且内容已超出一屏），说明滚动坐标由更上层承担，继续向上找，
  // 最终兜底页面滚动元素。每次调用均重新探测（数据量变化后容器角色可能切换，缓存会锁死错误容器）。
  // 起点取 cardsRef 自身（而非父级）：T-52 分页 fill 态下卡片容器自身 overflow-y:auto 成为滚动承载，
  // 从父级起找会跳过它、误取页面级滚动容器致虚拟视口高度失真。
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => {
      let el: HTMLElement | null = cardsRef.current ?? null
      while (el) {
        const s = getComputedStyle(el)
        if (/(auto|scroll|overlay)/.test(s.overflowY || '')) {
          if (el.scrollHeight === el.clientHeight && el.scrollHeight > window.innerHeight) {
            el = el.parentElement
            continue
          }
          return el
        }
        el = el.parentElement
      }
      return document.scrollingElement ?? document.documentElement
    },
    estimateSize: () => CARD_ESTIMATE,
    overscan: 5,
    measureElement: (el) => el.getBoundingClientRect().height,
  })
  return (
    // T-52 满高：分页态加 fill 修饰类（列表区占满剩余高度，卡片容器内部滚动）；无限滚动 flow 态不加
    <div className={'sr-task-mlist' + (infinite ? '' : ' sr-task-mlist-fill')}>
      <div className="sr-task-mtool">
        <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
          {infinite ? `已加载 ${rows.length} / 共 ${total} 条` : `共 ${total} 条`}
        </Text>
      </div>
      {rows.length === 0 ? (
        <div className="sr-task-mempty">
          <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
            {loading ? '加载中…' : '暂无任务'}
          </Text>
        </div>
      ) : (
        <div className="sr-task-mcards" ref={cardsRef}>
          <div style={{ height: virtualizer.getTotalSize(), position: 'relative', width: '100%' }}>
            {virtualizer.getVirtualItems().map((vi) => {
              const r = rows[vi.index]
              return (
                <div
                  key={r.id}
                  data-index={vi.index}
                  ref={virtualizer.measureElement}
                  style={{ position: 'absolute', top: 0, left: 0, width: '100%', transform: `translateY(${vi.start}px)`, paddingBottom: CARD_GAP }}
                >
                  <div
                    className="sr-task-mcard"
                    role="button"
                    tabIndex={0}
                    onClick={() => onOpen(r)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(r) }
                    }}
                  >
                    <div className="sr-task-mcard-head">
                      <span className="sr-task-mcard-type">{TYPE_LABELS[r.job_type] ?? r.job_type}</span>
                      <span className="sr-task-mcard-id">#{r.id}</span>
                      <div style={{ flex: 1 }} />
                      <TaskStatusTag job={r} />
                    </div>
                    {isArchived(r.id) && (
                      <Badge variant="light" color="gray" style={{ fontSize: 'var(--sr-font-xs)' }}>已归档</Badge>
                    )}
                    <div className="sr-task-mcard-expr">
                      {taskExpr(r) ? <FormulaCode expr={taskExpr(r)} style={{ fontSize: 'var(--sr-font-xs)' }} /> : '—'}
                    </div>
                    {(r.status === 'pending' || r.status === 'running') && (
                      <Progress
                        value={Math.round(r.progress ?? 0)}
                        size="xs"
                        color="var(--sr-accent)"
                        striped
                        animated
                      />
                    )}
                    {r.status === 'paused' && (
                      <Progress
                        value={Math.round(r.progress ?? 0)}
                        size="xs"
                        color="var(--sr-accent)"
                      />
                    )}
                    {r.status === 'done' && <div className="sr-task-mcard-summary"><SummaryCell job={r} /></div>}
                    <div className="sr-task-mcard-meta">
                      <span>{datasetName(r.dataset_id) || (r.dataset_id != null ? `数据集 #${r.dataset_id}` : '—')}</span>
                      <span>{r.created_at ? formatFullTime(r.created_at) : '—'}</span>
                    </div>
                    <div className="sr-task-mcard-actions">
                      <TaskActions
                        job={r}
                        archived={isArchived(r.id)}
                        onPause={onPause}
                        onResume={onResume}
                        onCancel={onCancel}
                        onDelete={onDelete}
                        onRerun={onRerun}
                        onArchive={onArchive}
                        onUnarchive={onUnarchive}
                      />
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}
      {!infinite && (
        <div className="sr-task-mpage">
          <Pagination size="sm" value={page} total={Math.max(1, Math.ceil(total / pageSize))}
            onChange={onPageChange} disabled={loading} />
        </div>
      )}
    </div>
  )
}
