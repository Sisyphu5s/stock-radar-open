/**
 * 下载/导出统一入口（T-15 图表截图 · 研究报告导出）。
 * 全部走 a[download] 浏览器原生下载,不引入第三方库;文件名由调用方组装（含代码/周期/日期等上下文）。
 */

/** 触发浏览器下载:临时 <a download> 挂载点击后移除(URL.revokeObjectURL 由 Blob 场景显式处理) */
function triggerDownload(filename: string, href: string) {
  const a = document.createElement('a')
  a.href = href
  a.download = filename
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
}

/** 下载文本内容（Markdown 报告/CSV 等）：Blob URL 下载,结束后立即 revoke */
export function downloadText(filename: string, text: string, mime = 'text/plain;charset=utf-8') {
  const blob = new Blob([text], { type: mime })
  const url = URL.createObjectURL(blob)
  triggerDownload(filename, url)
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

/** 下载 dataURL 图片（PNG 截图：lwc takeScreenshot / echarts getDataURL 产物） */
export function downloadDataUrl(filename: string, dataUrl: string) {
  triggerDownload(filename, dataUrl)
}
