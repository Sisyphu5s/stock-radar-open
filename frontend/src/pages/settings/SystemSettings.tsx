import { Alert, Badge, Button, Code, DataList, Group, NumberInput, SegmentedControl, Switch, Tabs, Text, TextInput, Textarea } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconAlertTriangle, IconApi, IconBell, IconBug, IconDeviceDesktop, IconPower, IconRobot, IconSettings } from '@tabler/icons-react'
import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { useDensityStore, useThemeStore } from '../../stores/useAppStore'
import type { DensityMode, ThemeMode } from '../../stores/useAppStore'
import { useViewport } from '../../app/useViewport'
import useWebNotifications from '../../hooks/useWebNotifications'
import { DataConnectionsSection } from './DataConnections'
import { LlmCopilotSection } from './LlmCopilot'
import { getDebugMode, setDebugMode } from '../../utils/debugLayout'
import type { DebugMode } from '../../utils/debug/debugTypes'
import { CardState, FormRow, StatStrip } from '../../components/ui'
import { useSystemOverview } from './SystemStatus'
import type { OverviewItemView } from './SystemStatus'
import { SettingBlock } from '../../templates'
import { getRuntimeConfig, setRuntimeConfig } from '../../api/client'
import type { RuntimeConfigItem } from '../../api/client'

/** antd message → @mantine/notifications（T-44：色/时长对齐 research/shared/toast.ts 契约） */
const notifySuccess = (m: string) => notifications.show({ message: m, color: 'teal', autoClose: 2500 })
const notifyError = (m: string) => notifications.show({ message: m, color: 'red', autoClose: 4000 })

const EXIT_KEY = 'sr-exit-requested'

interface SectionDef {
  key: string
  label: string
  icon: ReactNode
}

const SECTIONS: SectionDef[] = [
  { key: 'appearance', label: '外观', icon: <IconDeviceDesktop size={16} /> },
  { key: 'notify', label: '通知', icon: <IconBell size={16} /> },
  { key: 'data', label: '数据连接', icon: <IconApi size={16} /> },
  { key: 'ai', label: 'AI', icon: <IconRobot size={16} /> },
  { key: 'debug', label: '开发工具', icon: <IconBug size={16} /> },
  { key: 'system', label: '配置与安全', icon: <IconSettings size={16} /> },
]

/** 通知:系统通知开关 + 权限状态 + 权限引导(拒绝后说明在浏览器设置开启)。
 *  通知触发条件见 hooks/useWebNotifications:仅页面不可见时走系统通知,页面可见仍站内浮窗。 */
function NotificationSection() {
  const web = useWebNotifications()
  const [requesting, setRequesting] = useState(false)

  const handleRequest = async () => {
    setRequesting(true)
    try {
      await web.requestPermission()
    } finally {
      setRequesting(false)
    }
  }

  const renderPermission = () => {
    switch (web.permission) {
      case 'granted':
        return <Text c="teal">已允许:页面不可见时新触发信号将弹出系统通知</Text>
      case 'denied':
        return (
          <Text c="red">已拒绝:请在浏览器设置中允许本网站的通知权限后重试</Text>
        )
      case 'unsupported':
        return (
          <Text c="yellow">
            当前环境不支持系统通知(需 https 或 localhost,且浏览器支持 Notification API),新信号提醒仅站内浮窗
          </Text>
        )
      default:
        return (
          <Group gap="var(--sr-gap-row)">
            <Button size="xs" variant="light" loading={requesting} onClick={handleRequest}>
              申请权限
            </Button>
            <Text c="dimmed" size="xs">页面不可见时通过系统通知提醒新触发信号</Text>
          </Group>
        )
    }
  }

  return (
    <SettingBlock
      title="通知"
      icon={<IconBell size={14} />}
      desc="新触发信号:页面可见时站内浮窗提醒,页面不可见时系统通知提醒"
    >
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-gap-row)' }}>
        <FormRow label="系统通知">
          <Switch
            checked={web.enabled}
            disabled={!web.supported}
            onChange={(e) => web.setEnabled(e.currentTarget.checked)}
            aria-label="启用系统通知"
          />
        </FormRow>
        <FormRow label="权限状态">{renderPermission()}</FormRow>
      </div>
    </SettingBlock>
  )
}

