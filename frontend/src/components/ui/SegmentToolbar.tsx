import { Flex, SegmentedControl } from '@mantine/core'
import type { ReactNode } from 'react'

/** 分段选项（value 为内部标识，label 为展示内容） */
export interface SegmentOption {
  value: string
  label: ReactNode
}

interface SegmentToolbarProps {
  value: string
  onChange: (value: string) => void
  options: SegmentOption[]
  /** 左侧附加操作区（如新建按钮），置于切换条右端 */
  extra?: ReactNode
  /** 单条模式：切换条在顶部独立成行（fill 页）；默认与 extra 同行 */
  block?: boolean
}

/**
 * 页面级视图切换条（薄壳）：Mantine SegmentedControl 的布局薄壳。
 * 承载「同一路由下多视图互斥切换」的布局行为（如模拟盘 项目/账户），
 * 事务语义（选中态/键盘）由 SegmentedControl 原生承担；间距走 --sr-gap-* 令牌。
 */
export default function SegmentToolbar({ value, onChange, options, extra, block = false }: SegmentToolbarProps) {
  const control = (
    <SegmentedControl
      size="xs"
      value={value}
      onChange={onChange}
      data={options.map((o) => ({ value: o.value, label: o.label }))}
    />
  )
  if (block) {
    return (
      <Flex direction="column" gap="var(--sr-gap-ctl)" style={{ minWidth: 0 }}>
        {control}
        {extra}
      </Flex>
    )
  }
  return (
    <Flex justify="space-between" align="center" gap="var(--sr-gap-ctl)" wrap="wrap" style={{ minWidth: 0 }}>
      {control}
      {extra}
    </Flex>
  )
}
