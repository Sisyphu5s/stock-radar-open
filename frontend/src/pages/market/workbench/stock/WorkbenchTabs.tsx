import { ActionIcon, Tooltip } from '@mantine/core'
import { IconX } from '@tabler/icons-react'
import HScroll from '../../../../components/ui/HScroll'
import { useWorkbenchTabs } from '../../../../stores/workbenchTabs'
import './WorkbenchTabs.css'

/**
 * 工作台顶部多股 tab 条(T-55):打开顺序(新开置末,上限 5),点击已有 tab 仅切换激活
 * 不重排(浏览器式,2026-08-15 用户裁决——避免点击后 tab 条跳来跳去);
 * 新开/深链由 StockWorkbench 走 open()(加入+激活+置末);右上角 × 关闭
 * (关闭激活 tab 时 store 自动切相邻);溢出用 HScroll 横滚。
 * tab 名称惰性回填:行情返回前仅显示 code,返回后「名称 + code」。
 */
export default function WorkbenchTabs() {
  const tabs = useWorkbenchTabs((s) => s.tabs)
  const activeCode = useWorkbenchTabs((s) => s.activeCode)
  const setActive = useWorkbenchTabs((s) => s.setActive)
  const close = useWorkbenchTabs((s) => s.close)

  // 空态(全部关闭,即将跳 /signals):不渲染 tab 条
  if (tabs.length === 0) return null

  return (
    <div className="sr-wb-tabs" role="tablist" aria-label="打开的股票工作台">
      <HScroll>
        <div className="sr-wb-tabs-inner">
          {tabs.map((t) => {
            const isActive = t.code === activeCode
            const label = t.name ? `${t.name} ${t.code}` : t.code
            return (
              <div
                key={t.code}
                role="tab"
                aria-selected={isActive}
                className={'sr-wb-tab' + (isActive ? ' is-active' : '')}
                onClick={() => setActive(t.code)}
                title={label}
              >
                <span className="sr-wb-tab-name">{t.name ?? t.code}</span>
                {t.name && <span className="sr-wb-tab-code">{t.code}</span>}
                <Tooltip label={`关闭 ${label}`} withArrow>
                  <ActionIcon
                    size="xs"
                    variant="subtle"
                    className="sr-wb-tab-close"
                    aria-label={`关闭 ${label}`}
                    onClick={(e) => {
                      e.stopPropagation()
                      close(t.code)
                    }}
                  >
                    <IconX size={12} />
                  </ActionIcon>
                </Tooltip>
              </div>
            )
          })}
        </div>
      </HScroll>
    </div>
  )
}
