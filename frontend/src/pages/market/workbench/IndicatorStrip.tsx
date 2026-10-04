import { Button, Popover } from '@mantine/core'
import { IconChartBar, IconChartLine } from '@tabler/icons-react'
import { useState } from 'react'
import { fmtParamValues } from './context'
import type { IndicatorParamValues } from '../../../api/client'
import './IndicatorStrip.css'

// 指标分组（参考东方财富）：趋势 / 摆动 / 量能；dmi/trix/dpo 为副图指标（SUB_KEYS 含），
// 归入「趋向」副图组（主图「趋势」组仅均线/BOLL/SAR 主图叠加）
const INDICATOR_GROUPS: { name: string; opts: { k: string; l: string }[] }[] = [
  { name: '趋势', opts: [
    { k: 'ma5', l: 'MA5' }, { k: 'ma10', l: 'MA10' }, { k: 'ma20', l: 'MA20' }, { k: 'ma30', l: 'MA30' },
    { k: 'ma60', l: 'MA60' }, { k: 'ma120', l: 'MA120' }, { k: 'ma250', l: 'MA250' },
    { k: 'ema12', l: 'EMA12' }, { k: 'ema26', l: 'EMA26' },
    { k: 'expma12', l: 'EXPMA12' }, { k: 'expma50', l: 'EXPMA50' },
    { k: 'boll', l: 'BOLL' }, { k: 'sar', l: 'SAR' },
  ]},
  { name: '趋向', opts: [
    { k: 'dmi', l: 'DMI' }, { k: 'trix', l: 'TRIX' }, { k: 'dpo', l: 'DPO' },
  ]},
  { name: '摆动', opts: [
    { k: 'macd', l: 'MACD' }, { k: 'kdj', l: 'KDJ' }, { k: 'rsi', l: 'RSI' },
    { k: 'wr', l: 'WR' }, { k: 'cci', l: 'CCI' }, { k: 'roc', l: 'ROC' },
    { k: 'mtm', l: 'MTM' }, { k: 'bias', l: 'BIAS' }, { k: 'psy', l: 'PSY' },
    { k: 'cmo', l: 'CMO' },
  ]},
  { name: '量能', opts: [
    { k: 'obv', l: 'OBV' }, { k: 'vr', l: 'VR' }, { k: 'emv', l: 'EMV' }, { k: 'atr', l: 'ATR' },
  ]},
]
// 主图条 = 趋势均线类（叠加在主图上）；副图条 = 摆动/量能等其他副图指标
export const MAIN_GROUPS = [INDICATOR_GROUPS[0]]
export const SUB_GROUPS = INDICATOR_GROUPS.slice(1)

/** 菜单项 field key → 可调优指标 spec key（用于展示实际生效参数） */
const FIELD_TO_SPEC: Record<string, string> = {
  ma5: 'ma', ma10: 'ma', ma20: 'ma', ma30: 'ma', ma60: 'ma', ma120: 'ma', ma250: 'ma',
  ema12: 'ema', ema26: 'ema', expma12: 'ema', expma50: 'ema',
  boll: 'boll', macd: 'macd', kdj: 'kdj', rsi: 'rsi', wr: 'wr', cci: 'cci', roc: 'roc',
  mtm: 'mtm', bias: 'bias', psy: 'psy', trix: 'trix', cmo: 'cmo', volume_ratio: 'volume_ratio',
}

interface MenuButtonProps {
  label: string
  main?: boolean
  groups: typeof INDICATOR_GROUPS
  overlays: string[]
  onToggle: (k: string) => void
  effectiveParams: Record<string, IndicatorParamValues>
  tunedInds: string[]
}

/** 指标分组菜单按钮：Popover 内分组多选（替代原两行小字开关条，避免窄栏挤压） */
export function IndicatorMenuButton({ label, main, groups, overlays, onToggle, effectiveParams, tunedInds }: MenuButtonProps) {
  const [open, setOpen] = useState(false)
  const active = groups.flatMap((g) => g.opts).filter((o) => overlays.includes(o.k))
  const Icon = main ? IconChartLine : IconChartBar
  return (
    <Popover
      opened={open}
      onChange={setOpen}
      position="bottom-start"
    >
      <Popover.Target>
        {/* Mantine 9 受控 Popover 需在 Target 手动 onClick 切换(库仅对非受控自动注入 toggle) */}
        <Button size="xs" leftSection={<Icon size={14} />} className={active.length > 0 ? 'sr-ind-menu-trigger-active' : ''} onClick={() => setOpen((o) => !o)}>
          <span>{label}{active.length > 0 ? ` ${active.length}` : ''}</span>
        </Button>
      </Popover.Target>
      <Popover.Dropdown>
        <div className="sr-ind-menu" role="group" aria-label={label + '指标选择'}>
          <div className="sr-ind-menu-title">
            {label}指标
            {active.length > 0 && <span className="sr-ind-menu-count">{active.length}</span>}
          </div>
          {groups.map((g) => (
            <div key={g.name}>
              <div className="sr-ind-menu-group">{g.name}</div>
              <div className="sr-ind-menu-grid">
                {g.opts.map((o) => {
                  const on = overlays.includes(o.k)
                  const specKey = FIELD_TO_SPEC[o.k]
                  const tuned = specKey && tunedInds.includes(specKey) && effectiveParams[specKey]
                    ? fmtParamValues(effectiveParams[specKey])
                    : undefined
                  return (
                    <div
                      key={o.k}
                      className={'sr-ind-menu-item' + (on ? ' sr-ind-menu-on' : '')}
                      role="checkbox"
                      aria-checked={on}
                      tabIndex={0}
                      onClick={() => onToggle(o.k)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault()
                          onToggle(o.k)
                        }
                      }}
                    >
                      <span className="sr-ind-menu-box"><i className="sr-ind-menu-dot" aria-hidden="true" /></span>
                      <span className="sr-ind-menu-label">{o.l}</span>
                      {tuned && <span className="sr-ind-menu-tuned" title={`已调优 ${tuned}`}>{tuned}</span>}
                    </div>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      </Popover.Dropdown>
    </Popover>
  )
}
