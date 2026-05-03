"""Buffer text deltas, emit TTS-sized chunks on sentence/length/timeout.

Port of friday v1's ``pipelines/ttsChunker.ts``. **Verify pipecat's own
sentence aggregator is insufficient before porting** — it likely covers most
of this.
"""
