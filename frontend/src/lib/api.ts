// HTTP client wrapper — centralizes fetch so the eslint fetch-ban applies
// everywhere except here. Currently unused (all communication goes via
// WebSocket), but the module must exist so the eslint override fires.

export async function apiGet<T>(path: string): Promise<T> {
  const res = await fetch(path, { credentials: 'same-origin' });
  if (!res.ok) {
    throw new Error(`GET ${path} failed: ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}
