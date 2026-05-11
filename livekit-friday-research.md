# LiveKit research for Friday voice migration

## Overview

This document consolidates the LiveKit research most relevant to migrating Friday's voice stack from Pipecat to LiveKit Agents. It focuses on the Python SDK surface, repository mapping, transport model, session and agent abstractions, turn handling, eventing, token auth, provider plugin landscape, and the pieces most likely to matter during implementation.

The goal is to preserve the useful LiveKit context as a working reference before writing a more implementation-specific migration spec.

## Repositories and packages

The core repository for the Python agent framework is `livekit/agents`, which contains the `livekit-agents` package and the primary abstractions such as `AgentSession`, `Agent`, `AgentServer`, turn handling, room I/O, and model integrations.[cite:1] The open-source LiveKit media server is in `livekit/livekit`, while browser integration is handled through the JavaScript client SDK in `livekit/client-sdk-js`.[cite:1]

The main package to center the migration around is `livekit-agents`. The supporting package and plugin surface that appear most relevant to Friday include:

| Package / module | Role | Why it matters for Friday |
|---|---|---|
| `livekit.agents` | Core framework | Provides `AgentSession`, `Agent`, `AgentServer`, `TurnHandlingOptions`, room I/O, model settings, tools, and lifecycle hooks.[cite:1] |
| `livekit.rtc` | Lower-level RTC primitives | Exposes room, participant, track, audio, data publishing, and RPC primitives that replace parts of the current custom voice control layer.[cite:1] |
| `livekit.plugins.deepgram` | STT plugin | Relevant because Friday already uses Deepgram in the current stack.[cite:1] |
| `livekit.plugins.elevenlabs` | STT and TTS plugin | Relevant because Friday already uses ElevenLabs in the current stack.[cite:1] |
| `livekit.plugins.cartesia` | TTS plugin | Relevant because Friday already uses Cartesia in the current stack.[cite:1] |
| `livekit.plugins.silero` | VAD plugin | Relevant because Friday already uses Silero-style VAD in the current stack.[cite:1] |
| `livekit.plugins.turn_detector.multilingual` | Turn detector | Most likely first candidate for replacing Friday's Pipecat turn-boundary setup.[cite:1] |
| `livekit.plugins.noise_cancellation` | Input cleanup | Useful for browser microphone quality and production hardening.[cite:1] |
| `livekit.api` / server SDK | Token generation | Needed for room token issuance to replace the current lightweight query-param auth approach.[cite:1] |

A useful starter codebase for reference is the Python starter app in `livekit-examples/agent-starter-python`, which shows the expected app entrypoint pattern and agent session setup.[cite:1]

## Core abstractions

### AgentSession

`AgentSession` is the most important conceptual replacement for Friday's current Pipecat pipeline assembly. Instead of manually wiring processors and transport stages, LiveKit uses a declarative session object that accepts STT, LLM, TTS, VAD, and turn handling configuration in one place.[cite:1]

A representative shape looks like this:

```python
from livekit.agents import AgentSession, TurnHandlingOptions, room_io
from livekit.plugins import silero, deepgram, cartesia
from livekit.plugins.turn_detector.multilingual import MultilingualModel

session = AgentSession(
    stt=deepgram.STT(model="nova-2"),
    llm=FridayLLMAdapter(),
    tts=cartesia.TTS(model="sonic-english"),
    vad=silero.VAD.load(),
    turn_handling=TurnHandlingOptions(
        turn_detection=MultilingualModel(),
    ),
)
```

`AgentSession.start(...)` then binds that session to a room and an agent instance, and it owns the speech loop, interruptions, state changes, and transcript-related events.[cite:1] This is the clearest replacement for the current `Pipeline`, `PipelineTask`, and `PipelineRunner` layering.[cite:1]

### Agent

The `Agent` class is the behavior layer. This is the place where Friday-specific orchestration would likely live, including greeting behavior, turn hooks, tool definitions, LLM routing, and session-specific augmentation of chat context.[cite:1]

The most relevant hook for Friday is `on_user_turn_completed(...)`, because that is the point where a completed user utterance can be routed into Friday's own provider/session abstraction.[cite:1] In practice, this is a strong candidate for where code context, session state, RAG, or other coding-assistant-specific logic would be injected.

### AgentServer and rtc_session

