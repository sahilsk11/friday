import { PipecatClient } from '@pipecat-ai/client-js';
import {
  PipecatClientProvider,
  usePipecatClient,
  usePipecatClientMicControl,
} from '@pipecat-ai/client-react';
import {
  ClientStatus,
  ConnectButton,
  TranscriptOverlay,
  VoiceVisualizer,
} from '@pipecat-ai/voice-ui-kit';
import { WavMediaManager, WebSocketTransport } from '@pipecat-ai/websocket-transport';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { Link, useParams } from 'react-router';

import { ActivityFeed } from '@/components/ActivityFeed';
import { ModelChip } from '@/components/ModelChip';
import { ThinkingIndicator } from '@/components/ThinkingIndicator';
import { fridayBaseUrl } from '@/lib/env';
import { useSelectedModel } from '@/lib/selectedModel';
import { getSession } from '@/lib/sessions';
import type { ModelRef, TranscriptEntry } from '@/types/api';

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
  const sp = new URLSearchParams({ session_id: sessionId });
  return `${base}/api/voice?${sp.toString()}`;
}

export default function VoiceRoom(): React.ReactElement {
  const { id } = useParams<{ id: string }>();
  const [client, setClient] = useState<PipecatClient | null>(null);
  // Tracks which session id this VoiceRoom has already started initializing
  // a transport for. Without this, React 18 StrictMode runs the setup effect
  // twice in dev (mount → cleanup → mount), and the first WavRecorder's
  // begin() is mid-flight when the second mount creates another one. The
  // websocket-transport SDK silently swallows begin() errors inside
  // WavMediaManager.initialize() and still sets _initialized=true, so
  // pcClient.initDevices() resolves successfully even though processor is
  // null. The next click of Connect then throws "Session ended: please call
  // .begin() first" from inside _wavRecorder.getStatus(). Gating on the id
  // here means cleanup runs harmlessly (no-ops while _initialized=false)
  // and the first init completes uninterrupted.
  const initStartedFor = useRef<string | null>(null);

  // Pre-load the persisted transcript so the activity feed shows past
  // turns immediately when reopening an existing session. Once mounted,
  // ActivityFeed only consumes live RTVI events.
  const sessionQuery = useQuery({
    queryKey: ['session', id],
    queryFn: () => {
      if (!id) throw new Error('missing session id');
      return getSession(id);
    },
    enabled: Boolean(id),
  });

  useEffect(() => {
    if (!id) return;
    if (initStartedFor.current === id) return;
    initStartedFor.current = id;
    // WavMediaManager (Web Audio API) over the default DailyMediaManager
    // — the Daily one pulls in @daily-co/daily-js, which spins up its
    // own WebRTC call object purely for mic capture. That's heavy and
    // breaks in headless Chromium. WavMediaManager just calls
    // getUserMedia + a recorder.
    const transport = new WebSocketTransport({
      wsUrl: buildWsUrl(id),
      mediaManager: new WavMediaManager(undefined, 16_000),
    });
    const pcClient = new PipecatClient({
      enableMic: true,
      enableCam: false,
      transport,
    });
    // Resolve mic device + getUserMedia ahead of connect. Without this,
    // PipecatClient.connect() opens the WS but never starts pushing
    // audio frames — STT sits idle. <PipecatAppBase> handles this via
    // initDevicesOnMount; we have to do it explicitly.
    void pcClient.initDevices().catch((err: unknown) => {
      console.error('initDevices failed', err);
    });
    setClient(pcClient);
    return () => {
      void pcClient.disconnect().catch(() => {
        // Already disconnected — ignore.
      });
    };
  }, [id]);

  if (!id) return <p className="p-6 text-sm text-red-300">missing session id</p>;
  if (!client) return <p className="p-6 text-sm text-neutral-400">initializing client…</p>;
  // Wait for the transcript fetch so ActivityFeed can seed in one shot —
  // its initial entries are read on mount and never reconciled later.
  if (sessionQuery.isLoading) {
    return <p className="p-6 text-sm text-neutral-400">loading session…</p>;
  }

  return (
    <PipecatClientProvider client={client}>
      <VoiceRoomShell
        sessionId={id}
        initialTranscript={sessionQuery.data?.transcript ?? []}
      />
    </PipecatClientProvider>
  );
}

