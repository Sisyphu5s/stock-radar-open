import { ActionIcon, Button, Tooltip } from '@mantine/core'
import clsx from 'clsx'
import type { ReactNode } from 'react'

interface IconTextButtonProps {
  icon: ReactNode
  text: ReactNode
  tooltip?: ReactNode
  onClick?: () => void
  type?: 'primary' | 'default' | 'text' | 'link' | 'dashed'
  danger?: boolean
  loading?: boolean
  disabled?: boolean
  size?: 'small' | 'middle'
  className?: string
  /** 落到内部 Mantine Button/ActionIcon 根元素的类（className 落于包装 span）：
   *  调用方需样式化按钮本体（收缩/高亮）时使用，避免依赖 .mantine-* 内部类 */
  buttonClassName?: string
  /** 无文字场景的 a11y 标签（纯图标时必须） */
  ariaLabel?: string
}

/** type → Mantine Button/ActionIcon 共用 variant（dashed 无等价物，outline 最接近） */
const TYPE_VARIANT = {
  primary: 'filled',
  default: 'default',
  text: 'subtle',
  link: 'transparent',
  dashed: 'outline',
} as const

/** 纯图标触控目标（px）：P1-a11y 要求 ≥28px；Mantine ActionIcon 内置档
 *  xs=18/sm=22 均不达标，故用数值尺寸 28（small）/ 32（middle），落在 28-32 区间 */
const ICON_ONLY_SIZE = { small: 28, middle: 32 } as const

/**
 * 「图标 + 文字」按钮基板：文字恒显示（不再随容器宽度隐藏）。
 * 纯图标分支（text == null）渲染 Mantine ActionIcon 而非 Button——
 * 原 Button+leftSection 图标不居中且高宽 26×30 不规整（P1 浮窗按钮多余方框根因）；
 * 文字分支维持 Button 渲染不变。className 落于包装 span（既有调用依赖该结构），
 * tooltip 兜底。
 */
export default function IconTextButton({
  icon, text, tooltip, onClick, type = 'default', danger, loading, disabled, size = 'small', className, buttonClassName, ariaLabel,
}: IconTextButtonProps) {
  const cls = clsx(className)
  const btnCls = buttonClassName ? clsx(buttonClassName) : undefined
  const btn = text != null ? (
    <Button
      size={size === 'small' ? 'xs' : 'sm'}
      variant={TYPE_VARIANT[type]}
      color={danger ? 'red' : undefined}
      loading={loading}
      disabled={disabled}
      leftSection={icon}
      onClick={onClick}
      aria-label={ariaLabel}
      className={btnCls}
    >
      <span>{text}</span>
    </Button>
  ) : (
    <ActionIcon
      size={ICON_ONLY_SIZE[size]}
      variant={TYPE_VARIANT[type]}
      color={danger ? 'red' : undefined}
      loading={loading}
      disabled={disabled}
      onClick={onClick}
      aria-label={ariaLabel}
      className={btnCls}
    >
      {icon}
    </ActionIcon>
  )
  return cls ? (
    <span className={cls}>{tooltip ? <Tooltip label={tooltip}>{btn}</Tooltip> : btn}</span>
  ) : (
    tooltip ? <Tooltip label={tooltip}>{btn}</Tooltip> : btn
  )
}
