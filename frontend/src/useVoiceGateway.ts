import { useEffect, useRef, useState, useCallback } from 'react';
import type { ServerMessage } from './types';

interface UseVoiceGatewayOptions {
  onMessage?: (message: ServerMessage) => void;
}

export function useVoiceGateway({ onMessage }: UseVoiceGatewayOptions = {}) {
  const [connected, setConnected] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessionTitle, setSessionTitle] = useState<string | null>(null);
  const [sessionState, setSessionState] = useState<string>('idle');
  const [transcript, setTranscript] = useState('');
  const [agentText, setAgentText] = useState('');
  const [lastError, setLastError] = useState<string | null>(null);

  // sessionId is also captured in a ref so audio onaudioprocess (which closes
  // over its own scope) can read the latest value without re-binding.
  const sessionIdRef = useRef<string | null>(null);
  const esRef = useRef<EventSource | null>(null);
  const connectRef = useRef<() => Promise<void>>(undefined);
  const audioContextRef = useRef<AudioContext | null>(null);
  const mediaStreamRef = useRef<MediaStream | null>(null);
  const isRecordingRef = useRef(false);

  // TTS playback. Chunks arrive as MP3 frames per turn; we buffer until
  // tts.ended (one self-contained MP3 blob) and play via AudioContext.
  // Streaming MP3 decode would shave latency but needs MediaSource Extensions
  // — overkill for short responses.
  const ttsBufRef = useRef<Map<string, Uint8Array[]>>(new Map());
  const playbackCtxRef = useRef<AudioContext | null>(null);

  const decodeBase64 = (b64: string): Uint8Array => {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  };

  const playTurn = useCallback(async (turnId: string) => {
    const chunks = ttsBufRef.current.get(turnId);
    if (!chunks || chunks.length === 0) return;
    ttsBufRef.current.delete(turnId);

    const total = chunks.reduce((n, c) => n + c.length, 0);
    const merged = new Uint8Array(total);
    let off = 0;
    for (const c of chunks) {
      merged.set(c, off);
      off += c.length;
    }

    if (!playbackCtxRef.current) {
      playbackCtxRef.current = new AudioContext();
    }
    const ctx = playbackCtxRef.current;
    try {
      const buf = await ctx.decodeAudioData(merged.buffer.slice(0));
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(ctx.destination);
      src.start();
    } catch (err) {
      setLastError(`Audio decode failed: ${String(err)}`);
    }
  }, []);

  const apiPostJson = useCallback(async (path: string, body?: unknown): Promise<Response> => {
    return fetch(path, {
      method: 'POST',
      credentials: 'include',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    });
  }, []);

  const apiPostBytes = useCallback(async (path: string, bytes: ArrayBuffer): Promise<Response> => {
    return fetch(path, {
      method: 'POST',
      credentials: 'include',
      headers: { 'content-type': 'application/octet-stream' },
      body: bytes,
    });
  }, []);

  const connect = useCallback(async () => {
    try {
      const r = await apiPostJson('/api/session');
      if (!r.ok) throw new Error(`session create failed (${r.status})`);
      const j = (await r.json()) as { sessionId: string };
      setSessionId(j.sessionId);
      sessionIdRef.current = j.sessionId;

      const es = new EventSource(`/api/session/${j.sessionId}/events`, {
        withCredentials: true,
      });
      es.onopen = () => setConnected(true);
      es.onerror = () => {
        setConnected(false);
        setLastError('Stream error');
      };
      es.onmessage = (ev) => {
        const message: ServerMessage = JSON.parse(ev.data);
        onMessage?.(message);

        switch (message.type) {
          case 'session.state':
            setSessionState(message.state);
            break;
          case 'stt.partial':
          case 'stt.final':
            setTranscript(message.text);
            break;
          case 'agent.text.delta':
            setAgentText((prev) => prev + message.text);
            break;
          case 'agent.text.final':
            setAgentText(message.text);
            break;
          case 'tts.audio.chunk': {
            const tid = message.turnId;
            const arr = ttsBufRef.current.get(tid) ?? [];
            arr.push(decodeBase64(message.audioBase64));
            ttsBufRef.current.set(tid, arr);
            break;
          }
          case 'tts.ended':
            void playTurn(message.turnId);
            break;
          case 'error':
            setLastError(message.message);
            break;
        }
      };
      esRef.current = es;
    } catch (err) {
      setLastError(`Connect failed: ${String(err)}`);
    }
  }, [apiPostJson, onMessage, playTurn]);

  // Store connect in ref so sendText can call latest version without dependency issues
  connectRef.current = connect;

  const startRecording = useCallback(async () => {
    const sid = sessionIdRef.current;
    if (!sid) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      mediaStreamRef.current = stream;
      audioContextRef.current = new AudioContext({ sampleRate: 16000 });
      isRecordingRef.current = true;

      await apiPostJson(`/api/session/${sid}/audio/start`, {
        sampleRate: 16000,
        encoding: 'pcm16',
      });

      const source = audioContextRef.current.createMediaStreamSource(stream);
      const processor = audioContextRef.current.createScriptProcessor(4096, 1, 1);

      processor.onaudioprocess = (e) => {
        if (!isRecordingRef.current) return;
        const inputData = e.inputBuffer.getChannelData(0);
        const pcmData = new Int16Array(inputData.length);
        for (let i = 0; i < inputData.length; i++) {
          pcmData[i] = Math.max(-1, Math.min(1, inputData[i])) * 0x7fff;
        }
        // Fire-and-forget. Each ~256ms audio frame becomes one POST. On HTTP/2
        // this is cheap (multiplexed); on HTTP/1.1 it's bounded by the
        // 6-conns-per-host cap and may queue, but recording windows are short.
        void apiPostBytes(`/api/session/${sid}/audio/chunk`, pcmData.buffer);
      };

      source.connect(processor);
      processor.connect(audioContextRef.current.destination);
    } catch {
      setLastError('Failed to start recording');
    }
  }, [apiPostJson, apiPostBytes]);

  const stopRecording = useCallback(() => {
    const sid = sessionIdRef.current;
    isRecordingRef.current = false;
    if (audioContextRef.current) {
      audioContextRef.current.close();
      audioContextRef.current = null;
    }
    if (mediaStreamRef.current) {
      mediaStreamRef.current.getTracks().forEach((t) => t.stop());
      mediaStreamRef.current = null;
    }
    if (sid) void apiPostJson(`/api/session/${sid}/audio/stop`);
  }, [apiPostJson]);

  const sendText = useCallback(
    async (text: string) => {
      if (!sessionIdRef.current && connectRef.current) {
        await connectRef.current();
      }
      const sid = sessionIdRef.current;
      if (sid) void apiPostJson(`/api/session/${sid}/turn`, { text, source: 'typed' });
    },
    [apiPostJson]
  );

  const cancelRun = useCallback(() => {
    const sid = sessionIdRef.current;
    if (sid) void apiPostJson(`/api/session/${sid}/cancel`);
  }, [apiPostJson]);

  const stopSpeaking = useCallback(() => {
    const sid = sessionIdRef.current;
    if (sid) void apiPostJson(`/api/session/${sid}/tts/stop`);
  }, [apiPostJson]);

  useEffect(() => {
    // Ping server to check if backend is alive - don't create session until user sends message
    fetch('/api/ping', { method: 'GET', credentials: 'include' })
      .then(() => setConnected(true))
      .catch(() => setConnected(false));
    return () => {
      esRef.current?.close();
    };
  }, []);

  return {
    connected,
    sessionId,
    sessionTitle,
    sessionState,
    transcript,
    agentText,
    error: lastError,
    startRecording,
    stopRecording,
    sendText,
    cancelRun,
    stopSpeaking,
    setSession: (newSessionId: string, title?: string) => {
      esRef.current?.close();
      sessionIdRef.current = newSessionId;
      setSessionId(newSessionId);
      if (title) setSessionTitle(title);
      const es = new EventSource(`/api/session/${newSessionId}/events`, {
        withCredentials: true,
      });
      es.onopen = () => setConnected(true);
      es.onerror = () => {
        setConnected(false);
        setLastError('Stream error');
      };
      es.onmessage = (ev) => {
        const message: ServerMessage = JSON.parse(ev.data);
        onMessage?.(message);

        switch (message.type) {
          case 'session.state':
            setSessionState(message.state);
            break;
          case 'stt.partial':
          case 'stt.final':
            setTranscript(message.text);
            break;
          case 'agent.text.delta':
            setAgentText((prev) => prev + message.text);
            break;
          case 'agent.text.final':
            setAgentText(message.text);
            break;
          case 'tts.audio.chunk': {
            const tid = message.turnId;
            const arr = ttsBufRef.current.get(tid) ?? [];
            arr.push(decodeBase64(message.audioBase64));
            ttsBufRef.current.set(tid, arr);
            break;
          }
          case 'tts.ended':
            void playTurn(message.turnId);
            break;
          case 'error':
            setLastError(message.message);
            break;
        }
      };
      esRef.current = es;
    },
    setSessionTitle,
  };
}
