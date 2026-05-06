import { PipecatClient, RTVIEvent } from '@pipecat-ai/client-js';
import {
  PipecatClientProvider,
  useRTVIClientEvent,
  usePipecatClient,
  usePipecatClientMicControl,
} from '@pipecat-ai/client-react';
import { ClientStatus, VoiceVisualizer } from '@pipecat-ai/voice-ui-kit';
import { WavMediaManager, WebSocketTransport } from '@pipecat-ai/websocket-transport';
import { useQuery } from '@tanstack/react-query';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router';

import { ActivityFeed } from '@/components/ActivityFeed';
import { ModelChip } from '@/components/ModelChip';
import { ThinkingIndicator } from '@/components/ThinkingIndicator';
import { fridayBaseUrl } from '@/lib/env';
import { useNarrateTools } from '@/lib/narrateTools';
import { useSelectedModel } from '@/lib/selectedModel';
import { getSession } from '@/lib/sessions';
import type { AgentState, ModelRef, TranscriptEntry } from '@/types/api';

interface PendingSessionState {
  harness: string;
  directory: string;
  title?: string;
}

// THE ONLY PAGE THAT IMPORTS @pipecat-ai/*.
//
// Per jarvis.md FE/BE separation rules:
//   - pipecat types stay on this page.
//   - App data flows through REST/SSE on the transcript page — never
//     RTVI there. The voice room itself bends that rule for tool /
//     assistant-text events (see TRANSPORT.md).
//
// Layout has two columns: voice controls on the left (visualizer,
// status, mic toggle, connect, live partial transcript) and the live
// activity feed on the right (your finals, the agent's replies, tool
// activity). Both halves consume the same WebSocket — see TRANSPORT.md.
//
// Why we don't use voice-ui-kit's <PipecatAppBase>: that shell hardcodes
// transportType to 'smallwebrtc' | 'daily' (WebRTC-only). The component
// pieces we use here are transport-agnostic — they just consume RTVI
// events from the provider.

function buildWsUrl(sessionId: string): string {
  const base = fridayBaseUrl.replace(/^http/, 'ws');
  return `${base}/api/voice?${new URLSearchParams({ session_id: sessionId })}`;
}

function buildPendingWsUrl(params: PendingSessionState): string {
  const base = fridayBaseUrl.replace(/^http/, 'ws');
  const sp = new URLSearchParams({ harness: params.harness, directory: params.directory });
  if (params.title) sp.set('title', params.title);
  return `${base}/api/voice?${sp}`;
}

