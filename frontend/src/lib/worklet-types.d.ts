// Ambient type declarations for AudioWorkletProcessor which lives in a
// worklet scope not covered by the standard DOM lib.
// See: https://developer.mozilla.org/en-US/docs/Web/API/AudioWorkletProcessor

declare class AudioWorkletProcessor {
  readonly port: MessagePort;
  process(
    inputs: Float32Array[][],
    outputs: Float32Array[][],
    parameters: Record<string, Float32Array>,
  ): boolean;
}

declare function registerProcessor(
  name: string,
  processorCtor: new (options?: AudioWorkletNodeOptions) => AudioWorkletProcessor,
): void;
