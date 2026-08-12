import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    target: 'baseline-widely-available',
    sourcemap: false,
    chunkSizeWarningLimit: 500,
  },
  optimizeDeps: { include: ['react', 'react-dom'] },
  server: { port: 5173, proxy: { '/api': 'http://127.0.0.1:8000' } },
})
