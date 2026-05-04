// TTS text chunker — buffers incoming text deltas and emits chunks suitable
// for a streaming TTS pipeline.  Chunks are emitted on sentence boundaries,
// when the buffer exceeds maxChars, or when the maxDelayMs timer fires.

export interface TtsChunkerOptions {
  /** Maximum characters before a forced flush. Default 200. */
  maxChars: number;
  /** Maximum delay in ms before a forced flush. Default 250. */
  maxDelayMs: number;
  /** If true, prefer flushing on sentence boundaries (. ! ? or newline). */
  sentenceBoundary: boolean;
  /** Called with each flushed chunk of text. */
  onChunk: (text: string) => void;
}

/** Sentence-ending characters followed by whitespace (or end-of-string). */
const SENTENCE_END_RE = /[.!?\n]/;

/**
 * Creates a TTS chunker that buffers text deltas and emits chunks.
 * Returns { push, flush, dispose }.
 */
export function createTtsChunker(opts: TtsChunkerOptions): {
  push(text: string): void;
  flush(): void;
  dispose(): void;
} {
  let buffer = '';
  let timer: ReturnType<typeof setTimeout> | null = null;

  function emit(): void {
    if (buffer.length === 0) return;
    const chunk = buffer;
    buffer = '';
    cancelTimer();
    opts.onChunk(chunk);
  }

  function cancelTimer(): void {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function scheduleTimer(): void {
    if (timer !== null) return; // already scheduled
    timer = setTimeout(() => {
      timer = null;
      emit();
    }, opts.maxDelayMs);
  }

  function push(text: string): void {
    buffer += text;

    // Flush immediately if the buffer exceeds maxChars.
    if (buffer.length >= opts.maxChars) {
      emit();
      return;
    }

    // If sentence-boundary mode: flush when the buffer ends with a sentence
    // terminator (optionally followed by whitespace).
    if (opts.sentenceBoundary) {
      const trimmed = buffer.trimEnd();
      if (trimmed.length > 0 && SENTENCE_END_RE.test(trimmed[trimmed.length - 1])) {
        emit();
        return;
      }
    }

    // Otherwise, start the deadline timer.
    scheduleTimer();
  }

  function flush(): void {
    emit();
  }

  function dispose(): void {
    cancelTimer();
    buffer = '';
  }

  return { push, flush, dispose };
}
