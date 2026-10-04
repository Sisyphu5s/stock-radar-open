import { Button, Drawer, Flex, Group, NumberInput, Progress, Select, Stack, Text, Textarea, TextInput } from '@mantine/core'
import { DateInput } from '@mantine/dates'
import { useRef, useState } from 'react'
import { createDataset } from '../../../api/client'
import type { DatasetBuildPayload, JobDetail } from '../../../api/client'
import { invalidateDatasets } from '../../../data/jobs'
import { useJobFlow } from '../../../hooks/useJobFlow'
import { saveLastDataset } from '../../../utils/useLastDataset'
import { cnTodayOf } from '../../../utils/time'
import { toast } from '../shared/toast'
import './datasets.css'

export interface UniverseOption {
  key: string
  name: string
}

interface BuildDrawerProps {
  open: boolean
  universes: UniverseOption[]
  onClose: () => void
  /** 构建成功（返回新数据集 id） */
  onBuilt?: (dsId: number) => void
}

/** 数据集构建抽屉：名称 / universe / 日期范围 / limit / 自定义代码（universe=custom 时）。
 *  提交走 createDataset 专用接口返回 job_id → useJobFlow 共享任务池轮询，单次提交不重复建任务。
 *  日期范围用 @mantine/dates DateInput 字符串模式（valueType=string，直接出 YYYY-MM-DD 串，提交仍 naive 串）；
 *  默认起止日期走 cnTodayOf（上海日历日，UTC+8），负时区用户不差一天。 */
export default function BuildDrawer({ open, universes, onClose, onBuilt }: BuildDrawerProps) {
  const [name, setName] = useState('')
  const [universe, setUniverse] = useState<string>()
  const [startDate, setStartDate] = useState(() => cnTodayOf(Date.now() - 365 * 5 * 86400000))
  const [endDate, setEndDate] = useState(() => cnTodayOf())
  const [limit, setLimit] = useState<number>(300)
  const [codes, setCodes] = useState('')
  const initRef = useRef(false)

  // universe 列表异步到达后补默认值（仅首次）
  if (!initRef.current && universes.length && !universe) {
    initRef.current = true
    setUniverse(universes[0].key)
  }

  // 构建任务 → useJob 共享轮询（替代 waitForJob 页面局部轮询）：结果从 job.result 读取
  const { job, polling: building, submit } = useJobFlow<JobDetail>({
    submit: async () => {
      // 单次提交：createDataset 已创建 dataset_build 任务并返回 job_id，
      // 等待其返回的 job_id 即可（不再二次 POST 重复建任务）
      const uni = universe || universes[0]?.key
      const d = await createDataset({
        name: name.trim() || `数据集 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })}`,
        universe: uni,
        start_date: startDate,
        end_date: endDate,
        limit,
        ...(uni === 'custom' ? { custom_codes: codes.split(/[,，\s]+/).filter(Boolean) } : {}),
      })
      toast.success(`数据集构建任务 #${d.job_id} 已提交，完成后自动刷新列表`)
      return { job_id: d.job_id }
    },
    makeOptimistic: (id) => ({ id, status: 'pending', progress: 0, result: null, job_type: 'dataset_build' }),
    onDone: (j) => {
      const res = (j.result ?? {}) as DatasetBuildPayload
      const info = res?.dataset ?? {}
      toast.success(`数据集构建完成: ${info.stock_count ?? '?'} 只股票 / ${info.row_count ?? '?'} 行`)
      if (info.id != null) saveLastDataset(info.id)
      invalidateDatasets()
      setName('')
      setCodes('')
      onClose()
      if (info.id != null) onBuilt?.(info.id)
    },
    onFailed: (j) => { toast.error('数据集构建失败: ' + (j.error ?? '构建失败')) },
  })

  const close = () => {
    if (building) return
    onClose()
  }

  const submitBuild = async () => {
    const uni = universe || universes[0]?.key
    if (!uni) { toast.warning('universe 配置加载中，请稍候重试'); return }
    if (uni === 'custom' && !codes.trim()) { toast.warning('请输入股票代码列表'); return }
    if (!startDate || !endDate) { toast.warning('请选择日期范围'); return }
    if (startDate > endDate) { toast.warning('开始日期不能晚于结束日期'); return }
    try {
      await submit()
    } catch (e: any) {
      toast.error('数据集构建失败: ' + (e?.response?.data?.detail ?? e?.message ?? e))
    }
  }

  const uniOptions = universes.map((u) => ({ value: u.key, label: `${u.name} (${u.key})` }))

  return (
    <Drawer
      title="构建数据集"
      opened={open}
      onClose={close}
      size="min(560px, 94vw)"
      position="right"
    >
      <Stack gap={12}>
        <div className="sr-ds-build-row">
          <span className="sr-ds-build-label">名称</span>
          <TextInput style={{ flex: 1, minWidth: 200 }} placeholder="数据集名称（留空自动命名）"
            value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div className="sr-ds-build-row">
          <span className="sr-ds-build-label">股票池</span>
          <Select style={{ flex: 1, minWidth: 200 }} value={universe} onChange={(v) => setUniverse(v ?? undefined)}
            data={uniOptions} placeholder="选择 universe" />
        </div>
        <div className="sr-ds-build-row">
          <span className="sr-ds-build-label">日期范围</span>
          <Flex gap="var(--sr-pad-xs)" style={{ flex: 1, minWidth: 0 }}>
            <DateInput
              valueFormat="YYYY-MM-DD"
              style={{ flex: '1 1 150px', width: '100%', minWidth: 0 }}
              value={startDate}
              onChange={(v) => setStartDate(v ?? '')}
              aria-label="开始日期"
            />
            <span style={{ color: 'var(--sr-text-3)' }}>~</span>
            <DateInput
              valueFormat="YYYY-MM-DD"
              style={{ flex: '1 1 150px', width: '100%', minWidth: 0 }}
              value={endDate}
              onChange={(v) => setEndDate(v ?? '')}
              aria-label="结束日期"
            />
          </Flex>
        </div>
        <div className="sr-ds-build-row">
          <span className="sr-ds-build-label">股票上限</span>
          <NumberInput min={0} max={5000} value={limit} onChange={(v) => setLimit(typeof v === 'number' ? v : 300)} />
          <Text span c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>只（hs300 成分全量时 0 = 不截断）</Text>
        </div>
        {universe === 'custom' && (
          <div style={{ width: '100%' }}>
            <div className="sr-ds-build-label" style={{ marginBottom: 'var(--sr-pad-sm)' }}>股票代码</div>
            <Textarea rows={3} placeholder="股票代码列表，逗号分隔，如: 600519,000001,300750"
              value={codes} onChange={(e) => setCodes(e.target.value)} />
          </div>
        )}
        {building && <Progress value={Math.round(job?.progress ?? 0)} size="xs" striped animated />}
        <Group justify="flex-end" gap="var(--sr-pad-md)" style={{ marginTop: 'var(--sr-pad-lg)' }}>
          <Button variant="default" onClick={close} disabled={building}>取消</Button>
          <Button variant="filled" loading={building} onClick={submitBuild}>构建</Button>
        </Group>
      </Stack>
    </Drawer>
  )
}
