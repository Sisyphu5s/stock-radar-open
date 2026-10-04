import LatexFormula from '../LatexFormula'
import FormulaCode from '../FormulaCode'

interface FormulaTextProps {
  /** 优先渲染 LaTeX；缺省回退 FormulaCode 分词着色 */
  tex?: string
  expr?: string
  block?: boolean
  size?: 'small' | 'middle'
}

/** 公式展示统一出口：LaTeX(KaTeX) 优先，失败/无 tex 回退 FormulaCode */
export default function FormulaText({ tex, expr, block, size = 'middle' }: FormulaTextProps) {
  if (tex) {
    return <LatexFormula tex={tex} block={block} size={size === 'small' ? 12 : undefined} />
  }
  if (expr) {
    return <FormulaCode expr={expr} />
  }
  return null
}
