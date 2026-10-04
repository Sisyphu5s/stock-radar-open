import { Component } from 'react'
import { Button, Stack, Text } from '@mantine/core'
import { IconAlertTriangle } from '@tabler/icons-react'
import type { ReactNode } from 'react'

interface Props { children: ReactNode }
interface State { error: Error | null }

/** 页面级错误兜底：任何渲染异常显示提示而非白屏。 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  render() {
    if (this.state.error) {
      return (
        <div className="sr-error-boundary">
          <Stack align="center" gap={8}>
            <IconAlertTriangle size={40} color="var(--sr-error)" aria-hidden />
            <Text fw={700} size="lg" c="var(--sr-text-1)">页面渲染出错</Text>
            <Text size="sm" c="var(--sr-text-2)" ta="center" style={{ maxWidth: 480 }}>
              {this.state.error.message.slice(0, 200)}
            </Text>
            <Button variant="filled" mt={8} onClick={() => window.location.reload()}>
              重新加载
            </Button>
          </Stack>
        </div>
      )
    }
    return this.props.children
  }
}
