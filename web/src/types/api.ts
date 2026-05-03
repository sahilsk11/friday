// Wire types matching the backend contract in BackendIntegration.md.
// Keep this file flat — types only, no logic.

export interface SessionRow {
  id: string;
  title: string | null;
  directory: string | null;
  created_at: string;
  updated_at: string;
}

export interface TranscriptEntry {
  role: 'user' | 'assistant';
  text: string;
  completed_at: string | null;
}

export interface SessionDetail {
  session: SessionRow;
  transcript: TranscriptEntry[];
}

export type AgentState = 'idle' | 'listening' | 'thinking' | 'speaking';

// SSE frame types (see GET /sessions/:id/events in BackendIntegration.md).
export type SessionEvent =
  | { type: 'state'; state: AgentState }
  | { type: 'text.delta'; text: string }
  | { type: 'text.final'; text: string };
