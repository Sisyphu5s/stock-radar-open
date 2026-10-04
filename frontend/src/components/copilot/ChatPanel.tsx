import { Button, Tooltip } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import {
  IconBolt, IconChartLine, IconClock, IconCopy, IconFlask, IconPencil,
  IconPlus, IconRefresh, IconRobot, IconStar, IconUser, IconX,
} from '@tabler/icons-react'
import { Bubble, Prompts, Sender, Welcome } from '@ant-design/x'
import type { BubbleItemType, BubbleListProps, PromptsItemType } from '@ant-design/x'
import { ConfigProvider, theme as antdTheme } from 'antd'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { streamCopilot } from '../../api/client'
import type { CopilotAction, CopilotHistoryMsg, SignalFilterCommand, SignalCenterFilters } from '../../api/client'
import { MAX_SESSIONS, useCopilotStore } from '../../stores/copilotStore'
import type { CopilotMsg, CtxProvider, SignalCenterCommandPayload } from '../../stores/copilotStore'
import { useThemeStore } from '../../stores/useAppStore'
import { semantic } from '../../theme/tokens'
import type { SemanticKey } from '../../theme/tokens'
import { adapterLabel, isMarketPath } from '../../utils/routeTitles'

/** antd 主题 token:从 tokens.ts 语义令牌(单一事实源)按当前主题取值。
 *  仅覆盖 @ant-design/x 组件树消费的关键 token(文字/背景/边框/主色),
 *  其余派生 token 交给 antd 的 dark/default algorithm。 */
const buildAntdToken = (isDark: boolean): Record<string, string> => {
  const pick = (k: SemanticKey) => semantic[k][isDark ? 'dark' : 'light']
  return {
    // 文字可读性核心:Welcome 标题/描述、Sender 输入、Prompts 条目
    colorText: pick('text1'),
    colorTextSecondary: pick('text2'),
    colorTextTertiary: pick('text3'),
    colorTextHeading: pick('text1'),
    colorTextPlaceholder: pick('text3'),
    colorTextLightSolid: pick('onAccent'),
    // 背景/边框:与 --sr-card-bg / --sr-border 对齐
    colorBgContainer: pick('cardBg'),
    colorBgContainerDisabled: pick('blockBg'),
    colorBgElevated: pick('cardBg'),
    colorBgLayout: pick('bg'),
    colorFillContent: pick('blockBg'),
    colorBorder: pick('border'),
    colorBorderSecondary: pick('border'),
    colorBorderInput: pick('border'),
    // 主色:发送按钮 / 聚焦态
    colorPrimary: pick('accent'),
  }
}

/** 模块级活动流注册表（窗口层可中止）：
 *  CopilotTab 发起流时注册 AbortController，流结束/停止时注销；
 *  组件卸载后仍可访问，供 CopilotWindow 在关窗/重开边界中止后台流，避免旧流继续写 store。 */
let activeAbortCtrl: AbortController | null = null
/** 模块级流序号：每次发起流递增；回调校验序号仍为最新才写 store（防重开同会话双流交替写） */
let streamSeq = 0

/** 中止当前活动流（若存在）；中止后注册表清空。供窗口层在开/关窗路径调用。 */
export function abortActiveStream(): void {
  activeAbortCtrl?.abort()
  activeAbortCtrl = null
}

/** 动态路径上下文 provider：按最长前缀匹配（/stocks/600519.SH → /stocks） */
const matchProvider = (providers: Record<string, CtxProvider>, pathname: string): CtxProvider | undefined => {
  let best: CtxProvider | undefined
  let bestLen = -1
  for (const [key, p] of Object.entries(providers)) {
    if (pathname === key || pathname.startsWith(key + '/')) {
      if (key.length > bestLen) { best = p; bestLen = key.length }
    }
  }
  return best
}

