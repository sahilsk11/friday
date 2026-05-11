declare module '@livekit/throws-transformer/throws' {
  export type Throws<Value, _Error = never> = Value;
}

declare module 'sdp-transform' {
  export interface MediaAttributes {
    rtp?: unknown;
  }
}
