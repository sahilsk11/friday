export interface AgentAdapter {
  createSession(input?: { title?: string }): Promise<{ sessionId: string }>;
  resumeSession(sessionId: string): Promise<{ sessionId: string }>;
  sendTurn(sessionId: string, text: string): Promise<{ turnId: string }>;
  cancelTurn(sessionId: string, turnId?: string): Promise<void>;
  subscribe(
    sessionId: string,
    handlers: AgentEventHandlers,
    clientSessionId: string
  ): Promise<() => Promise<void>>;
}

export interface AgentEventHandlers {
  onTextDelta(text: string, turnId?: string): void;
  onTextFinal?(text: string, turnId?: string): void;
  onToolEvent?(event: {
    phase: 'start' | 'update' | 'end';
    toolName: string;
    message?: string;
  }): void;
  onState?(state: 'running' | 'idle' | 'done'): void;
  onError?(error: Error): void;
}

export interface AgentSession {
  id: string;
  title?: string;
}