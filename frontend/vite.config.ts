/// <reference types="vitest" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  // Absolute base: deep-route refresh must resolve assets from the site root.
  // TODO(Phase 11): subpath (ROOT_PATH) support needs a build-time absolute
  // base plus a matching BrowserRouter basename — a relative base can never
  // work with history routing.
  base: '/',
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8080',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: './src/test/setup.ts',
  },
})
