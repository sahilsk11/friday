"""OpencodeProcessor — bridges OpencodeSession events into the pipecat pipeline.

Frame contract (informed by reading pipecat ~Nov 2025):
- Consumes ``TranscriptionFrame`` (final, ``finalized=True``) → POSTs a turn to opencode
- Consumes ``InterruptionFrame`` → cancels the in-flight opencode turn
- Emits ``LLMTextFrame`` for streaming opencode text deltas (matches the
  contract that the assistant aggregator and TTS service expect)
- Emits ``TTSSpeakFrame`` for canned acks ("on it") that bypass the LLM path

Use ``self.create_task()`` for async work, never raw ``asyncio.create_task()`` —
pipecat's ``TaskManager`` tracks and cleans these up on shutdown.
"""
