import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The browser only ever talks to our server: /api and /ws are proxied, and no
// provider key is ever present in the client bundle.
//
// Port 8090, not the 8080 default: this machine already has something bound
// to 8080, so the backend here always runs with `--port 8090` to match.
// Deliberately local/uncommitted -- do not push this to origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8090', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8090', ws: true },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
