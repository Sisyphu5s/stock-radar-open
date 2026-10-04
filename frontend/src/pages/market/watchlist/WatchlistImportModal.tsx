import { Button, FileInput, Group, Modal, Textarea } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconUpload } from '@tabler/icons-react'
import { useState } from 'react'
import { importWatchlistCodes, parseCodesInput } from '../../../api/watchlistGroups'
import { errMsg } from '../../../utils/format'

interface WatchlistImportModalProps {
  opened: boolean
  onClose: () => void
  /** 导入成功后回调（页面失效 watchlist 池并 toast）；T-131:added/invalid 分开消费,
   *  全非法输入不再误报「代码均已关注」 */
  onImported: (added: number, invalid: number) => void
}

/**
 * 批量导入 Modal（T-12）：Textarea 粘贴代码（逗号/空格/换行分隔，支持 600519 / 600519.SH）
 * 或上传 .csv 文件（读取文本与粘贴内容合并）；导入为「关注」语义（幂等，无效代码忽略）。
 */
export default function WatchlistImportModal({ opened, onClose, onImported }: WatchlistImportModalProps) {
  const [text, setText] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [importing, setImporting] = useState(false)

  const doImport = async () => {
    let merged = text
    if (file) {
      try {
        const fc = await file.text()
        merged = merged ? `${merged}\n${fc}` : fc
      } catch {
        notifications.show({ color: 'red', message: '读取 CSV 文件失败' })
        return
      }
    }
    const codes = parseCodesInput(merged)
    if (!codes.length) {
      notifications.show({ color: 'yellow', message: '未识别到任何股票代码' })
      return
    }
    setImporting(true)
    try {
      const r = await importWatchlistCodes(codes)
      setText('')
      setFile(null)
      onImported(r.added, r.invalid)
      onClose()
    } catch (e: any) {
      notifications.show({ color: 'red', message: '导入失败: ' + errMsg(e) })
    } finally {
      setImporting(false)
    }
  }

  return (
    <Modal opened={opened} onClose={onClose} title="批量导入关注" size="md" centered>
      <Textarea
        label="粘贴股票代码"
        description="支持 600519 / 600519.SH 格式，逗号、空格、换行分隔"
        placeholder={'600519, 000001.SH\n300750\n601318'}
        autosize minRows={4} maxRows={8}
        value={text}
        onChange={(e) => setText(e.currentTarget.value)}
        mb="sm"
      />
      <FileInput
        label="或上传 CSV 文件"
        accept=".csv,text/csv"
        placeholder="选择 .csv 文件（读取其中代码与上方内容合并导入）"
        value={file}
        onChange={setFile}
        mb="md"
      />
      <Group justify="flex-end" gap={8}>
        <Button size="compact-sm" variant="default" onClick={onClose}>取消</Button>
        <Button size="compact-sm" leftSection={<IconUpload size={14} />} loading={importing} onClick={() => void doImport()}>
          导入关注
        </Button>
      </Group>
    </Modal>
  )
}
