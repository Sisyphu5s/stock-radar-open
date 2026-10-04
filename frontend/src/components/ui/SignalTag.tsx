import { Badge } from '@mantine/core'
import { antdToMantine } from '../../utils/antdColorCompat'

/**
 * antd Tag 色名 → Mantine 色名(薄壳适配层)。
 * 色值事实源仍是 tokens.ts 的 signalColor/signalLabelMap 出口(antd 色名,
 * 页面直连 antd Tag 的场景仍消费),SignalTag 内部翻译为 Mantine Badge 可识别色名;
 * 翻译表单一事实源为 utils/antdColorCompat.ts(P1-45 收敛),此处不存本地表。
 */

interface SignalTagProps {
  code: string
  /** 标签映射(signalLabelMap 结果);缺省直接显示 code */
  labelOf?: (code: string) => { text: string; color: string }
  size?: 'small' | 'middle'
}

/** 信号标签(Mantine Badge 薄壳):彩色浅底 + 圆点,统一信号展示(信号中心/雷达/关注页) */
export default function SignalTag({ code, labelOf, size = 'middle' }: SignalTagProps) {
  const cfg = labelOf ? labelOf(code) : { text: code, color: 'default' }
  const color = antdToMantine(cfg.color)
  return (
    <Badge
      variant="light"
      color={color}
      radius="var(--sr-radius-tag)"
      size={size === 'small' ? 'sm' : 'md'}
      leftSection={<span aria-hidden style={{ width: 6, height: 6, borderRadius: '50%', background: 'currentColor' }} />}
      style={{ fontWeight: 500, fontSize: size === 'small' ? 'var(--sr-font-meta)' : 'var(--sr-font-sm)' }}
    >
      {cfg.text}
    </Badge>
  )
}
