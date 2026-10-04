import { create } from 'zustand'
import { STORAGE_KEYS } from '../utils/storageKeys'

export interface CopilotMsg {
  role: 'user' | 'assistant'
  content: string
  /** 客户端本地生成时刻（毫秒）；旧持久化会话可能缺失 */
  ts?: number
  /** 发送时刻的 LLM 上下文摘要（仅 user 消息、仅内存态，T-72）：
   *  不随 UI 气泡显示、不落 localStorage（persist 统一剥离）；
   *  仅用于多轮历史上送时拼回 content，供后端追上下文。 */
  ctxSummary?: string
}

export interface CopilotSession {
  id: string
  mode: 'market' | 'research'
  title: string
  messages: CopilotMsg[]
  ts: number
}

export interface CtxProvider {
  key: string
  context: () => Record<string, unknown>
  onFill?: (expr: string) => void
}

/** 信号中心应用筛选命令的载荷（camelCase，与信号中心页面状态同名；replace 语义，未携带字段=清空旧值）。 */
export type SignalCenterCommandPayload = {
  period?: string
  timeRange?: string
  excludeToday?: boolean
  sectors?: string[]
  signals?: string[]
  match?: 'any' | 'all'
  watchlist?: boolean
}

/** 一次性待执行命令（非持久化）：copilot 面板 queue，信号中心页面消费后清除。
 *  不入 sessions 持久化结构，刷新/重开即丢。 */
export interface SignalCenterCommand {
  id: string
  createdAt: number
  /** 命令类型：目前仅信号中心应用筛选 */
  type: 'signal_center.apply_filters'
  payload: SignalCenterCommandPayload
}

const STORAGE_KEY = STORAGE_KEYS.copilotSessions
/** 会话上限：超出后静默淘汰最旧（调用方负责提示） */
export const MAX_SESSIONS = 20
const PERSIST_THROTTLE_MS = 2000
const PERSIST_THROTTLE_LEN_DELTA = 500

let lastPersistTs = 0
let lastPersistLen = 0

function loadSessions(): CopilotSession[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((s): s is CopilotSession => (
        !!s && typeof s === 'object'
        && typeof (s as CopilotSession).id === 'string'
        && ((s as CopilotSession).mode === 'market' || (s as CopilotSession).mode === 'research')
        && Array.isArray((s as CopilotSession).messages)
        && typeof (s as CopilotSession).title === 'string'
      ))
      .slice(-MAX_SESSIONS)
  } catch {
    return []
  }
}

function persist(sessions: CopilotSession[]) {
  // T-72：持久化剥离 ctxSummary——上下文摘要仅内存态（多轮历史上送用），
  // localStorage 只留问题文本，避免页面状态明文落盘。
  const clean = sessions.map((s) => ({
    ...s,
    messages: s.messages.map((m) => ({
      role: m.role,
      content: m.content,
      ...(m.ts !== undefined ? { ts: m.ts } : {}),
    })),
  }))
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(clean)) } catch { /* ignore */ }
}

function genId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return Date.now().toString(36) + Math.random().toString(36).slice(2)
}

const initialSessions = loadSessions()

interface CopilotState {
  sessions: CopilotSession[]
  activeId: string | null
  open: boolean
  providers: Record<string, CtxProvider>
  /** 非持久化一次性待执行命令（信号中心操作）；消费后置 null */
  pendingCommand: SignalCenterCommand | null
  /** 外部唤起（个股「研究此股」）预填 Sender 的输入草稿；面板消费后置 null */
  draft: string | null
  setDraft: (text: string | null) => void
  openPanel: () => void
  closePanel: () => void
  toggle: () => void
  newSession: (mode: 'market' | 'research') => string
  selectSession: (id: string) => void
  deleteSession: (id: string) => void
  renameSession: (id: string, title: string) => void
  clearSession: (id: string) => void
  appendMessage: (id: string, role: CopilotMsg['role'], content: string, ctxSummary?: string) => void
  /** 增量追加流式内容到会话最后一条 assistant 消息（P2-71：delta 为增量片段，非累计全文） */
  appendChunk: (id: string, delta: string) => void
  /** 强制落盘指定会话（流结束/中止后调用，避免节流吞掉最后一段） */
  flush: (id: string) => void
  /** 移除会话最后一条消息（重试前清理失败的错误消息；空会话忽略） */
  popLastMessage: (id: string) => void
  /** 队列一条信号中心命令（覆盖未消费的旧命令，latest wins；不落盘） */
  queueCommand: (payload: SignalCenterCommandPayload) => void
  /** 消费并清除一次性命令（消费方读取 pendingCommand 后调用） */
  consumeCommand: () => void
  registerProvider: (key: string, provider: CtxProvider) => void
  unregisterProvider: (key: string) => void
}