export default function VoiceRoom(): React.ReactElement {
  const { id } = useParams<{ id: string }>();
  const location = useLocation();
  const navigate = useNavigate();

  // id === 'new' means we arrived from the new-session modal with pending params.
  const isPending = id === 'new';
  const pendingParams = isPending ? (location.state as PendingSessionState | null) : null;

  // resolvedSessionId is null while the server hasn't confirmed a session_id yet.
  const [resolvedSessionId, setResolvedSessionId] = useState<string | null>(
    isPending ? null : (id ?? null),
  );
  const [client, setClient] = useState<PipecatClient | null>(null);

  // Pre-load the persisted transcript so the activity feed shows past
  // turns immediately when reopening an existing session.
  const sessionQuery = useQuery({
    queryKey: ['session', resolvedSessionId],
    queryFn: () => {
      if (!resolvedSessionId) throw new Error('session id missing');
      return getSession(resolvedSessionId);
    },
    enabled: Boolean(resolvedSessionId),
  });

  // Called by SessionCreatedListener when the server sends session-created.
  const onSessionCreated = useCallback(
    (newId: string) => {
      setResolvedSessionId(newId);
      // Replace the URL in-place so the back button doesn't return to /s/new.
      void navigate(`/s/${newId}`, { replace: true });
    },
    [navigate],
  );

  // WS init — runs once on mount regardless of subsequent URL changes.
  // The ref gate makes it safe under React 18 StrictMode's double-invoke:
  // cleanup runs before _initialized is set so disconnect() is a no-op,
  // and the second mount sees the ref already set and returns early.
  // When the URL later changes from /s/new to /s/<realId> (after
  // session-created), the effect does NOT re-run — the existing transport
  // stays connected without interruption.
  const initRef = useRef(false);
  useEffect(() => {
    if (isPending && !pendingParams) return; // no params → don't connect
    if (initRef.current) return;
    initRef.current = true;

    const wsUrl =
      isPending && pendingParams ? buildPendingWsUrl(pendingParams) : id ? buildWsUrl(id) : null;
    if (!wsUrl) return;

    // WavMediaManager (Web Audio API) over the default DailyMediaManager
    // — the Daily one pulls in @daily-co/daily-js, which spins up its
    // own WebRTC call object purely for mic capture. WavMediaManager just
    // calls getUserMedia + a recorder.
    const transport = new WebSocketTransport({
      wsUrl,
      mediaManager: new WavMediaManager(undefined, 16_000),
    });
    // Mic starts disabled; AutoEnableMicOnConnect flips it once connected.
    const pcClient = new PipecatClient({ enableMic: false, enableCam: false, transport });
    void (async () => {
      try {
        await pcClient.initDevices();
        await pcClient.connect();
      } catch (err) {
        console.error('voice setup failed', err);
      }
    })();
    setClient(pcClient);
    return () => {
      void pcClient.disconnect().catch((err: unknown) => {
        console.warn('voice disconnect failed', err);
      });
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Invalid direct navigation to /s/new without params.
  if (isPending && !pendingParams) {
    return (
      <div className="p-6">
        <p className="mb-2 text-sm text-neutral-400">
          no session parameters — go back and create a session.
        </p>
        <Link to="/" className="text-sm text-neutral-400 hover:text-neutral-200">
          ← sessions
        </Link>
      </div>
    );
  }

  if (!client) return <p className="p-6 text-sm text-neutral-400">initializing…</p>;
  // Wait for the transcript fetch so ActivityFeed can seed in one shot.
  if (sessionQuery.isLoading) {
    return <p className="p-6 text-sm text-neutral-400">loading session…</p>;
  }

  return (
    <PipecatClientProvider client={client}>
      <VoiceRoomShell
        sessionId={resolvedSessionId ?? ''}
        harness={sessionQuery.data?.session.harness ?? pendingParams?.harness ?? null}
        initialTranscript={sessionQuery.data?.transcript ?? []}
        initialAgentState={sessionQuery.data?.agent_state ?? 'idle'}
        isPending={!resolvedSessionId}
        onSessionCreated={onSessionCreated}
      />
    </PipecatClientProvider>
  );
}

function VoiceRoomShell({
  sessionId,
  harness,
  initialTranscript,
  initialAgentState,
  isPending,
  onSessionCreated,
}: {
  sessionId: string;
  harness: string | null;
  initialTranscript: TranscriptEntry[];
  initialAgentState: AgentState;
  isPending: boolean;
  onSessionCreated: (id: string) => void;
}): React.ReactElement {
  const { model: selectedModel, setModel } = useSelectedModel(harness);
  const { narrateTools, setNarrateTools } = useNarrateTools();
  return (
    <div className="mx-auto flex h-screen max-w-5xl flex-col px-6 py-6">
      {isPending && <SessionCreatedListener onSessionCreated={onSessionCreated} />}
      <header className="mb-6 flex items-center justify-between">
        <Link to="/" className="text-sm text-neutral-400 hover:text-neutral-200">
          ← sessions
        </Link>
        <div className="flex items-center gap-3">
          <NarrateToolsToggle value={narrateTools} onChange={setNarrateTools} />
          <ModelChip harness={harness} selected={selectedModel} onChange={setModel} />
          {sessionId && !isPending && (
            <Link
              to={`/s/${sessionId}/transcript`}
              className="rounded-md border border-neutral-700 px-3 py-1.5 text-xs hover:border-neutral-500"
            >
              transcript
            </Link>
          )}
        </div>
      </header>

      <div className="grid flex-1 gap-6 overflow-hidden md:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <section className="flex flex-col items-stretch gap-4 rounded-2xl border border-neutral-800 bg-neutral-950 p-5">
          <div className="flex items-center justify-center text-xs text-neutral-400">
            <ClientStatus />
          </div>

          <AutoEnableMicOnConnect />

          {/* Local mic visualizer — proves we're hearing you. Only rendered
              while the mic is actually open; otherwise we showed a green
              waveform that made it look like STT was running even when the
              user hadn't started a turn yet. */}
          <LocalMicVisualizer />

          {/* Bot visualizer — pulses while TTS plays. */}
          <div className="flex h-16 items-center justify-center rounded-xl border border-neutral-800 bg-black/40">
            <VoiceVisualizer
              participantType="bot"
              barColor="#a3a3a3"
              barCount={32}
              barGap={2}
              barWidth={3}
            />
          </div>

          {/* "Thinking…" indicator — visible only while opencode is busy.
              Bridges the long silent window (probe measured 11–40s) between
              prompt accepted and first content event. Tied to opencode's
              session state, not a dumb timer. Seeded from REST so a
              refresh mid-turn renders the right state on first paint;
              the WS reasserts it on connect via OpencodeProcessor. */}
          <ThinkingIndicator initialThinking={initialAgentState === 'thinking'} />

          {/* Live partial transcript — running text the server emits as
              ElevenLabs commits each ~500ms-pause segment. Replaces (not
              appends) on each message; the final lock-in for the activity
              feed is a separate `user-transcript-final` message. */}
          <RunningUserTranscript />
          {/* Bot-side partial transcripts (live captions of the assistant's
              spoken reply) would go here — the voice-ui-kit version pulled
              both local and bot from the same component. We dropped local
              for our own; bot side never showed reliably and isn't a
              priority. Add back via a dedicated component if needed. */}

          {/* Single Start/Send toggle. Idle: mic muted, no STT spend. Click
              to open the mic and start streaming audio; click again to
              commit the transcript and re-mute. Spacebar (when not focused
              in an input) toggles the same action. ElevenLabs realtime STT
              is in MANUAL commit mode — Send is what actually finalizes
              the transcript. */}
          <RecordButton model={selectedModel} />
          {/* Manual barge-in. Stops TTS mid-sentence and aborts the in-flight
              opencode turn without opening the mic. RecordButton already
              interrupts when started mid-narration, so this is for "shut
              up but I'm not ready to talk yet." */}
          <InterruptButton />

          <div className="mt-auto flex items-center justify-end gap-3">
            <MuteToggle />
            <SpeakerToggle />
          </div>
        </section>

        {/* Activity feed: live conversation + tool starts. */}
        <section className="flex min-h-0 flex-col rounded-2xl border border-neutral-800 bg-neutral-950">
          <header className="flex items-center justify-between border-b border-neutral-800 px-4 py-2.5">
            <span className="text-xs uppercase tracking-wider text-neutral-500">activity</span>
          </header>
          <div className="flex-1 overflow-y-auto">
            <ActivityFeed initialTranscript={initialTranscript} />
          </div>
        </section>
      </div>
    </div>
  );
}

// Listens for the session-created server message the backend sends when a
// new session's SDK UUID is known. Fires once and triggers URL update.
function SessionCreatedListener({
  onSessionCreated,
}: {
  onSessionCreated: (id: string) => void;
}): null {
  const onServerMessage = useCallback(
    (raw: unknown) => {
      const inner: unknown = (raw as { data?: unknown } | null)?.data ?? raw;
      if (typeof inner !== 'object' || inner === null) return;
      if ((inner as { type?: unknown }).type !== 'session-created') return;
      const sid = (inner as { session_id?: unknown }).session_id;
      if (typeof sid === 'string') onSessionCreated(sid);
    },
    [onSessionCreated],
  );
  useRTVIClientEvent(RTVIEvent.ServerMessage, onServerMessage);
  return null;
}

// Flips the mic on as soon as the transport reports Connected. Lives as
// its own component so it can use usePipecatClientMicControl — that hook
// only works inside <PipecatClientProvider>.
function AutoEnableMicOnConnect(): null {
  const { enableMic } = usePipecatClientMicControl();
  const onConnected = useCallback(() => {
    enableMic(true);
  }, [enableMic]);
  useRTVIClientEvent(RTVIEvent.Connected, onConnected);
  return null;
}

// Live running transcript — what we've heard so far this turn.
//
// Replaces (not appends) on each `user-transcript-running` server message
// the backend emits while mirroring STT text. Clears on
// `user-transcript-final` (turn ended, locked into the feed) and on a
// fresh turn's first running message. This is intentionally simple state:
// one string, one box.
//
// We rolled our own instead of voice-ui-kit's <TranscriptOverlay> because
// that component listens to pipecat's built-in user-transcript RTVI event,
// which we disabled on the server (the observer fans every commit out as
// a separate "final=true" — see ActivityFeed and server.py for why).
function RunningUserTranscript(): React.ReactElement {
  const [text, setText] = useState('');
  const onServerMessage = useCallback((raw: unknown) => {
    const inner: unknown = (raw as { data?: unknown } | null)?.data ?? raw;
    if (typeof inner !== 'object' || inner === null) return;
    const t = (inner as { type?: unknown }).type;
    if (t === 'user-transcript-running') {
      const next = (inner as { text?: unknown }).text;
      if (typeof next === 'string') setText(next);
    } else if (t === 'user-transcript-final') {
      setText('');
    }
  }, []);
  useRTVIClientEvent(RTVIEvent.ServerMessage, onServerMessage);
  return (
    <div className="min-h-[3rem] rounded-xl border border-neutral-800 bg-black/40 px-3 py-2 text-sm text-neutral-300">
      {text || <span className="text-neutral-600">…</span>}
    </div>
  );
}

// Renders the green local-audio waveform only while the mic is actually
// open. When muted (idle, or after Send), shows a static "muted" tile so
// it's obvious nothing is being streamed to STT.
function LocalMicVisualizer(): React.ReactElement {
  const { isMicEnabled } = usePipecatClientMicControl();
  if (!isMicEnabled) {
    return (
      <div className="flex h-32 items-center justify-center rounded-xl border border-neutral-800 bg-black/40 text-xs text-neutral-500">
        mic muted
      </div>
    );
  }
  return (
    <div className="flex h-32 items-center justify-center rounded-xl border border-neutral-800 bg-black/40">
      <VoiceVisualizer
        participantType="local"
        barColor="#10b981"
        barCount={48}
        barGap={2}
        barWidth={4}
      />
    </div>
  );
}

// Tracks whether opencode is mid-turn, by listening for the same
// `agent-state` RTVI message ThinkingIndicator consumes. Used to decide
// whether RecordButton needs to fire `interrupt` before unmuting.
function useAgentBusy(): boolean {
  const [busy, setBusy] = useState(false);
  const onServerMessage = useCallback((raw: unknown) => {
    const inner: unknown = (raw as { data?: unknown } | null)?.data ?? raw;
    if (
      typeof inner !== 'object' ||
      inner === null ||
      (inner as { type?: unknown }).type !== 'agent-state'
    ) {
      return;
    }
    const state = (inner as { state?: unknown }).state;
    if (typeof state !== 'string') return;
    setBusy(state === 'thinking');
  }, []);
  useRTVIClientEvent(RTVIEvent.ServerMessage, onServerMessage);
  return busy;
}

// Single primary action: Start (idle) ⇄ Send (recording).
//
// - Start: unmutes the local mic so audio frames flow to the server STT.
//   If the agent is currently narrating, also fires `interrupt` first so
//   the user can barge in cleanly; this matches what the Interrupt button
//   does, but folded into the same action.
// - Send: fires `end-turn` (force-commits the in-progress transcript on
//   ElevenLabs) and re-mutes the mic so we stop spending on audio while
//   the agent works. Piggybacks the model + narrate-tools toggle so the
//   server can stamp them on the next finalized transcription.
//
// Spacebar is wired here too: pressing space anywhere outside text inputs
// or focused buttons toggles Start/Send. Buttons keep their own native
// space-to-activate so the global handler skips them and we don't fire
// the toggle twice.
function RecordButton({ model }: { model: ModelRef | null }): React.ReactElement {
  const client = usePipecatClient();
  const { enableMic, isMicEnabled } = usePipecatClientMicControl();
  const { narrateTools } = useNarrateTools();
  const agentBusy = useAgentBusy();

  const handleToggle = useCallback(() => {
    if (!client) return;
    if (isMicEnabled) {
      const payload: { model?: ModelRef; narrateTools: boolean } = { narrateTools };
      if (model) payload.model = model;
      client.sendClientMessage('end-turn', payload);
      enableMic(false);
    } else {
      // agentBusy reflects opencode's thinking state — it goes idle the
      // moment opencode finishes, while TTS may still be draining its
      // audio queue. interrupt aborts opencode + clears TTS; stop-speaking
      // only clears TTS. Pick the lighter one when there's nothing left
      // to abort, so a barge-in on the TTS tail doesn't kill a turn the
      // user is still happy to receive.
      if (agentBusy) client.sendClientMessage('interrupt');
      else client.sendClientMessage('stop-speaking');
      enableMic(true);
    }
  }, [client, isMicEnabled, enableMic, model, narrateTools, agentBusy]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.code !== 'Space' || e.repeat) return;
      const target = e.target as HTMLElement | null;
      if (target) {
        const tag = target.tagName;
        // Buttons get native space-to-activate; let them handle it so we
        // don't double-fire. Inputs/textareas/contenteditable need space
        // for actual typing.
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'BUTTON') return;
        if (target.isContentEditable) return;
      }
      e.preventDefault();
      handleToggle();
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
    };
  }, [handleToggle]);

  const recording = isMicEnabled;
  return (
    <button
      type="button"
      disabled={!client}
      onClick={handleToggle}
      className={
        recording
          ? 'rounded-md bg-emerald-600 px-4 py-3 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50'
          : 'rounded-md bg-neutral-800 px-4 py-3 text-sm font-medium text-neutral-100 hover:bg-neutral-700 disabled:opacity-50'
      }
    >
      {recording ? 'Send (space)' : 'Start (space)'}
    </button>
  );
}

