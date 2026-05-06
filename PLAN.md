# Pipecat Turn-Taking Migration Plan

## Problem

Friday currently uses Pipecat for transport, frame flow, STT, TTS, RTVI, and interruption frames, but it does not use Pipecat's turn-taking layer for the core "when should opencode receive a user turn?" decision.

The current voice pipeline in `server/friday/voice/server.py` is:

```text
transport.input()
  -> ElevenLabs STT, CommitStrategy.VAD, 500ms silence
  -> TurnAccumulator
  -> ProviderSessionProcessor
  -> TTS
  -> RTVI
  -> transport.output()
```

`TurnAccumulator` was added because ElevenLabs VAD-mode commits are frequent transcript segments, not conversational turn boundaries. That part is directionally correct, but the implementation still uses "no committed transcript arrived for N seconds" as a proxy for "the user stopped talking." That proxy fails when the user resumes speaking after a short pause and keeps talking for several seconds before ElevenLabs emits the next committed segment.

Observed failure:

```text
user says clause A
pause ~500ms
ElevenLabs VAD commits clause A
user starts clause B and keeps speaking
no new commit arrives while clause B is ongoing
TurnAccumulator silence timer fires
opencode receives clause A
TTS starts while user is still speaking
user stops because TTS talked over them
clause B later commits as the next turn
loop repeats
```

## Pipecat Findings

Pipecat already separates STT finalization from user turn lifecycle.

- ElevenLabs realtime STT defaults to `CommitStrategy.MANUAL`, described as "Pipecat VAD"; `CommitStrategy.VAD` is "ElevenLabs VAD." See `/Users/sahil.kapur/Projects/pipecat/src/pipecat/services/elevenlabs/stt.py:511`.
- In manual mode, Pipecat sends ElevenLabs a commit when it receives a real `VADUserStoppedSpeakingFrame`. See `/Users/sahil.kapur/Projects/pipecat/src/pipecat/services/elevenlabs/stt.py:663`.
- ElevenLabs committed transcripts become `TranscriptionFrame`s, but `finalized=True` is set only for manual mode. See `/Users/sahil.kapur/Projects/pipecat/src/pipecat/services/elevenlabs/stt.py:947` and `/Users/sahil.kapur/Projects/pipecat/src/pipecat/services/elevenlabs/stt.py:991`.
- Pipecat's `LLMUserAggregatorParams` accepts `vad_analyzer`, `user_turn_strategies`, `user_turn_stop_timeout`, and `user_idle_timeout`. See `/Users/sahil.kapur/Projects/pipecat/src/pipecat/processors/aggregators/llm_response_universal.py:97`.
- `LLMUserAggregator` wires a `UserTurnController` internally and emits `on_user_turn_stopped` with the aggregated user message. See `/Users/sahil.kapur/Projects/pipecat/src/pipecat/processors/aggregators/llm_response_universal.py:452` and `/Users/sahil.kapur/Projects/pipecat/src/pipecat/processors/aggregators/llm_response_universal.py:770`.
- `SpeechTimeoutUserTurnStopStrategy` is the built-in "wait after VAD stop, wait for STT finalization/latency, then stop the user turn" strategy. See `/Users/sahil.kapur/Projects/pipecat/src/pipecat/turns/user_stop/speech_timeout_user_turn_stop_strategy.py:26`.
- That strategy cancels pending stop timers when the user starts speaking again. See `/Users/sahil.kapur/Projects/pipecat/src/pipecat/turns/user_stop/speech_timeout_user_turn_stop_strategy.py:122`.
- It only triggers stop when the user is not speaking and transcript text exists. See `/Users/sahil.kapur/Projects/pipecat/src/pipecat/turns/user_stop/speech_timeout_user_turn_stop_strategy.py:250`.

Canonical Pipecat voice examples use:

```text
transport.input()
  -> STT
  -> user_aggregator
  -> LLM
  -> TTS
  -> transport.output()
  -> assistant_aggregator
```

Relevant examples:

- `/Users/sahil.kapur/Projects/pipecat/examples/voice/voice-elevenlabs.py`
- `/Users/sahil.kapur/Projects/pipecat/examples/transports/transports-small-webrtc.py`
- `/Users/sahil.kapur/Projects/pipecat/examples/features/features-concurrent-llm-rtvi-ignored-sources.py`
- `/Users/sahil.kapur/Projects/pipecat/examples/voice/voice-assemblyai-turn-detection.py`

