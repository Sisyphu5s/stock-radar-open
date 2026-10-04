import { Autocomplete } from '@mantine/core'
import { useNavigate } from 'react-router-dom'
import { useState } from 'react'
import { useStockSearch } from '../hooks/useStockSearch'

export default function StockSearch() {
  const navigate = useNavigate()
  const [value, setValue] = useState('')
  const { options, onQuery, onSelect } = useStockSearch()

  return (
    <Autocomplete
      placeholder="搜索股票代码 / 名称，选中跳转工作台"
      className="sr-ctl-h"
      aria-label="搜索股票"
      // 宽度放宽至可读下限 120px（移动端 36vw），与顶栏标题放宽（layout.css 40vw）配套，避免双窄互挤
      style={{ width: 'clamp(120px, 36vw, 260px)' }}
      clearable
      // 搜索由 useStockSearch 防抖驱动，options 已是过滤结果：禁用内置过滤避免把服务端结果滤掉
      filter={(input) => input.options}
      value={value}
      data={options}
      onOptionSubmit={(code) => {
        setValue('')
        onSelect(code)
        navigate(`/stocks/${encodeURIComponent(code)}`)
      }}
      onChange={(q) => {
        setValue(q)
        onQuery(q)
      }}
    />
  )
}
