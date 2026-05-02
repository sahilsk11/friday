import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // SSE-down + POST-up replaces the old WebSocket proxy. Vite passes
      // text/event-stream through without buffering, so deltas arrive live.
      '/api': {
        target: 'http://localhost:3000',
        changeOrigin: true,
      },
    },
  },
});
