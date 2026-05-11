# Backend Clean Architecture Plan - Phase 8 DONE

## Purpose

This document defines the backend layer model we want for Friday, compares it
against the current implementation, and turns the coupling concerns into a
refactor plan.

The structure is intentionally two-part:

1. Start with the layers and what each layer should own.
2. List the current problems as boundary violations, with the desired shape and
   migration steps for each.

That gives us both a target architecture and an audit checklist.

## Step 1 Completion Notes

Phase 1 is complete. We added the neutral `friday.domain` package, moved the
shared provider ports, provider DTOs, `AgentState`, and provider registry there,
updated active imports to use the neutral package, and left compatibility
re-exports under `agent.core` so existing callers keep working.

## Phase 2 Completion Notes

Phase 2 is complete. We added repository records and ports in
`friday.domain.repositories`, made the SQLite `NarratorStore` implement the
combined repository port, moved stored record types into the neutral domain
package, and updated `NarratorManager` to depend on repository protocols instead
of the concrete SQLite store.

## Phase 3 Completion Notes

Phase 3 is complete. We added `SessionQueryService` in
`friday.application.sessions`, moved the session list/detail store-provider
merge logic out of FastAPI route handlers, wired the service at startup, and
kept API schema construction in the delivery layer.

## Phase 4 Completion Notes

Phase 4 is complete. Narrator snapshots now use provider-neutral
`provider_state` and `provider_context` keys, narrator prompt text refers to the
coding provider instead of OpenCode as the generic source of truth, and the
temporary `opencode_context` fallback has been removed after confirming no
active consumers remain.

## Phase 5 Completion Notes

Phase 5 is complete. `create_narrator_brain` now lives in
`server.app.narrator_brain_factory`, concrete narrator LLM clients are no longer
imported by the narrator policy module, and `JsonNarratorBrain` depends only on
the `JsonChatClient` port defined with the narrator policy types.

## Phase 6 Completion Notes

Phase 6 is complete. `NarratorManager` now delegates snapshot construction,
progress scheduling, provider event ingestion, and final recovery to focused
application collaborators in `friday.application` while preserving the existing
route behavior, persistence schema, provider wiring, and narrator output
semantics.

## Phase 7 Completion Notes

Phase 7 is complete. The LiveKit agent now depends on a neutral
`NarratorBackendClient` protocol and `NarratorEvent` DTO in
`friday.application.voice`, the HTTP implementation lives in
`agent.narrator_client`, narrator event de-duplication and voice playback
decisions are separated from LiveKit calls, and the LiveKit wire DTO mapping is
isolated in `agent.livekit_message_mapper`.

## Phase 8 Completion Notes

Phase 8 is complete. FastAPI startup wiring now lives in
`server.app.composition`; `server.app.main` delegates lifespan construction to
that composition module and keeps route handlers focused on request parsing,
dependency lookup, service calls, and schema mapping. Server composition imports
provider adapters from `friday.infra.providers` rather than directly importing
`agent.core`.

## Post-Phase Cleanup Notes

The backend cleanup sequence after Phase 8 moved concrete provider,
persistence, and narrator LLM infrastructure into `friday.infra`, added a
voice-dispatch preparation service so `ensure_voice_agent` no longer reads the
store directly, and removed the narrator brain's legacy `opencode_context`
snapshot fallback. Compatibility shims remain for older import paths such as
`agent.core`, `server.app.narrator_store`, and `server.app.narrator_llm`.

## Target Layer Model

Clean architecture should make dependency direction obvious:

```text
delivery adapters
  -> application services
    -> domain policies and ports
      <- infrastructure adapters
```

Inner layers should not import outer layers. Infrastructure implements ports
defined by the inner layers. Composition roots wire concrete implementations
together at process startup.

## Layers

### 1. Delivery Layer

Owns external request and response protocols.

Current examples:

- FastAPI routes in `server/app/main.py`
- Pydantic API schemas in `server/app/schemas.py`
- LiveKit room RPC/data handling in `agent/main.py`

