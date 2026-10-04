import { StrictMode, useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import { ReactQueryDevtools } from '@tanstack/react-query-devtools'
// Mantine 9 样式(T-37):必须在本项目 index.css 之前加载,
// 否则 Mantine baseline.css 的 body 背景/字体规则会覆盖 --sr-* 令牌
import '@mantine/core/styles.css'
import '@mantine/notifications/styles.css'
import { DatesProvider } from '@mantine/dates'
import { ModalsProvider } from '@mantine/modals'
import { Notifications } from '@mantine/notifications'
import dayjs from 'dayjs'
import utc from 'dayjs/plugin/utc'
import timezone from 'dayjs/plugin/timezone'
import 'dayjs/locale/zh-cn'
import './index.css'
import './styles/layout.css'
import './styles/ui-polish.css'
import './styles/interaction.css'
import './styles/float-tokens.css'
import App from './App'
import { queryClient } from './data/queryBase'
import { useThemeStore, useDensityStore } from './stores/useAppStore'
import { MantineStyleProvider } from './theme/mantineTheme'

// ---- dayjs 一次性配置(T-37)----
// 时区纪律:后端 naive ISO 一律按上海时区解析(禁浏览器本地时区)。
// @mantine/dates 不内置 timezone 字段(9.5.1 类型确认),时区由 dayjs 全局默认承担,
// 与本项目 "禁 new Date 按浏览器时区" 的既有纪律同源。
dayjs.extend(utc)
dayjs.extend(timezone)
dayjs.locale('zh-cn')
dayjs.tz.setDefault('Asia/Shanghai')

function Root() {
  const themeMode = useThemeStore((s) => s.theme)
  const density = useDensityStore((s) => s.density)

  // 同步 body data-theme（驱动 CSS 变量）
  useEffect(() => {
    document.body.setAttribute('data-theme', themeMode)
  }, [themeMode])
  // 同步 body data-density（驱动密度 CSS 变量覆盖）
  useEffect(() => {
    document.body.setAttribute('data-density', density)
  }, [density])

  return (
    <MantineStyleProvider>
      <DatesProvider settings={{ locale: 'zh-cn' }}>
        <ModalsProvider>
          <Notifications position="top-right" />
          <QueryClientProvider client={queryClient}>
            <App />
            {/* Devtools 仅 dev 环境挂载（import.meta.env.DEV 为编译期常量，prod 构建摇树消除） */}
            {import.meta.env.DEV && <ReactQueryDevtools initialIsOpen={false} />}
          </QueryClientProvider>
        </ModalsProvider>
      </DatesProvider>
    </MantineStyleProvider>
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
