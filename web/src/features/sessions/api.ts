import { apiClient } from '@/lib/api';
import type {
  CreateSessionInput,
  CreateSessionResponse,
  EnsureVoiceAgentResponse,
  HarnessInfo,
  ModelsResponse,
  NarratorEventsResponse,
  SessionDetailResponse,
  SessionSummary,
} from '@/types/api';

export function listSessions(): Promise<SessionSummary[]> {
  return apiClient.get<SessionSummary[]>('/api/sessions');
}

export function getSession(sessionId: string): Promise<SessionDetailResponse> {
  return apiClient.get<SessionDetailResponse>(`/api/sessions/${sessionId}`);
}

export function listHarnesses(): Promise<HarnessInfo[]> {
  return apiClient.get<HarnessInfo[]>('/api/harnesses');
}

export function listModels(harness: string): Promise<ModelsResponse> {
  return apiClient.get<ModelsResponse>(`/api/models?harness=${encodeURIComponent(harness)}`);
}

export function createSession(input: CreateSessionInput): Promise<CreateSessionResponse> {
  return apiClient.post<CreateSessionResponse>('/api/sessions', input);
}

export function updateSessionTitle(
  sessionId: string,
  title: string | null,
): Promise<SessionSummary> {
  return apiClient.patch<SessionSummary>(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    title,
  });
}

export function joinExistingSession(
  sessionId: string,
  input: {
    directory: string;
    harness: string;
    model_id?: string;
    title?: string;
  },
): Promise<CreateSessionResponse> {
  return apiClient.post<CreateSessionResponse>('/api/sessions', {
    chat_id: sessionId,
    directory: input.directory,
    harness: input.harness,
    title: input.title,
    ...(input.model_id ? { model_id: input.model_id } : {}),
  });
}

export function ensureVoiceAgent(
  sessionId: string,
  input: { room_name: string },
): Promise<EnsureVoiceAgentResponse> {
  return apiClient.post<EnsureVoiceAgentResponse>(
    `/api/sessions/${encodeURIComponent(sessionId)}/voice-agent`,
    input,
  );
}

export function submitNarratorTurn(
  sessionId: string,
  input: {
    source: 'text' | 'voice';
    text: string;
  },
): Promise<NarratorEventsResponse> {
  return apiClient.post<NarratorEventsResponse>(
    `/api/narrator/sessions/${encodeURIComponent(sessionId)}/turns`,
    input,
  );
}

export function listNarratorEvents(
  sessionId: string,
  input: {
    afterId: number;
    limit?: number;
  },
): Promise<NarratorEventsResponse> {
  const params = new URLSearchParams({
    after_id: String(Math.max(0, input.afterId)),
    limit: String(input.limit ?? 50),
  });
  return apiClient.get<NarratorEventsResponse>(
    `/api/narrator/sessions/${encodeURIComponent(sessionId)}/events?${params.toString()}`,
  );
}
