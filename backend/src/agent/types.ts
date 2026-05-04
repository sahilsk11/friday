// AgentAdapter interface — verbatim from spec §4.4.
// All coding-agent backends (OpenCode, Hermes, Claude Code, etc.) implement this.

export interface AgentAdapter {
  createSession(input?: { title?: string }): Promise<{ sessionId: string }>;

  resumeSession(sessionId: string): Promise<{ sessionId: string }>;

  // Send a human turn to the backend. Returns a logical turnId.
  sendTurn(sessionId: string, text: string): Promise<{ turnId: string }>;

  // Cancel a running turn (best-effort).
  cancelTurn(sessionId: string, turnId?: string): Promise<void>;

  // Subscribe to streaming events for a given session.
  // Returns an unsubscribe function.
  subscribe(
    sessionId: string,
    handlers: {
      onTextDelta(text: string, turnId?: string): void;
      onTextFinal?(text: string, turnId?: string): void;
      onToolEvent?(event: {
        phase: 'start' | 'update' | 'end';
        toolName: string;
        message?: string;
      }): void;
      onState?(state: 'running' | 'idle' | 'done'): void;
      onError?(error: Error): void;
    },
  ): Promise<() => Promise<void>>;
}
