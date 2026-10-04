import { useEffect, useRef, useState } from 'react'

/**
 * 研究页「表达式 LaTeX 实时预览（防抖 + 竞态守卫）」统一实现
 * （替代原 FactorEvaluation/Backtest 各自的 400ms 防抖 + cancelled 拷贝）：
 *
 * - 空表达式立即清空预览（不发起请求）
 * - 防抖 debounceMs（默认 400）后调用 render；迟到的旧请求结果丢弃（cancelled 竞态守卫）
 * - render 返回 null 或抛错 → 预览置 null（页面文本回退）
 *
 * 用法：
 *   const previewLatex = useLatexPreview(expression, async (expr) => (await toLatex(expr)).latex)
 */
export function useLatexPreview(
  expression: string,
  render: (expr: string) => Promise<string | null>,
  debounceMs = 400,
): string | null {
  const [previewLatex, setPreviewLatex] = useState<string | null>(null)
  const renderRef = useRef(render)
  renderRef.current = render
  useEffect(() => {
    if (!expression.trim()) { setPreviewLatex(null); return }
    let cancelled = false
    const timer = setTimeout(() => {
      renderRef.current(expression.trim()).then((latex) => {
        if (!cancelled) setPreviewLatex(latex)
      }).catch(() => { if (!cancelled) setPreviewLatex(null) })
    }, debounceMs)
    return () => { cancelled = true; clearTimeout(timer) }
  }, [expression, debounceMs])
  return previewLatex
}
