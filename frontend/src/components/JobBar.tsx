import { Button, Group, Indicator, Loader, Popover, Progress, Text, Tooltip } from '@mantine/core'
import { IconCalendarClock, IconChevronRight } from '@tabler/icons-react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useExperimentsPage, useJobs } from '../data/jobs'
import type { JobListItem } from '../api/client'
import { jobResultPath } from '../utils/jobs'
import EmptyState from './ui/EmptyState'
import StatusTag from './ui/StatusTag'

/**
 * 顶栏任务入口（快速查看 + 操作）：突出「进行中 / 已暂停」数量徽标 + 任务弹层
 * （进行中 + 已暂停 + 最近完成），底部「查看全部任务」跳转 /tasks。
 * 职责区分：侧栏「任务」组 = 任务页导航（/tasks 任务管理）；顶栏 = 快速查看与操作。
 * 文案用「任务」而非「任务中心」，避免与侧栏组/任务页产生重复入口歧义。
 * 已暂停任务点击进入任务中心查看/恢复，不在顶栏堆叠操作控件。
 * 双轨数据源：
 * - 徽标计数常驻：轻量分页查询（limit=1 只取 total，status 过滤）每 15s 轮询，
 *   弹层关闭也保持进行中/已暂停计数可见（不再依赖弹层内全量列表）；
 * - 弹层内容条件订阅：仅弹层打开时订阅全量任务列表（200 条，15s 轮询），关闭即停表——
 *   顶栏常驻不再持续拉 200 条（与 jobs.ts useJobs 注释一致）。
 */
export default function JobBar() {
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  // 弹层内全量列表（条件订阅：仅打开时轮询）
  const jobs = useJobs(open)
  const loading = jobs === undefined
  const list = jobs ?? []
  const active = list.filter((j) => j.status === 'pending' || j.status === 'running')
  const pausedList = list.filter((j) => j.status === 'paused')
  const finished = list.filter((j) => j.status === 'done' || j.status === 'failed').slice(0, 5)
  // 常驻徽标计数：轻量分页查询（limit=1 只取筛选后 total，不拉全量），弹层关闭时徽标仍可见
  const activeCount = useExperimentsPage({ limit: 1, status: 'active' })
  const pausedCount = useExperimentsPage({ limit: 1, status: 'paused' })
  const badgeCount = (activeCount.value?.total ?? 0) + (pausedCount.value?.total ?? 0)

  const row = (j: JobListItem) => {
    const isActive = j.status === 'running' || j.status === 'pending'
    const isPaused = j.status === 'paused'
    const go = () => navigate(isPaused ? '/tasks' : jobResultPath(j.job_type, j.id, j.params))
    return (
      <div
        key={j.id}
        className="sr-task-row"
        onClick={go}
        role="button"
        tabIndex={0}
        aria-label={`任务 #${j.id} ${j.label}`}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go() }
        }}
      >
        {isPaused ? (
          <Tooltip label="协作式暂停：在下一个计算检查点停下，可进入任务中心「继续」从断点恢复">
            <StatusTag status={j.status} kind="job" />
          </Tooltip>
        ) : (
          <StatusTag status={j.status} kind="job" />
        )}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sr-gap-row)' }}>
            <Text span style={{ fontSize: 'var(--sr-font-sm)', flexShrink: 0 }}>{j.label}</Text>
            <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)', flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {j.expr}
            </Text>
            <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)', flexShrink: 0 }}>
              #{j.id}
            </Text>
          </div>
          {(isActive || isPaused) && (
            <Progress
              size="xs"
              value={Math.round(j.progress ?? 0)}
              color="var(--sr-accent)"
              striped={isActive}
              animated={isActive}
            />
          )}
        </div>
        <Button size="xs" variant="subtle" onClick={go} aria-label={`打开任务 #${j.id}`}>
          <IconChevronRight size={14} />
        </Button>
      </div>
    )
  }

  const content = (
    <div style={{ width: 'clamp(240px, 30vw, 320px)', maxWidth: 'calc(100vw - 32px)' }}>
      <Group gap={8} style={{ marginBottom: 'var(--sr-gap-row)' }}>
        <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>任务</Text>
        <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
          {active.length} 进行中 / {pausedList.length} 已暂停 / {list.length} 总
        </Text>
      </Group>
      <div style={{ maxHeight: 340, overflow: 'auto' }}>
        {loading ? (
          <div style={{ textAlign: 'center', padding: 'var(--sr-pad-2xl) 0' }}><Loader size="sm" /></div>
        ) : list.length === 0 ? (
          <EmptyState description="暂无任务" />
        ) : (
          <>
            {active.map(row)}
            {pausedList.length > 0 && (
              <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>已暂停（可继续/取消）:</Text>
            )}
            {pausedList.map(row)}
            {finished.length > 0 && (
              <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>最近完成:</Text>
            )}
            {finished.map(row)}
          </>
        )}
      </div>
      <div style={{ borderTop: '1px solid var(--sr-border)', marginTop: 'var(--sr-pad-xs)', paddingTop: 'var(--sr-pad-xs)' }}>
        <Button
          variant="subtle" fullWidth
          onClick={() => navigate('/tasks')}
          style={{ fontSize: 'var(--sr-font-sm)' }}
          rightSection={<IconChevronRight size={14} />}
        >
          查看全部任务
        </Button>
      </div>
    </div>
  )

  return (
    <Popover opened={open} onChange={setOpen} position="bottom-end" withArrow={false}>
      <Popover.Target>
        {/* Mantine 9 受控 Popover 需在 Target 手动 onClick 切换(库仅对非受控自动注入 toggle) */}
        <Tooltip label={`进行中 ${activeCount.value?.total ?? 0} · 已暂停 ${pausedCount.value?.total ?? 0}`}>
          <Indicator label={badgeCount} size={16} offset={6} disabled={badgeCount === 0} inline>
            <Button size="xs" variant="subtle" leftSection={<IconCalendarClock size={16} />} className="sr-header-ico" aria-label="任务" onClick={() => setOpen((o) => !o)}>
              {/* 窄屏（≤768，index.css .sr-hide-sm）只留图标，防 390px 顶栏溢出 */}
              <span className="sr-hide-sm">任务</span>
            </Button>
          </Indicator>
        </Tooltip>
      </Popover.Target>
      <Popover.Dropdown style={{ maxWidth: 'calc(100vw - 32px)' }}>
        {content}
      </Popover.Dropdown>
    </Popover>
  )
}