// Compact pill toggle for "speak tool starts out loud." Off is the default
// — the activity feed shows tools regardless; this only governs TTS.
function NarrateToolsToggle({
  value,
  onChange,
}: {
  value: boolean;
  onChange: (v: boolean) => void;
}): React.ReactElement {
  return (
    <button
      type="button"
      onClick={() => {
        onChange(!value);
      }}
      title={value ? 'Tool narration on — click to mute' : 'Tool narration off — click to enable'}
      className={
        value
          ? 'rounded-md border border-emerald-700 bg-emerald-950 px-3 py-1.5 text-xs text-emerald-200 hover:border-emerald-500'
          : 'rounded-md border border-neutral-700 px-3 py-1.5 text-xs text-neutral-400 hover:border-neutral-500'
      }
    >
      narrate tools: {value ? 'on' : 'off'}
    </button>
  );
}

// Tells the server to stop TTS and abort the running opencode turn. The
// server pushes InterruptionTaskFrame upstream — pipecat clears TTS and
// STT buffers automatically; OpencodeProcessor also calls /abort. After
// tapping this, the user speaks fresh and taps Send when ready.
function InterruptButton(): React.ReactElement {
  const client = usePipecatClient();
  return (
    <button
      type="button"
      disabled={!client}
      onClick={() => {
        if (!client) return;
        client.sendClientMessage('interrupt');
      }}
      className="rounded-md border border-red-700 bg-red-950 px-4 py-2 text-sm font-medium text-red-200 hover:bg-red-900 disabled:opacity-50"
    >
      Interrupt
    </button>
  );
}

