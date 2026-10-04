/**
 * antd Tag 色名 → Mantine Badge 色名(工作台本域共享,转发到单一事实源)。
 * 数据源：SIGNAL_CATEGORY_COLOR / SECTOR_COLOR / labelMap.color 均为 antd 色名字符串，
 * 本域 Badge 渲染处统一经 antdToMantine 转译（KlineToolbar / TimelinePanel 共用，禁止复制两份）。
 * 色值事实源不变（tokens.ts）；翻译表收敛于 utils/antdColorCompat.ts（P1-45），
 * 本文件仅 re-export，保持 8 个调用方 import 路径与签名不变。
 */
export { antdToMantine } from '../../../utils/antdColorCompat'
