export interface HarnessInfo {
  id: string;
  name: string;
}

export interface ModelInfo {
  model_ref: string;
  model_id: string;
  model_name: string;
  provider_id: string;
  provider_name: string;
}

export interface ModelsResponse {
  default: string | null;
  models: ModelInfo[];
}

export interface SessionSummary {
  created_at: string;
  directory: string | null;
  harness: string;
  id: string;
  model_id: string | null;
  title: string | null;
  updated_at: string;
}

export interface CurrentModel {
  model_id: string;
  provider_id: string;
}

export interface TranscriptEntry {
  completed_at: string | null;
  error?: string | null;
  model?: CurrentModel | null;
  parts: Record<string, unknown>[];
  role: string;
  text: string;
}

export interface SessionDetailResponse {
  agent_state: 'idle' | 'listening' | 'thinking' | 'speaking';
  current_model: CurrentModel | null;
  narrator_transcript: TranscriptEntry[];
  session: SessionSummary;
  transcript: TranscriptEntry[];
}

export interface NarratorEventResponse {
  created_at: string;
  id: number;
  payload: Record<string, unknown>;
  text: string | null;
  type: string;
}

export interface NarratorEventsResponse {
  events: NarratorEventResponse[];
}

export interface CreateSessionInput {
  directory: string;
  harness: string;
  model_id: string;
  participant_name?: string;
  title?: string;
}

export interface CreateSessionResponse {
  directory: string | null;
  expires_in_seconds: number;
  harness: string | null;
  livekit_url: string;
  model_id: string | null;
  participant_identity: string;
  participant_name: string;
  room_name: string;
  session_id: string;
  title: string | null;
  token: string;
}

export interface EnsureVoiceAgentResponse {
  dispatched: boolean;
  room_name: string;
}

export interface SessionRouteState {
  sessionPayload?: CreateSessionResponse;
}

export const sessionQueryKeys = {
  harnesses: ['harnesses'] as const,
  models: (harness: string) => ['models', harness] as const,
  session: (sessionId: string) => ['sessions', sessionId] as const,
  sessions: ['sessions'] as const,
};