/** 外观：主题 / 密度，全局 store 驱动，改动即时生效 */
function AppearanceSection() {
  const theme = useThemeStore((s) => s.theme)
  const toggleTheme = useThemeStore((s) => s.toggleTheme)
  const density = useDensityStore((s) => s.density)
  const setDensity = useDensityStore((s) => s.setDensity)
  return (
    <SettingBlock title="外观" icon={<IconDeviceDesktop size={14} />} desc="全局即时生效">
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-gap-row)' }}>
        <FormRow label="主题">
          <SegmentedControl
            value={theme}
            onChange={(v) => { const next = v as ThemeMode; if (next !== theme) toggleTheme() }}
            data={[
              { value: 'light', label: '亮色' },
              { value: 'dark', label: '暗色' },
            ]}
          />
        </FormRow>
        <FormRow label="密度">
          <SegmentedControl
            value={density}
            onChange={(v) => { const next = v as DensityMode; if (next !== density) setDensity(next) }}
            data={[
              { value: 'compact', label: '紧凑' },
              { value: 'comfort', label: '舒适' },
            ]}
          />
        </FormRow>
      </div>
    </SettingBlock>
  )
}

/** 开发工具：Debug 三态模式（off/visual/headless）。写入 localStorage 并广播 'sr-debug-mode'，
 *   MainLayout 监听事件实时挂载/卸载；读初始值 + 监听事件双向同步（快捷键切换也能反映到本控件）。 */
function DebugSection() {
  const [mode, setMode] = useState<DebugMode>(getDebugMode)

  useEffect(() => {
    const onMode = (e: Event) => {
      const m = (e as CustomEvent).detail?.mode
      if (m === 'off' || m === 'visual' || m === 'headless') setMode(m)
    }
    window.addEventListener('sr-debug-mode', onMode)
    return () => window.removeEventListener('sr-debug-mode', onMode)
  }, [])

  return (
    <SettingBlock
      title="Debug 模式"
      icon={<IconBug size={14} />}
      desc="可视化 = 边缘 outline 覆盖层 + 贴边计数工作台；纯代码 = headless 检查，结果分组输出到浏览器 console（每 10s 重跑）；Ctrl+Shift+D 在开/关间快速切换。"
    >
      <FormRow label="模式">
        <SegmentedControl
          value={mode}
          onChange={(v) => setDebugMode(v as DebugMode)}
          data={[
            { value: 'off', label: '关闭' },
            { value: 'visual', label: '可视化' },
            { value: 'headless', label: '纯代码' },
          ]}
        />
      </FormRow>
    </SettingBlock>
  )
}

/** 运行参数单行（T-77）：env 基线的可编辑覆盖，保存调 PUT /system/config/runtime。
 *  类型驱动控件：int/float → NumberInput，str → TextInput，json → JSON 文本域。 */
function RuntimeParamRow({ item, onSaved }: { item: RuntimeConfigItem; onSaved: () => void }) {
  const isNum = item.type === 'int' || item.type === 'float'
  const initial = useRef<string>(
    item.type === 'json' ? JSON.stringify(item.value, null, 2) : String(item.value),
  )
  const [raw, setRaw] = useState<string>(initial.current)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const dirty = raw !== initial.current

  const save = async () => {
    setErr(null)
    let value: unknown = raw
    if (item.type === 'json') {
      try {
        value = JSON.parse(raw)
      } catch {
        setErr('JSON 格式错误，请检查后重试')
        return
      }
    } else if (isNum) {
      const n = Number(raw)
      if (!Number.isFinite(n)) {
        setErr('必须为数字')
        return
      }
      value = item.type === 'int' ? Math.trunc(n) : n
    }
    setSaving(true)
    try {
      await setRuntimeConfig(item.key, value)
      initial.current = raw
      notifySuccess(`已保存「${item.label}」`)
      onSaved()
    } catch (e: any) {
      setErr('保存失败: ' + String(e?.response?.data?.detail ?? e?.message ?? e))
    } finally {
      setSaving(false)
    }
  }

  const control = isNum ? (
    <NumberInput
      size="xs"
      value={raw}
      onChange={(v) => setRaw(String(v))}
      style={{ width: 'clamp(120px, 22vw, 200px)' }}
      aria-label={item.label}
    />
  ) : item.type === 'json' ? (
    <Textarea
      size="xs"
      value={raw}
      onChange={(e) => setRaw(e.currentTarget.value)}
      minRows={4}
      style={{ width: 'clamp(240px, 52vw, 460px)', fontFamily: 'monospace' }}
      aria-label={item.label}
    />
  ) : (
    <TextInput
      size="xs"
      value={raw}
      onChange={(e) => setRaw(e.currentTarget.value)}
      style={{ width: 'clamp(160px, 30vw, 260px)' }}
      aria-label={item.label}
    />
  )

  return (
    <FormRow label={item.label} width="clamp(140px, 20vw, 210px)">
      <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-gap-row)', minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sr-gap-row)', flexWrap: 'wrap', minWidth: 0 }}>
          {control}
          <Button size="xs" variant="light" loading={saving} disabled={!dirty} onClick={() => void save()}>
            保存
          </Button>
          <Badge size="sm" variant="light" color={item.apply === 'immediate' ? 'teal' : 'yellow'}>
            {item.apply === 'immediate' ? '立即生效' : '重启生效'}
          </Badge>
        </div>
        <Text c="dimmed" size="xs">{item.description}</Text>
        {err && <Text c="red" size="xs">{err}</Text>}
      </div>
    </FormRow>
  )
}