## Design Goal

Use Pipecat for turn-taking. Keep Friday custom code only where the domain is actually Friday-specific:

- attaching to an opencode session
- sending a completed user turn to opencode
- streaming opencode deltas/tool events back into Pipecat TTS and RTVI
- controlling Friday-specific UI toggles like model choice, tool narration, and TTS enabled state

## Target Pipeline

Preferred target:

```text
transport.input()
  -> ElevenLabsRealtimeSTTService(commit_strategy=MANUAL)
  -> Pipecat user turn aggregation/controller
  -> ProviderTurnDispatcher
  -> ProviderSessionProcessor response bridge
  -> TTS
  -> RTVI
  -> transport.output()
```

If `LLMUserAggregator` fits cleanly, use it directly and bind its `on_user_turn_stopped` event to opencode dispatch.

If it does not fit cleanly because Friday does not use `LLMContextFrame` or a Pipecat `LLMService`, write a thin adapter around Pipecat's `UserTurnController`/strategies. Do not reimplement VAD timers or transcript buffering by hand.

## Implementation Steps

### 1. Prove the Current Bug with a Probe

Add a focused backend probe or test that models this sequence without live credentials:

```text
TranscriptionFrame("clause A")
sleep less than Friday's current flush window
simulate user speaking again with no new STT commit
sleep past Friday's current flush window
assert no provider turn is sent while user is speaking
```

This should fail against the current `TurnAccumulator` behavior. Keep this as the regression test before migration.

### 2. Remove ElevenLabs VAD-Mode Commit Ownership

Change `_select_stt()` in `server/friday/voice/server.py` to use upstream `ElevenLabsRealtimeSTTService` in default/manual mode:

```python
ElevenLabsRealtimeSTTService(api_key=elevenlabs_key)
```

Do not use:

```python
CommitStrategy.VAD
ElevenLabsRealtimeSTTSettings(vad_silence_threshold_secs=0.5)
ElevenLabsRealtimeSTTServiceForceCommit
```

Expected result:

- local/Pipecat VAD determines speech stop
- Pipecat sends ElevenLabs commit on real `VADUserStoppedSpeakingFrame`
- ElevenLabs returns `TranscriptionFrame(finalized=True)`
- no need to force commits using synthetic VAD frames

### 3. Introduce Pipecat Turn Ownership

First attempt: use `LLMContextAggregatorPair` or `LLMUserAggregator` with:

```python
LLMUserAggregatorParams(
    vad_analyzer=SileroVADAnalyzer(),
    user_turn_strategies=UserTurnStrategies(
        stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=...)]
    ),
)
```

The exact `user_speech_timeout` should start conservative, likely `1.2s` to `2.0s`, because this is a coding assistant and users naturally pause while thinking. Pipecat's default `0.6s` is optimized for conversational assistants, not dictating multi-clause engineering requests.

Use Pipecat's `on_user_turn_stopped` event as the sole hands-free trigger for opencode.

### 4. Replace `TurnAccumulator`

Delete or retire `server/friday/voice/turn_accumulator.py` once the Pipecat path owns turn end.

The replacement should have one of these shapes:

Option A, preferred:

```text
STT -> LLMUserAggregator -> ProviderTurnDispatcher
```

`ProviderTurnDispatcher` receives the aggregated user message from the aggregator event and calls `session.send_turn(...)`.

Option B, if `LLMUserAggregator` is too coupled to `LLMContextFrame`:

```text
STT -> VADProcessor -> UserTurnProcessor -> ProviderTurnAggregator
```

`ProviderTurnAggregator` should consume `TranscriptionFrame`s and flush only on `UserStoppedSpeakingFrame` produced by Pipecat's `UserTurnProcessor`. This still delegates VAD and turn stop timing to Pipecat.

### 5. Rework Manual Send

Current manual Send sends an `end-turn` client message, calls `accumulator.arm_flush()`, and pushes a synthetic `VADUserStoppedSpeakingFrame`.

Replace that with an explicit Friday control path that does not lie to Pipecat's VAD state.

Acceptable options:

