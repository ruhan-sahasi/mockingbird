import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The API runs on its own in development (uvicorn on port 8000), so the dev server passes /api
// and the interview socket on to it. The page then talks to one origin, as it does in production.
// `vite preview` uses the same proxy.
const API = 'localhost:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': `http://${API}`,
      '/ws': { target: `ws://${API}`, ws: true },
    },
  },
})