/** 错误消息识别：客户端 catch / 请求失败 / 后端 LLM 错误文本（重试按钮与历史过滤共用） */
const isErrorText = (t: string): boolean =>
  t.startsWith('[错误]') || t.startsWith('[请求失败') || t.startsWith('[LLM ')

/** 客户端 token 估算：CJK 按 1 字符/token、其余按 4 字符/token（cl100k 近似，仅提示用） */
const estimateTokens = (text: string): number => {
  const cjk = (text.match(/[\u4e00-\u9fff\u3400-\u4dbf]/g) ?? []).length
  return Math.ceil(cjk + (text.length - cjk) / 4) || 1
}

/** 消息本地时间（HH:mm；ts 缺失的旧持久化消息返回空串） */
const fmtClock = (ts?: number): string => {
  if (!ts) return ''
  const d = new Date(ts)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 会话内最近 N 轮历史（最多 2N 条消息），过滤错误文本避免污染上下文。
 *  T-72：user 消息的 ctxSummary（发送时刻上下文摘要）仅内存态，上送历史时拼回
 *  content，后端可追多轮上下文；UI 气泡与 localStorage 均只留问题原文。 */
const MAX_HISTORY_TURNS = 8
const toHistory = (msgs: CopilotMsg[]): CopilotHistoryMsg[] =>
  msgs.filter((m) => !isErrorText(m.content))
    .slice(-MAX_HISTORY_TURNS * 2)
    .map((m) => ({
      role: m.role,
      content: m.ctxSummary ? `${m.ctxSummary}\n\n${m.content}` : m.content,
    }))

// ===== Hermes 页面快照（T-93）：发送对话瞬间收集当前页面元素，随 context 上送 =====
/** 页面快照整体限长（与后端 SNAPSHOT_MAX_CHARS 一致） */
const SNAPSHOT_MAX_CHARS = 8000
const MAX_HEADINGS = 15
const MAX_BUTTONS = 20
/** 历史 user 消息中 context 摘要上限：压缩后进多轮历史，8 轮总量可控 */
const CTX_SUMMARY_MAX_CHARS = 600
/** 长回复折叠阈值（字符） */
const LONG_MSG_CHARS = 800

/** 收集当前页面 DOM 快照：路由/标题 + h1-h3 卡片标题 + button 文本（去重、截断、限长）。
 *  document 不可用（SSR/测试）时返回空快照。 */
const collectPageSnapshot = (pathname: string): Record<string, unknown> => {
  if (typeof document === 'undefined') return { route: pathname, pageTitle: adapterLabel(pathname) ?? pathname }
  const textOf = (el: Element): string => (el.textContent ?? '').trim().replace(/\s+/g, ' ')
  const headings: string[] = []
  document.querySelectorAll('h1,h2,h3').forEach((el) => {
    const t = textOf(el)
    if (t && !headings.includes(t) && headings.length < MAX_HEADINGS) headings.push(t)
  })
  const buttons: string[] = []
  document.querySelectorAll('button').forEach((el) => {
    const t = textOf(el)
    if (t && !buttons.includes(t) && buttons.length < MAX_BUTTONS) buttons.push(t)
  })
  const snap: Record<string, unknown> = {
    route: pathname,
    pageTitle: adapterLabel(pathname) ?? pathname,
    headings,
    buttons,
  }
  // 整体限长：超限逐项收缩（先丢按钮、再丢卡片标题），保证 JSON 体积受控
  let text = ''
  try {
    text = JSON.stringify(snap)
  } catch { /* ignore */ }
  if (text.length > SNAPSHOT_MAX_CHARS) {
    while (buttons.length && JSON.stringify(snap).length > SNAPSHOT_MAX_CHARS) buttons.pop()
    while (headings.length && JSON.stringify(snap).length > SNAPSHOT_MAX_CHARS) headings.pop()
  }
  return snap
}

/** context → 精简文本摘要（进多轮历史用）：只取标量/短数组，整体限长 CTX_SUMMARY_MAX_CHARS。
 *  page 快照不重复进摘要——页面状态已随发送时的 context 注入，历史中避免体积膨胀。 */
const ctxSummary = (ctx: Record<string, unknown>): string => {
  const parts: string[] = []
  for (const [k, v] of Object.entries(ctx)) {
    if (k === 'page' || v == null || v === '') continue
    let s = ''
    if (typeof v === 'string') s = v
    else if (typeof v === 'number' || typeof v === 'boolean') s = String(v)
    else if (Array.isArray(v)) s = v.length ? v.map((x) => String(x)).join(', ').slice(0, 120) : ''
    else if (typeof v === 'object') {
      try { s = JSON.stringify(v).slice(0, 200) } catch { continue }
    } else continue
    if (s) parts.push(`${k}=${s}`)
  }
  return parts.join('；').slice(0, CTX_SUMMARY_MAX_CHARS)
}

const PERIOD_LABEL: Record<string, string> = {
  '1': '1分', '5': '5分', '15': '15分', '30': '30分', '60': '60分',
  daily: '日线', weekly: '周线', monthly: '月线',
}
const TIME_RANGE_LABEL: Record<string, string> = {
  today: '今日', '3d': '近3日', '7d': '近7日', '14d': '近2周', '30d': '近30日', '60d': '近2月', '90d': '近3月', all: '全部',
}

/** 信号中心命令 → 助手消息：基于 filters 生成简洁中文操作消息（wire 无 summary/intent，纯由筛选清单描述） */
const describeCommand = (cmd: SignalFilterCommand): string => {
  const f = cmd.filters
  const parts: string[] = []
  if (f.period) parts.push(`周期 ${PERIOD_LABEL[f.period] ?? f.period}`)
  if (f.time_range) parts.push(`触发时间 ${TIME_RANGE_LABEL[f.time_range] ?? f.time_range}`)
  if (f.exclude_today) parts.push('排除当日')
  if (f.sectors?.length) parts.push(`板块 ${f.sectors.join('/')}`)
  if (f.signal_types?.length) parts.push(`信号 ${f.signal_types.join('/')}`)
  if (f.signal_match === 'all') parts.push('全部命中')
  if (f.watchlist_only) parts.push('仅看关注')
  const list = parts.length ? `已应用筛选：${parts.join('；')}` : '已应用信号中心筛选'
  return `${list}，已跳转信号中心`
}

/** 个股跳转命令 → 助手消息（前端固定导航 /stocks/{code}，不执行任何数据操作） */
const describeNavigate = (code: string): string => `已打开 ${code} 个股工作台`

/** 解析命令 wire 筛选（snake_case）→ 信号中心 store 载荷（camelCase，replace 语义）。
 *  必须写入全部字段（含空数组/false/0），确保 replace 真正清掉旧筛选而非只覆盖已提及项。 */
const toCommandPayload = (f: SignalCenterFilters): SignalCenterCommandPayload => ({
  period: f.period ?? 'daily',
  timeRange: f.time_range ?? '3d',
  excludeToday: f.exclude_today ?? false,
  sectors: f.sectors ?? [],
  signals: f.signal_types ?? [],
  match: f.signal_match ?? 'any',
  watchlist: f.watchlist_only ?? false,
})

/** 空态快捷提问（按会话域给出可执行问法；label 即发送给 LLM 的原文） */
const SUGGESTIONS: Record<'market' | 'research', PromptsItemType[]> = {
  market: [
    { key: 'm-signals', icon: <IconBolt size={16} aria-hidden />, label: '最近有什么信号？', description: '查看近期触发的信号' },
    { key: 'm-watchlist', icon: <IconStar size={16} aria-hidden />, label: '只看关注列表', description: '在信号中心过滤关注股票' },
    { key: 'm-today', icon: <IconClock size={16} aria-hidden />, label: '今日信号', description: '只看今日触发的信号' },
  ],
  research: [
    { key: 'r-backtest', icon: <IconFlask size={16} aria-hidden />, label: '怎么开始回测？', description: '回测流程与入口' },
    { key: 'r-eval', icon: <IconChartLine size={16} aria-hidden />, label: '因子评估怎么做？', description: '评估流程与指标' },
  ],
}

/** 长回复可读性（T-93）：>LONG_MSG_CHARS 折叠为摘要 + 「展开全文」；文本走 pre-wrap 保留段落/换行。
 *  项目无 markdown 依赖（不引新包），段落/换行由 CSS white-space: pre-wrap 承担。 */
function MsgContent({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false)
  const long = text.length > LONG_MSG_CHARS
  const shown = long && !expanded ? text.slice(0, LONG_MSG_CHARS) + '…' : text
  return (
    <div className="sr-cop-msg-content">
      <div className="sr-cop-msg-text">{shown}</div>
      {long && !expanded && (
        <Button size="compact-xs" variant="subtle" className="sr-cop-msg-expand" onClick={() => setExpanded(true)}>
          展开全文
        </Button>
      )}
    </div>
  )
}

/** 会话标签条：多会话 Tab（mode 色点 + 标题 + 关闭 X），点击切换 / 双击重命名 / 新建按钮。 */
export function SessionTabs() {
  const location = useLocation()
  const sessions = useCopilotStore((s) => s.sessions)
  const activeId = useCopilotStore((s) => s.activeId)
  const newSession = useCopilotStore((s) => s.newSession)
  const selectSession = useCopilotStore((s) => s.selectSession)
  const deleteSession = useCopilotStore((s) => s.deleteSession)
  const renameSession = useCopilotStore((s) => s.renameSession)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [editValue, setEditValue] = useState('')

  const routeMode: 'market' | 'research' = isMarketPath(location.pathname) ? 'market' : 'research'

  const commitRename = () => {
    if (editingId && editValue.trim()) renameSession(editingId, editValue)
    setEditingId(null)
  }

  return (
    <div className="sr-cop-tabs" role="tablist">
      {sessions.map((s, idx) => {
        const active = s.id === activeId
        // tab 键盘：Enter/Space 选中；←/→ 循环切换（重命名 input 聚焦时方向键不劫持）
        const onTabKey = (e: ReactKeyboardEvent<HTMLDivElement>) => {
          if (e.target instanceof HTMLInputElement) return
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selectSession(s.id); return }
          if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
            e.preventDefault()
            const next = (idx + (e.key === 'ArrowRight' ? 1 : -1) + sessions.length) % sessions.length
            selectSession(sessions[next].id)
          }
        }
        return (
          <div
            key={s.id}
            role="tab"
            aria-selected={active}
            aria-label={s.title}
            tabIndex={active ? 0 : -1}
            className={'sr-cop-tab' + (active ? ' sr-cop-tab-active' : '')}
            title={s.title}
            onClick={() => selectSession(s.id)}
            onKeyDown={onTabKey}
            onDoubleClick={() => { setEditingId(s.id); setEditValue(s.title) }}
          >
            <span className={'sr-cop-tab-dot ' + (s.mode === 'market' ? 'sr-cop-dot-market' : 'sr-cop-dot-research')} />
            {editingId === s.id ? (
              <input
                className="sr-cop-rename"
                value={editValue}
                autoFocus
                onChange={(e) => setEditValue(e.target.value)}
                onClick={(e) => e.stopPropagation()}
                onBlur={commitRename}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') commitRename()
                  if (e.key === 'Escape') {
                    e.stopPropagation() // 阻止冒泡到窗口层 Escape 关窗监听
                    setEditingId(null)
                  }
                }}
              />
            ) : (
              <span className="sr-cop-tab-title">{s.title}</span>
            )}
            <button
              className="sr-cop-tab-close"
              title="关闭会话"
              aria-label="关闭会话"
              onClick={(e) => { e.stopPropagation(); deleteSession(s.id) }}
            >
              <IconX size={10} aria-hidden />
            </button>
          </div>
        )
      })}
      <Tooltip label="新建会话">
        <button
          className="sr-cop-tab-new" aria-label="新建会话"
          onClick={() => {
            // 上限提示：不再静默丢最旧（newSession 内部仍会淘汰，此处先行告知）
            if (sessions.length >= MAX_SESSIONS) {
              notifications.show({ message: `会话数已达上限 ${MAX_SESSIONS}，最旧会话将被清理`, color: 'yellow' })
            }
            newSession(routeMode)
          }}
        >
          <IconPlus size={12} aria-hidden />
        </button>
      </Tooltip>
    </div>
  )
}

