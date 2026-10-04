import { Badge, Button, Code, DataList, Group, Text, TextInput } from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconCircleCheck, IconCircleX, IconRefresh, IconRobot, IconSettings } from '@tabler/icons-react'
import { useEffect, useState } from 'react'
import QueryRedirect from '../../app/adapters/QueryRedirect'
import CardState from '../../components/ui/CardState'
import { SettingBlock } from '../../templates'
import { getCopilotConfig, setRuntimeConfig, testCopilotChat } from '../../api/client'
import type { CopilotConfig } from '../../api/client'

/** antd message → @mantine/notifications（T-44：色/时长对齐 research/shared/toast.ts 契约） */
const notifySuccess = (m: string) => notifications.show({ message: m, color: 'teal', autoClose: 2500 })

/** LLM / AI 区块（无 PageHeader / Card，供设置工作区嵌入）。 */
export function LlmCopilotSection() {
  const [config, setConfig] = useState<CopilotConfig | null>(null)
  const [loadErr, setLoadErr] = useState<string | null>(null)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<string | null>(null)
  // T-77：模型名可编辑（走运行时配置通道 /system/config/runtime），Base URL 与 API Key 保持只读
  const [modelDraft, setModelDraft] = useState('')
  const [savingModel, setSavingModel] = useState(false)
  const [modelErr, setModelErr] = useState<string | null>(null)

  const load = async () => {
    try {
      const c = await getCopilotConfig()
      setConfig(c)
      setModelDraft(c.model)
      setLoadErr(null)
    } catch (e: any) { setLoadErr('配置加载失败: ' + (e?.message ?? e)) }
  }
  useEffect(() => { void load() }, [])

  const saveModel = async () => {
    const next = modelDraft.trim()
    if (!next) {
      setModelErr('模型名不能为空')
      return
    }
    setSavingModel(true)
    setModelErr(null)
    try {
      await setRuntimeConfig('llm_model', next)
      notifySuccess(`模型已更新为「${next}」（下次 LLM 调用生效）`)
      await load()
    } catch (e: any) {
      setModelErr('保存失败: ' + String(e?.response?.data?.detail ?? e?.message ?? e))
    } finally {
      setSavingModel(false)
    }
  }

  const test = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      await testCopilotChat({ question: '请回复"连接正常"', context: { stock_code: 'TEST' } })
      // 成功判定走 2xx（axios 非 2xx 已抛错）；超时由 client 侧 15s 硬超时（ECONNABORTED）判定
      setTestResult('连接正常（模型已回复）')
    } catch (e: any) {
      if (e?.code === 'ECONNABORTED') {
        setTestResult('连接超时（15s 无响应）')
      } else {
        // 失败展示后端 detail（FastAPI 错误体）或原文摘要
        setTestResult('连接失败: ' + String(e?.response?.data?.detail ?? e?.message ?? e))
      }
    } finally { setTesting(false) }
  }

  return (
    <>
      <SettingBlock
        title="LLM Copilot 配置"
        icon={<IconRobot size={14} />}
        desc={
          <>
            模型名可直接编辑（保存后立即生效，写入运行时配置）；Base URL / API Key 通过
            环境变量加载（<Code>SR_LLM_BASE_URL</Code> / <Code>SR_LLM_API_KEY</Code>，
            写入 backend/.env），修改后重启后端生效；页面同时提供连通性测试。
          </>
        }
      >
        <CardState
          loading={config == null && !loadErr}
          error={loadErr}
          onRetry={load}
          loadingText="正在获取配置…"
        >
          {config && (
            <DataList size="sm" orientation="horizontal" withDivider labelWidth={88}>
              <DataList.Item>
                <DataList.ItemLabel>Base URL</DataList.ItemLabel>
                <DataList.ItemValue><Code>{config.base_url}</Code></DataList.ItemValue>
              </DataList.Item>
              <DataList.Item>
                <DataList.ItemLabel>模型</DataList.ItemLabel>
                <DataList.ItemValue>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--sr-gap-row)', minWidth: 0 }}>
                    <Group gap={6} wrap="wrap">
                      <TextInput
                        size="xs"
                        value={modelDraft}
                        onChange={(e) => setModelDraft(e.currentTarget.value)}
                        style={{ width: 'clamp(160px, 30vw, 260px)' }}
                        aria-label="LLM 模型名"
                      />
                      <Button
                        size="xs"
                        variant="light"
                        loading={savingModel}
                        disabled={modelDraft.trim() === '' || modelDraft.trim() === config.model}
                        onClick={() => void saveModel()}
                      >
                        保存
                      </Button>
                    </Group>
                    {modelErr && <Text c="red" size="xs">{modelErr}</Text>}
                  </div>
                </DataList.ItemValue>
              </DataList.Item>
              <DataList.Item>
                <DataList.ItemLabel>API Key</DataList.ItemLabel>
                <DataList.ItemValue>
                  {config.configured
                    ? <Badge color="green" leftSection={<IconCircleCheck size={12} />}>已配置</Badge>
                    : <Badge color="red" leftSection={<IconCircleX size={12} />}>未配置</Badge>}
                </DataList.ItemValue>
              </DataList.Item>
            </DataList>
          )}
        </CardState>
        {/* 测试连接操作行：纯容器（无 label），用 flex div 承载，不走 FormRow hack */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--sr-pad-lg)', flexWrap: 'wrap', marginTop: 'var(--sr-pad-lg)' }}>
          <Button leftSection={<IconRefresh size={14} />} loading={testing} onClick={test}>测试连接</Button>
          {testResult && (
            <Text style={{ color: testResult.startsWith('连接正常') ? 'var(--sr-success)' : 'var(--sr-error)' }}>
              {testResult}
            </Text>
          )}
        </div>
      </SettingBlock>

      <SettingBlock
        title="上下文体系"
        icon={<IconSettings size={14} />}
        style={{ marginTop: 'var(--sr-gap-card)' }}
        desc={
          <>
            <span style={{ display: 'block', marginBottom: 'var(--sr-pad-sm)' }}>
              · <b>市场监控上下文</b>：当前股票、K线区间、指标数值、信号条件、市场状态 —— 用于个股工作台面板
            </span>
            <span style={{ display: 'block', marginBottom: 'var(--sr-pad-sm)' }}>
              · <b>因子研究上下文</b>：数据集、特征字段、算子集合、当前公式、实验结果 —— 用于因子挖掘/回测/评估面板
            </span>
            <span style={{ display: 'block' }}>
              · <b>Hermes 页面快照</b>：发送对话时自动收集当前页面元素（路由/卡片标题/按钮文本），随市场/研究上下文一并上送，助手可感知当前页面状态（限长 8000 字符）
            </span>
          </>
        }
        children={null}
      />
    </>
  )
}

/** 旧深链 /settings/copilot：合并到单一设置工作区（query 原样保留，如 ?section=ai） */
export default function LlmCopilot() {
  return <QueryRedirect to="/settings" />
}
