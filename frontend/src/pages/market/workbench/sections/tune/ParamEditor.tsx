import { Badge, Button, NumberInput, Text, Tooltip } from '@mantine/core'
import { IconHelpCircle } from '@tabler/icons-react'
import { useEffect, useState } from 'react'
import type { IndicatorParamValues } from '../../../../../api/client'
import { TUNE_LABEL, useWorkbench } from '../../context'
import { PARAM_HINTS, stepPrecision } from './constants'
import { antdToMantine } from '../../tagColor'

/** 单指标参数编辑块：individual 模式多选时每选中指标一个分区（spec.params 动态 InputNumber 网格）。
 *  空态保持 null（不回填默认），提交/应用时 commit 回退 spec.defaults。 */
function ParamEditorBlock({ ind }: { ind: string }) {
  const c = useWorkbench()
  const {
    tuneScope, specs, effectiveParams, manualParams, globalParams,
    applyManualParams, clearManualParams, saveGlobalParams, resetGlobalParams,
  } = c
  const spec = specs[ind]
  const globalDoc = globalParams[ind]
  const [values, setValues] = useState<Record<string, number | null>>({})
  // 全局默认保存/重置 in-flight（网络 PUT）：请求未返回时按钮 loading + 防并发
  const [globalBusy, setGlobalBusy] = useState<'save' | 'reset' | null>(null)
  const saveGlobal = async () => {
    if (globalBusy) return
    setGlobalBusy('save')
    try { await saveGlobalParams(ind, commit(values)) } finally { setGlobalBusy(null) }
  }
  const resetGlobal = async () => {
    if (globalBusy) return
    setGlobalBusy('reset')
    try { await resetGlobalParams(ind) } finally { setGlobalBusy(null) }
  }

  // 生效参数依赖字符串化：60s 轮询刷新 effectiveParams 引用但值未变时，
  // 不重置表单（保护用户未提交的输入）；仅值真正变化（手动应用/清除/保存全局后）才重灌
  const effectiveStr = spec ? JSON.stringify(effectiveParams[ind] ?? spec.defaults) : ''
  useEffect(() => {
    if (!spec) return
    setValues(JSON.parse(effectiveStr))
  }, [ind, spec, effectiveStr])

  if (!spec) return null
  // 提交：null 回退 spec.defaults（空态不落库）
  const commit = (v: Record<string, number | null>): IndicatorParamValues => {
    const out: IndicatorParamValues = {}
    for (const [k, val] of Object.entries(v)) if (val != null) out[k] = val
    return { ...spec.defaults, ...out }
  }
  return (
    <div className="sr-param-block">
      <div className="sr-param-name">
        {spec.name ?? TUNE_LABEL[ind] ?? ind.toUpperCase()}
        {manualParams[ind] != null && (
          <Badge variant="light" color={antdToMantine('green')} radius="var(--sr-radius-tag)" size="sm"
            style={{ marginInlineStart: 6, marginInlineEnd: 0 }}>已应用 ✓</Badge>
        )}
      </div>
      <div className="sr-param-grid">
        {spec.params.map((p) => {
          const hint = PARAM_HINTS[`${ind}:${p.key}`] ?? PARAM_HINTS[p.key]
          return (
            <div key={p.key} className="sr-param-item">
              <span className="sr-param-label">
                {p.label}
                {hint && (
                  <Tooltip label={hint}>
                    <IconHelpCircle size={11} style={{ color: 'var(--sr-text-3)', marginLeft: 4, cursor: 'help' }} aria-label={`${p.label}说明`} />
                  </Tooltip>
                )}
              </span>
              <NumberInput
                size="xs"
                min={p.min}
                max={p.max}
                step={p.step}
                decimalScale={p.type === 'float' ? stepPrecision(p.step) : 0}
                value={values[p.key] ?? undefined}
                onChange={(v) => setValues((s) => ({ ...s, [p.key]: v === '' ? null : Number(v) }))}
                style={{ width: '100%' }}
              />
              <span className="sr-param-default">系统默认 {p.default}（范围 {p.min}~{p.max}{p.step !== 1 ? `，步长 ${p.step}` : ''}）</span>
            </div>
          )
        })}
      </div>
      <div className="sr-param-actions">
        {/* 恢复默认参数=面板内回填 spec.defaults 不落库；清除手动覆盖=删除 localStorage 手动覆盖（语义分离） */}
        <Button size="xs" variant="default" onClick={() => setValues({ ...spec.defaults })}>恢复默认参数</Button>
        <Button size="xs" variant="default" disabled={!manualParams[ind]} onClick={() => clearManualParams(ind)}>清除手动覆盖</Button>
        {tuneScope === 'global' ? (
          <Button size="xs" variant="filled" loading={globalBusy === 'save'} onClick={() => void saveGlobal()} aria-label="保存为全局默认">保存为全局默认</Button>
        ) : (
          <Button size="xs" variant="filled" onClick={() => applyManualParams(ind, commit(values))} aria-label="应用到当前图表">应用到当前图表</Button>
        )}
        {globalDoc && <Button size="xs" variant="default" color="red" loading={globalBusy === 'reset'} onClick={() => void resetGlobal()}>重置全局</Button>}
      </div>
    </div>
  )
}

/** 指标参数编辑器（individual）：按选中指标分组展示，每指标一个分区（与 tuneInds 联动，多选全可编辑）。
 *  combined 模式不渲染（只显示范围控制）。契约加载失败由面板 Alert 兜底。 */
export function IndicatorParamEditor() {
  const c = useWorkbench()
  const { tuneInds, specs } = c
  if (tuneInds.length === 0) return null
  const inds = tuneInds.filter((ind) => specs[ind])
  if (inds.length === 0) {
    return <Text span size="sm" style={{ color: 'var(--sr-text-3)', display: 'block', marginTop: 'var(--sr-pad-lg)' }}>参数配置加载中…</Text>
  }
  return (
    <div className="sr-param-editor">
      {inds.map((ind) => (
        <ParamEditorBlock key={ind} ind={ind} />
      ))}
    </div>
  )
}
