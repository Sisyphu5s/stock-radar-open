/**
 * 表达式 IDE（CodeMirror 6 封装，T-18）
 * ------------------------------------------------------------
 * 轻量受控编辑器，能力边界：
 *  - 语法高亮：StreamLanguage 自定义 tokenizer——算子 → operator 色（accent 蓝）、
 *    特征列（open/high/low/close/volume/amount/pct_change）→ atom 色（公式紫），
 *    数字/括号/运算符号保持默认文本色；色板走设计令牌（semantic + FormulaCode 同源紫），
 *    随 useThemeStore 的 isDark 切换（Compartment reconfigure，保留文档与光标）。
 *  - 算子补全：输入字母触发，补全项 = 算子（insert 带左括号）+ 特征列。
 *  - 括号匹配：bracketMatching + closeBrackets（自动补右括号/配对删除）。
 *  - 错误文本不重复：校验/错误展示仍走现有链路（宿主 useLatexPreview → LatexPreviewPanel），
 *    本组件只做高亮 + 补全 + 括号匹配，避免双份校验。
 *  - 包体：本组件经 React.lazy 动态 import（见 FactorExprPanel），CodeMirror 不进主 chunk。
 *
 * 半受控契约：外部 value 变化（示例按钮 / URL 恢复 / 历史任务恢复）经 effect 回灌编辑器；
 * 用户输入经 updateListener → onChange 上行，与宿主原 Textarea 的 value/onChange 契约一致。
 */
import { useEffect, useRef } from 'react'
import type { CSSProperties } from 'react'
import { Compartment, EditorState } from '@codemirror/state'
import { EditorView, highlightActiveLine, keymap, placeholder } from '@codemirror/view'
import { bracketMatching, HighlightStyle, StreamLanguage, syntaxHighlighting } from '@codemirror/language'
import type { StreamParser } from '@codemirror/language'
import { autocompletion, closeBrackets, closeBracketsKeymap } from '@codemirror/autocomplete'
import type { Completion, CompletionContext, CompletionResult } from '@codemirror/autocomplete'
import { defaultKeymap, history, historyKeymap } from '@codemirror/commands'
import { tags } from '@lezer/highlight'
import { useThemeStore } from '../../stores/useAppStore'
import { semantic } from '../../theme/tokens'
import { RESEARCH_FEATURES, RESEARCH_OPERATORS } from '../../pages/research/constants'
import '../ExprEditor.css'

/** 特征紫与 FormulaCode（.sr-formula-feat）同源：亮 #7c3aed / 暗 #a78bfa */
const FEATURE_COLOR = { light: '#7c3aed', dark: '#a78bfa' } as const

const OP_SET = new Set<string>(RESEARCH_OPERATORS)
const FEAT_SET = new Set<string>(RESEARCH_FEATURES)

/** 因子表达式 tokenizer：标识符 → 算子/特征；数字、括号、运算符号 → 默认色 */
const exprParser: StreamParser<unknown> = {
  name: 'factorExpr',
  token(stream) {
    if (stream.eatSpace()) return null
    // match 类型为 boolean | RegExpMatchArray | null；typeof 收窄后取 word[0]（完整标识符）
    const m = stream.match(/^([A-Za-z_][A-Za-z0-9_]*)/)
    if (m && typeof m !== 'boolean') {
      if (OP_SET.has(m[0])) return 'operator'
      if (FEAT_SET.has(m[0])) return 'atom'
      return null
    }
    if (stream.match(/^(\d+(?:\.\d+)?)/)) return null
    stream.next()
    return null
  },
  tokenTable: { operator: tags.operator, atom: tags.atom },
}

const exprLanguage = StreamLanguage.define(exprParser)

/** 主题相关扩展（语法高亮 + 编辑器基础外观），随 isDark 变化经 Compartment 重建 */
function themeExts(isDark: boolean, placeholderText: string | undefined) {
  const t = isDark ? 'dark' : 'light'
  return [
    syntaxHighlighting(HighlightStyle.define([
      { tag: tags.operator, color: semantic.accent[t] },
      { tag: tags.atom, color: FEATURE_COLOR[t] },
    ])),
    EditorView.theme({
      '&': {
        color: semantic.text1[t],
        fontFamily: "ui-monospace, 'SF Mono', 'Menlo', 'Monaco', 'Courier New', monospace",
        fontSize: 'var(--sr-font-page)',
      },
      '.cm-content': { caretColor: semantic.accent[t], padding: '6px 0' },
      '.cm-cursor, .cm-dropCursor': { borderLeftColor: semantic.accent[t] },
      '&.cm-focused': { outline: 'none' },
      '.cm-activeLine': {
        backgroundColor: isDark ? 'rgba(148, 163, 184, 0.08)' : 'rgba(16, 24, 40, 0.04)',
      },
      '&.cm-focused .cm-matchingBracket': {
        backgroundColor: isDark ? 'rgba(76, 154, 255, 0.22)' : 'rgba(22, 104, 220, 0.14)',
        outline: `1px solid ${semantic.accent[t]}`,
        borderRadius: '2px',
      },
      '&.cm-focused .cm-nonmatchingBracket': {
        backgroundColor: isDark ? 'rgba(248, 113, 113, 0.18)' : 'rgba(220, 38, 38, 0.10)',
      },
    }, { dark: isDark }),
    placeholderText ? placeholder(placeholderText) : [],
  ]
}

