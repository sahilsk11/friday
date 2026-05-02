export interface TtsAdapter {
  start(options: {
    sessionId: string;
    turnId: string;
    voiceId: string;
    modelId: string;
    onChunk(chunk: { sequence: number; audioBase64: string; mimeType: 'audio/mpeg' | 'audio/pcm' }): void;
    onStart(): void;
    onEnd(): void;
    onError(error: Error): void;
  }): Promise<void>;

  sendText(text: string): void;

  flush(): void;

  stop(): Promise<void>;
}