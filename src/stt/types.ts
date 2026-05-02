export interface SttAdapter {
  start(options: {
    sessionId: string;
    language?: string;
    onPartial(text: string): void;
    onFinal(text: string): void;
    onError(error: Error): void;
  }): Promise<void>;

  sendAudio(chunk: ArrayBuffer): void;

  stop(): Promise<void>;
}