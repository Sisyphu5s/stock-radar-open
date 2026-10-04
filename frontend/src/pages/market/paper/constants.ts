/** 模拟盘共享常量：创建向导 / 编辑表单 / 监控 / 运行态共用，单一事实源 */
import { PERIOD_OPTIONS } from '../../../utils/periods'

/** 监控轮询间隔（ms）：watch 项目只读轮询周期，全项目唯一事实源 */
export const WATCH_INTERVAL_MS = 15000

/** 监控轮询间隔 UI 文案：唯一事实源（禁止在组件内硬编码「每 15 秒」） */
export const WATCH_INTERVAL_TEXT = '每 15 秒'

/** 股票代码合法性（创建向导 ProjectWizard 与编辑表单 ParamSummary 共用，防双处重复漂移） */
export const PAPER_CODE_RE = /^[\dA-Za-z.]{6,12}$/

/**
 * 模拟盘周期白名单：后端 paper 仅支持 1/5/15/30/60 分钟与日线（周/月由日线重采样、
 * 与逐 bar 收益口径不一致，VALID_PERIODS 不支持），全量 PERIOD_OPTIONS 里过滤掉周/月。
 */
export const PAPER_PERIOD_OPTIONS = PERIOD_OPTIONS.filter((o) => o.value !== 'weekly' && o.value !== 'monthly')
