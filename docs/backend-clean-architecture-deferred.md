# Backend Clean Architecture Deferred Items

This document tracks backend clean-architecture cleanup that we are intentionally
not doing yet. These are known boundary smells or follow-up opportunities, not
current correctness issues.

## Voice Agent Still Polls Narrator Events

Current state:

- The LiveKit agent still polls `/api/narrator/sessions/{session_id}/events`
  through the backend client interface.
- Polling is now behind `NarratorBackendClient`, but the delivery mechanism is
  unchanged.

Why skipped:

- The Phase 7 target explicitly listed push delivery as a later consideration.
- Replacing polling needs either a server-sent events endpoint, WebSocket
  channel, or backend-to-room publish path, and that changes runtime behavior
  beyond a boundary cleanup.

What a future fix could look like:

- Add a streaming event port to the backend client protocol.
- Implement SSE or room-data push as infrastructure.
- Keep `VoiceInteractionService` unchanged by feeding it narrator events from
  the new delivery mechanism.

## Compatibility Re-exports For Old Import Paths

Current state:

- `agent/core/provider.py`, `agent/core/state.py`, and
  `agent/core/session_registry.py` are compatibility shims that re-export the
  new `friday.domain` definitions.
- `agent/core/opencode_provider.py`, `agent/core/codex_provider.py`, and
  `agent/core/events.py` are compatibility shims for provider infrastructure
  now living under `friday.infra.providers`.
- `server/app/narrator_store.py` and `server/app/narrator_llm.py` are
  compatibility shims for infrastructure now living under `friday.infra`.
- Active server and provider code imports the neutral packages directly, but
  the old paths still work.

Why skipped:

- The boundary moves prioritized low-risk package changes without breaking any
  older imports, scripts, or probes that may still reference the old paths.

What a future fix could look like:

- Search the repo and any external scripts for the old import paths.
- Delete the shim modules once nothing depends on them.

When to do it:

- After the next round of manual/runtime testing confirms no external callers
  still use the old paths.

## Combined `NarratorRepository` Bridge

Current state:

- The smaller repository protocols exist, but `NarratorManager` accepts the
  combined `NarratorRepository` protocol.

Why skipped:

- `NarratorManager` still owns session, turn, event, message, provider-event,
  recovery, and narration workflows.
- Splitting the injected repositories before splitting the manager would add
  constructor complexity without reducing much behavior yet.

What a future fix could look like:

- Split `NarratorManager` into smaller services.
- Inject only the repository protocol each service needs.
- Remove or narrow the combined bridge protocol once it is no longer useful.

When to do it:

- During the planned `NarratorManager` decomposition phase.

## Session Query Service Still Uses Provider Registry Directly

Current state:

- `SessionQueryService` uses `ProviderRegistry` to list providers and resolve
  session ownership.

Why skipped:

- Phase 3 moved query orchestration out of routes without splitting provider
  query ports.
- The provider port split is already listed as a later clean architecture phase.

What a future fix could look like:

- Introduce narrower provider query ports such as `ProviderSessionQuery`,
  `ProviderTranscriptQuery`, and `ProviderModelCatalog`.
- Inject only the query ports needed by `SessionQueryService`.

When to do it:

- After repository and package boundaries stabilize.
- When a new provider adapter or tests need finer-grained fakes.

## Stored Record Names Are Still Persistence-Flavored

Current state:

- Domain repository records are named `StoredSession`, `StoredTurn`,
  `StoredNarratorEvent`, and related names.

Why skipped:

- Phase 2 moved the records to the neutral domain package without changing
  behavior or vocabulary across the codebase.
- Renaming them now would create churn without changing the architecture much.

What a future fix could look like:

- Introduce domain names such as `SessionRecord`, `TurnRecord`,
  `NarratorEventRecord`, or richer domain entities if the distinction becomes
  useful.
- Keep SQLite row parsing models separate if persistence shape diverges from
  domain shape.

When to do it:

- If persistence rows and application/domain records start needing different
  fields or invariants.
- When broader service decomposition makes the naming distracting.
