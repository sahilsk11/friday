import { apiClient } from './api';
import type { ModelRef, ModelsResponse, SessionDetail, SessionRow } from '@/types/api';

// Thin typed wrappers around the REST surface. Pages call these
// directly via TanStack Query — no extra abstraction layer.

export function listSessions(directory?: string): Promise<SessionRow[]> {
  const qs = directory ? `?directory=${encodeURIComponent(directory)}` : '';
  return apiClient.get<SessionRow[]>(`/sessions${qs}`);
}

export function createSession(title?: string, directory?: string): Promise<SessionRow> {
  const body: { title?: string; directory?: string } = {};
  if (title) body.title = title;
  if (directory) body.directory = directory;
  return apiClient.post<SessionRow>('/sessions', body);
}

export function getSession(id: string): Promise<SessionDetail> {
  return apiClient.get<SessionDetail>(`/sessions/${id}`);
}

export function postTurn(
  id: string,
  text: string,
  model?: ModelRef,
): Promise<{ session_id: string }> {
  const body: { text: string; model?: ModelRef } = { text };
  if (model) body.model = model;
  return apiClient.post<{ session_id: string }>(`/sessions/${id}/turn`, body);
}

export function listModels(): Promise<ModelsResponse> {
  return apiClient.get<ModelsResponse>('/models');
}
