import { Alert, Badge, Button, MultiSelect, NumberInput, Progress, SegmentedControl, Select, Text, Tooltip } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconAlertTriangle, IconFlask, IconHelpCircle, IconInfoCircle } from '@tabler/icons-react'
import { useWorkbench, fmtParamValues, TUNE_LABEL } from '../../context'
import { antdToMantine } from '../../tagColor'
import { ALGO_OPTIONS, TARGET_GROUPS_DATA } from './constants'
import { IndicatorParamEditor } from './ParamEditor'
import { TuneResultsPanel } from './ResultsPanel'
import { ClearAllTuneButton } from './ClearAll'
import './tune.css'

/** 指标调优（桌面 inspector 与 <992 Tabs 内完整可用）：多选指标 / 整体优化 / 应用范围三态 / 对比总表 */
export function TuneSection() {
  const c = useWorkbench()
  const {
    tuneMode, setTuneMode, tuneScope, setTuneScope,
    tuneInds, setTuneInds, tuneOptions,
    tuneHorizon, setTuneHorizon, tuneTarget, setTuneTarget, tuneAlgorithm, setTuneAlgorithm,
    tuneProgress, tuning, cancelling, runTune, cancelTune,
    tunedInds, effectiveParams,
    tuneCatalogErr, tuneContractErr, retryTuneContract,
  } = c

  return (
    <div className="sr-tune-panel">
      {/* 边界说明（P1-50）：调优与信号扫描互不相干——调优只改 K 线图指标显示，
          扫描信号参数由模板条件决定。Drawer 侧栏打开即见，避免「改了没生效」误解 */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 6, marginBottom: 8, minWidth: 0 }}>
        <IconInfoCircle size={14} style={{ color: 'var(--sr-text-3)', marginTop: 2, flexShrink: 0 }} />
        <Text span size="xs" style={{ color: 'var(--sr-text-3)', lineHeight: 1.6 }}>
          调优只影响 K 线图指标显示，不影响信号扫描（信号参数由模板条件决定）
        </Text>
      </div>
      <div className="sr-tune-head">
        <span className="sr-tune-title">
          <IconFlask size={14} style={{ color: 'var(--sr-accent)' }} />
          <Text span fw={600}>指标调优</Text>
        </span>
        <div className="sr-tune-eff">
          <span className="sr-tune-eff-label">生效参数</span>
          {tunedInds.length
            ? tunedInds.map((ind) => (
              <Badge key={ind} variant="light" color={antdToMantine('blue')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">
                {TUNE_LABEL[ind] ?? ind.toUpperCase()} = {fmtParamValues(effectiveParams[ind])}
              </Badge>
            ))
            : <Badge variant="light" color={antdToMantine('default')} radius="var(--sr-radius-tag)" size="sm" className="sr-tune-tag">无</Badge>}
        </div>
        <div className="sr-tune-head-actions">
          <ClearAllTuneButton />
        </div>
      </div>

      {/* 契约加载失败显性化：/indicators/catalog 与 /indicators/params 失败 → Alert（错误信息 + 重试），不再静默「加载中」 */}
      {(tuneCatalogErr || tuneContractErr) && (
        <div className="sr-tune-alerts">
          {tuneCatalogErr && (
            <Alert color="yellow" icon={<IconAlertTriangle size={16} />} title="调优指标目录加载失败">
              {tuneCatalogErr}
              <Button size="xs" variant="default" onClick={retryTuneContract} aria-label="重试加载指标目录" style={{ marginTop: 8 }}>重试</Button>
            </Alert>
          )}
          {tuneContractErr && (
            <Alert color="yellow" icon={<IconAlertTriangle size={16} />} title="指标参数契约加载失败">
              {tuneContractErr}
              <Button size="xs" variant="default" onClick={retryTuneContract} aria-label="重试加载参数契约" style={{ marginTop: 8 }}>重试</Button>
            </Alert>
          )}
        </div>
      )}

      <div className="sr-tune-controls">
        <div className="sr-tune-field sr-tune-seg-field">
          <span className="sr-tune-label">优化模式</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexWrap: 'wrap' }}>
            <SegmentedControl
              size="xs"
              value={tuneMode}
              onChange={(v) => setTuneMode(v as 'individual' | 'combined')}
              data={[
                { value: 'individual', label: '逐个优化' },
                { value: 'combined', label: '整体优化' },
              ]}
              aria-label="优化模式"
              style={{ height: 'var(--sr-ctl-h)' }}
              styles={{ label: { height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' } }}
            />
            <Tooltip label="整体优化：≤3 个指标参数组合联合评分">
              <IconHelpCircle size={11} style={{ color: 'var(--sr-text-3)', cursor: 'help' }} aria-label="优化模式说明" />
            </Tooltip>
          </div>
        </div>
        <div className="sr-tune-field sr-tune-seg-field">
          <span className="sr-tune-label">应用范围</span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
            <Tooltip label="本图表：仅此浏览器、此股票生效；全部股票：写入后端全局指标默认">
              <SegmentedControl
                size="xs"
                value={tuneScope}
                onChange={(v) => setTuneScope(v as 'chart' | 'global')}
                data={[
                  { value: 'chart', label: '本图表' },
                  { value: 'global', label: '全部股票' },
                ]}
                aria-label="应用范围"
                style={{ height: 'var(--sr-ctl-h)' }}
                styles={{ label: { height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' } }}
              />
            </Tooltip>
            {tuneScope === 'global' && <Badge variant="light" color={antdToMantine('orange')} radius="var(--sr-radius-tag)" size="sm">影响所有股票</Badge>}
          </div>
        </div>
        <label className="sr-tune-field sr-tune-field-ind">
          <span className="sr-tune-label">
            指标
            {tuneMode === 'combined' && (
              <Tooltip label="整体优化最多选择 3 个指标">
                <IconHelpCircle size={11} style={{ color: 'var(--sr-text-3)', marginLeft: 4, cursor: 'help' }} aria-label="整体优化指标数量限制" />
              </Tooltip>
            )}
          </span>
          <MultiSelect
            size="xs"
            value={tuneInds}
            onChange={(v) => {
              // 整体优化受控裁剪 ≤3：超出时自动移除最早选择的指标并提示（不再静默截断）
              if (tuneMode === 'combined' && v.length > 3) {
                notifications.show({ color: 'yellow', message: '整体优化最多 3 个指标' })
                setTuneInds(v.slice(-3))
                return
              }
              setTuneInds(v)
            }}
            data={tuneOptions}
            searchable
            clearable
            placeholder={tuneMode === 'combined' ? '选择 2~3 个指标整体调优' : '选择要调优的指标'}
            style={{ width: '100%', minWidth: 0 }}
            aria-label="选择调优指标"
          />
          {tuneMode === 'combined' && tuneInds.length < 2 && (
            <Text span size="xs" style={{ color: 'var(--sr-warning)', marginTop: 2 }}>需选择 2~3 个指标</Text>
          )}
        </label>
        <label className="sr-tune-field">
          <span className="sr-tune-label">目标</span>
          <Select
            size="xs"
            value={tuneTarget}
            onChange={(v) => { if (v) setTuneTarget(v) }}
            data={TARGET_GROUPS_DATA}
            searchable
            style={{ width: '100%', minWidth: 0 }}
          />
        </label>
        <label className="sr-tune-field">
          <span className="sr-tune-label">
            评估周期（未来N日收益）
            <Tooltip label="IC 评估的是指标对未来 N 日收益的预测能力">
              <IconHelpCircle size={11} style={{ color: 'var(--sr-text-3)', marginLeft: 4, cursor: 'help' }} aria-label="评估周期说明" />
            </Tooltip>
          </span>
          <NumberInput
            size="xs"
            min={1}
            max={20}
            value={tuneHorizon ?? undefined}
            onChange={(v) => setTuneHorizon(v === '' ? null : Number(v))}
            style={{ width: '100%' }}
          />
        </label>
        <label className="sr-tune-field">
          <span className="sr-tune-label">算法</span>
          <Select
            size="xs"
            value={tuneAlgorithm}
            onChange={(v) => { if (v) setTuneAlgorithm(v as 'grid' | 'random' | 'fast') }}
            data={ALGO_OPTIONS}
            style={{ width: '100%', minWidth: 0 }}
          />
        </label>
        <div className="sr-tune-run">
          <Button
            variant="filled"
            size="xs"
            loading={tuning}
            disabled={tuneInds.length === 0 || tuning || (tuneMode === 'combined' && tuneInds.length < 2)}
            onClick={runTune}
            aria-label="运行调优"
            className="sr-tune-run-btn"
          >
            调优({tuneInds.length})
          </Button>
          {tuning && (
            <Button size="xs" variant="default" disabled={cancelling} onClick={cancelTune} aria-label="取消调优" className="sr-tune-run-btn">
              {cancelling ? '正在取消…' : '取消'}
            </Button>
          )}
        </div>
      </div>

      {tuneProgress && (
        <div className="sr-tune-progress" aria-label="调优进度">
          <Progress
            value={tuneProgress.total > 0 ? Math.round((tuneProgress.done / tuneProgress.total) * 100) : 0}
            size="sm"
            style={{ flex: 1, minWidth: 80, margin: 0 }}
          />
          <span>
            正在调优 {tuneProgress.current === '__combined__' ? '整体组合' : (TUNE_LABEL[tuneProgress.current] ?? tuneProgress.current)} {tuneProgress.done}/{tuneProgress.total}
          </span>
        </div>
      )}

      {/* 参数编辑器：individual 模式按选中指标分组展示（combined 模式不显示逐参数编辑器，只保留范围控制） */}
      {tuneMode === 'individual' && !tuneContractErr && (
        <div className="sr-tune-editor">
          <IndicatorParamEditor />
        </div>
      )}

      <TuneResultsPanel />
    </div>
  )
}
