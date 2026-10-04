import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    // 端口/代理目标可用环境变量覆盖（开发环境与部署环境同跑时错开端口）
    port: Number(process.env.SR_FRONTEND_PORT) || 5173,
    strictPort: true,
    // Bind to loopback by default; set SR_FRONTEND_HOST explicitly for network access.
    host: process.env.SR_FRONTEND_HOST || '127.0.0.1',
    proxy: {
      '/api': { target: process.env.SR_PROXY_TARGET || 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