/** 运行参数（T-77）：env 基线的可编辑运行时覆盖，GET/PUT /system/config/runtime。 */
function RuntimeParamsSection() {
  const [items, setItems] = useState<RuntimeConfigItem[] | null>(null)
  const [loadErr, setLoadErr] = useState<string | null>(null)

  const load = async () => {
    try {
      setItems(await getRuntimeConfig())
      setLoadErr(null)
    } catch (e: any) {
      setLoadErr('运行参数加载失败: ' + (e?.message ?? e))
    }
  }
  useEffect(() => { void load() }, [])

  return (
    <SettingBlock
      title="运行参数"
      icon={<IconSettings size={14} />}
      desc={
        <>
          编辑并保存后可覆盖 <Code>backend/.env</Code> 的对应环境变量（保存值为当前生效值）；
          「重启生效」项（扫描调度 / 任务并发）需重启后端，其余保存后立即生效。
        </>
      }
    >
      <CardState loading={items == null && !loadErr} error={loadErr} onRetry={load} loadingText="正在加载运行参数…">
        {items && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-gap-card)' }}>
            {items.map((it) => (
              <RuntimeParamRow key={it.key} item={it} onSaved={() => void load()} />
            ))}
          </div>
        )}
      </CardState>
    </SettingBlock>
  )
}

