"""WebRTC signaling + per-connection pipeline assembly.

Pipeline shape::

    transport.input()
      → STT (Deepgram)
      → user_aggregator
      → OpencodeProcessor              # replaces the LLM slot
      → TTS (Cartesia)
      → transport.output()
      → assistant_aggregator
      → RTVIObserver                   # surfaces UI state to voice-ui-kit

App data (sessions, transcripts, agent state) flows via REST/SSE in
``friday.api.sessions`` — **not** via RTVI custom messages.
"""
