import { useEffect, useRef, useState } from 'react';

import type { ChatEvent } from '@/hooks/useGateway.ts';
import { useGateway } from '@/hooks/useGateway.ts';

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

interface MessageRowProps {
  evt: ChatEvent;
}

function MessageRow({ evt }: MessageRowProps) {
  switch (evt.kind) {
    case 'user':
      return (
        <div className="msg msg--user">
          <span className="msg__label">You</span>
          <span className="msg__text">{evt.text}</span>
        </div>
      );

    case 'agent':
      return (
        <div className="msg msg--agent">
          <span className="msg__label">Agent{evt.final ? '' : ' ...'}</span>
          <span className="msg__text">{evt.text}</span>
        </div>
      );

    case 'tool':
      return (
        <div className="msg msg--tool">
          <span className="msg__badge">{evt.phase}</span>
          <span className="msg__tool-name">{evt.toolName}</span>
          {evt.message ? <span className="msg__text">{evt.message}</span> : null}
        </div>
      );

    case 'error':
      return (
        <div className="msg msg--error">
          <span className="msg__badge">error:{evt.code}</span>
          <span className="msg__text">{evt.message}</span>
        </div>
      );

    default: {
      const _exhaustive: never = evt;
      return <div>{JSON.stringify(_exhaustive)}</div>;
    }
  }
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

export default function App() {
  const { state, createSession, sendTurn, cancelRun, stopTts, startMic, stopMic } =
    useGateway();
  const [inputText, setInputText] = useState('');
  const [handsFree, setHandsFree] = useState(false);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to bottom when messages arrive.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [state.messages]);

  // When hands-free is toggled on/off, start/stop mic accordingly.
  useEffect(() => {
    if (handsFree && state.sessionId) {
      startMic();
    } else if (!handsFree && state.micActive) {
      stopMic();
    }
    // We only want to react to handsFree toggle, not every state change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [handsFree]);

  function handleSend() {
    const text = inputText.trim();
    if (!text) return;
    setInputText('');
    sendTurn(text);
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  function handlePttDown() {
    if (!state.sessionId) return;
    startMic();
  }

  function handlePttUp() {
    if (state.micActive) stopMic();
  }

  const statusLabel = state.connected
    ? state.sessionId
      ? `Connected  session: ${state.sessionId.slice(0, 8)}...`
      : 'Connected  (no session)'
    : 'Disconnected';

  return (
    <div className="app">
      {/* Header */}
      <header className="app-header">
        <span className={`status-dot ${state.connected ? 'status-dot--on' : 'status-dot--off'}`} />
        <span className="status-label">{statusLabel}</span>
        <span className="session-state">{state.sessionState}</span>
        <button onClick={createSession}>New session</button>
      </header>

      {/* Message list */}
      <main className="app-main">
        {state.messages.length === 0 && !state.partialTranscript ? (
          <p className="empty-hint">No messages yet. Create a session and send a turn.</p>
        ) : (
          state.messages.map((evt) => <MessageRow key={evt.id} evt={evt} />)
        )}
        {/* Live partial transcript */}
        {state.partialTranscript ? (
          <div className="msg msg--partial">
            <span className="msg__label">You (speaking...)</span>
            <span className="msg__text msg__text--partial">{state.partialTranscript}</span>
          </div>
        ) : null}
        <div ref={bottomRef} />
      </main>

      {/* Footer */}
      <footer className="app-footer">
        <input
          type="text"
          className="footer-input"
          placeholder="Type a message..."
          value={inputText}
          onChange={(e) => {
            setInputText(e.target.value);
          }}
          onKeyDown={handleKeyDown}
          disabled={!state.sessionId}
        />
        <button onClick={handleSend} disabled={!state.sessionId || inputText.trim() === ''}>
          Send
        </button>
        <button onClick={cancelRun} disabled={!state.sessionId}>
          Cancel
        </button>
        <button onClick={stopTts} disabled={state.sessionState !== 'speaking'}>
          Stop speaking
        </button>
        {/* Push-to-talk: hold to speak */}
        <button
          className={state.micActive ? 'btn--mic-active' : ''}
          onPointerDown={handlePttDown}
          onPointerUp={handlePttUp}
          onPointerLeave={handlePttUp}
          disabled={!state.sessionId}
        >
          {state.micActive ? 'Listening...' : 'Hold to talk'}
        </button>
        {/* Hands-free toggle */}
        <button
          className={handsFree ? 'btn--hf-active' : ''}
          onClick={() => {
            setHandsFree((v) => !v);
          }}
          disabled={!state.sessionId}
        >
          {handsFree ? 'Hands-free ON' : 'Hands-free'}
        </button>
      </footer>

      <style>{styles}</style>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Inline styles (plain CSS, no Tailwind yet)
// ---------------------------------------------------------------------------

const styles = `
.app {
  display: flex;
  flex-direction: column;
  height: 100%;
  max-width: 860px;
  margin: 0 auto;
  background: #fff;
  box-shadow: 0 0 0 1px #e5e5e5;
}

.app-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 16px;
  border-bottom: 1px solid #e5e5e5;
  background: #fafafa;
  flex-shrink: 0;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}
.status-dot--on  { background: #22c55e; }
.status-dot--off { background: #ef4444; }

.status-label {
  font-size: 13px;
  color: #555;
  flex: 1;
}

.session-state {
  font-size: 11px;
  background: #e5e7eb;
  border-radius: 999px;
  padding: 2px 8px;
  color: #374151;
}

.app-main {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.empty-hint {
  color: #aaa;
  font-size: 13px;
  text-align: center;
  margin-top: 40px;
}

.msg {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-width: 80%;
}

.msg--user    { align-self: flex-end; }
.msg--agent   { align-self: flex-start; }
.msg--tool    { align-self: flex-start; opacity: 0.75; }
.msg--error   { align-self: flex-start; }
.msg--partial { align-self: flex-end; opacity: 0.65; }

.msg__label {
  font-size: 11px;
  font-weight: 600;
  color: #888;
}

.msg__text {
  background: #f1f1f1;
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 13px;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}

.msg--user .msg__text {
  background: #2563eb;
  color: #fff;
}

.msg--partial .msg__text {
  background: #93c5fd;
  color: #1e3a8a;
}

.msg__text--partial {
  font-style: italic;
}

.msg--error .msg__text {
  background: #fee2e2;
  color: #7f1d1d;
}

.msg__badge {
  font-size: 10px;
  background: #e5e7eb;
  border-radius: 4px;
  padding: 1px 5px;
  font-family: monospace;
  color: #374151;
  width: fit-content;
}

.msg__tool-name {
  font-size: 12px;
  font-weight: 600;
  color: #6b7280;
}

.app-footer {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  border-top: 1px solid #e5e5e5;
  background: #fafafa;
  flex-shrink: 0;
  flex-wrap: wrap;
}

.footer-input {
  flex: 1;
  min-width: 0;
}

.btn--mic-active {
  background: #ef4444;
  color: #fff;
}

.btn--hf-active {
  background: #22c55e;
  color: #fff;
}
`;