Should:

- Parse transport-specific inputs.
- Validate request DTOs.
- Map application results into API responses or LiveKit messages.
- Translate application errors into HTTP/RPC errors.

Should not:

- Choose concrete provider implementations.
- Build application decisions directly from database records.
- Know SQLite, OpenCode, Codex, or LLM client details.

### 2. Application Layer

Owns use cases and workflow orchestration.

Current examples:

- `NarratorManager` in `server/app/narrator.py`
- Session creation and turn submission flows currently split between
  `server/app/main.py` and `NarratorManager`

Should:

- Implement use cases such as create session, submit turn, cancel turn, list
  session detail, recover missing final, and list narrator events.
- Coordinate provider sessions, narrator policy, persistence ports, and event
  publishing.
- Depend only on domain models and ports.

Should not:

- Depend on concrete SQLite store classes.
- Depend on FastAPI, LiveKit, or HTTP client implementations.
- Embed provider-specific names such as `opencode_context` in generic narrator
  workflows.

### 3. Domain Layer

Owns stable business concepts, policies, and port interfaces.

Current examples:

- `Provider` and `ProviderSession` protocols in `friday/domain/provider.py`
- `NarratorBrain` protocol and narrator decision types in
  `server/app/narrator_brain.py`
- `AgentState` in `friday/domain/state.py`
- Stored session and event dataclasses in `friday/domain/repositories.py`

Should:

- Define provider-neutral types such as sessions, turns, messages, models,
  provider events, narrator events, and agent state.
- Define ports for providers, narrator policy, repositories, clocks, and event
  delivery.
- Contain pure decision logic when possible.

Should not:

- Import FastAPI, LiveKit, SQLite, HTTPX, OpenCode, Codex, or concrete LLM
  clients.
- Live under a package name that implies one runtime process owns it, such as
  `agent.core`, if the server also depends on it.

### 4. Provider Infrastructure Layer

Owns concrete coding-provider integrations.

Current examples:

- OpenCode provider in `friday/infra/providers/opencode.py`
- Codex provider in `friday/infra/providers/codex.py`
- OpenCode SSE event parsing in `friday/infra/providers/events.py`

Should:

- Implement provider ports.
- Translate provider-specific wire formats into domain events and messages.
- Own provider-specific HTTP, SSE, CLI, file layout, and retry behavior.

Should not:

- Leak provider-specific names into application snapshots or narrator policy.
- Be imported directly by route handlers except in the composition root.

### 5. Persistence Infrastructure Layer

Owns durable storage.

Current example:

- SQLite `NarratorStore` in
  `friday/infra/persistence/sqlite_narrator_store.py`

Should:

- Implement repository ports such as session repository, turn repository,
  narrator event repository, provider event repository, and narrator transcript
  repository.
- Own SQL, migrations, row parsing, locks, and connection lifecycle.

Should not:

- Be the type application services are coupled to directly.
- Define domain language only because rows happen to use it.

### 6. Narrator LLM Infrastructure Layer

Owns concrete LLM clients used by narrator policy.

Current examples:

- `OpenAICompatibleJsonChatClient` in `friday/infra/narrator_llm/json_chat.py`
- `OpenCodeServerJsonChatClient` in `friday/infra/narrator_llm/json_chat.py`

Should:

- Implement a narrow JSON chat port.
- Hide provider-specific HTTP bodies, model formatting, tool disabling, and
  response parsing.

Should not:

- Be constructed from inside domain/policy modules.

### 7. Realtime Voice Infrastructure Layer

Owns LiveKit and speech vendor integration.

Current examples:

- LiveKit agent process in `agent/main.py`
- LiveKit token and dispatch helpers in `server/app/livekit_tokens.py`
- ElevenLabs STT/TTS construction in `agent/main.py`

Should:

- Implement voice transport and speech adapter ports.
- Translate LiveKit RPC/data events into application commands.
- Translate application/narrator events into room data messages and TTS.