- If using manual mic mode, disabling the mic should allow real audio/VAD idle handling to close the turn. Verify this with an audio probe.
- If a hard "send now" action is needed, add a dedicated Friday control frame or method on the turn adapter that flushes the current Pipecat aggregation. Do not synthesize `VADUserStoppedSpeakingFrame` unless the user actually stopped speaking according to VAD.

The subagent finding is important: a synthetic `VADUserStoppedSpeakingFrame` is an anti-pattern because the same frame also affects turn timing, TTFB metrics, reconnect gating, and user-speaking state.

### 6. Preserve UI Transcript Behavior

The UI wants visible partial/running transcript text before opencode receives a turn.

Preserve that by forwarding/interpreting:

- `InterimTranscriptionFrame` for live text where possible
- `TranscriptionFrame` for finalized utterance text
- final aggregated user turn from Pipecat as the activity-feed lock-in

Do not use Pipecat's built-in RTVI user transcript event if it still emits per-STT-segment finals that conflict with Friday's feed semantics. It is fine to keep custom RTVI server messages for Friday UI state, but those messages should mirror Pipecat-owned turn events.

### 7. Keep Provider Response Bridge

`ProviderSessionProcessor` remains useful for:

- forwarding opencode text deltas as `LLMTextFrame`
- bracketing response start/end
- applying narration filtering
- emitting tool/assistant RTVI messages
- honoring `tts_enabled`
- cancelling opencode on real interruption

But it should no longer consume raw STT `TranscriptionFrame`s as turns. A completed user turn should reach it through a Pipecat turn event or a dedicated provider dispatcher.

### 8. Verification

Required tests/probes before claiming done:

- Unit: current failure sequence does not dispatch while VAD/user-speaking is active.
- Unit: finalized/manual ElevenLabs-style transcript reaches opencode once per turn.
- Unit: user resumes speaking during the post-stop timeout cancels pending dispatch.
- Unit: explicit Interrupt still aborts opencode and clears TTS.
- Integration: run Pipecat's relevant focused test locally for reference:

```bash
cd /Users/sahil.kapur/Projects/pipecat
uv run pytest tests/test_user_turn_stop_strategy.py -q
```

Previous exploration result: `24 passed`.

- Friday backend tests:

```bash
cd /Users/sahil.kapur/Projects/friday-pipecat-turn-plan/server
uv run pytest tests/test_turn_accumulator.py tests/test_pipecat_adapter.py -q
```

These tests will need updates as `TurnAccumulator` is retired.

- Live voice behavior: open the voice room, talk with a short pause mid-sentence, continue talking for several seconds, and verify opencode does not start until the true turn end.

## Non-Goals

- Do not swap transport back to WebRTC as part of this change.
- Do not change opencode provider/session persistence.
- Do not redesign the ActivityFeed UI.
- Do not introduce a second queue in Friday; opencode still owns provider-side queuing.
- Do not keep both `TurnAccumulator` and Pipecat turn aggregation long-term. That would preserve the same ambiguity under two names.

## Expected Deletions

- `server/friday/voice/elevenlabs_force_commit.py`
- most or all of `server/friday/voice/turn_accumulator.py`
- tests that assert commit-based flush semantics
- server comments claiming "Why no Silero VAD"

## Expected New/Changed Files

- `server/friday/voice/server.py`
- `server/friday/voice/pipecat_adapter.py`
- a new small provider turn adapter if `LLMUserAggregator` cannot call opencode directly
- `server/tests/test_pipecat_turn_integration.py` or equivalent
- updated frontend send/control behavior in `web/src/pages/VoiceRoom.tsx` only if manual Send cannot be expressed cleanly through the backend turn adapter

## Open Questions

1. Can `LLMUserAggregator` be used without an actual Pipecat `LLMService`, by listening to `on_user_turn_stopped` and preventing downstream `LLMContextFrame` from triggering anything?
2. Should Friday use `SpeechTimeoutUserTurnStopStrategy` or Pipecat's default smart-turn analyzer? Start with `SpeechTimeoutUserTurnStopStrategy`; evaluate smart turn only after the basic migration is correct.
3. What is the right `user_speech_timeout` for coding dictation? Test `1.2s`, `1.5s`, and `2.0s` with real voice.
4. Should the Start/Send UI remain, or should voice mode become always-listening with explicit Interrupt? Decide after the backend turn fix is proven.

