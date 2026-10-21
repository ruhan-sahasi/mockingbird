import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The API runs on its own in development (uvicorn on port 8000), so the dev server passes /api
// and the interview socket on to it. The page then talks to one origin, as it does in production.
// `vite preview` uses the same proxy.
//
// /api keeps the page's Host header (changeOrigin: false; Vite's shorthand would set it to
// localhost:8000). The server accepts a start only from a page whose Origin is its own site or
// the host the request was sent to (routes_api.py), and locally that host is this dev server.
const API = 'localhost:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: `http://${API}`, changeOrigin: false },
      '/ws': { target: `ws://${API}`, ws: true },
    },
  },
})
