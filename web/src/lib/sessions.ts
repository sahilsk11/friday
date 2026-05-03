import { apiClient } from './api';
import type { SessionDetail, SessionRow } from '@/types/api';

// Thin typed wrappers around the REST surface. Pages call these
// directly via TanStack Query — no extra abstraction layer.

export function listSessions(directory?: string): Promise<SessionRow[]> {
  const qs = directory ? `?directory=${encodeURIComponent(directory)}` : '';
  return apiClient.get<SessionRow[]>(`/sessions${qs}`);
}

export function createSession(title?: string): Promise<SessionRow> {
  return apiClient.post<SessionRow>('/sessions', title ? { title } : {});
}

export function getSession(id: string): Promise<SessionDetail> {
  return apiClient.get<SessionDetail>(`/sessions/${id}`);
}

export function postTurn(id: string, text: string): Promise<{ session_id: string }> {
  return apiClient.post<{ session_id: string }>(`/sessions/${id}/turn`, { text });
}
