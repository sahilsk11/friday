interface ChunkOptions {
  maxChars: number;
  maxDelayMs: number;
  sentenceBoundary: boolean;
}

export class TtsChunker {
  private buffer = '';
  private lastFlushTime = Date.now();
  private options: ChunkOptions;
  private onChunk: (text: string) => void;

  constructor(options: ChunkOptions, onChunk: (text: string) => void) {
    this.options = options;
    this.onChunk = onChunk;
  }

  addText(text: string): void {
    this.buffer += text;

    if (this.buffer.length >= this.options.maxChars) {
      this.flush(true);
      return;
    }

    if (this.options.sentenceBoundary && this.hasSentenceBoundary(this.buffer)) {
      this.flush(true);
      return;
    }

    if (Date.now() - this.lastFlushTime >= this.options.maxDelayMs) {
      this.flush(true);
    }
  }

  flush(_force = false): void {
    if (this.buffer.length > 0) {
      this.onChunk(this.buffer);
      this.buffer = '';
    }
    this.lastFlushTime = Date.now();
  }

  private hasSentenceBoundary(text: string): boolean {
    const sentenceEnders = ['.', '!', '?', '。', '！', '？'];
    const lastChar = text.trim().slice(-1);
    return sentenceEnders.includes(lastChar);
  }

  getBuffer(): string {
    return this.buffer;
  }
}