import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The build lands in the Python service, which serves it with the API.
export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    outDir: '../../services/console/fa_console/static',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1600,
    sourcemap: false,
  },
  server: { port: 5173, proxy: { '/api': 'http://127.0.0.1:8099' } },
})
