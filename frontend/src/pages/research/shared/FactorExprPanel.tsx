import { Button, Select, Text, Textarea } from '@mantine/core'
import { IconBolt } from '@tabler/icons-react'
import { lazy, Suspense } from 'react'
import type { ReactNode } from 'react'
import FormulaCode from '../../../components/FormulaCode'
import LatexFormula from '../../../components/LatexFormula'

/**
 * 表达式 IDE（CodeMirror 6，T-18）：动态 import 拆独立 chunk，首屏不加载 CodeMirror；
 * 加载前回退原生 Textarea（同契约渐进增强）。
 */
const ExprEditor = lazy(() =>
  import('../../../components/ui/ExprEditor').then((m) => ({ default: m.ExprEditor })),
)

interface FactorExprPanelProps {
  expression: string
  onExpression: (v: string) => void
  factors: { name: string; expression: string }[]
  examples: string[]
  /** TextArea 占位文案（页面可定制） */
  textareaPlaceholder?: string
  /** 面板顶部定制块（如回测页「公式因子/神经网络因子」来源切换） */
  header?: ReactNode
  /** 定制内容块：存在时替换默认的 Select+TextArea+示例（如神经网络来源选择） */
  children?: ReactNode
}

/**
 * 因子表达式面板（因子库选择 + 手输/示例按钮）：Backtest/Evaluation 配置页共用。
 * header/children 插槽承载页面特有来源切换（SegmentedControl + 来源内容），交互语义不变。
 */
export function FactorExprPanel(p: FactorExprPanelProps) {
  return (
    <div className="sr-run-panel">
      <div className="sr-run-panel-title">因子表达式</div>
      {p.header}
      {p.children ? p.children : (
        <>
          <Select
            searchable
            clearable
            className="sr-ctl-h"
            placeholder="从因子库选择因子"
            data={p.factors.map((f) => ({ value: f.expression, label: `${f.name} — ${f.expression}` }))}
            onChange={(v) => { if (v != null) p.onExpression(v) }}
          />
          <div style={{ marginTop: 'var(--sr-pad-md)' }}>
            <Suspense fallback={(
              <Textarea
                value={p.expression}
                onChange={(e) => p.onExpression(e.target.value)}
                placeholder={p.textareaPlaceholder}
                autosize
                minRows={2}
                maxRows={4}
                style={{ fontSize: 'var(--sr-font-page)', fontFamily: 'monospace' }}
              />
            )}>
              <ExprEditor
                value={p.expression}
                onChange={p.onExpression}
                placeholder={p.textareaPlaceholder}
                minHeight="3.2em"
              />
            </Suspense>
          </div>
          <div style={{ marginTop: 'var(--sr-pad-md)', display: 'flex', alignItems: 'center', gap: 'var(--sr-pad-sm)', flexWrap: 'wrap' }}>
            <Text c="dimmed" style={{ fontSize: 'var(--sr-font-sm)' }}>示例: </Text>
            {p.examples.map((e) => (
              <Button
                key={e} size="xs" variant="default" style={{ fontFamily: 'monospace', fontSize: 'var(--sr-font-xs)', maxWidth: '100%' }} onClick={() => p.onExpression(e)}
              >
                {/* T-127:按钮内公式 nowrap 超长溢出窄容器——内层 span 承接省略号裁剪 */}
                <span style={{ display: 'block', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '100%' }}>
                  <FormulaCode expr={e} />
                </span>
              </Button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

interface LatexPreviewPanelProps {
  /** 最终待渲染 LaTeX（父组件算好 preview/result 合并后的值） */
  tex: string | null
}

/** LaTeX 公式渲染预览面板（Backtest/Evaluation 配置页共用） */
export function LatexPreviewPanel({ tex }: LatexPreviewPanelProps) {
  return (
    <div className="sr-run-panel">
      <div className="sr-run-panel-title">
        <IconBolt style={{ color: 'var(--sr-accent)' }} />
        公式渲染 (LaTeX)
      </div>
      <LatexFormula tex={tex} block />
      {!tex && (
        <Text style={{ margin: 0, textAlign: 'center', color: 'var(--sr-text-2)', fontSize: 'var(--sr-font-sm)' }}>
          暂无公式预览
        </Text>
      )}
    </div>
  )
}
