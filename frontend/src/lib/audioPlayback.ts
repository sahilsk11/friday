// Buffered MP3 playback for streaming TTS chunks using MediaSource + SourceBuffer.
//
// Individual MP3 fragments cannot be played with separate <audio> elements
// because each fragment lacks MPEG headers for seeking/duration. MediaSource
// Extension (MSE) allows us to append raw MP3 data incrementally.
//
// User-gesture note: browsers require a user gesture before audio.play() is
// allowed. We call play() on the first append; if it rejects (policy) we log
// the error — the user can tap/click to unblock if needed.

export interface PlaybackHandle {
  /** Enqueue a decoded MP3/audio byte chunk for playback. */
  append(bytes: Uint8Array<ArrayBuffer>): void;
  /** Signal end of stream — no more chunks will come. */
  end(): void;
  /** Hard-stop and tear down the MediaSource immediately. */
  stop(): void;
}

export interface PlaybackOpts {
  onEnded?(): void;
}

export function createPlayback(opts: PlaybackOpts): PlaybackHandle {
  const audio = new Audio();
  audio.autoplay = false;

  const mediaSource = new MediaSource();
  audio.src = URL.createObjectURL(mediaSource);

  let sourceBuffer: SourceBuffer | null = null;
  let ended = false;
  let stopped = false;
  // Queue of chunks pending SourceBuffer availability.
  const queue: Uint8Array<ArrayBuffer>[] = [];

  const flushQueue = () => {
    if (!sourceBuffer || sourceBuffer.updating || queue.length === 0) return;
    const next = queue.shift();
    if (!next) return;
    try {
      sourceBuffer.appendBuffer(next);
    } catch (err) {
      console.error('[audioPlayback] appendBuffer error', err);
    }
  };

  mediaSource.addEventListener('sourceopen', () => {
    if (stopped) return;
    sourceBuffer = mediaSource.addSourceBuffer('audio/mpeg');

    sourceBuffer.addEventListener('updateend', () => {
      if (queue.length > 0) {
        flushQueue();
      } else if (ended && !sourceBuffer?.updating) {
        try {
          if (mediaSource.readyState === 'open') {
            mediaSource.endOfStream();
          }
        } catch (err) {
          console.error('[audioPlayback] endOfStream error', err);
        }
      }
    });

    // Start playback; errors are expected on first paint before user gesture.
    audio.play().catch((err: unknown) => {
      console.warn('[audioPlayback] play() rejected (user gesture required?)', err);
    });

    flushQueue();
  });

  audio.addEventListener('ended', () => {
    opts.onEnded?.();
  });

  return {
    append(bytes: Uint8Array<ArrayBuffer>): void {
      if (stopped) return;
      queue.push(bytes);
      if (mediaSource.readyState === 'open') {
        flushQueue();
      }
      // If mediaSource is not yet open, chunks stay in queue until 'sourceopen'.
    },

    end(): void {
      if (stopped) return;
      ended = true;
      // If there are still pending chunks, endOfStream is deferred to updateend.
      if (
        mediaSource.readyState === 'open' &&
        queue.length === 0 &&
        !sourceBuffer?.updating
      ) {
        try {
          mediaSource.endOfStream();
        } catch (err) {
          console.error('[audioPlayback] endOfStream error', err);
        }
      }
    },

    stop(): void {
      if (stopped) return;
      stopped = true;
      audio.pause();
      queue.length = 0;
      try {
        if (mediaSource.readyState === 'open') {
          mediaSource.endOfStream();
        }
      } catch {
        // Ignore — we are tearing down.
      }
      URL.revokeObjectURL(audio.src);
      audio.src = '';
    },
  };
}