/** 补全源：算子（insert 带左括号提示）+ 特征列；输入字母时触发（无单词前缀不弹） */
function makeCompletionSource(operators: readonly string[], features: readonly string[]) {
  const options: Completion[] = [
    ...operators.map((op) => ({ label: op, type: 'function', apply: `${op}(`, detail: '算子' })),
    ...features.map((f) => ({ label: f, type: 'atom', detail: '特征' })),
  ]
  return (ctx: CompletionContext): CompletionResult | null => {
    const word = ctx.matchBefore(/[A-Za-z_][A-Za-z0-9_]*/)
    if (!word || (word.from === word.to && !ctx.explicit)) return null
    return { from: word.from, options }
  }
}

function completionExts(operators: readonly string[], features: readonly string[]) {
  return autocompletion({ override: [makeCompletionSource(operators, features)] })
}

export interface ExprEditorProps {
  value: string
  onChange: (v: string) => void
  placeholder?: string
  /** 编辑器最小高度（CSS 长度），默认 3.2em（约 2 行 monospace；内容超出滚动） */
  minHeight?: string
  /** 补全算子清单（默认研究域全量算子，见 pages/research/constants.ts） */
  operators?: readonly string[]
  /** 补全特征清单（默认研究域特征列） */
  features?: readonly string[]
}

export function ExprEditor({
  value,
  onChange,
  placeholder: ph,
  minHeight = '3.2em',
  operators = RESEARCH_OPERATORS,
  features = RESEARCH_FEATURES,
}: ExprEditorProps) {
  const isDark = useThemeStore((s) => s.theme) === 'dark'
  const hostRef = useRef<HTMLDivElement | null>(null)
  const viewRef = useRef<EditorView | null>(null)
  const onChangeRef = useRef(onChange)
  onChangeRef.current = onChange
  const valueRef = useRef(value)
  valueRef.current = value

  const themeCompartment = useRef(new Compartment()).current
  const completionCompartment = useRef(new Compartment()).current

  // 仅挂载一次创建 EditorView；主题/补全源变化走 Compartment reconfigure，外部 value 变化走半受控同步
  useEffect(() => {
    if (!hostRef.current) return
    const view = new EditorView({
      parent: hostRef.current,
      state: EditorState.create({
        doc: valueRef.current,
        extensions: [
          exprLanguage,
          EditorView.lineWrapping,
          highlightActiveLine(),
          bracketMatching(),
          closeBrackets(),
          history(),
          keymap.of([...closeBracketsKeymap, ...defaultKeymap, ...historyKeymap]),
          EditorView.updateListener.of((u) => {
            if (u.docChanged) onChangeRef.current(u.state.doc.toString())
          }),
          themeCompartment.of(themeExts(isDark, ph)),
          completionCompartment.of(completionExts(operators, features)),
        ],
      }),
    })
    viewRef.current = view
    return () => { view.destroy(); viewRef.current = null }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 外部 value → 编辑器（示例按钮 / URL 恢复 / 历史任务恢复回灌）
  useEffect(() => {
    const view = viewRef.current
    if (!view) return
    const cur = view.state.doc.toString()
    if (cur !== value) view.dispatch({ changes: { from: 0, to: cur.length, insert: value } })
  }, [value])

  // 主题 / 补全源变化 → 动态重建对应扩展（保留文档与光标）
  useEffect(() => {
    viewRef.current?.dispatch({ effects: [
      themeCompartment.reconfigure(themeExts(isDark, ph)),
      completionCompartment.reconfigure(completionExts(operators, features)),
    ] })
  }, [isDark, ph, operators, features, themeCompartment, completionCompartment])

  return (
    <div
      ref={hostRef}
      className="sr-expr-editor"
      style={{ '--sr-expr-min-h': minHeight } as CSSProperties}
    />
  )
}

export default ExprEditor
