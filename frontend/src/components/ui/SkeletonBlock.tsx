import { Skeleton } from '@mantine/core'

type SkeletonVariant = 'list' | 'card' | 'text'

interface SkeletonBlockProps {
  /** 骨架形态：list=标题+多行段落（列表页首载） / card=段落（卡片内容） / text=纯段落 */
  variant?: SkeletonVariant
  /** list/text 形态的段落行数 */
  rows?: number
}

/**
 * 加载骨架薄壳（Mantine Skeleton 统一封装）：
 * 替换页面/区块首载的裸 Spin，列表与卡片形态与区块宽度自适应，animate 脉动动效默认开启。
 * list=标题(40%宽高16)+rows 段段落；card/text=rows 段段落(全宽高12)，行间距 8px。
 * 后续页面接入：`{loading ? <SkeletonBlock rows={4} /> : <内容/>}`。
 */
export default function SkeletonBlock({ variant = 'list', rows = 3 }: SkeletonBlockProps) {
  const lines = Array.from({ length: rows }, (_, i) => <Skeleton key={i} height={12} radius="sm" />)
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {variant === 'list' && <Skeleton height={16} width="40%" radius="sm" />}
      {lines}
    </div>
  )
}
