import {
  type PipecatBaseChildProps,
  Card,
  CardContent,
  ConnectButton,
  Divider,
  ErrorCard,
  PipecatAppBase,
  SpinLoader,
  UserAudioControl,
  VoiceVisualizer,
} from '@pipecat-ai/voice-ui-kit';
import { Link, useParams } from 'react-router';

import { apiUrl } from '@/lib/api';

// THE ONLY PAGE THAT IMPORTS @pipecat-ai/voice-ui-kit.
//
// Per jarvis.md FE/BE separation rules:
//   - voice-ui-kit / pipecat types are confined to this file (and any
//     dedicated hook it spawns).
//   - App data (sessions, transcripts) flows through REST/SSE on
//     other pages — not through RTVI custom messages.
//
// Backend contract (BackendIntegration.md):
//   - PipecatAppBase points `connectParams.webrtcUrl` at /api/offer.
//   - `requestData.session_id` attaches to the existing opencode
//     session so the user picks up where they left off.

export default function VoiceRoom() {
  const { id } = useParams<{ id: string }>();

  if (!id) return <p className="p-6 text-sm text-red-300">missing session id</p>;

  return (
    <div className="mx-auto flex h-screen max-w-2xl flex-col px-6 py-6">
      <header className="mb-6 flex items-baseline justify-between">
        <Link to="/" className="text-sm text-neutral-400 hover:text-neutral-200">
          ← sessions
        </Link>
        <Link
          to={`/s/${id}/transcript`}
          className="rounded-md border border-neutral-700 px-3 py-1.5 text-xs hover:border-neutral-500"
        >
          transcript
        </Link>
      </header>

      <div className="flex flex-1 items-center justify-center">
        <PipecatAppBase
          connectParams={{
            webrtcUrl: apiUrl('/api/offer'),
            requestData: { session_id: id },
          }}
          // waitForICEGathering: force the SmallWebRTCTransport to wait
          // until ICE gathering completes before POSTing the offer. The
          // default (false) trickles candidates in via PATCH, but if the
          // browser gathers TCP-active candidates before UDP host ones,
          // the initial offer can ship with only TCP placeholders. aiortc
          // doesn't accept TCP candidates as a server, so every pair
          // fails the STUN check, ICE never reaches "connected", and the
          // client loops on 21s reconnect cycles. With this flag the
          // offer always contains the full UDP candidate set.
          transportOptions={{ waitForICEGathering: true }}
          initDevicesOnMount
          transportType="smallwebrtc"
          noThemeProvider
        >
          {({ client, handleConnect, handleDisconnect, error }: PipecatBaseChildProps) =>
            !client ? (
              <SpinLoader />
            ) : error ? (
              <ErrorCard>{error}</ErrorCard>
            ) : (
              <Card size="lg" shadow="xlong" rounded="xl">
                <CardContent className="flex flex-col gap-4">
                  <VoiceVisualizer participantType="bot" className="bg-accent rounded-lg" />
                  <Divider />
                  <div className="flex flex-col gap-4">
                    <UserAudioControl size="lg" />
                    <ConnectButton
                      size="lg"
                      onConnect={() => {
                        void handleConnect?.();
                      }}
                      onDisconnect={() => {
                        void handleDisconnect?.();
                      }}
                    />
                  </div>
                </CardContent>
              </Card>
            )
          }
        </PipecatAppBase>
      </div>
    </div>
  );
}
