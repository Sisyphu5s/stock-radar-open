/**
 * 持久化存储键注册表（P2-20，起步版）——localStorage / sessionStorage 键的单一事实源。
 *
 * 规则：
 * 1. 所有跨文件引用、或跨会话存活的持久化键（完整键或前缀）必须登记为下方常量并引用，
 *    禁止在业务代码散落 'sr-xxx' 字符串。
 * 2. 完整键直接引用 STORAGE_KEYS 常量；前缀型键引用 KEY_PREFIXES 常量（键 = 前缀 + 动态段）。
 * 3. 新建键一律经 buildKey() 生成（带键级版本号，破坏性迁移时 +1，旧键读取须保留兼容）；
 *    既有键禁止经 buildKey 重造——值变更会丢用户存量数据，直接引用常量。
 * 4. 值内带版本的前缀型键（sr-cache: / sr-ind-manual:v2 等）由值内版本字段管理，键级版本不适用。
 *
 * 待迁移清单（页面私有键，仅登记不迁移；迁移完成即落为 STORAGE_KEYS 常量并从清单移除）：
 * - workbench/persist.ts：sr-ind-manual:v2、sr-wb-split、sr-wb-hsplit、sr-wb-tune-scope、
 *   wb:tune-scope（legacy）、sr-tuned-*（前缀，历史记忆迁移用）
 * - workbench/useWorkbenchData.ts：sr-tuned-*（前缀，清残留）
 * - signalCenter：sr-signal-schemes（useSignalSchemes.ts）、sr-signal-dir（context.ts，sessionStorage）
 * - screener/useScreenerSchemes.ts：sr-screener-schemes
 * - PaperTrading.tsx：sr-paper-watching
 * - research/tasks/taskPrefs.ts：sr-tasks-*（前缀）
 * - FactorWorkbench.tsx：sr-research-last-tab（sessionStorage）
 * - watchlist/WatchlistFloat.tsx：sr-wl-float-collapsed
 * - utils/notify.ts：sr-notify-enabled、sr-notify-sent-id
 * - utils/useLastDataset.ts：sr-last-dataset
 * - discovery/resultCache.ts：sr-last-gp-result
 * - utils/compare.ts：sr-compare-recent
 * - MainLayout.tsx：sr-nav-collapsed-v2、sr-exit-requested
 * - kline/klineDrawing.ts：sr-kline-tools:*（前缀）
 * - utils/debugLayout.ts：sr-debug-mode、sr-debug（legacy）
 * - NeuralStudy.tsx：nn:state（sessionStorage）、discovery/useFmForm.ts：fm:state（sessionStorage）
 * - useFloatWindow / InfiniteScrollToggle / ListInfinite：键由调用方动态传入，不入注册表
 */

/** 命名空间前缀：所有键统一 sr- 开头 */
export const STORAGE_NAMESPACE = 'sr'

/** 键级版本（新建键默认带 v1 后缀；破坏性迁移时 +1，旧键读取须保留兼容） */
export const KEY_VERSION = 1

/** 构造带键级版本号的持久化键：sr-{domain}-{name}-v{version}。仅用于新建键，禁止重造既有键。 */
export function buildKey(domain: string, name: string, version: number = KEY_VERSION): string {
  return `${STORAGE_NAMESPACE}-${domain}-${name}-v${version}`
}

/** 完整键常量（值即键名，改动 = 用户存量数据丢失） */
export const STORAGE_KEYS = {
  /** 主题模式（light/dark，T-47 暗色默认）：设置页主题偏好，localStorage；index.html 预置脚本同构读取防白闪 */
  theme: 'sr-theme',
  /** 密度模式（compact/comfort）：设置页布局偏好，localStorage */
  density: 'sr-density',
  /** Copilot 会话列表（JSON 数组，上限 MAX_SESSIONS=20）：Copilot 面板，localStorage */
  copilotSessions: 'sr-copilot-sessions',
  /** 关注提醒拉取游标（事件 id，=拉取水位）：关注提醒浮窗增量轮询，localStorage */
  watchlistNotifyCursor: 'sr-watchlist-notify-cursor',
  /** 关注提醒已读水位（事件 id，=UI 水位，与游标解耦）：关注提醒浮窗，localStorage */
  watchlistSeenId: 'sr-wl-seen-id',
} as const

/** 前缀型键常量（键 = 前缀 + 动态段） */
export const KEY_PREFIXES = {
  /** 跨会话持久缓存（sr-cache: + key）：api/client.ts persistedGet 唯一授权跨会话持久通道，
   *  仅限低频只读资源（迷你图 kline 等），轮询资源禁走；localStorage */
  persistCache: 'sr-cache:',
  /** 会话内通用状态（sr-state: + key）：utils/stateMemory.ts usePersistentState，sessionStorage */
  sessionState: 'sr-state:',
} as const