function VoiceRoomShell({
  sessionId,
  initialTranscript,
}: {
  sessionId: string;
  initialTranscript: TranscriptEntry[];
}): React.ReactElement {
  const { model: selectedModel, setModel } = useSelectedModel();
  return (
    <div className="mx-auto flex h-screen max-w-5xl flex-col px-6 py-6">
      <header className="mb-6 flex items-center justify-between">
        <Link to="/" className="text-sm text-neutral-400 hover:text-neutral-200">
          ← sessions
        </Link>
        <div className="flex items-center gap-3">
          <ModelChip selected={selectedModel} onChange={setModel} />
          <Link
            to={`/s/${sessionId}/transcript`}
            className="rounded-md border border-neutral-700 px-3 py-1.5 text-xs hover:border-neutral-500"
          >
            transcript
          </Link>
        </div>
      </header>

      <div className="grid flex-1 gap-6 overflow-hidden md:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <section className="flex flex-col items-stretch gap-4 rounded-2xl border border-neutral-800 bg-neutral-950 p-5">
          <div className="flex items-center justify-center text-xs text-neutral-400">
            <ClientStatus />
          </div>

          {/* Local mic visualizer — proves we're hearing you. */}
          <div className="flex h-32 items-center justify-center rounded-xl border border-neutral-800 bg-black/40">
            <VoiceVisualizer
              participantType="local"
              barColor="#10b981"
              barCount={48}
              barGap={2}
              barWidth={4}
            />
          </div>

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
              session state, not a dumb timer. */}
          <ThinkingIndicator />

          {/* Live partial transcript — words appear as you speak. */}
          <div className="min-h-[3rem] rounded-xl border border-neutral-800 bg-black/40 px-3 py-2 text-sm text-neutral-300">
            <TranscriptOverlay participant="local" size="sm" />
          </div>

          {/* Tap-to-end-turn. ElevenLabs realtime STT is in MANUAL commit
              mode (see TRANSPORT.md) — it only finalizes a transcript when
              we explicitly say so. Without this button, you'd talk forever
              and the agent would never get a turn. We don't currently have
              hands-free auto-end-of-turn detection. */}
          <SendTurnButton />
          {/* Manual barge-in. Stops TTS mid-sentence and aborts the in-flight
              opencode turn. Send and Interrupt are independent — interrupt
              just shuts the bot up; you tap Send when you've got a new turn
              ready. No VAD yet, so this is the only way to barge in. */}
          <InterruptButton />

          <div className="mt-auto flex items-center justify-between gap-3">
            <MicToggle />
            <ConnectControl />
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

// "Send" button — tells the server we're done speaking, force-commits the
// in-progress transcript on the ElevenLabs side. Server-side handler
// pushes a synthetic VADUserStoppedSpeakingFrame upstream. We piggyback
// the user's current model selection onto the message so the server can
// stamp it on the next finalized transcription before forwarding to
// opencode. Disabled while disconnected.
function SendTurnButton(): React.ReactElement {
  const client = usePipecatClient();
  const { model } = useSelectedModel();
  return (
    <button
      type="button"
      disabled={!client}
      onClick={() => {
        if (!client) return;
        const payload: { model?: ModelRef } = {};
        if (model) payload.model = model;
        client.sendClientMessage('end-turn', payload);
      }}
      className="rounded-md bg-emerald-600 px-4 py-3 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
    >
      Send turn ⏎
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

// Hand-rolled mic toggle. The kit's <UserAudioControl> reads
// `client.selectedCam` via usePipecatClientMediaDevices, which throws on
// WebSocketTransport. usePipecatClientMicControl is the camera-free hook.
function MicToggle(): React.ReactElement {
  const { enableMic, isMicEnabled } = usePipecatClientMicControl();
  return (
    <button
      type="button"
      onClick={() => {
        enableMic(!isMicEnabled);
      }}
      className="rounded-md border border-neutral-700 px-3 py-2 text-sm hover:border-neutral-500"
    >
      {isMicEnabled ? 'mute mic' : 'unmute mic'}
    </button>
  );
}

// The kit's <ConnectButton> is a state-aware *display* — it doesn't call
// client.connect()/disconnect() itself; we wire the callbacks here.
function ConnectControl(): React.ReactElement {
  const client = usePipecatClient();
  return (
    <ConnectButton
      onConnect={() => {
        if (!client) return;
        void client.connect().catch((err: unknown) => {
          console.error('connect failed', err);
        });
      }}
      onDisconnect={() => {
        if (!client) return;
        void client.disconnect().catch(() => {
          // Already disconnected — ignore.
        });
      }}
    />
  );
}
