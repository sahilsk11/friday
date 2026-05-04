import { useCallback, useEffect, useRef, useState } from 'react';

import type { AudioCaptureHandle } from '@/lib/audioCapture.ts';
import { startAudioCapture } from '@/lib/audioCapture.ts';
import type { PlaybackHandle } from '@/lib/audioPlayback.ts';
import { createPlayback } from '@/lib/audioPlayback.ts';
import type { GatewayConnection } from '@/lib/ws.ts';
import { connectGateway } from '@/lib/ws.ts';
import type { ServerMessage } from '@/protocol.ts';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type SessionState = 'idle' | 'listening' | 'transcribing' | 'running' | 'speaking' | 'error';

export type ChatEventKind = 'user' | 'agent' | 'tool' | 'error';

export interface UserEvent {
  kind: 'user';
  id: string;
  text: string;
}

export interface AgentEvent {
  kind: 'agent';
  id: string; // turnId
  text: string;
  final: boolean;
}

export interface ToolEvent {
  kind: 'tool';
  id: string;
  toolName: string;
  phase: 'start' | 'update' | 'end';
  message?: string;
}

export interface ErrorEvent {
  kind: 'error';
  id: string;
  code: string;
  message: string;
}

export type ChatEvent = UserEvent | AgentEvent | ToolEvent | ErrorEvent;

export interface GatewayState {
  connected: boolean;
  sessionId: string | null;
  sessionState: SessionState;
  messages: ChatEvent[];
  micActive: boolean;
  partialTranscript: string;
}