LiveKit Agents supplies its own agent app server model. The common pattern is to define an `AgentServer`, register an `@server.rtc_session(...)` entrypoint, and then run the server with `agents.cli.run_app(server)`.[cite:1]

That means LiveKit is not just replacing the voice transport; it also provides the job-dispatch and room-entry model that currently sits around Friday's FastAPI WebSocket route.[cite:1] This reduces the amount of custom lifecycle wiring needed for per-room agent processes.

## Transport model and auth

### Rooms instead of raw WebSocket audio

LiveKit's transport model is room-based WebRTC rather than a raw WebSocket carrying serialized PCM frames. In practice, the browser joins a LiveKit room, the agent joins as another participant, and audio flows through the LiveKit server rather than through a FastAPI endpoint using a custom protobuf serializer.[cite:1]

For Friday, that changes the transport boundary significantly. The browser-side integration would move toward `@livekit/client`, while the backend would no longer own low-level audio frame transport in the same way.[cite:1]

### Token-based auth

LiveKit's standard pattern is short-lived room tokens rather than an ad hoc query parameter. This implies a token endpoint on the Friday API side that creates a JWT scoped to room join rights, identity, and room name.[cite:1]

A representative server-side pattern looks like:

```python
from livekit.api import AccessToken, VideoGrants

token = (
    AccessToken()
    .with_identity(user_id)
    .with_grants(VideoGrants(room_join=True, room=room_name))
    .to_jwt()
)
```

This is the direct conceptual replacement for the current thin auth arrangement around the voice endpoint.[cite:1]

## Turn handling and interruption model

### TurnHandlingOptions

LiveKit consolidates turn behavior under `TurnHandlingOptions`, rather than spreading it across multiple manually composed strategies. This is one of the biggest ergonomic differences from Pipecat.[cite:1]

The relevant modes include VAD-only, STT-based endpointing, manual turn control, realtime-LLM-based turn detection, and a multilingual turn detector model designed to incorporate transcript context.[cite:1] For Friday, the strongest default candidate is `MultilingualModel()` combined with explicit endpointing bounds.[cite:1]

### Endpointing and the coding pause window

LiveKit's endpointing options are the most natural place to encode Friday's coding-assistant pause behavior. The key knobs are `min_delay` and `max_delay`, which let the system wait after the user stops speaking before finalizing a turn.[cite:1]

That makes endpointing configuration the first place to test before considering any custom detector implementation. The practical translation is that Friday's pause window is more likely to become session configuration than a stack of custom turn strategy classes.[cite:1]

### Interruptions

Interruptions are treated as a first-class concept in LiveKit Agents. The session can be configured with interruption settings such as enabling or disabling interruptions, choosing a mode, requiring a minimum number of words or duration, and resuming if an interruption is later judged false.[cite:1]

This is important because Friday today distinguishes between aborting an in-flight turn and simply stopping speech output. The built-in interruption system covers part of that surface, but a production migration will likely still need explicit custom controls for "interrupt" versus "stop speaking" behavior at the agent or data-channel layer.[cite:1]

## Data, events, and UI state

### Session events

LiveKit Agents exposes structured session events such as agent state changes, user state changes, transcript events, and conversation item additions.[cite:1] These are the most relevant primitives for replacing Pipecat's RTVI-based voice state surface.

Representative event categories include:

- Agent state transitions such as initializing, listening, thinking, speaking, and closing.[cite:1]
- User state transitions such as speaking, listening, and away.[cite:1]
- User transcription events and finalized conversation items.[cite:1]

That event surface is enough to drive a voice UI directly, or to bridge into Friday's existing SSE stream for frontend compatibility.[cite:1]

### Data channel and RPC

LiveKit also exposes lower-level participant data publishing and RPC registration. This is relevant when the frontend needs to send session controls or receive custom JSON messages outside the built-in speech lifecycle.[cite:1]

That makes the LiveKit data channel and RPC mechanisms the most likely replacement for Friday's current custom RTVI messages for things like sticky settings, custom control events, and non-audio UI synchronization.[cite:1]

## STT, TTS, and VAD integrations

The plugin ecosystem lines up well with Friday's current provider choices. That makes the migration more about transport and session orchestration than about replacing speech vendors.[cite:1]

### STT