// Mic mute/unmute toggle. Independent of the Start/Send flow — lets the
// user mute without committing a transcript. Useful when stepping away
// mid-turn or pausing temporarily.
function MuteToggle(): React.ReactElement {
  const { enableMic, isMicEnabled } = usePipecatClientMicControl();
  const handleToggle = useCallback(() => {
    enableMic(!isMicEnabled);
  }, [enableMic, isMicEnabled]);
  return (
    <button
      type="button"
      onClick={handleToggle}
      title={isMicEnabled ? 'Mic on — click to mute' : 'Mic muted — click to unmute'}
      className={
        isMicEnabled
          ? 'rounded-md border border-neutral-700 px-3 py-1.5 text-xs text-neutral-100 hover:border-neutral-500'
          : 'rounded-md border border-red-700 bg-red-950 px-3 py-1.5 text-xs text-red-200 hover:border-red-500'
      }
    >
      mic: {isMicEnabled ? 'on' : 'off'}
    </button>
  );
}

// Speaker on/off. Master gate for TTS audio output.
//
// Default off on every fresh page load — silent until the user opts in.
// That neatly avoids two annoyances:
//
//   1. Refresh mid-turn doesn't suddenly start narrating mid-sentence.
//   2. When opencode is several steps ahead of TTS, hitting this button
//      stops the queue immediately instead of waiting for it to drain.
//
// The state lives on the server (`OpencodeProcessor.tts_enabled`) so we
// can drop TTSSpeakFrame and LLMTextFrame *before* synthesis runs — a
// client-only mute would still pay the ElevenLabs bill. Toggle here
// pushes a `set-tts` RTVI message; the server flips the flag on the
// active processor. Component state, no localStorage — that's how we
// get the "default off on reload" behavior.
function SpeakerToggle(): React.ReactElement {
  const client = usePipecatClient();
  const [enabled, setEnabled] = useState(false);
  const handleToggle = useCallback(() => {
    if (!client) return;
    const next = !enabled;
    setEnabled(next);
    client.sendClientMessage('set-tts', { enabled: next });
    // Toggling off only flips the gate that drops *new* TTS frames —
    // audio already synthesized and queued in transport.output keeps
    // playing. Pair the flag flip with stop-speaking so the user gets
    // immediate silence.
    if (!next) client.sendClientMessage('stop-speaking');
  }, [client, enabled]);
  return (
    <button
      type="button"
      disabled={!client}
      onClick={handleToggle}
      title={enabled ? 'Speaker on — click to mute' : 'Speaker off — click to enable TTS'}
      className={
        enabled
          ? 'rounded-md border border-emerald-700 bg-emerald-950 px-3 py-1.5 text-xs text-emerald-200 hover:border-emerald-500'
          : 'rounded-md border border-neutral-700 px-3 py-1.5 text-xs text-neutral-400 hover:border-neutral-500'
      }
    >
      speaker: {enabled ? 'on' : 'off'}
    </button>
  );
}