export interface UseGatewayReturn {
  state: GatewayState;
  createSession(): void;
  sendTurn(text: string): void;
  cancelRun(): void;
  stopTts(): void;
  startMic(): void;
  stopMic(): void;
  togglePtt(): void;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

let _idCounter = 0;
function nextId(): string {
  _idCounter += 1;
  return String(_idCounter);
}

export function useGateway(): UseGatewayReturn {
  const connRef = useRef<GatewayConnection | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const captureRef = useRef<AudioCaptureHandle | null>(null);
  const playbackRef = useRef<PlaybackHandle | null>(null);

  const [state, setState] = useState<GatewayState>({
    connected: false,
    sessionId: null,
    sessionState: 'idle',
    messages: [],
    micActive: false,
    partialTranscript: '',
  });

  // Keep a ref to current sessionId so callbacks do not close over stale value.
  const syncSessionId = useCallback((id: string | null) => {
    sessionIdRef.current = id;
    setState((prev) => ({ ...prev, sessionId: id }));
  }, []);

  const appendMessage = useCallback((evt: ChatEvent) => {
    setState((prev) => ({ ...prev, messages: [...prev.messages, evt] }));
  }, []);

  // Accumulate delta text into the latest in-progress agent turn.
  const applyDelta = useCallback((turnId: string, delta: string) => {
    setState((prev) => {
      const msgs = prev.messages;
      const idx = [...msgs]
        .reverse()
        .findIndex((m) => m.kind === 'agent' && m.id === turnId && !m.final);
      if (idx === -1) {
        const newEvt: AgentEvent = { kind: 'agent', id: turnId, text: delta, final: false };
        return { ...prev, messages: [...msgs, newEvt] };
      }
      const realIdx = msgs.length - 1 - idx;
      const updated = msgs.map((m, i) => {
        if (i !== realIdx) return m;
        const agentEvt = m as AgentEvent;
        return { ...agentEvt, text: agentEvt.text + delta };
      });
      return { ...prev, messages: updated };
    });
  }, []);

  const markAgentFinal = useCallback((turnId: string, finalText: string) => {
    setState((prev) => {
      const msgs = prev.messages;
      const idx = [...msgs]
        .reverse()
        .findIndex((m) => m.kind === 'agent' && m.id === turnId);
      if (idx === -1) {
        const newEvt: AgentEvent = { kind: 'agent', id: turnId, text: finalText, final: true };
        return { ...prev, messages: [...msgs, newEvt] };
      }
      const realIdx = msgs.length - 1 - idx;
      const updated = msgs.map((m, i) => {
        if (i !== realIdx) return m;
        return { ...(m as AgentEvent), text: finalText, final: true };
      });
      return { ...prev, messages: updated };
    });
  }, []);

  // Lazily create or return the existing playback handle.
  const getOrCreatePlayback = useCallback((): PlaybackHandle => {
    if (!playbackRef.current) {
      playbackRef.current = createPlayback({
        onEnded: () => {
          // Nothing extra needed — session state update comes from backend.
        },
      });
    }
    return playbackRef.current;
  }, []);

  const handleMessage = useCallback(
    (msg: ServerMessage) => {
      switch (msg.type) {
        case 'session.created':
          syncSessionId(msg.sessionId);
          // Reset playback for new session.
          playbackRef.current?.stop();
          playbackRef.current = null;
          break;

        case 'session.resumed':
          syncSessionId(msg.sessionId);
          break;

        case 'session.state':
          setState((prev) => ({ ...prev, sessionState: msg.state }));
          break;

        case 'stt.partial':
          setState((prev) => ({ ...prev, partialTranscript: msg.text }));
          break;

        case 'stt.final':
          // Clear partial and append final user message.
          setState((prev) => ({
            ...prev,
            partialTranscript: '',
            messages: [
              ...prev.messages,
              { kind: 'user', id: nextId(), text: msg.text } satisfies UserEvent,
            ],
          }));
          break;

        case 'tts.started':
          // Each turn gets its own playback. If a previous instance is still
          // around (stop never fired), tear it down and create a fresh one.
          if (playbackRef.current) {
            playbackRef.current.stop();
            playbackRef.current = null;
          }
          getOrCreatePlayback();
          break;

        case 'tts.audio.chunk': {
          const decoded = atob(msg.audioBase64);
          const buf = new ArrayBuffer(decoded.length);
          const bytes = new Uint8Array(buf);
          let byteIdx = 0;
          for (const ch of decoded) {
            bytes[byteIdx] = ch.charCodeAt(0);
            byteIdx += 1;
          }
          getOrCreatePlayback().append(bytes as Uint8Array<ArrayBuffer>);
          break;
        }

        case 'tts.ended':
          // Tell the current playback to finish and DROP the handle so the next
          // turn gets a fresh MediaSource. Without this, every subsequent
          // append no-ops because endOfStream() puts MediaSource in 'ended'.
          playbackRef.current?.end();
          playbackRef.current = null;
          break;

        case 'turn.accepted':
          // The user typed text was already appended in sendTurn — nothing to do.
          break;

        case 'agent.text.delta':
          applyDelta(msg.turnId, msg.text);
          break;

        case 'agent.text.final':
          markAgentFinal(msg.turnId, msg.text);
          break;

        case 'agent.tool':
          appendMessage({
            kind: 'tool',
            id: nextId(),
            toolName: msg.toolName,
            phase: msg.phase,
            message: msg.message,
          });
          break;

        case 'error':
          appendMessage({
            kind: 'error',
            id: nextId(),
            code: msg.code,
            message: msg.message,
          });
          // Any STT-related error: stop local capture so we don't flood the gateway
          // with audio.chunk messages that have no adapter to receive them.
          if (msg.code.startsWith('stt_')) {
            const handle = captureRef.current;
            if (handle) {
              captureRef.current = null;
              setState((prev) => ({ ...prev, micActive: false }));
              handle.stop().catch((err: unknown) => {
                console.error('[useGateway] capture stop on stt error', err);
              });
            }
          }
          break;

        case 'run.cancelled':
          setState((prev) => ({ ...prev, sessionState: 'idle' }));
          break;

        case 'agent.status':
        case 'pong':
          break;

        default: {
          const _exhaustive: never = msg;
          console.warn('Unhandled server message', _exhaustive);
        }
      }
    },
    [syncSessionId, appendMessage, applyDelta, markAgentFinal, getOrCreatePlayback],
  );

  useEffect(() => {
    let cancelled = false;
    let myConn: GatewayConnection | null = null;
    const conn = connectGateway({
      onMessage: handleMessage,
      onOpen: () => {
        if (cancelled) return;
        setState((prev) => ({ ...prev, connected: true }));
      },
      onClose: () => {
        if (cancelled) return;
        setState((prev) => ({ ...prev, connected: false }));
        // Only clear the ref if THIS connection is still the active one
        // (StrictMode's first-mount cleanup must not nullify the second mount's conn).
        if (connRef.current === myConn) connRef.current = null;
      },
      onError: () => {
        if (cancelled) return;
        setState((prev) => ({ ...prev, connected: false }));
      },
    });
    myConn = conn;
    connRef.current = conn;

    return () => {
      cancelled = true;
      conn.close();
      if (connRef.current === myConn) connRef.current = null;
      // Tear down audio on unmount.
      captureRef.current?.stop().catch((err: unknown) => {
        console.error('[useGateway] capture stop error on unmount', err);
      });
      captureRef.current = null;
      playbackRef.current?.stop();
      playbackRef.current = null;
    };
  }, [handleMessage]);

  const createSession = useCallback(() => {
    connRef.current?.send({ type: 'session.create' });
  }, []);

  const sendTurn = useCallback((text: string) => {
    const sid = sessionIdRef.current;
    if (!sid) {
      console.warn('No active session, cannot send turn');
      return;
    }
    setState((prev) => ({
      ...prev,
      messages: [...prev.messages, { kind: 'user', id: nextId(), text } satisfies UserEvent],
    }));
    connRef.current?.send({ type: 'turn.send', sessionId: sid, text, source: 'typed' });
  }, []);

  const cancelRun = useCallback(() => {
    const sid = sessionIdRef.current;
    if (!sid) return;
    connRef.current?.send({ type: 'run.cancel', sessionId: sid });
  }, []);

  const stopTts = useCallback(() => {
    const sid = sessionIdRef.current;
    if (!sid) return;
    connRef.current?.send({ type: 'tts.stop', sessionId: sid });
    playbackRef.current?.stop();
    playbackRef.current = null;
  }, []);

  const startMic = useCallback(() => {
    const sid = sessionIdRef.current;
    if (!sid) {
      console.warn('[useGateway] startMic: no active session');
      return;
    }
    if (captureRef.current) return; // already capturing

    connRef.current?.send({
      type: 'audio.start',
      sessionId: sid,
      sampleRate: 16000,
      encoding: 'pcm16',
    });

    startAudioCapture({
      sampleRate: 16000,
      onChunk: (pcm16Bytes, sequence) => {
        const currentSid = sessionIdRef.current;
        if (!currentSid) return;
        // Base64-encode without using atob (works in all modern environments).
        let binary = '';
        for (const byte of pcm16Bytes) {
          binary += String.fromCharCode(byte);
        }
        const chunkBase64 = btoa(binary);
        connRef.current?.send({
          type: 'audio.chunk',
          sessionId: currentSid,
          chunkBase64,
          sequence,
        });
      },
    })
      .then((handle) => {
        captureRef.current = handle;
        setState((prev) => ({ ...prev, micActive: true }));
      })
      .catch((err: unknown) => {
        console.error('[useGateway] startAudioCapture failed', err);
        // Roll back the audio.start if capture failed.
        const currentSid = sessionIdRef.current;
        if (currentSid) {
          connRef.current?.send({ type: 'audio.stop', sessionId: currentSid });
        }
      });
  }, []);

  const stopMic = useCallback(() => {
    const sid = sessionIdRef.current;
    const handle = captureRef.current;
    if (!handle) return;
    captureRef.current = null;
    setState((prev) => ({ ...prev, micActive: false }));
    handle.stop().catch((err: unknown) => {
      console.error('[useGateway] capture stop error', err);
    });
    if (sid) {
      connRef.current?.send({ type: 'audio.stop', sessionId: sid });
    }
  }, []);

  const togglePtt = useCallback(() => {
    if (captureRef.current) {
      stopMic();
    } else {
      startMic();
    }
  }, [startMic, stopMic]);

  return { state, createSession, sendTurn, cancelRun, stopTts, startMic, stopMic, togglePtt };
}
