import { Badge, Button, Flex, Text } from '@mantine/core'
import { IconDeviceFloppy, IconGlobe } from '@tabler/icons-react'
import CardState from '../../../components/ui/CardState'

interface GlobalParamBarProps {
  params: Record<string, string>
  loading: boolean
  /** 是否可保存（存在最优调优结果） */
  canSave: boolean
  onSave: () => void
  onApply: () => void
  applying: boolean
}

/**
 * 全局参数栏（T-43：自 FactorEvaluation 调优 Modal 拆出）：
 * 全局参数 Badge 列表 / 空态 + 「保存为全局参数」「应用全局参数」按钮。
 * 展示与操作解耦：数据（params/loading）与动作（onSave/onApply）由 TuneModal 提供。
 */
export default function GlobalParamBar({ params, loading, canSave, onSave, onApply, applying }: GlobalParamBarProps) {
  return (
    <CardState loading={loading} loadingText="加载中…" minHeight="auto" style={{ marginBottom: 'var(--sr-pad-xl)' }}>
      <Flex wrap="wrap" align="center" gap="var(--sr-pad-md)">
        <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>全局参数</Text>
        {Object.keys(params).length === 0 ? (
          <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>暂无全局参数</Text>
        ) : (
          Object.entries(params).map(([k, v]) => (
            <Badge key={k} color="blue" variant="light" style={{ fontFamily: 'monospace', fontSize: 'var(--sr-font-xs)' }}>
              {k}={v}
            </Badge>
          ))
        )}
        <span style={{ marginLeft: 'auto' }} />
        <Button size="xs" variant="default" leftSection={<IconDeviceFloppy size={14} />} disabled={!canSave} onClick={onSave}>
          保存为全局参数
        </Button>
        <Button size="xs" variant="default" leftSection={<IconGlobe size={14} />} loading={applying} onClick={onApply}>
          应用全局参数
        </Button>
      </Flex>
    </CardState>
  )
}
