import clsx from 'clsx'
import { Text } from '@mantine/core'
import type { CSSProperties, ReactNode } from 'react'
import './settings.css'

export interface SettingBlockProps {
  /** 区块标题（必填） */
  title: ReactNode
  /** 标题前图标（可选） */
  icon?: ReactNode
  /** 标题下说明文字（可选） */
  desc?: ReactNode
  children: ReactNode
  className?: string
  style?: CSSProperties
}

/**
 * 设置卡片组区块（UI-TEMPLATES.md T4 的 CardGroup 承载件）。
 *
 * 结构：section.sr-set-section > div.sr-set-block > 标题行 + 说明 + 内容区，
 * 对应 `PageShell > PageHeader + CardGroup + FormRow` 中 CardGroup 的一个卡片单元。
 *
 * 消费方：设置域四页 —— SystemSettings / DataConnections / LlmCopilot / SystemStatus
 * （M3 批 F 将各页私有 `sr-set-*` 结构收编至此）。
 *
 * 样式类 .sr-set-section / .sr-set-block / .sr-set-block-title 定义于
 * 本组件同级 settings.css(T-44 由 pages/settings/settings.css 收敛,模板自带样式)。
 */
export function SettingBlock({ title, icon, desc, children, className, style }: SettingBlockProps) {
  return (
    <section className={clsx('sr-set-section', className)} style={style}>
      <div className="sr-set-block">
        <div className="sr-set-block-title">
          {icon != null && <span style={{ marginRight: 'var(--sr-pad-xs)' }}>{icon}</span>}
          {title}
        </div>
        {desc != null && (
          <Text c="dimmed" style={{ fontSize: 'var(--sr-font-xs)' }}>
            {desc}
          </Text>
        )}
        <div style={{ marginTop: 'var(--sr-gap-row)' }}>{children}</div>
      </div>
    </section>
  )
}
