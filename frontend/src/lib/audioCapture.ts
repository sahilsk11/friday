// Audio capture: mic -> PCM16 chunks sent via onChunk callback.
//
// Approach:
//   1. getUserMedia mono stream
//   2. AudioContext at 16 kHz (browsers may clamp to 44100 / 48000 — see note below)
//   3. AudioWorkletNode for lock-free, off-main-thread sample collection
//   4. Inline worklet via Blob + URL.createObjectURL (no public/ file needed)
//   5. If AudioWorklet unavailable, fall back to ScriptProcessorNode
//
// Resampling note: macOS Chrome/Safari commonly clamp AudioContext.sampleRate
// to 44100 or 48000. When that happens we emit every Nth sample where
//   N = Math.round(context.sampleRate / targetSampleRate)
// This is nearest-sample decimation — good enough for speech STT.

export interface AudioCaptureHandle {
  stop(): Promise<void>;
}

export interface AudioCaptureOpts {
  onChunk(pcm16Bytes: Uint8Array, sequence: number): void;
  sampleRate?: number; // default 16000
}

// How many PCM16 samples per emitted chunk (~50 ms of audio at target rate).
const CHUNK_SAMPLES = 800; // 800 samples * 2 bytes = 1600 bytes; two of these = 50 ms

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Convert a Float32 frame (possibly downsampled) to Int16 little-endian bytes. */
function float32ToPcm16(samples: Float32Array): Uint8Array {
  const buf = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buf);
  for (let i = 0; i < samples.length; i++) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, clamped * 0x7fff, /* littleEndian */ true);
  }
  return new Uint8Array(buf);
}

/** Decimate a Float32Array by keeping every Nth sample. */
function decimate(input: Float32Array, step: number): Float32Array {
  if (step <= 1) return input;
  const out = new Float32Array(Math.floor(input.length / step));
  for (let i = 0; i < out.length; i++) {
    out[i] = input[i * step];
  }
  return out;
}

// ---------------------------------------------------------------------------
// Worklet source (inlined as a string, loaded via Blob URL)
// ---------------------------------------------------------------------------

const WORKLET_CODE = `
// PCM collector worklet.
// Accumulates input samples and posts them to the main thread in chunks.
const CHUNK_SAMPLES = ${CHUNK_SAMPLES};

class PcmCollectorProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._buf = new Float32Array(CHUNK_SAMPLES * 2);
    this._offset = 0;
  }

  process(inputs) {
    const channel = inputs[0]?.[0];
    if (!channel) return true;

    let idx = 0;
    while (idx < channel.length) {
      const room = this._buf.length - this._offset;
      const take = Math.min(room, channel.length - idx);
      this._buf.set(channel.subarray(idx, idx + take), this._offset);
      this._offset += take;
      idx += take;

      if (this._offset >= CHUNK_SAMPLES) {
        // Post a copy of the first CHUNK_SAMPLES worth of samples.
        this.port.postMessage(this._buf.slice(0, CHUNK_SAMPLES));
        // Shift remaining samples to front.
        const remaining = this._offset - CHUNK_SAMPLES;
        this._buf.copyWithin(0, CHUNK_SAMPLES, CHUNK_SAMPLES + remaining);
        this._offset = remaining;
      }
    }
    return true;
  }
}

registerProcessor('pcm-collector', PcmCollectorProcessor);
`;

// ---------------------------------------------------------------------------
// Main entry point
// ---------------------------------------------------------------------------

export async function startAudioCapture(opts: AudioCaptureOpts): Promise<AudioCaptureHandle> {
  const targetRate = opts.sampleRate ?? 16000;

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    video: false,
  });

  // Note: macOS often clamps AudioContext.sampleRate to 44100 or 48000 even
  // when 16000 is requested. We handle this with decimation below.
  const ctx = new AudioContext({ sampleRate: targetRate });
  const actualRate = ctx.sampleRate;
  const decimationStep = Math.round(actualRate / targetRate);

  const source = ctx.createMediaStreamSource(stream);
  let sequence = 0;

  const stopTracks = () => {
    for (const track of stream.getTracks()) {
      track.stop();
    }
  };

  // Try AudioWorklet first, fall back to ScriptProcessorNode.
  if (typeof AudioWorkletNode !== 'undefined' && ctx.audioWorklet) {
    const blob = new Blob([WORKLET_CODE], { type: 'application/javascript' });
    const url = URL.createObjectURL(blob);
    try {
      await ctx.audioWorklet.addModule(url);
    } finally {
      URL.revokeObjectURL(url);
    }

    const workletNode = new AudioWorkletNode(ctx, 'pcm-collector');

    workletNode.port.onmessage = (ev: MessageEvent<Float32Array>) => {
      const raw = ev.data;
      const resampled = decimationStep > 1 ? decimate(raw, decimationStep) : raw;
      const bytes = float32ToPcm16(resampled);
      sequence += 1;
      opts.onChunk(bytes, sequence);
    };

    source.connect(workletNode);
    workletNode.connect(ctx.destination);

    return {
      async stop(): Promise<void> {
        workletNode.disconnect();
        source.disconnect();
        stopTracks();
        await ctx.close();
      },
    };
  }

  // Fallback: ScriptProcessorNode (deprecated but widely supported).
  // bufferSize 4096 gives ~93 ms at 44100 Hz.
  // createScriptProcessor is deprecated but widely available as a fallback.
  const bufferSize = 4096;
  const processor = ctx.createScriptProcessor(bufferSize, 1, 1);
  let accumulator = new Float32Array(0);

  processor.onaudioprocess = (ev: AudioProcessingEvent) => {
    const input = ev.inputBuffer.getChannelData(0);
    const resampled = decimationStep > 1 ? decimate(input, decimationStep) : input;

    // Append to accumulator.
    const combined = new Float32Array(accumulator.length + resampled.length);
    combined.set(accumulator, 0);
    combined.set(resampled, accumulator.length);
    accumulator = combined;

    // Emit CHUNK_SAMPLES-sized chunks.
    while (accumulator.length >= CHUNK_SAMPLES) {
      const chunk = accumulator.slice(0, CHUNK_SAMPLES);
      accumulator = accumulator.slice(CHUNK_SAMPLES);
      const bytes = float32ToPcm16(chunk);
      sequence += 1;
      opts.onChunk(bytes, sequence);
    }
  };

  source.connect(processor);
  processor.connect(ctx.destination);

  return {
    async stop(): Promise<void> {
      processor.disconnect();
      source.disconnect();
      stopTracks();
      await ctx.close();
    },
  };
}
