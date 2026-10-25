import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  const api = env.VITE_API_BASE_URL || 'http://localhost:8010'
  if (api.startsWith('//')) throw new Error('Use an explicit API scheme or a relative path')
  const origin = api.startsWith('/') ? '' : new URL(api).origin
  if (origin && !/^https?:\/\//.test(origin)) throw new Error('VITE_API_BASE_URL must use HTTP(S)')
  return {
    plugins: [
      react(),
      {
        name: 'api-content-security-policy',
        transformIndexHtml(html, context) {
          const port = context.server?.config.server.port
          const sockets = port ? `ws://127.0.0.1:${port} ws://localhost:${port}` : ''
          return html.replace('__CONNECT_SOURCES__', `${origin} ${sockets}`.trim())
        },
      },
    ],
    server: { port: 5173, strictPort: true },
  }
})
