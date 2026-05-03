// Single source of truth for runtime config. Always-absolute base URL:
// the same code path runs in `vite dev`, `vite preview`, production
// builds, and any test harness. No dev proxy, no NODE_ENV branching.
//
// Resolution order:
//   1. import.meta.env.VITE_FRIDAY_BASE_URL — inlined at build time,
//      also readable in dev. Set via .env, .env.local, or per-process
//      env (e.g. `VITE_FRIDAY_BASE_URL=... npm run dev`).
//   2. http://localhost:8765 — friday's default port (see PLAN.md).
const raw = (import.meta.env.VITE_FRIDAY_BASE_URL as string | undefined) ?? '';
const trimmed = raw.replace(/\/$/, '');

export const fridayBaseUrl = trimmed || 'http://localhost:8765';
