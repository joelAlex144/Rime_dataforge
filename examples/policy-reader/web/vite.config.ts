import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The browser only ever talks to our server: /api and /ws are proxied, and no
// provider key is ever present in the client bundle.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      // Upload and its follow-ups (POST /documents, .../report, .../accept,
      // .../enrichment, DELETE /documents/{id}) do NOT live under /api. Without
      // this rule the dev server answered POST /documents with its own 404 and
      // the upload failed before it ever reached the reader; the build served
      // from web/dist was fine, which is why only `npm run dev` was affected.
      // POST /documents replies text/event-stream (one event per ingest stage),
      // so the proxy must not buffer it.
      '/documents': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8080', ws: true },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
