"""FastAPI router for session CRUD + SSE event stream.

Endpoints:
- ``GET    /sessions``               list with metadata
- ``POST   /sessions``               create new (also creates opencode session)
- ``GET    /sessions/{id}``          metadata + transcript
- ``GET    /sessions/{id}/events``   SSE stream of live updates
- ``POST   /sessions/{id}/turn``     text turn (voice path uses this after STT)
- ``POST   /sessions/{id}/cancel``   interrupt current run
"""
