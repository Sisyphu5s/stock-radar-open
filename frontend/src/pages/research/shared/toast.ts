/**
 * 研究域消息助手（T-43 antd message → Mantine notifications 收敛）。
 * ------------------------------------------------------------
 * 映射：antd message.success/warning/error/info → notifications.show（色 = Mantine 语义色）。
 * 批量进度消息：antd message.open({key, duration:0}) → loading(id)/update(id)/dismiss(id)，
 * 语义同 notifications（id 可更新、autoClose:false 常驻，结束时手动 hide）。
 * 单一事实源：色/时长常量集中在此，页面不再直接 import @mantine/notifications。
 */
import { notifications } from '@mantine/notifications'

const TONE_COLOR: Record<'success' | 'error' | 'warning' | 'info', string> = {
  success: 'teal',
  error: 'red',
  warning: 'yellow',
  info: 'blue',
}

const AUTO_CLOSE_MS: Record<'success' | 'error' | 'warning' | 'info', number> = {
  success: 2500,
  error: 4000,
  warning: 3200,
  info: 2500,
}

export const toast = {
  success: (message: string) => notifications.show({
    message, color: TONE_COLOR.success, autoClose: AUTO_CLOSE_MS.success,
  }),
  error: (message: string) => notifications.show({
    message, color: TONE_COLOR.error, autoClose: AUTO_CLOSE_MS.error,
  }),
  warning: (message: string) => notifications.show({
    message, color: TONE_COLOR.warning, autoClose: AUTO_CLOSE_MS.warning,
  }),
  info: (message: string) => notifications.show({
    message, color: TONE_COLOR.info, autoClose: AUTO_CLOSE_MS.info,
  }),
  /** 常驻加载态消息（批量操作计数等；需按 id 更新/关闭） */
  loading: (id: string, text: string) => notifications.show({
    id, message: text, autoClose: false, loading: true,
  }),
  update: (id: string, text: string) => notifications.update({ id, message: text }),
  dismiss: (id: string) => notifications.hide(id),
}
