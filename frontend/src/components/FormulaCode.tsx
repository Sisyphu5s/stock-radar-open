import { useMemo } from 'react'
import type { CSSProperties } from 'react'

interface FormulaToken {
  text: string
  cls: 'sr-formula-op' | 'sr-formula-feat' | 'sr-formula-num' | 'sr-formula-plain'
}

/** 按正则分词：函数/算子（字母+下划线+左括号）→ op；特征列 → feat；数字 → num；其余 → plain */
function tokenize(expr: string): FormulaToken[] {
  const out: FormulaToken[] = []
  const re = /([A-Za-z_][A-Za-z0-9_]*)(?=\s*\()|(open|high|low|close|volume|amount)|\b(\d+(?:\.\d+)?)\b|([()])|([^A-Za-z0-9_()]+)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(expr)) !== null) {
    if (m[1] !== undefined) out.push({ text: m[1], cls: 'sr-formula-op' })
    else if (m[2] !== undefined) out.push({ text: m[2], cls: 'sr-formula-feat' })
    else if (m[3] !== undefined) out.push({ text: m[3], cls: 'sr-formula-num' })
    else out.push({ text: m[0], cls: 'sr-formula-plain' })
  }
  return out
}

/** 因子公式行内动态着色（函数蓝 / 特征紫 / 数字橙） */
export default function FormulaCode({ expr, style }: { expr: string; style?: CSSProperties }) {
  const tokens = useMemo(() => tokenize(expr), [expr])
  return (
    <span className="sr-formula-code" style={style}>
      {tokens.map((t, i) => <span key={i} className={t.cls}>{t.text}</span>)}
    </span>
  )
}
