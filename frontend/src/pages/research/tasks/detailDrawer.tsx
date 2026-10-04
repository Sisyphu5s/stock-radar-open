import { Accordion, Badge, Button, Drawer, Group, Progress, Stack, Text } from '@mantine/core'
import { IconArrowBackUp, IconChevronRight, IconCopy, IconRadar } from '@tabler/icons-react'
import { useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import FormulaCode from '../../../components/FormulaCode'
import { jobResultPath } from '../../../utils/jobs'
import { formatFullTime } from '../../../utils/time'
import { useViewport } from '../../../app/useViewport'
import { useJobEvents } from '../../../hooks/useJobEvents'
import type { JobEvent } from '../../../hooks/useJobEvents'
import type { JobDetail } from '../../../api/client'
import { TYPE_LABELS, taskExpr } from './constants'
import { ResultContent } from './summary'
import TaskStatusTag from './taskStatusTag'
import TaskActions from './taskActions'
import DataList from '../shared/DataList'
import { toast } from '../shared/toast'

interface DetailDrawerProps {
  open: boolean
  /** 当前展示的任务：列表快照（label/expr/summary/created_at）与完整任务（result/finished_at/完整 error）合并 */
  job: JobDetail | null
  loading: boolean
  /** 详情查询失败（404/网络/500）：不再被压成 undefined 永久转圈（T-128） */
  error?: boolean
  onRetryDetail?: () => void
  datasetName: (id?: number | null) => string
  onClose: () => void
  onPause: (id: number) => void
  onResume: (id: number) => void
  onCancel: (id: number) => void
  onDelete: (id: number) => void
  onRerun: (id: number) => void
  /** 归档状态（本地标记）+ 操作：已归档时展示「取消归档」 */
  archived: boolean
  onArchive: (id: number) => void
  onUnarchive: (id: number) => void
}

/** 右侧详情抽屉：参数 / 进度 / 错误 / 结果摘要 / 创建与完成时间 / 结果跳转 + 与列表一致的操作区 */
export default function TaskDetailDrawer({
  open, job, loading, error = false, onRetryDetail, datasetName, onClose, onPause, onResume, onCancel, onDelete, onRerun,
  archived, onArchive, onUnarchive,
}: DetailDrawerProps) {
  const navigate = useNavigate()
  const isMobile = useViewport() === 'mobile'
  const active = job?.status === 'running' || job?.status === 'pending'
  const done = job?.status === 'done'
  // T-139:运行中任务 SSE 实时事件流（阶段文案/进度/终态;抽屉关闭 job=null 时不订阅）
  const { events, connected, livePhase } = useJobEvents(job?.id)
  const eventText = (ev: JobEvent): string => {
    if (ev.type === 'phase') return ev.phase ?? '阶段更新'
    if (ev.type === 'progress') return `进度 ${Math.round(ev.progress ?? 0)}%`
    if (ev.type === 'done') return '已完成'
    if (ev.type === 'failed') return `失败${ev.error ? ': ' + ev.error : ''}`
    if (ev.type === 'cancelled') return '已取消'
    return ev.type
  }
  const recentEvents = (events ?? []).filter((e) => e.type === 'phase' || e.type === 'progress').slice(-5)
  const resultRoute = job ? jobResultPath(job.job_type, job.id, job.params) : '/tasks'
  const hasResultPage = resultRoute !== '/tasks'
  // params 全量 JSON 默认折叠：显示「参数（N 项）」，展开按钮 + 复制按钮，保持可复制
  const paramsJson = JSON.stringify(job?.params ?? {}, null, 2)
  const paramCount = Object.keys(job?.params ?? {}).length
  const copyParams = useCallback(() => {
    const p = navigator.clipboard
    if (!p) { toast.error('复制失败'); return }
    // writeText 异步 Promise：同步 try/catch 捕获不到失败，必须 then/catch 分支提示
    p.writeText(paramsJson)
      .then(() => toast.success('参数已复制'))
      .catch(() => toast.error('复制失败'))
  }, [paramsJson])

  return (
    <Drawer
      className="sr-task-drawer"
      title={job ? `任务详情 #${job.id}` : '任务详情'}
      opened={open}
      onClose={onClose}
      size="min(720px, 92vw)"
      position="right"
    >
      {!job ? (
        <Text span c="dimmed">
          {error ? (
            <Group gap={8}>
              <Text span c="red">加载失败（任务可能已删除或服务不可用）</Text>
              <Button size="xs" variant="subtle" onClick={onRetryDetail} aria-label="重试加载任务详情">重试</Button>
            </Group>
          ) : loading ? '加载中…' : '任务不存在'}
        </Text>
      ) : (
        <Stack gap={12}>
          <div className="sr-task-drawer-status">
            <Badge variant="light" color="gray">{TYPE_LABELS[job.job_type] ?? job.job_type}</Badge>
            <TaskStatusTag job={job} />
          </div>

          {(active || job.status === 'paused') && (
            <Progress
              value={Math.round(job.progress ?? 0)}
              color="var(--sr-accent)"
              striped={active}
              animated={active}
            />
          )}

          {/* T-139:运行中任务实时事件流(SSE livePhase + 最近阶段/进度) */}
          {(active || job.status === 'paused') && (
            <div role="status" aria-live="polite" style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-gap-row)' }}>
              <Group gap={6} wrap="wrap">
                <Badge variant="light" color={connected ? 'teal' : 'gray'} size="sm">
                  {connected ? '实时' : '重连中'}
                </Badge>
                {livePhase && <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>{livePhase}</Text>}
              </Group>
              {recentEvents.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                  {recentEvents.map((ev, i) => (
                    <Badge key={i} variant="light" color="blue" size="sm" style={{ fontWeight: 400 }}>
                      {eventText(ev)}
                    </Badge>
                  ))}
                </div>
              )}
            </div>
          )}

          <DataList column={isMobile ? 1 : 2} bordered size="small"
            items={[
              { key: 'type', label: '类型', children: <Badge variant="light" color="gray">{TYPE_LABELS[job.job_type] ?? job.job_type}</Badge> },
              { key: 'status', label: '状态', children: <TaskStatusTag job={job} /> },
              { key: 'ds', label: '数据集', children: datasetName(job.dataset_id) || (job.dataset_id != null ? `#${job.dataset_id}` : '—') },
              { key: 'created', label: '创建时间', children: formatFullTime(job.created_at) },
              { key: 'finished', label: '完成时间', children: formatFullTime(job.finished_at) },
            ]}
          />

          {taskExpr(job) && (
            <div>
              <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>表达式</Text>
              <div className="sr-task-drawer-block">
                <FormulaCode expr={taskExpr(job)} />
              </div>
            </div>
          )}

          <div>
            <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>参数 (params)</Text>
            <Accordion
              className="sr-task-params"
              style={{ marginTop: 'var(--sr-pad-xs)' }}
            >
              <Accordion.Item value="params">
                <Accordion.Control>{paramCount > 0 ? `查看 ${paramCount} 项参数` : '无参数'}</Accordion.Control>
                <Accordion.Panel>
                  {/* 复制按钮放 Panel 头部：Accordion.Control 本身为 button，内嵌 Button 属非法嵌套 */}
                  <Group justify="flex-end" style={{ marginBottom: 'var(--sr-pad-xs)' }}>
                    <Button size="xs" variant="subtle" leftSection={<IconCopy size={14} />} disabled={paramCount === 0}
                      onClick={copyParams}>
                      复制
                    </Button>
                  </Group>
                  <pre className="sr-task-drawer-pre">{paramsJson}</pre>
                </Accordion.Panel>
              </Accordion.Item>
            </Accordion>
          </div>

          {job.error && (
            <div>
              <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-error)' }}>错误信息</Text>
              <div className="sr-task-drawer-block" style={{ color: 'var(--sr-error)' }}>
                {job.error}
              </div>
            </div>
          )}

          <ResultContent job={job} />

          <div className="sr-task-drawer-actions">
            <TaskActions
              job={job}
              mode="text"
              archived={archived}
              onPause={onPause}
              onResume={onResume}
              onCancel={onCancel}
              onDelete={onDelete}
              onRerun={onRerun}
              onArchive={onArchive}
              onUnarchive={onUnarchive}
            />
            {/* 业务对象互链：market_scan 任务 → 信号中心；paper_experiment 深链走 jobResultPath（跳转结果） */}
            {job.job_type === 'market_scan' && (
              <Button variant="filled" leftSection={<IconRadar size={14} />} onClick={() => navigate('/signals')}>
                前往信号中心
              </Button>
            )}
            {done && hasResultPage && (
              <Button variant="filled" leftSection={<IconChevronRight size={14} />} onClick={() => navigate(resultRoute)}>
                跳转结果
              </Button>
            )}
            {/* 结果页跳转后的返回路径（含深链/浏览器返回场景），保证随时可回到任务列表 */}
            <Button variant="default" leftSection={<IconArrowBackUp size={14} />} onClick={() => { navigate('/tasks'); onClose() }}>
              返回任务列表
            </Button>
          </div>
        </Stack>
      )}
    </Drawer>
  )
}
