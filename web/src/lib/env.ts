// Resolve the absolute base URL friday's REST/SSE/WS endpoints live at.
//
// In dev the FE (vite :5173) and BE (uvicorn :8765) are on different
// origins, so VITE_FRIDAY_BASE_URL is required. In prod they share an
// origin (FastAPI mounts web/dist) — derive from window.location so the
// same bundle works at any host without rebuild.

function resolveBaseUrl(): string {
  const override = (import.meta.env.VITE_FRIDAY_BASE_URL as string | undefined) ?? '';
  const trimmed = override.replace(/\/$/, '');
  if (trimmed) return trimmed;
  if (typeof window !== 'undefined') {
    return `${window.location.protocol}//${window.location.host}`;
  }
  // SSR / node test env without a window — bare default.
  return 'http://localhost:8765';
}

export const fridayBaseUrl = resolveBaseUrl();
