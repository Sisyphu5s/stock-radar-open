import { Code } from '@mantine/core'
import { useMemo } from 'react'
import katex from 'katex'
import 'katex/dist/katex.min.css'

interface Props {
  /** LaTeX 字符串 */
  tex?: string | null
  /** 块级展示（居中大号） */
  block?: boolean
  /** 渲染失败时是否回退显示原始文本 */
  fallback?: boolean
  style?: React.CSSProperties
  /** 行内字号 */
  size?: number
}

/** 因子公式 LaTeX 渲染（KaTeX）：解析失败时回退为代码文本。 */
export default function LatexFormula({ tex, block, fallback = true, style, size }: Props) {
  const html = useMemo(() => {
    if (!tex) return null
    try {
      return katex.renderToString(tex, {
        throwOnError: true,
        displayMode: !!block,
        strict: false,
        output: 'html',
      })
    } catch {
      return null
    }
  }, [tex, block])

  if (html === null) {
    if (!fallback) return null
    return (
      <Code
        style={{
          fontSize: block ? 15 : 12.5,
          padding: block ? '6px 14px' : '2px 8px',
          borderRadius: 8,
          background: 'var(--sr-hover-bg)',
          fontFamily: "'SF Mono', 'Menlo', 'Monaco', 'Courier New', monospace",
          letterSpacing: 0.2,
          ...style,
        }}
      >
        {tex}
      </Code>
    )
  }

  if (block) {
    return (
      <div
        style={{
          textAlign: 'center',
          padding: '20px 16px',
          borderRadius: 6,
          background: 'var(--sr-block-bg)',
          border: '1px solid var(--sr-border)',
          overflowX: 'auto',
          ...style,
        }}
        dangerouslySetInnerHTML={{ __html: html }}
      />
    )
  }

  return (
    <span
      style={{
        fontSize: size ?? 14,
        lineHeight: 1.6,
        verticalAlign: 'middle',
        whiteSpace: 'nowrap',
        letterSpacing: 0.1,
        ...style,
      }}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}