Should not:

- Own turn semantics beyond transport-level concerns.
- Poll backend state directly if an event delivery mechanism can replace it.

### 8. Composition Roots

Own process startup and dependency wiring.

Current examples:

- FastAPI composition in `server/app/composition.py`
- LiveKit agent entrypoint in `agent/main.py`

Should:

- Instantiate settings, concrete infrastructure adapters, repositories, and
  application services.
- Be the only place that imports most concrete adapters.

Should not:

- Contain use-case logic beyond startup and shutdown wiring.

## Current Layer Inventory

Current backend packages map more cleanly than when this plan started, but some
entrypoint-era compatibility modules remain:

- `server/app/main.py`: delivery layer with route handlers and schema mapping.
- `server/app/composition.py`: FastAPI composition root.
- `server/app/narrator.py`: narrator application service coordinating focused
  collaborators.
- `friday/application/narrator_*.py`: narrator snapshot, progress, provider
  event ingestion, recovery, and state collaborators.
- `friday/infra/persistence/sqlite_narrator_store.py`: SQLite persistence
  adapter.
- `server/app/narrator_brain.py`: narrator policy and JSON-chat port.
- `server/app/narrator_brain_factory.py`: narrator brain factory used by
  composition.
- `friday/infra/narrator_llm/json_chat.py`: narrator LLM infrastructure.
- `server/app/livekit_tokens.py`: LiveKit infrastructure.
- `friday/domain/provider.py`: provider ports and shared provider models.
- `friday/infra/providers/opencode.py`: OpenCode provider infrastructure
  adapter.
- `friday/infra/providers/codex.py`: Codex provider infrastructure adapter.
- `friday/infra/providers/events.py`: OpenCode wire parser.
- `agent/core/*`, `server/app/narrator_store.py`, and
  `server/app/narrator_llm.py`: compatibility shims for older import paths.
- `agent/main.py`: LiveKit delivery adapter, speech infrastructure, voice state
  orchestration, HTTP client to backend, and TTS response handling.
- `agent/narrator_client.py`: HTTP client adapter from LiveKit agent to FastAPI.
- `agent/protocol.py`: LiveKit data/RPC protocol DTOs.

## Problems And Desired Shape

### Problem 1: Shared Provider Ports Lived Under `agent.core`

Current state:

- Shared provider ports and state now live under `friday.domain`.
- `agent.core` remains as a compatibility shim for older imports.

Desired shape:

- Move shared domain/provider abstractions into a neutral package, for example
  `friday/domain` or `backend/domain`.
- Keep concrete OpenCode and Codex implementations under infrastructure, for
  example `friday/infra/providers`.
- Let both FastAPI and LiveKit agent code depend on the neutral package.

Migration:

1. Create a neutral backend package for shared domain types and ports.
2. Move `Provider`, `ProviderSession`, `ModelChoice`, `ModelCatalog`,
   `SessionInfo`, `Message`, and `AgentState` there.
3. Update OpenCode/Codex adapters to import from the neutral package.
4. Update server imports.
5. Leave compatibility re-exports temporarily if needed.

### Problem 2: `NarratorManager` Is Too Broad

Current issue:

- `NarratorManager` handles session lifecycle, turn lifecycle, provider binding,
  provider event subscriptions, persistence writes, progress scheduling, final
  recovery, narrator snapshot construction, and narrator brain invocation.

Desired shape:

- Keep `NarratorManager` or rename it to an application service, but split
  responsibilities around ports and smaller collaborators.
- Candidate services:
  - `SessionService`
  - `TurnService`
  - `ProviderEventIngestor`
  - `NarrationService`
  - `RecoveryService`

Migration:

1. Introduce repository ports before splitting behavior.
2. Extract snapshot building into a pure domain/application component.
3. Extract progress scheduling behind a small scheduler interface.
4. Extract provider event handlers into a provider event ingestor.
5. Keep route behavior stable while moving internals.

### Problem 3: Application Layer Depends Directly On SQLite Store

