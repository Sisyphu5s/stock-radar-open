import { ActionIcon, Menu, Text, Tooltip } from '@mantine/core'
import { IconCheck, IconFolderPlus } from '@tabler/icons-react'
import type { WatchlistGroup } from '../../../api/watchlistGroups'

interface WatchlistGroupMenuProps {
  /** 目标股票代码 */
  code: string
  /** 该股已所在分组 id 集合（菜单勾选态：已入组显示 ✓，点击为移出） */
  memberGroupIds: ReadonlySet<number>
  groups: WatchlistGroup[]
  /** 切换分组归属：code 已入组 → 移出，否则移入 */
  onToggle: (code: string, groupId: number) => void
  /** 菜单「新建分组…」入口：打开分组管理 Modal */
  onManage: () => void
}

/**
 * 行操作「移入分组」（T-12）：Menu 列出全部分组，已入组打勾（点击移出），
 * 未入组点击移入；尾部「新建分组…」直达管理 Modal。桌面表格与移动卡片共用。
 * 触控目标 ≥28px（P1-a11y-5 同星标），点击不触发行跳转。
 */
export default function WatchlistGroupMenu({ code, memberGroupIds, groups, onToggle, onManage }: WatchlistGroupMenuProps) {
  return (
    <Menu position="bottom-start" withinPortal>
      <Tooltip label="移入分组">
        <Menu.Target>
          <ActionIcon
            variant="subtle" color="gray"
            className="sr-wl-gmenu"
            style={{ minWidth: 28, minHeight: 28, flexShrink: 0 }}
            onClick={(e) => e.stopPropagation()}
            aria-label={`移入分组 ${code}`}
          >
            <IconFolderPlus size={15} />
          </ActionIcon>
        </Menu.Target>
      </Tooltip>
      <Menu.Dropdown>
        <Menu.Label>移入分组</Menu.Label>
        {groups.length === 0 ? (
          <Menu.Item disabled>暂无分组，可先新建</Menu.Item>
        ) : (
          groups.map((g) => {
            const inGroup = memberGroupIds.has(g.id)
            return (
              <Menu.Item
                key={g.id}
                leftSection={inGroup ? <IconCheck size={14} /> : null}
                rightSection={<Text size="xs" c="dimmed">{g.count}</Text>}
                onClick={(e) => { e.stopPropagation(); onToggle(code, g.id) }}
              >
                {g.name}
              </Menu.Item>
            )
          })
        )}
        <Menu.Divider />
        <Menu.Item onClick={(e) => { e.stopPropagation(); onManage() }}>新建分组…</Menu.Item>
      </Menu.Dropdown>
    </Menu>
  )
}