Supported and directly relevant options include Deepgram and ElevenLabs, both available through LiveKit plugins.[cite:1] LiveKit also supports inference-based shorthand model configuration, which can simplify setup if LiveKit Cloud is used.[cite:1]

### TTS

Cartesia and ElevenLabs both appear directly supported in the plugin surface.[cite:1] This is useful because it means Friday can keep its preferred voices and quality tradeoffs while still changing frameworks.[cite:1]

### VAD

Silero VAD is exposed through `silero.VAD.load()`, which makes it a straightforward conceptual replacement for the current Pipecat-side VAD usage.[cite:1]

## Room I/O and media configuration

LiveKit's room I/O surface lets the agent configure audio input, noise cancellation, text output synchronization, participant linkage, and cleanup behavior on disconnect.[cite:1] This is where the session's media-level behavior gets shaped.

A representative pattern uses `room_io.RoomOptions(...)` and nested audio-input options. This is relevant to Friday because the current stack manually owns many of these concerns through transport parameters and processor ordering, while LiveKit moves them into structured session configuration.[cite:1]

Noise cancellation is also available via plugin integration. That is useful for production-hardening browser microphone input without adding more custom transport logic.[cite:1]

## Self-hosting and local-first deployment

LiveKit's documentation supports self-hosting the media server. This matters because Friday is local-first and may benefit from running a local or sidecar LiveKit server rather than depending on a cloud-hosted service.[cite:1]

The self-hosting docs and server repository are the main places to look for deployment details, including network and ICE behavior.[cite:1] For Friday specifically, this is where the migration analysis should focus when revisiting the earlier WebRTC latency issues observed on multi-interface machines.

The main questions worth carrying forward are:

- Whether a local binary or Docker sidecar is the best default packaging approach.[cite:1]
- How to constrain interface and ICE behavior to avoid the long connection setup path that previously made WebRTC unattractive in Friday's current environment.[cite:1]
- Whether local-first development and production can share the same room/token model cleanly.[cite:1]

## High-value mapping to Friday

The LiveKit research suggests the following high-level conceptual mapping for a future implementation:

| Current Friday concept | Likely LiveKit concept | Notes |
|---|---|---|
| Pipecat transport and protobuf audio frames | LiveKit room + WebRTC transport | Transport shifts out of FastAPI WebSocket ownership.[cite:1] |
| `Pipeline`, `PipelineTask`, `PipelineRunner` | `AgentSession` and `session.start(...)` | Session orchestration becomes declarative.[cite:1] |
| `ProviderSessionProcessor` | Custom LLM adapter and/or `Agent` logic | Friday-specific provider integration remains custom.[cite:1] |
| Pipecat turn strategies | `TurnHandlingOptions` and endpointing | Strongest area to simplify the current setup.[cite:1] |
| RTVI voice state messages | Session events + data channel / RPC | Same UI concepts, different transport surface.[cite:1] |
| Current auth query param | LiveKit room token | Standardized room-join auth model.[cite:1] |
| Existing STT/TTS provider set | LiveKit speech plugins | Minimal vendor churn required.[cite:1] |

## Most important places to look next

For implementation planning, the highest-value docs and repos to keep open are:

- LiveKit Agents docs landing page, for overall framework structure and navigation.[cite:1]
- Agent session and agent logic docs, for lifecycle, eventing, and core hooks.[cite:1]
- Turn handling and endpointing docs, because this is the most direct replacement for Friday's current turn strategy composition.[cite:1]
- STT and TTS integration docs, to confirm exact provider constructors and available options for Deepgram, ElevenLabs, and Cartesia.[cite:1]
- LiveKit self-hosting docs and the `livekit/livekit` server repo, for the deployment and networking model.[cite:1]
- The Python starter repository, for idiomatic application structure.[cite:1]

## Working conclusion

The most encouraging part of the research is that LiveKit appears to align with Friday's current speech-vendor choices while dramatically simplifying the transport and session orchestration model.[cite:1] The biggest migration work is not vendor replacement; it is adapting Friday's provider/session abstraction and custom voice control surface to LiveKit's room, event, and agent model.[cite:1]

That makes the migration tractable in layers. The first implementation pass can likely preserve Friday's core provider architecture and most non-voice application logic while swapping in LiveKit for transport, session lifecycle, turn handling, and voice UI signaling.[cite:1]
