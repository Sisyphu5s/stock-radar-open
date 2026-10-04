import { Alert, Button, Flex, Tabs } from '@mantine/core'
import type { ReactNode } from 'react'
import { useEffect, useState } from 'react'

export interface ResultsTabItem {
  key: string
  label: ReactNode
  children: ReactNode
}

interface ResultsTabsProps {
  /** 页签定义（key/label/children）；惰性渲染（首次激活才挂载，之后保持挂载） */
  items: ResultsTabItem[]
  /** 受控页签（可选）；缺省走内部 defaultActiveKey */
  activeKey?: string
  defaultActiveKey?: string
  onChange?: (key: string) => void
  /** 激活页签变化回调（父组件图表 effect 依赖 activeKey，保证首访容器有尺寸时 init 图表） */
  onActiveChange?: (key: string) => void
  /** 参数已变更 stale 提示（Alert 位于页签上方；不传则不渲染） */
  stale?: boolean
  staleMessage?: string
  onRerun?: () => void
}

/**
 * 结果页统一页签壳：指标/图表/风险/分层 Tabs（sr-results-tabs + size sm）。
 * 保持 antd「惰性渲染 + 访问过不销毁」语义（visited set + display:none，切回不丢图表实例）：
 * - 未访问页签不挂载：隐藏 tab 预渲染会在 0 尺寸容器内 init 图表（宽度 0 白屏）；
 * - 访问后保持挂载：切回不销毁（destroyInactiveTabPane=false 语义）。
 * 两结果页（EvaluationResults/BacktestResults）共用，行为零差异。
 */
export default function ResultsTabs(p: ResultsTabsProps) {
  const { items, activeKey, defaultActiveKey = 'metrics', onChange, stale, staleMessage, onRerun, onActiveChange } = p
  const initialKey = activeKey ?? defaultActiveKey
  const [innerKey, setInnerKey] = useState<string>(initialKey)
  const [visited, setVisited] = useState<Set<string>>(() => new Set([initialKey]))
  const key = activeKey ?? innerKey

  // 激活页签首次出现 → 标记已访问（触发面板挂载）
  useEffect(() => {
    if (!visited.has(key)) setVisited((prev) => new Set(prev).add(key))
  }, [key, visited])
  // 激活变化上报（父组件图表 effect 依赖，切到图表 tab 时 ref 已挂载可 init）
  useEffect(() => {
    onActiveChange?.(key)
  }, [key, onActiveChange])

  // B4:Mantine 9 受控 Tabs 无回退——value 不在 items 中时所有 panel 失活(全部隐藏),
  // 不像 antd/Mantine 旧版自动回退首项。调用方必须保证 activeKey 恒在 items 内;
  // 以下仅 dev 告警辅助排查,不改运行时语义(受控态失效时 alert 定位调用方)。
  const itemKeys = items.map((i) => i.key).join(',')
  useEffect(() => {
    if (activeKey != null && !items.some((i) => i.key === activeKey)) {
      console.warn(
        `[ResultsTabs] activeKey=${JSON.stringify(activeKey)} 不在 items(${itemKeys}) 中，` +
        'Mantine 9 受控 Tabs 无回退，全部 panel 将失活；请调用方保证 activeKey 与 items 同步',
      )
    }
  }, [activeKey, itemKeys, items])

  const go = (k: string | null) => {
    const key = k ?? defaultActiveKey
    if (activeKey == null) setInnerKey(key)
    onChange?.(key)
  }

  return (
    <>
      {stale && (
        <Alert
          color="yellow"
          title={(
            <Flex justify="space-between" align="center" gap="var(--sr-pad-md)">
              <span>{staleMessage ?? '参数已变更，当前结果基于旧参数'}</span>
              {onRerun && <Button size="xs" variant="light" onClick={onRerun}>重新运行</Button>}
            </Flex>
          )}
        />
      )}
      <div className="sr-run-panel">
        <Tabs value={key} onChange={go} className="sr-results-tabs" classNames={{ list: 'sr-results-tabs-list', tab: 'sr-results-tabs-tab' }}>
          <Tabs.List>
            {items.map((i) => (
              <Tabs.Tab key={i.key} value={i.key}>{i.label}</Tabs.Tab>
            ))}
          </Tabs.List>
        </Tabs>
        <div className="sr-results-panels">
          {items.filter((i) => visited.has(i.key)).map((i) => (
            <div key={i.key} className="sr-results-tabpane" style={{ display: key === i.key ? undefined : 'none' }}>
              {i.children}
            </div>
          ))}
        </div>
      </div>
    </>
  )
}
