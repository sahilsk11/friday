import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Dev server on port 5173.
// Proxies /ws to the local voice gateway (WebSocket) and /health to HTTP.
export default defineConfig({
  plugins: [react()],
  resolve: { tsconfigPaths: true },
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      '/ws': {
        target: 'ws://127.0.0.1:8787',
        ws: true,
        rewrite: (path) => path,
      },
      '/health': {
        target: 'http://127.0.0.1:8787',
        changeOrigin: true,
      },
    },
  },
});
