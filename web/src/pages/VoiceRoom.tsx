import { PipecatClient } from '@pipecat-ai/client-js';
import {
  PipecatClientProvider,
  usePipecatClient,
  usePipecatClientMicControl,
  usePipecatClientTransportState,
} from '@pipecat-ai/client-react';
import { WavMediaManager, WebSocketTransport } from '@pipecat-ai/websocket-transport';
import { useEffect, useState } from 'react';
import { Link, useParams } from 'react-router';

import { fridayBaseUrl } from '@/lib/env';

// THE ONLY PAGE THAT IMPORTS @pipecat-ai/*.
//
// Per jarvis.md FE/BE separation rules:
//   - pipecat types stay in this file.
//   - App data flows through REST/SSE on other pages — never RTVI here.
//
// We construct PipecatClient with WebSocketTransport directly. We don't
// use voice-ui-kit's <PipecatAppBase> (only supports `smallwebrtc` /
// `daily`) and we don't use its UI components either — they call camera
// APIs (`selectedCam`) unconditionally and WebSocketTransport throws on
// those. The minimal UI below is built straight on the React hooks.
//
// Why WebSocket and not WebRTC: friday is one user per machine, browser
// and server share localhost in dev / origin in prod. WebRTC's NAT-
// traversal handshake added 8-15s of connect latency for nothing.
// WebSocket connects in ~50ms.

function buildWsUrl(sessionId: string): string {
  const base = fridayBaseUrl.replace(/^http/, 'ws');
  const sp = new URLSearchParams({ session_id: sessionId });
  return `${base}/api/voice?${sp.toString()}`;
}

export default function VoiceRoom() {
  const { id } = useParams<{ id: string }>();
  const [client, setClient] = useState<PipecatClient | null>(null);

  useEffect(() => {
    if (!id) return;
    // Use WavMediaManager (Web Audio API) instead of the default
    // DailyMediaManager. The Daily one pulls in @daily-co/daily-js which
    // spins up its own WebRTC call object purely for mic capture — heavy,
    // and broken in headless Chromium where it queries video devices that
    // don't exist. WavMediaManager just calls getUserMedia + a recorder.
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
    // PipecatClient.connect() opens the WS but never starts pushing audio
    // frames — STT sits idle. PipecatAppBase does this via
    // `initDevicesOnMount`; we have to do it explicitly.
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

  return (
    <PipecatClientProvider client={client}>
      <VoiceRoomShell sessionId={id} />
    </PipecatClientProvider>
  );
}

function VoiceRoomShell({ sessionId }: { sessionId: string }) {
  return (
    <div className="mx-auto flex h-screen max-w-2xl flex-col px-6 py-6">
      <header className="mb-6 flex items-baseline justify-between">
        <Link to="/" className="text-sm text-neutral-400 hover:text-neutral-200">
          ← sessions
        </Link>
        <Link
          to={`/s/${sessionId}/transcript`}
          className="rounded-md border border-neutral-700 px-3 py-1.5 text-xs hover:border-neutral-500"
        >
          transcript
        </Link>
      </header>

      <div className="flex flex-1 items-center justify-center">
        <div className="flex w-full max-w-sm flex-col gap-4 rounded-2xl border border-neutral-800 bg-neutral-950 p-6 shadow-xl">
          <TransportStatePill />
          <MicToggle />
          <ConnectControl />
        </div>
      </div>
    </div>
  );
}

function TransportStatePill() {
  const state = usePipecatClientTransportState();
  const color =
    state === 'ready' || state === 'connected'
      ? 'bg-emerald-600'
      : state === 'connecting' || state === 'authenticating' || state === 'initializing'
        ? 'bg-amber-600'
        : state === 'error'
          ? 'bg-red-600'
          : 'bg-neutral-700';
  return (
    <div className="flex items-center justify-center gap-2 text-xs text-neutral-400">
      <span className={`h-2 w-2 rounded-full ${color}`} />
      {state}
    </div>
  );
}

function MicToggle() {
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

function ConnectControl() {
  const client = usePipecatClient();
  const state = usePipecatClientTransportState();
  const [busy, setBusy] = useState(false);
  const isConnected =
    state === 'ready' || state === 'connected' || state === 'authenticated';
  const label = busy ? '…' : isConnected ? 'Disconnect' : 'Connect';

  return (
    <button
      type="button"
      disabled={busy || !client}
      onClick={() => {
        if (!client) return;
        setBusy(true);
        const action = isConnected ? client.disconnect() : client.connect();
        void action.finally(() => {
          setBusy(false);
        });
      }}
      className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500 disabled:opacity-50"
    >
      {label}
    </button>
  );
}