/** 配置与安全：环境配置展示 / 退出系统（服务状态与缓存统计已迁「系统状态」页 /status） */
function SystemSection() {
  const [exitInput, setExitInput] = useState('')
  const [exiting, setExiting] = useState(false)

  const confirmExit = async () => {
    setExiting(true)
    try {
      const r = await fetch('/api/v1/system/exit', { method: 'POST' })
      if (r.ok) {
        try { localStorage.setItem(EXIT_KEY, '1') } catch { /* ignore */ }
        window.location.reload()
        return
      }
      // 服务仍在线且明确失败（4xx/5xx）→ 报错
      throw new Error('HTTP ' + r.status)
    } catch (e: any) {
      // 网络错误/连接中断（服务已开始退出、响应丢失属预期）→ 视为成功；仅明确 HTTP 失败才报错
      if (e instanceof Error && /^HTTP \d+$/.test(e.message)) {
        notifyError('退出失败: ' + e.message)
        setExiting(false)
      } else {
        notifySuccess('退出请求已发出，系统即将关闭')
        try { localStorage.setItem(EXIT_KEY, '1') } catch { /* ignore */ }
        setTimeout(() => window.location.reload(), 800)
      }
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-gap-card)' }}>
      <SettingBlock
        title="配置项（backend/.env，前缀 SR_）"
        icon={<IconSettings size={14} />}
        desc="配置项为只读展示：其中 6 项已支持在下方「运行参数」直接编辑（运行时覆盖，无需改 .env）；其余修改 backend/.env 中的环境变量后重启后端生效，完整示例见 .env.example。"
      >
        <DataList size="sm" orientation="horizontal" withDivider labelWidth={72}>
          <DataList.Item>
            <DataList.ItemLabel>数据源</DataList.ItemLabel>
            <DataList.ItemValue><Code>SR_DATA_PROVIDER</Code></DataList.ItemValue>
          </DataList.Item>
          <DataList.Item>
            <DataList.ItemLabel>GP 后端</DataList.ItemLabel>
            <DataList.ItemValue><Code>SR_GP_BACKEND</Code></DataList.ItemValue>
          </DataList.Item>
          <DataList.Item>
            <DataList.ItemLabel>LLM</DataList.ItemLabel>
            <DataList.ItemValue>
              <Group gap={4}><Code>SR_LLM_BASE_URL</Code><Code>SR_LLM_API_KEY</Code><Code>SR_LLM_MODEL</Code></Group>
            </DataList.ItemValue>
          </DataList.Item>
          <DataList.Item>
            <DataList.ItemLabel>扫描</DataList.ItemLabel>
            <DataList.ItemValue><Code>SR_MARKET_SCAN_LIMIT</Code></DataList.ItemValue>
          </DataList.Item>
        </DataList>
      </SettingBlock>

      <RuntimeParamsSection />

      <SettingBlock title="第三方组件" icon={<IconSettings size={14} />}>
        <Text size="xs" c="dimmed">
          TradingView Lightweight Charts™ Copyright (C) 2025 TradingView, Inc.{' '}
          <a href="https://www.tradingview.com/" target="_blank" rel="noopener noreferrer">TradingView</a>
        </Text>
      </SettingBlock>

      <SettingBlock title="危险操作" icon={<IconAlertTriangle size={14} />}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-gap-card)' }}>
          <Alert
            color="yellow"
            icon={<IconAlertTriangle size={16} />}
            title="退出系统将停止市场扫描、释放数据库并关闭后端服务"
          >
            重新运行 README 中的后端启动命令即可再次启动。请在下方输入「退出」以启用确认按钮。
          </Alert>
          {/* 原生 form 承载 Enter 提交语义（原 antd Form onFinish），行布局交给 FormRow */}
          <form onSubmit={(e) => { e.preventDefault(); if (exitInput.trim() === '退出') void confirmExit() }}>
            <FormRow label="退出确认">
              <Group gap="var(--sr-gap-row)">
                <TextInput
                  value={exitInput}
                  onChange={(e) => setExitInput(e.currentTarget.value)}
                  placeholder="输入「退出」以启用确认"
                  style={{ width: 'clamp(160px, 35vw, 240px)' }}
                  maxLength={8}
                />
                <Button
                  color="red"
                  variant="outline"
                  leftSection={<IconPower size={14} />}
                  disabled={exitInput.trim() !== '退出'}
                  loading={exiting}
                  type="submit"
                >
                  退出系统
                </Button>
              </Group>
            </FormRow>
          </form>
        </div>
      </SettingBlock>
    </div>
  )
}

/** 单一设置工作区：桌面左侧 section 导航，移动 Tabs；区块组件不再嵌套 PageHeader / Card。
 *  分区深链：#/settings?section=<key>（HashRouter 下 query 在 hash 内，与 useResearchUrlSync 先例一致）。
 *  Mantine 迁移（T-44）：移动端 Tabs → Mantine Tabs（无「更多」折叠，5 分区横向滚动，行为降级）。 */