/** 会话聊天面板：Bubble.List 消息 + Sender 输入；直接 SSE 流式对话，LLM 在流内产出筛选命令（action）时执行并跳转。 */
export function CopilotTab() {
  const location = useLocation()
  const navigate = useNavigate()
  const sessions = useCopilotStore((s) => s.sessions)
  const activeId = useCopilotStore((s) => s.activeId)
  const providers = useCopilotStore((s) => s.providers)
  const newSession = useCopilotStore((s) => s.newSession)
  const appendMessage = useCopilotStore((s) => s.appendMessage)
  const appendChunk = useCopilotStore((s) => s.appendChunk)
  const popLastMessage = useCopilotStore((s) => s.popLastMessage)
  const queueCommand = useCopilotStore((s) => s.queueCommand)
  const draft = useCopilotStore((s) => s.draft)
  const setDraft = useCopilotStore((s) => s.setDraft)
  const [input, setInput] = useState('')
  const [phase, setPhase] = useState<'idle' | 'streaming'>('idle')
  const abortRef = useRef<AbortController | null>(null)
  /** 发送中的同步 ref 守卫：setPhase 异步，双击/连点会穿透 useState 守卫导致并发双流 */
  const sendingRef = useRef(false)
  /** 最近一次提问原文（流式错误重试按钮复用） */
  const lastQuestionRef = useRef('')

  // 外部唤起预填（个股「研究此股」）：把「分析 {code}」填入 Sender 待发送，消费后清空草稿
  useEffect(() => {
    if (draft == null) return
    setInput(draft)
    setDraft(null)
  }, [draft, setDraft])

  const routeMode: 'market' | 'research' = isMarketPath(location.pathname) ? 'market' : 'research'
  const active = sessions.find((s) => s.id === activeId) ?? null
  const provider = matchProvider(providers, location.pathname)
  const hasMessages = !!active && active.messages.length > 0
  // 暗色下 --sr-accent(#4c9aff) 上白字仅 2.85:1 → 换深蓝底 + --sr-text-1 文字（亮色保持 accent+白字）
  const isDark = useThemeStore((s) => s.theme) === 'dark'

  /** 前端固定动作执行器：command → 唯一执行动作（映射表承载，模型不允许返回任意路径/执行任意操作）。
   *  仅窗口仍开且会话仍为当前活跃会话才执行：后台流（关窗/切会话）不得入队命令或强制跳转。 */
  const ACTION_HANDLERS: Record<string, (action: CopilotAction, sessionId: string) => void> = {
    'signal_center.apply_filters': (action, sid) => {
      if (action.command !== 'signal_center.apply_filters') return
      // LLM 产出的筛选命令（后端已严格校验）：入队待执行 + 助手消息说明 + 固定导航 /signals
      queueCommand(toCommandPayload(action.filters))
      appendMessage(sid, 'assistant', describeCommand(action))
      navigate('/signals')
    },
    'stock.navigate': (action, sid) => {
      if (action.command !== 'stock.navigate') return
      // 双保险格式校验：后端已校验，前端防旧缓存/异常事件注入任意路径
      if (!/^\d{6}(\.(SH|SZ|BJ))?$/.test(action.code)) return
      appendMessage(sid, 'assistant', describeNavigate(action.code))
      navigate(`/stocks/${action.code}`)
    },
  }

  /** 发送上下文：provider context + Hermes 页面快照（发送瞬间收集，随请求上送） */
  const buildCtx = (): Record<string, unknown> => {
    const base = provider?.context?.() ?? { route: location.pathname, title: adapterLabel(location.pathname) ?? location.pathname }
    return { ...base, page: collectPageSnapshot(location.pathname) }
  }

  /** 核心流式发送：追加空 assistant 占位并启动 SSE 流（ask / retry 共用，调用方负责 sendingRef 守卫）。
   *  mode 固定取当前路由域——请求端点必须与当前路由的 context provider 对齐，
   *  否则旧会话跨域后仍按创建时 mode 发端点，造成「市场路由却发研究端点」的上下文错配。 */
  const runStream = async (
    sessionId: string,
    question: string,
    ctx: Record<string, unknown>,
    history: CopilotHistoryMsg[],
  ) => {
    appendMessage(sessionId, 'assistant', '')
    setPhase('streaming')
    const abort = new AbortController()
    abortRef.current = abort
    const seq = ++streamSeq // 本次流序号：旧流回调/收尾校验非最新即丢弃（重开双流守卫）
    activeAbortCtrl = abort
    try {
      await streamCopilot(routeMode, { question, context: ctx, history }, (chunk) => {
        if (abort.signal.aborted || seq !== streamSeq) return
        // P2-71：只上送增量 delta，store 就地追加到末条消息——不再每 chunk 累计全文回写
        //（消除 O(N²) 字符串复制与消息数组整体替换/复制）
        appendChunk(sessionId, chunk)
      }, abort.signal, (action) => {
        if (abort.signal.aborted || seq !== streamSeq) return
        const st = useCopilotStore.getState()
        if (!st.open || st.activeId !== sessionId) return
        ACTION_HANDLERS[action.command]?.(action, sessionId)
      })
    } catch (e: any) {
      if (seq !== streamSeq) return // 已被新流取代的旧流：丢弃尾部写入，不污染会话
      if (e?.name === 'AbortError') {
        // P2-71：已流式内容已逐块增量写进 store 末条消息，此处只追加尾部标记
        appendChunk(sessionId, '（已停止）')
        return
      }
      const errText = `[错误] ${e?.message ?? e}`
      const last = useCopilotStore.getState().sessions.find((s) => s.id === sessionId)?.messages.at(-1)
      if (last && last.role === 'assistant' && last.content === '') appendChunk(sessionId, errText)
      else appendMessage(sessionId, 'assistant', errText)
    } finally {
      sendingRef.current = false
      if (seq === streamSeq) {
        activeAbortCtrl = null
        abortRef.current = null
        setPhase('idle')
        // 流结束/中止强制落盘最后一段（appendChunk 节流可能吞掉末段），关闭/重开不丢
        useCopilotStore.getState().flush(sessionId)
      }
    }
  }

  const ask = async (q: string) => {
    const question = q.trim()
    if (!question || sendingRef.current) return // 执行中（流式）禁止重复发送：ref 同步守卫防双击并发双流
    sendingRef.current = true
    setInput('')
    let sessionId = activeId
    if (!sessionId || !sessions.some((s) => s.id === sessionId)) {
      if (sessions.length >= MAX_SESSIONS) {
        notifications.show({ message: `会话数已达上限 ${MAX_SESSIONS}，最旧会话将被清理`, color: 'yellow' })
      }
      sessionId = newSession(routeMode)
    }
    lastQuestionRef.current = question
    const ctx = buildCtx()
    // 多轮历史上送：当前问题之前的最近 N 轮会话消息，后端据此追问答
    const history = toHistory(active?.messages ?? [])
    // T-72：user 消息 content 只存问题原文（UI 气泡与持久化均不含上下文）；
    // 发送时刻的上下文摘要存入消息内存态字段 ctxSummary（persist 剥离，不落盘），
    // 后续轮次经 toHistory 拼回上送，模型仍可从历史回溯页面状态。
    const summary = ctxSummary(ctx)
    appendMessage(sessionId, 'user', question, summary || undefined)
    await runStream(sessionId, question, ctx, history)
  }

  /** 流式错误重试：移除失败的错误消息尾巴，重放同一问题（用户提问保留在历史中，不重复追加） */
  const retry = async () => {
    const question = lastQuestionRef.current
    if (!question || sendingRef.current) return
    const st = useCopilotStore.getState()
    const session = st.sessions.find((s) => s.id === st.activeId)
    if (!session) return
    const msgs = session.messages
    const last = msgs[msgs.length - 1]
    // 仅当末条为错误消息才重试（按钮仅在错误末条渲染，此处双保险）
    if (!last || last.role !== 'assistant' || !isErrorText(last.content)) return
    sendingRef.current = true
    popLastMessage(session.id) // 移除错误尾巴；用户提问（ctxSummary 独立字段，经 toHistory 拼回）仍在历史中
    const after = useCopilotStore.getState().sessions.find((s) => s.id === session.id)?.messages ?? []
    const history = toHistory(after.slice(0, -1)) // 排除末条 user 提问（经 question 字段发送）
    const ctx = buildCtx() // 重试时重新采集：context 与页面快照均为最新
    await runStream(session.id, question, ctx, history)
  }

  const stop = () => abortRef.current?.abort()

  const copyText = (t: string) => {
    try {
      navigator.clipboard.writeText(t)
      notifications.show({ message: '已复制', color: 'teal' })
    } catch { /* ignore */ }
  }

  const extractCandidate = (content: string): string | null => {
    const fence = content.match(/```[\s\S]*?```/)
    if (fence) {
      const inner = fence[0].replace(/```/g, '').trim()
      if (inner) return inner
    }
    const withClose = content.match(/[a-z_]+\([^)]*close[^)]*\)/)
    if (withClose) return withClose[0].trim()
    const prefixed = content.match(/(?:rank|ts_|delta)[a-z_]*\([^)]*\)/)
    if (prefixed) return prefixed[0].trim()
    return null
  }

  /** 消息页脚：时间 · token 估算元信息 + 操作按钮（复制 / 重试 / 填入表达式；仅 idle 且助手消息显示操作） */
  const msgFooter = (m: CopilotMsg, isLast: boolean): ReactNode => {
    const text = m.content
    if (!text) return null
    const showActions = phase === 'idle' && m.role === 'assistant'
    const isErr = isErrorText(text)
    const candidate = showActions && !isErr && provider?.onFill ? extractCandidate(text) : null
    return (
      <div className="sr-cop-msg-actions">
        <span className="sr-cop-msg-meta">
          {fmtClock(m.ts)}{m.ts ? ' · ' : ''}约 {estimateTokens(text)} tokens
        </span>
        {showActions && isLast && isErr ? (
          <Button size="xs" variant="default" leftSection={<IconRefresh size={14} aria-hidden />} onClick={() => void retry()}>重试</Button>
        ) : null}
        {showActions && candidate ? (
          <Button size="xs" variant="default" leftSection={<IconPencil size={14} aria-hidden />} onClick={() => provider?.onFill?.(candidate)}>
            填入表达式
          </Button>
        ) : null}
        {showActions ? (
          <Button
            size="compact-xs" variant="subtle" aria-label="复制" className="sr-copy-btn"
            onClick={() => copyText(text)}
          >
            <IconCopy size={11} aria-hidden />
          </Button>
        ) : null}
      </div>
    )
  }

  /** 消息列表：Bubble.List 渲染，流式尾气泡 loading 三点；每消息页脚走 msgFooter（时间/token/操作） */
  const items: BubbleItemType[] = useMemo(() => {
    if (!active) return []
    const msgs = active.messages
    const lastIdx = msgs.length - 1
    return msgs.map((m, i) => {
      const isStreamingTail = phase === 'streaming' && i === lastIdx
        && m.role === 'assistant' && m.content === ''
      return {
        key: `${i}:${m.role}`,
        role: m.role,
        // 折叠 + pre-wrap 段落/换行（T-93 长回复可读性）
        content: <MsgContent text={m.content} />,
        ...(isStreamingTail ? { loading: true } : null),
        footer: msgFooter(m, i === lastIdx),
      }
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, phase, provider])

  /** 角色样式：沿用 var(--sr-*) 令牌；操作按钮已下沉到 items 级 footer（msgFooter） */
  const roles: BubbleListProps['role'] = useMemo(() => ({
    assistant: {
      placement: 'start',
      avatar: (
        <span className="sr-cop-avatar sr-cop-avatar-assistant" aria-hidden>
          <IconRobot size={14} />
        </span>
      ),
      styles: {
        content: {
          background: 'var(--sr-block-bg)',
          color: 'var(--sr-text-1)',
          border: '1px solid var(--sr-border)',
        },
      },
    },
    user: {
      placement: 'end',
      avatar: (
        <span className="sr-cop-avatar" aria-hidden>
          <IconUser size={14} />
        </span>
      ),
      styles: {
        content: {
          /* 用户气泡：亮=accent 实底 + on-accent 白字；暗=--sr-accent-deep 深蓝底（同系，避免 accent 上白字低对比）+ 文字色 */
          background: isDark ? 'var(--sr-accent-deep)' : 'var(--sr-accent)',
          color: isDark ? 'var(--sr-text-1)' : 'var(--sr-on-accent)',
        },
      },
    },
  }), [isDark])

  return (
    <div className="sr-cop-chat">
      {hasMessages ? (
        <Bubble.List className="sr-cop-body" autoScroll items={items} role={roles} />
      ) : (
        <div className="sr-cop-body sr-cop-welcome">
          <Welcome
            variant="borderless"
            icon={<IconRobot size={32} aria-hidden />}
            title="AI 助手"
            description={routeMode === 'market'
              ? '问市场信号，或让我帮你筛选信号中心'
              : '聊研究思路，或让我帮你准备回测 / 因子评估'}
          />
          <Prompts
            title="你可以这样问我"
            items={SUGGESTIONS[routeMode]}
            onItemClick={({ data }) => {
              const q = typeof data.label === 'string' ? data.label : ''
              if (q) ask(q)
            }}
          />
        </div>
      )}
      <Sender
        className="sr-cop-sender"
        value={input}
        onChange={setInput}
        onSubmit={(msg) => ask(msg)}
        onCancel={stop}
        loading={phase === 'streaming'}
        readOnly={phase === 'streaming'}
        placeholder="输入消息…"
        autoSize={{ minRows: 2, maxRows: 6 }}
      />
    </div>
  )
}

/** 抽屉内容：会话标签条 + 聊天面板。
 *  顶层包 antd ConfigProvider：@ant-design/x 组件树（Welcome/Prompts/Sender/Bubble）的
 *  文字/背景默认取 antd token（无 Provider 时固定亮色 → 暗色窗口黑字不可读），
 *  此处按当前主题切换 algorithm + 关键 token 对齐 --sr-* 语义（见 buildAntdToken）。
 *  作用域收在 Copilot 组件树内，不扩大到整个 app（main.tsx 仅 Mantine Provider）。 */
export function ChatPanel() {
  const isDark = useThemeStore((s) => s.theme) === 'dark'
  return (
    <ConfigProvider
      theme={{
        algorithm: isDark ? antdTheme.darkAlgorithm : antdTheme.defaultAlgorithm,
        token: buildAntdToken(isDark),
      }}
    >
      <SessionTabs />
      <CopilotTab />
    </ConfigProvider>
  )
}