Current state:

- `NarratorManager`, session query orchestration, and voice-dispatch
  preparation depend on repository protocols.
- Route handlers do not read from `NarratorStore` directly.
- FastAPI composition still constructs the concrete SQLite store, which is
  expected composition-root behavior.

Desired shape:

- Application layer depends on repository protocols.
- SQLite implementation lives in infrastructure.
- Query use cases expose response-friendly application models so routes do not
  assemble domain data from store rows and provider calls directly.

Migration:

1. Define repository protocols:
   - `SessionRepository`
   - `NarratorMessageRepository`
   - `NarratorEventRepository`
   - `ProviderEventRepository`
   - `TurnRepository`
2. Make `NarratorStore` implement those protocols.
3. Change application services to accept protocols.
4. Move API query assembly from route handlers into application services.

### Problem 4: Provider-Specific Naming Leaked Into Generic Narrator Logic

Current state:

- Narrator snapshots use `provider_state` and `provider_context`.
- Narrator prompts refer to the coding provider as the generic source of truth.
- The temporary `opencode_context` snapshot fallback has been removed.

Desired shape:

- Use provider-neutral names:
  - `provider_state`
  - `provider_context`
  - `provider_events`
  - `provider_final_text`
- Let adapter-specific details stay in adapter payloads only.

Migration:

1. Rename snapshot keys.
2. Update narrator prompt text to say "coding provider" or "provider session".
3. Update probe scripts and tests.
4. Remove temporary compatibility once no active consumers remain.

### Problem 5: Policy Factory Constructed Infrastructure

Current state:

- `server/app/narrator_brain.py` defines `NarratorBrain`, policy types, and the
  JSON chat client port.
- `server/app/narrator_brain_factory.py` owns factory selection.
- Concrete JSON chat clients live in `friday.infra.narrator_llm`.

Desired shape:

- `NarratorBrain` and policy implementations live in domain/application.
- Concrete chat clients live in infrastructure.
- A composition module constructs the chosen concrete brain at startup.

Migration:

1. Move `create_narrator_brain` to a composition or infrastructure factory
   module.
2. Keep `NarratorBrain`, `NarratorDecision`, `EventedNarratorBrain`, and
   `JsonNarratorBrain` separate from concrete client classes.
3. Inject `JsonChatClient` into `JsonNarratorBrain`.

### Problem 6: Provider Port Combines Commands, Queries, And Catalogs

Current issue:

- `Provider` includes session creation, session attachment, transcript queries,
  session listing, and model catalog listing.

Desired shape:

- Split ports when the codebase needs finer-grained boundaries:
  - `ProviderSessionFactory`
  - `ProviderSession`
  - `ProviderSessionQuery`
  - `ProviderTranscriptQuery`
  - `ProviderModelCatalog`

Migration:

1. Do not split immediately unless tests or new adapters need it.
2. First move the existing protocol to the neutral domain package.
3. Split after repository and package boundaries are clean.

### Problem 7: FastAPI Routes Contained Query Orchestration

Current state:

- `list_sessions` and `get_session_detail` delegate store/provider merge logic
  to `SessionQueryService`.
- Routes translate application results into API schemas.

Desired shape:

- Routes call application query services.
- Application layer returns DTO-like application results.
- Routes only translate to API schemas.

Migration:

1. Add `SessionQueryService`.
2. Move session merge logic out of `server/app/main.py`.
3. Keep Pydantic schemas in delivery layer.

### Problem 8: LiveKit Agent Owns Transport And Workflow Concerns

Current issue:

- `agent/main.py` handles LiveKit session setup, STT/TTS setup, RPC parsing,
  command locking, backend HTTP calls, polling, event translation, and TTS
  playback.

Desired shape:

- LiveKit agent remains the realtime delivery adapter.
- Move backend HTTP calls behind an application/client port.
- Move voice playback decisions into a small voice interaction service.
- Keep LiveKit-specific `session.say`, RPC, and room data publish calls in the
  adapter.

