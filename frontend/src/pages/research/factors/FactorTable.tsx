import { Badge, Button, Group, Text } from '@mantine/core'
import { IconChartLine, IconFlask } from '@tabler/icons-react'
import { useMemo } from 'react'
import type { SrColumn } from '../../../components/ui/tableTypes'
import type { FactorInfo, FactorVersion } from '../../../api/client'
import { fmtRatio, fmtStab } from '../../../utils/format'
import FormulaText from '../../../components/ui/FormulaText'
import { VersionStatusTag, PUBLISH_STEP_KEYS } from '../validation/PublishStepsPanel'

export const scoreOf = (v: FactorVersion | undefined): number => {
  if (!v) return 0
  const ic = Number(v.oos_ic ?? v.val_ic ?? v.train_ic ?? 0)
  const st = Number(v.stability ?? 0)
  return Math.abs(ic) * 100 * (0.5 + Math.min(Math.max(st, 0), 1))
}

export const latestVersion = (f: FactorInfo): FactorVersion | undefined =>
  [...f.versions].sort((a, b) => b.version - a.version)[0]

/** 列定义 useMemo 稳定引用：DataTable 已 memo，调用方 onEval/onBacktest 须 useCallback 稳定（FactorLibrary 已配合） */
export function useFactorColumns(onEval: (f: FactorInfo) => void, onBacktest: (f: FactorInfo) => void): SrColumn<FactorInfo>[] {
  return useMemo<SrColumn<FactorInfo>[]>(() => [
    {
      title: '名称', dataIndex: 'name', ellipsis: true, minWidth: 120,
      render: (v) => <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>{v}</Text>,
    },
    {
      // kind 字段已收编至 client.ts FactorInfo（expr/nn），nn 因子显示 NN 标签
      title: '类型', align: 'center', width: 56, minWidth: 56,
      render: (_, f) => {
        const kind = f.kind
        return kind === 'nn'
          ? <Badge variant="light" color="grape" style={{ marginInlineEnd: 0 }}>NN</Badge>
          : null
      },
    },
    {
      title: '表达式', dataIndex: 'expression', ellipsis: true, minWidth: 180,
      render: (_, f) => {
        const v = latestVersion(f)
        return <FormulaText tex={v?.latex} expr={f.expression} size="small" />
      },
    },
    { title: 'Train IC', dataIndex: 'train_ic', align: 'right', className: 'sr-num-col', minWidth: 70, render: (_, f) => fmtRatio(latestVersion(f)?.train_ic, 4) },
    { title: 'Val IC', dataIndex: 'val_ic', align: 'right', className: 'sr-num-col', minWidth: 70, render: (_, f) => fmtRatio(latestVersion(f)?.val_ic, 4) },
    { title: 'OOS IC', dataIndex: 'oos_ic', align: 'right', className: 'sr-num-col', minWidth: 70, render: (_, f) => fmtRatio(latestVersion(f)?.oos_ic, 4) },
    { title: '稳定性', dataIndex: 'stability', align: 'right', className: 'sr-num-col', minWidth: 70, render: (_, f) => fmtStab(latestVersion(f)?.stability) },
    {
      title: '库评分', align: 'right', className: 'sr-num-col', minWidth: 70,
      render: (_, f) => {
        const sc = scoreOf(latestVersion(f))
        return <Text fw={600} style={{ color: sc >= 3 ? 'var(--sr-up)' : 'var(--sr-text-1)' }}>{sc.toFixed(2)}</Text>
      },
    },
    {
      title: '状态', align: 'center',
      render: (_, f) => {
        const v = latestVersion(f)
        return v ? <VersionStatusTag status={v.status} /> : <Text span c="dimmed">—</Text>
      },
    },
    {
      title: '流程', align: 'center',
      render: (_, f) => {
        const v = latestVersion(f)
        const n = v ? PUBLISH_STEP_KEYS.filter((k) => v[k as keyof FactorVersion]).length : 0
        return <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>{v ? `${n}/3` : '—'}</Text>
      },
    },
    {
      title: '操作', align: 'center',
      render: (_, f) => (
        <Group gap={4} onClick={(e) => e.stopPropagation()}>
          <Button size="xs" leftSection={<IconFlask size={14} />} onClick={() => onEval(f)}>评估</Button>
          <Button size="xs" leftSection={<IconChartLine size={14} />} onClick={() => onBacktest(f)}>回测</Button>
        </Group>
      ),
    },
  ], [onEval, onBacktest])
}
