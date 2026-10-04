import { Badge, Button, Text } from '@mantine/core'
import { IconArrowBackUp, IconCircleCheck, IconClock, IconRocket } from '@tabler/icons-react'
import type { FactorVersion } from '../../../api/client'
import { fmtRatio, fmtStab } from '../../../utils/format'
import StatusTag, { PUBLISH_STATUS_META } from '../../../components/ui/StatusTag'

export interface PublishStep {
  key: string
  name: string
  desc: string
}

/** 三步发布顺序（与后端 PUBLISH_STEPS 对齐） */
export const PUBLISH_STEP_KEYS = ['oos_verified', 'stability_checked', 'complexity_checked']

function stepValue(v: FactorVersion | undefined, key: string): string {
  if (!v) return '—'
  if (key === 'oos_verified') return fmtRatio(Math.abs(Number(v.oos_ic ?? 0)))
  if (key === 'stability_checked') return fmtStab(v.stability)
  if (key === 'complexity_checked') return v.complexity == null ? '—' : String(v.complexity)
  return '—'
}

interface PublishStepsPanelProps {
  steps: PublishStep[]
  version?: FactorVersion | null
  onAdvance: (step: string) => void | Promise<void>
  onRevert?: (step: string) => void | Promise<void>
  advancing?: boolean
  /** 只读模式（移动端 / 参数过期）：隐藏执行与撤销按钮，仅展示状态 */
  readOnly?: boolean
}

/** 三步发布检查面板：扁平三格（非卡片嵌套），阈值说明来自后端 PUBLISH_STEPS（desc）+ 当前值（stepValue）
 *  组成「当前值 vs 阈值」达标清单，执行前可判定是否达标，不再盲执行。执行语义以后端为准。 */
export default function PublishStepsPanel({ steps, version, onAdvance, onRevert, advancing, readOnly }: PublishStepsPanelProps) {
  const v = version ?? undefined
  const published = v?.status === 'published'
  const rejected = v?.status === 'rejected'
  const displaySteps = steps.length
    ? steps
    : PUBLISH_STEP_KEYS.map((k) => ({ key: k, name: k, desc: '' }))

  return (
    <div className="sr-publish-steps">
      {displaySteps.map((s) => {
        const key = s.key
        const done = !!v?.[key as keyof FactorVersion]
        const next = !done && PUBLISH_STEP_KEYS.find((k) => !v?.[k as keyof FactorVersion]) === key
        const blocked = published || rejected || !v
        return (
          <div key={key} className={`sr-publish-step${done ? ' is-done' : ''}`}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
              <Text fw={600} style={{ fontSize: 'var(--sr-font-title)' }}>{s.name}</Text>
              {done ? (
                <Badge leftSection={<IconCircleCheck size={14} />} color="teal" variant="light" style={{ flexShrink: 0 }}>已达标</Badge>
              ) : next ? (
                <Badge leftSection={<IconRocket size={14} />} color="blue" variant="light" style={{ flexShrink: 0 }}>待执行</Badge>
              ) : (
                <Badge leftSection={<IconClock size={14} />} color="gray" variant="light" style={{ flexShrink: 0 }}>待处理</Badge>
              )}
            </div>
            <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)', display: 'block', marginTop: 4 }}>
              阈值: {s.desc || '—'}
            </Text>
            <div style={{ marginTop: 6, display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <Text style={{ fontSize: 'var(--sr-font-sm)', color: 'var(--sr-text-2)' }}>当前值</Text>
              <Text fw={600} style={{ fontSize: 'var(--sr-font-sm)' }}>{stepValue(v, key)}</Text>
              {!readOnly && !blocked && (
                <span style={{ marginLeft: 'auto', display: 'inline-flex', gap: 6 }}>
                  {done && onRevert && (
                    <Button size="xs" variant="default" leftSection={<IconArrowBackUp size={14} />} onClick={() => onRevert(key)}>撤销</Button>
                  )}
                  {next && (
                    <Button size="xs" variant="filled" leftSection={<IconRocket size={14} />} loading={advancing} onClick={() => onAdvance(key)}>
                      执行该步骤
                    </Button>
                  )}
                </span>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

/** 发布流程状态脚注：已发布 / 已拒绝 / 下一步提示（文案对齐后端实际语义，不虚构硬门槛） */
export function PublishFlowStatus({ version, steps }: { version?: FactorVersion | null; steps: PublishStep[] }) {
  const v = version
  if (!v) {
    return (
      <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
        暂无版本记录，创建因子将生成草稿 v1，按序完成三步检查后发布。
      </Text>
    )
  }
  if (v.status === 'published') {
    return (
      <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
        已完成全部三步检查，可在因子库与市场信号系统「已发布因子」中选用。
      </Text>
    )
  }
  if (v.status === 'rejected') {
    return (
      <Text c="red" style={{ fontSize: 'var(--sr-font-sm)' }}>
        {v.note || '该版本未通过检查，已终止发布流程。'}
      </Text>
    )
  }
  const nextKey = PUBLISH_STEP_KEYS.find((k) => !v[k as keyof FactorVersion])
  const s = steps.find((x) => x.key === nextKey)
  return (
    <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>
      下一步：{s?.name ?? nextKey}（{s?.desc ?? ''}）— 按序完成三步检查后自动发布。
    </Text>
  )
}

/** 版本发布状态 Tag（含 rejected 特判） */
export function VersionStatusTag({ status }: { status: string }) {
  if (status === 'rejected') return <Badge color="red" variant="light">已拒绝</Badge>
  return <StatusTag status={status} kind="publish" />
}

/** 步骤推进成功的状态文案（advanceVersion 返回的最新状态） */
export function stepResultLabel(status: string): string {
  return PUBLISH_STATUS_META[status]?.label ?? (status === 'rejected' ? '已拒绝' : status)
}