export default function SystemSettings() {
  const viewport = useViewport()
  const isMobile = viewport === 'mobile'
  const [hovered, setHovered] = useState<string | null>(null)
  const navigate = useNavigate()
  // L0 结论条数据源与 /status 同源（useSystemOverview：/health + 数据源健康，30s 轮询）
  const ov = useSystemOverview()
  const tint = (v: OverviewItemView) => (v.color ? <span style={{ color: v.color }}>{v.text}</span> : v.text)

  const sectionFromUrl = (): string => {
    const q = new URLSearchParams(window.location.hash.split('?')[1] ?? '')
    const s = q.get('section')
    return s && SECTIONS.some((x) => x.key === s) ? s : 'appearance'
  }
  const [active, setActive] = useState<string>(sectionFromUrl)

  // URL 深链变化（浏览器前进后退 / 外部直达 #/settings?section=data）→ 同步分区
  useEffect(() => {
    const onHash = () => setActive(sectionFromUrl())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const select = (k: string) => {
    setActive(k)
    const base = window.location.hash.split('?')[0]
    const target = base + '?section=' + k
    if (window.location.hash !== target) window.location.replace(target)
  }

  const renderSection = () => {
    switch (active) {
      case 'notify': return <NotificationSection />
      case 'data': return <DataConnectionsSection />
      case 'ai': return <LlmCopilotSection />
      case 'debug': return <DebugSection />
      case 'system': return <SystemSection />
      default: return <AppearanceSection />
    }
  }

  return (
    <div className="sr-page">
      {/* L0 结论条（05 §5.7）：数据源 N/M 正常 · 服务运行中；点击跳 /status 详情；色走语义令牌 */}
      <StatStrip
        loading={ov.loading}
        scrollable={isMobile}
        items={[
          {
            key: 'source',
            label: '数据源',
            value: tint(ov.source),
            onClick: () => navigate('/status'),
          },
          {
            key: 'service',
            label: '服务',
            value: tint(ov.service),
            onClick: () => navigate('/status'),
          },
        ]}
      />
      {isMobile ? (
        <>
          <Tabs value={active} onChange={(k) => { if (k) select(k) }}>
            <Tabs.List style={{ marginBottom: 'var(--sr-pad-lg)', paddingRight: 'var(--sr-pad-md)', overflowX: 'auto', flexWrap: 'nowrap' }}>
              {SECTIONS.map((s) => (
                <Tabs.Tab key={s.key} value={s.key} leftSection={s.icon}>{s.label}</Tabs.Tab>
              ))}
            </Tabs.List>
          </Tabs>
          <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 'var(--sr-gap-page)' }}>
            {/* 2026-08-13 复审:内容不满时垂直居中(margin auto,超高时归零不裁剪),消除下方留白 */}
            <div style={{ margin: 'auto 0' }}>{renderSection()}</div>
          </div>
        </>
      ) : (
        <div style={{ display: 'flex', gap: 'var(--sr-gap-page)', alignItems: 'flex-start', width: '100%', minWidth: 0 }}>
          <nav
            aria-label="设置分区"
            style={{
              width: 'clamp(148px, 18vw, 184px)',
              flexShrink: 0,
              display: 'flex',
              flexDirection: 'column',
              gap: 2,
              padding: 'var(--sr-pad-sm)',
              border: '1px solid var(--sr-border)',
              borderRadius: 'var(--sr-radius-ctl)',
              background: 'var(--sr-card-bg)',
              position: 'sticky',
              top: 'calc(var(--sr-header-h) + var(--sr-safe-top) + var(--sr-content-pad))',
            }}
          >
            {SECTIONS.map((s) => {
              const isActive = active === s.key
              return (
                <button
                  key={s.key}
                  onClick={() => select(s.key)}
                  onMouseEnter={() => setHovered(s.key)}
                  onMouseLeave={() => setHovered(null)}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 'var(--sr-gap-ctl)',
                    padding: 'var(--sr-pad-sm) var(--sr-pad-lg)',
                    border: 'none',
                    borderRadius: 'var(--sr-radius-tag)',
                    background: isActive || hovered === s.key ? 'var(--sr-hover-bg)' : 'transparent',
                    color: isActive || hovered === s.key ? 'var(--sr-text-1)' : 'var(--sr-text-2)',
                    fontWeight: isActive ? 600 : 400,
                    fontSize: 'var(--sr-font-page)',
                    fontFamily: 'inherit',
                    textAlign: 'left',
                    cursor: 'pointer',
                    boxShadow: isActive ? 'inset 2px 0 0 var(--sr-accent)' : undefined,
                    transition: 'background .15s ease, color .15s ease',
                  }}
                >
                  {s.icon}{s.label}
                </button>
              )
            })}
          </nav>
          <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 'var(--sr-gap-page)' }}>
            {/* 2026-08-13 复审:内容不满时垂直居中(margin auto,超高时归零不裁剪),消除下方留白 */}
            <div style={{ margin: 'auto 0' }}>{renderSection()}</div>
          </div>
        </div>
      )}
    </div>
  )
}
