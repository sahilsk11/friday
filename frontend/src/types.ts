export type ClientMessage =
  | { type: 'session.create'; title?: string }
  | { type: 'session.resume'; sessionId: string }
  | { type: 'audio.start'; sessionId: string; sttProvider?: 'elevenlabs'; sampleRate: number; encoding: 'pcm16'; language?: string }
  | { type: 'audio.chunk'; sessionId: string; chunkBase64: string; sequence: number }
  | { type: 'audio.stop'; sessionId: string }
  | { type: 'turn.send'; sessionId: string; text: string; source: 'typed' | 'stt-final' }
  | { type: 'run.cancel'; sessionId: string; turnId?: string }
  | { type: 'tts.stop'; sessionId: string }
  | { type: 'config.update'; sessionId?: string; config: Partial<RuntimeConfig> }
  | { type: 'ping'; ts: number };

export type ServerMessage =
  | { type: 'session.created'; sessionId: string; title?: string }
  | { type: 'session.resumed'; sessionId: string }
  | { type: 'session.state'; sessionId: string; state: 'idle' | 'listening' | 'transcribing' | 'running' | 'speaking' | 'error' }
  | { type: 'stt.partial'; sessionId: string; text: string }
  | { type: 'stt.final'; sessionId: string; text: string }
  | { type: 'turn.accepted'; sessionId: string; turnId: string; queued: boolean }
  | { type: 'agent.text.delta'; sessionId: string; turnId: string; text: string }
  | { type: 'agent.text.final'; sessionId: string; turnId: string; text: string }
  | { type: 'agent.status'; sessionId: string; turnId?: string; status: 'thinking' | 'tool_running' | 'idle' | 'done'; message?: string }
  | { type: 'agent.tool'; sessionId: string; turnId?: string; phase: 'start' | 'update' | 'end'; toolName: string; message?: string }
  | { type: 'tts.started'; sessionId: string; turnId: string }
  | { type: 'tts.audio.chunk'; sessionId: string; turnId: string; sequence: number; audioBase64: string; mimeType: 'audio/mpeg' | 'audio/pcm' }
  | { type: 'tts.ended'; sessionId: string; turnId: string }
  | { type: 'run.cancelled'; sessionId: string; turnId?: string }
  | { type: 'error'; sessionId?: string; code: string; message: string; retryable?: boolean }
  | { type: 'pong'; ts: number };

export type RuntimeConfig = {
  sttProvider: 'elevenlabs';
  ttsProvider: 'elevenlabs';
  ttsVoiceId: string;
  ttsModelId: string;
  language?: string;
  autoSpeak: boolean;
  autoSendFinalTranscript: boolean;
  chunking: { maxChars: number; maxDelayMs: number; sentenceBoundary: boolean };
};