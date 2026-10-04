import { Button, Popover, Radio, Text } from '@mantine/core'
import { useState } from 'react'
import { useWorkbench } from '../../context'

/** 头部「清除全部调优」入口：Popover 内三档清除范围（manual/global/all） */
export function ClearAllTuneButton() {
  const c = useWorkbench()
  const { tunedInds, clearTune } = c
  const [open, setOpen] = useState(false)
  const [scope, setScope] = useState<'manual' | 'global' | 'all'>('manual')
  const [clearing, setClearing] = useState(false)
  const n = tunedInds.length
  return (
    <Popover
      opened={open}
      onChange={(o) => {
        // 清除进行中禁止重开
        if (o && clearing) return
        setOpen(o)
      }}
      position="bottom-end"
      width="min(320px, calc(100vw - 64px))"
      shadow="md"
    >
      <Popover.Target>
        <Button size="xs" variant="subtle" color="red" disabled={n === 0} aria-label="清除全部调优" onClick={() => setOpen(true)}>
          清除全部调优
        </Button>
      </Popover.Target>
      <Popover.Dropdown>
        <div className="sr-tune-clear-body">
          <Radio.Group
            value={scope}
            onChange={(v) => setScope(v as 'manual' | 'global' | 'all')}
            aria-label="清除范围"
          >
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <Radio value="manual" label="仅本图表手动覆盖" />
              <Radio value="global" label="仅全局默认" />
              <Radio value="all" label="全部" />
            </div>
          </Radio.Group>
          <div className="sr-tune-clear-desc">
            {scope === 'manual' && <Text span size="xs" style={{ color: 'var(--sr-text-3)' }}>将清除 {n} 个指标的手动覆盖（仅本股票）</Text>}
            {scope === 'global' && <Text span size="xs" style={{ color: 'var(--sr-text-3)' }}>将重置 {n} 个指标的全局默认</Text>}
            {scope === 'all' && <Text span size="xs" style={{ color: 'var(--sr-text-3)' }}>将清除 {n} 个指标的手动覆盖并重置 {n} 个指标的全局默认</Text>}
            {scope !== 'manual' && (
              <Text span size="xs" style={{ color: 'var(--sr-warning)' }}>影响所有股票</Text>
            )}
          </div>
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 8 }}>
            <Button size="xs" variant="default" disabled={clearing} onClick={() => setOpen(false)}>取消</Button>
            <Button
              size="xs"
              variant="filled"
              color="red"
              loading={clearing}
              onClick={() => {
                setClearing(true)
                clearTune(scope).finally(() => {
                  setClearing(false)
                  setOpen(false)
                })
              }}
            >
              确认清除
            </Button>
          </div>
        </div>
      </Popover.Dropdown>
    </Popover>
  )
}
