import { useCallback, useEffect, useRef, useState } from 'react'
import { searchStocks } from '../api/client'

/**
 * 股票防抖搜索唯一实现（全项目统一走此 hook，禁止另写 300ms 防抖 + seq 竞态守卫）：
 * - onQuery：300ms 防抖 + seq 序号竞态守卫（迟到的旧请求结果丢弃）+ 空串立即清空
 * - onSelect：选中后立即取消在途搜索 / 置空 options（防选中后的 onChange 触发残留搜索）
 * - clear：手动清空（关闭弹层 / 重置表单等场景）
 * - 卸载时清理 timer；搜索失败静默（下拉无结果，不弹错误）
 */
export function useStockSearch(): {
  options: { value: string; label: string }[]
  onQuery: (q: string) => void
  onSelect: (code: string, label?: string) => void
  clear: () => void
} {
  const [options, setOptions] = useState<{ value: string; label: string }[]>([])
  const timerRef = useRef<number | null>(null)
  const seqRef = useRef(0)

  const clear = useCallback(() => {
    if (timerRef.current != null) {
      window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
    seqRef.current++
    setOptions([])
  }, [])

  const onQuery = useCallback((q: string) => {
    if (timerRef.current != null) {
      window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
    const t = q.trim()
    if (!t) {
      seqRef.current++
      setOptions([])
      return
    }
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      const seq = ++seqRef.current
      searchStocks(t)
        .then((list) => {
          if (seq !== seqRef.current) return
          setOptions(list.map((s) => ({ value: s.code, label: `${s.name}（${s.code}）` })))
        })
        .catch(() => { /* 搜索失败静默 */ })
    }, 300)
  }, [])

  const onSelect = useCallback((code: string) => {
    // 选中后立即取消在途搜索并置空结果（label 参数为 API 对齐预留，调用方自行决定展示）
    if (timerRef.current != null) {
      window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
    seqRef.current++
    setOptions([])
    void code
  }, [])

  useEffect(() => () => {
    if (timerRef.current != null) window.clearTimeout(timerRef.current)
  }, [])

  return { options, onQuery, onSelect, clear }
}