export const useCopilotStore = create<CopilotState>((set, get) => ({
  sessions: initialSessions,
  activeId: initialSessions.length ? initialSessions[initialSessions.length - 1].id : null,
  open: false,
  providers: {},
  pendingCommand: null,
  draft: null,
  setDraft: (text) => set({ draft: text }),
  openPanel: () => set({ open: true }),
  closePanel: () => set({ open: false }),
  toggle: () => set((st) => ({ open: !st.open })),
  newSession: (mode) => {
    const id = genId()
    set((st) => {
      const sessions = [...st.sessions, {
        id, mode, title: mode === 'market' ? '市场会话' : '研究会话',
        messages: [], ts: Date.now(),
      }].slice(-MAX_SESSIONS)
      persist(sessions)
      return { sessions, activeId: id }
    })
    return id
  },
  selectSession: (id) => set((st) => (st.sessions.some((s) => s.id === id) ? { activeId: id } : st)),
  deleteSession: (id) => set((st) => {
    const sessions = st.sessions.filter((s) => s.id !== id)
    persist(sessions)
    return {
      sessions,
      activeId: st.activeId === id ? (sessions.length ? sessions[sessions.length - 1].id : null) : st.activeId,
    }
  }),
  renameSession: (id, title) => set((st) => {
    const t = title.trim()
    if (!t) return st
    const sessions = st.sessions.map((s) => (s.id === id ? { ...s, title: t } : s))
    persist(sessions)
    return { sessions }
  }),
  clearSession: (id) => set((st) => {
    const sessions = st.sessions.map((s) => (s.id === id ? { ...s, messages: [], ts: Date.now() } : s))
    persist(sessions)
    return { sessions }
  }),
  appendMessage: (id, role, content, ctxSummary) => set((st) => {
    const sessions = st.sessions.map((s) => (
      s.id === id ? { ...s, messages: [...s.messages, { role, content, ts: Date.now(), ...(ctxSummary ? { ctxSummary } : {}) }], ts: Date.now() } : s
    ))
    persist(sessions)
    return { sessions }
  }),
  /** 增量追加流式内容（P2-71：消除「每 chunk 全量写回」的 O(N²) 字符串复制）：
   *  - delta 为增量片段，状态只就地重建末条消息对象，不再整体复制消息数组（`[...s.messages]`）；
   *  - 消息数组就地改末元素：外层 sessions 数组/session 对象仍为全新引用，React 依赖外层引用渲染，
   *    末条消息唯一可变对象，就地更新不破坏其余消息引用；
   *  - 首块前无 assistant 占位时新建消息；
   *  - 持久化节流按末条累计长度（增量流下等价于旧「累计全文长度」判定）。 */
  appendChunk: (id, delta) => set((st) => {
    let len = 0
    const sessions = st.sessions.map((s) => {
      if (s.id !== id) return s
      const msgs = s.messages
      const last = msgs[msgs.length - 1]
      if (last && last.role === 'assistant') {
        const content = last.content + delta
        len = content.length
        msgs[msgs.length - 1] = { role: 'assistant', content, ts: last.ts }
      } else {
        len = delta.length
        msgs.push({ role: 'assistant', content: delta, ts: Date.now() })
      }
      return { ...s, messages: msgs, ts: Date.now() }
    })
    const now = Date.now()
    if (now - lastPersistTs >= PERSIST_THROTTLE_MS || Math.abs(len - lastPersistLen) > PERSIST_THROTTLE_LEN_DELTA) {
      lastPersistTs = now
      lastPersistLen = len
      persist(sessions)
    }
    return { sessions }
  }),
  flush: (id) => {
    // 写入的是当前已同步的状态（appendChunk 为同步 set），节流不落盘的末段在此补写
    const { sessions } = get()
    if (!id || sessions.some((s) => s.id === id)) persist(sessions)
  },
  popLastMessage: (id) => set((st) => {
    const sessions = st.sessions.map((s) => {
      if (s.id !== id || !s.messages.length) return s
      return { ...s, messages: s.messages.slice(0, -1), ts: Date.now() }
    })
    persist(sessions)
    return { sessions }
  }),
  queueCommand: (payload) => set({
    pendingCommand: {
      id: genId(), createdAt: Date.now(),
      type: 'signal_center.apply_filters', payload,
    },
  }),
  consumeCommand: () => set({ pendingCommand: null }),
  registerProvider: (key, provider) => set((st) => ({
    providers: { ...st.providers, [key]: { ...provider, key } },
  })),
  unregisterProvider: (key) => set((st) => {
    const providers = { ...st.providers }
    delete providers[key]
    return { providers }
  }),
}))