Migration:

1. Extract the backend narrator HTTP client behind an interface.
2. Extract event-to-room-message mapping.
3. Consider replacing polling with server-sent or room-data event delivery later.

## Proposed Package Shape

Exact names can change, but this is the direction:

```text
friday/
  domain/
    state.py
    provider.py
    narrator.py
    repositories.py
  application/
    sessions.py
    turns.py
    narration.py
    recovery.py
  infra/
    persistence/
      sqlite_narrator_store.py
    providers/
      opencode.py
      codex.py
      opencode_events.py
    narrator_llm/
      openai_compatible.py
      opencode_server.py
    livekit/
      dispatch.py
  server/
    main.py
    schemas.py
    composition.py
  voice_agent/
    main.py
    protocol.py
    backend_client.py
```

This does not need to be done in one large move. The initial step can be a
neutral `friday` package that receives shared abstractions while existing
`server` and `agent` entrypoints stay in place.

## Refactor Sequence

### Phase 1: Name The Boundaries

- Add neutral domain package.
- Move provider/state abstractions out of `agent.core`.
- Update imports.
- Keep behavior unchanged.
- Add a small import-direction test or lint check if practical.

### Phase 2: Add Repository Ports

- Define repository protocols.
- Make `NarratorStore` implement them.
- Update `NarratorManager` to accept protocols instead of concrete store.
- Keep SQLite schema and behavior unchanged.

### Phase 3: Move Query Orchestration Out Of Routes

- Add session query service.
- Move `list_sessions` and `get_session_detail` merge logic there.
- Keep FastAPI handlers thin.

### Phase 4: Provider-Neutral Narrator Snapshots

- Rename `opencode_*` snapshot keys to `provider_*`.
- Update narrator prompt and probe fixtures.
- Remove temporary compatibility once no active consumers remain.

### Phase 5: Split Narrator Policy From Infrastructure Factory

- Move concrete narrator brain factory out of policy module.
- Keep `JsonNarratorBrain` dependent only on `JsonChatClient`.
- Wire selected implementation in composition root.

### Phase 6: Decompose `NarratorManager`

- Extract provider event ingestion.
- Extract snapshot builder.
- Extract progress scheduler.
- Extract recovery service if still warranted.

### Phase 7: Clean Up Voice Agent Boundaries

- Extract backend narrator client interface.
- Extract LiveKit message mapper.
- Keep LiveKit/ElevenLabs code isolated to voice infrastructure.

### Phase 8: Move Composition Wiring Out Of Routes

- Move FastAPI startup wiring into a composition module.
- Keep concrete provider, repository, narrator brain, and application service
  construction out of the route module.
- Preserve existing provider startup behavior and shutdown ordering.

## Acceptance Criteria

The refactor is complete enough when:

- Inner/domain modules do not import FastAPI, LiveKit, HTTPX, SQLite, OpenCode,
  Codex, or concrete LLM clients.
- `server` does not import from `agent.core`.
- Concrete adapters import domain/application ports, not the other way around.
- Route handlers are mostly request parsing, dependency lookup, service calls,
  and response mapping.
- `NarratorManager` or its replacement can be unit tested with fake providers,
  fake repositories, and fake narrator brain.
- Provider-specific names do not appear in generic narrator snapshots or API
  models unless they are explicitly adapter payload fields.

## Open Decisions

- Keep `server` and `agent` as top-level entrypoint packages, or move them under
  the neutral root?
- Should provider query ports be split immediately, or after the first boundary
  cleanup?
- Should LiveKit agent keep polling narrator events, or should backend push
  events to the room or a streaming endpoint?
- Should stored dataclasses remain persistence records, or should we introduce
  separate domain entities and persistence row models?

## Immediate Next Step

Use the deferred-items document for the remaining cleanup queue. The main
leftovers are polling-based narrator event delivery, compatibility shims for old
import paths, the broad `NarratorRepository` bridge, provider query-port
splitting, and persistence-flavored stored record names.
