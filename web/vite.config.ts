import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';
import tsconfigPaths from 'vite-tsconfig-paths';

// Single source of truth for runtime config. Both `vite` (dev) and
// `vite preview` bind to PORT with strictPort:true so a taken port
// fails loudly instead of silently incrementing.
//
// The backend URL is *not* read here. The running app reads it via
// import.meta.env.VITE_FRIDAY_BASE_URL so the same bundle can be
// pointed at any backend without rebuilding. See src/lib/env.ts.
const port = Number(process.env.PORT ?? 5173);

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss(), tsconfigPaths()],
  server: { port, strictPort: true, host: true },
  preview: { port, strictPort: true, host: true },
  // voice-ui-kit lazy-loads transports via dynamic string imports
  // (`import("@pipecat-ai/small-webrtc-transport")`) so vite's static
  // scanner can't see them. Force pre-bundle so they resolve at runtime.
  optimizeDeps: {
    include: ['@pipecat-ai/small-webrtc-transport', '@pipecat-ai/client-js'],
  },
});
