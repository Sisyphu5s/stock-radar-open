/** 统一数值格式化：全项目数字呈现一致（量化终端规范）。 */
import { formatFullTime } from './time'

/** 百分比：0.1523 → '15.23%' */
export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return (v * 100).toFixed(digits) + '%'
}

/** 稳定性（0~1）：0.65 → '65%' */
export function fmtStab(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return (v * 100).toFixed(digits) + '%'
}

/** 原始比例（IC/换手等）：0.0123 → '0.0123'，恒 4 位 */
export function fmtRatio(v: number | null | undefined, digits = 4): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return v.toFixed(digits)
}

/** 千分位数字：15263.5 → '15,263.5' */
export function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return v.toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: digits })
}

/** 金额万/亿自适应：1.5263e12 → '1.53万亿' */
export function fmtAmount(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const abs = Math.abs(v)
  if (abs >= 1e12) return (v / 1e12).toFixed(2) + '万亿'
  if (abs >= 1e8) return (v / 1e8).toFixed(2) + '亿'
  if (abs >= 1e4) return (v / 1e4).toFixed(2) + '万'
  return v.toLocaleString('zh-CN', { maximumFractionDigits: 0 })
}

/** 金额带单位：raw 串已带 万亿/亿/万 后缀则原样保留（接口原始语义），否则按数值自适应（元 → 万/亿/万亿） */
export function fmtMoneyWithUnit(num: number, raw: string | number): string {
  if (typeof raw === 'string' && /[万亿万元]+$/.test(raw.trim())) return raw
  return fmtAmount(num)
}

/** 胜率格式化：后端可能返回 0~1 小数或直接百分比原值，展示层自适应（|v|>1 视为百分比原值，否则按比例） */
export function fmtWinRate(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return Math.abs(v) > 1 ? v.toFixed(1) + '%' : fmtPct(v, 1)
}

/** 历史任务时间展示：naive ISO 字符串直解（禁 new Date 按浏览器时区解析），空值兜底 '—' */
export function fmtTime(iso?: string | null): string {
  return iso ? formatFullTime(iso, { withYear: false, withSeconds: false }) : '—'
}

/** 数字化容错：number 原样；非空字符串转 number；其余 null */
export function toNum(v: unknown): number | null {
  return typeof v === 'number' ? v
    : typeof v === 'string' && v.trim() !== '' ? Number(v)
    : null
}

/** 涨跌百分比着色：正 → 涨红（var(--sr-up)）、负 → 跌绿（var(--sr-down)）、零/非法 → 中性文字色；
 *  返回 CSS var() 字符串，供表格/统计数值着色（暗色自动提亮） */
export function pctColor(v: number | null | undefined): string {
  if (v == null || Number.isNaN(v) || v === 0) return 'var(--sr-text-2)'
  return v > 0 ? 'var(--sr-up)' : 'var(--sr-down)'
}

/** 接口错误提取：优先后端 detail（如 FastAPI 校验信息），其次 e.message，最后 String(e) 兜底 */
export function errMsg(e: unknown): string {
  const r = e as { response?: { data?: { detail?: string } }; message?: string }
  return r?.response?.data?.detail ?? r?.message ?? String(e)
}
