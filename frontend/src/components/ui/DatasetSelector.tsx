import { Select } from '@mantine/core'
import { saveLastDataset } from '../../utils/useLastDataset'

export interface DatasetOption {
  id: number
  name?: string
}

interface DatasetSelectorProps {
  datasets: DatasetOption[]
  value?: number
  onChange?: (id: number) => void
  width?: number | string
  placeholder?: string
  /** 数据集加载中（展示 spinner，禁止选择） */
  loading?: boolean
  /** 数据集为空时禁用选择 */
  disabled?: boolean
}

/** 数据集选择器：研究区四页共用（选择即持久化 sr-last-dataset，下次自动恢复） */
export default function DatasetSelector({ datasets, value, onChange, width = 'clamp(160px, 22vw, 280px)', placeholder = '选择数据集', loading = false, disabled = false }: DatasetSelectorProps) {
  return (
    <Select
      className="sr-ctl-h"
      style={{ width }}
      value={value != null ? String(value) : null}
      placeholder={placeholder}
      disabled={disabled || loading}
      data={datasets.map((d) => ({ value: String(d.id), label: d.name ?? `数据集 #${d.id}` }))}
      styles={{ input: { height: 'var(--sr-ctl-h)', minHeight: 'var(--sr-ctl-h)' } }}
      onChange={(v) => {
        // 取消选择（v=null）不触发 onChange/saveLastDataset，与 antd 单选原行为一致
        if (v != null) {
          onChange?.(Number(v))
          saveLastDataset(Number(v))
        }
      }}
    />
  )
}
