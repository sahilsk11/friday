import { useState, useEffect } from 'react';
import { useVoiceGateway } from './useVoiceGateway';
import { Sidebar } from './Sidebar';

interface Session {
  id: string;
  title: string;
  created: number;
}

export default function App() {
  const [inputText, setInputText] = useState('');
  const [messages, setMessages] = useState<Array<{ role: 'user' | 'agent'; text: string }>>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(false);
const {
    connected,
    sessionTitle,
    sessionState,
    transcript,
    agentText,
    error,
    startRecording,
    stopRecording,
    sendText,
    cancelRun,
    stopSpeaking,
    setSession,
    setSessionTitle,
  } = useVoiceGateway();
  const [isRecording, setIsRecording] = useState(false);

  useEffect(() => {
    fetch('/api/sessions', { credentials: 'include' })
      .then(r => r.json())
      .then(d => setSessions(d.sessions || []))
      .catch(() => {});
  }, []);

  useEffect(() => {
    if (agentText) {
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last?.role === 'agent') {
          return [...prev.slice(0, -1), { role: 'agent', text: agentText }];
        }
        return [...prev, { role: 'agent', text: agentText }];
      });
    }
  }, [agentText]);

  useEffect(() => {
    if (transcript) {
      setMessages((prev) => {
        const last = prev[prev.length - 1];
        if (last?.role === 'user') {
          return [...prev.slice(0, -1), { role: 'user', text: transcript }];
        }
        return [...prev, { role: 'user', text: transcript }];
      });
    }
  }, [transcript]);

  const handleNewSession = async () => {
    setMessages([]);
    setSessionTitle(null);
    setSidebarOpen(false);
    const r = await fetch('/api/session', { method: 'POST', credentials: 'include' });
    if (r.ok) {
      const j = await r.json();
      setSession(j.sessionId, 'New Session');
    }
  };

  const handleSelectSession = async (opencodeSessionId: string) => {
    const session = sessions.find(s => s.id === opencodeSessionId);
    const title = session?.title || 'Session';

    // Fetch existing messages
    const mr = await fetch(`/api/session/messages/${opencodeSessionId}`, { credentials: 'include' });
    const msgData = await mr.json();
    const priorMessages: Array<{ role: 'user' | 'agent'; text: string }> = [];

    if (msgData.messages) {
      for (const m of msgData.messages) {
        if (m.content) {
          priorMessages.push({
            role: m.role === 'user' ? 'user' : 'agent',
            text: m.content
          });
        }
      }
    }

    // Adopt the session
    const r = await fetch(`/api/session/adopt/${opencodeSessionId}`, {
      method: 'POST',
      credentials: 'include',
    });
    if (r.ok) {
      const j = await r.json();
      setSession(j.sessionId, title);
      if (priorMessages.length > 0) {
        setMessages(priorMessages);
      }
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (inputText.trim()) {
      sendText(inputText);
      setInputText('');
    }
  };

  const toggleRecording = () => {
    if (isRecording) {
      stopRecording();
      setIsRecording(false);
    } else {
      startRecording();
      setIsRecording(true);
    }
  };

  const getStatusColor = () => {
    switch (sessionState) {
      case 'listening':
        return '#4ade80';
      case 'transcribing':
      case 'running':
        return '#fbbf24';
      case 'speaking':
        return '#60a5fa';
      case 'error':
        return '#f87171';
      default:
        return '#9ca3af';
    }
  };

  return (
    <div style={{ height: '100vh', display: 'flex', padding: '20px', gap: '20px', boxSizing: 'border-box' }}>
      <Sidebar sessions={sessions} onSelect={handleSelectSession} onNew={handleNewSession} open={sidebarOpen} />

      <div style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
        <header style={{ display: 'flex', alignItems: 'center', gap: '16px', marginBottom: '20px', padding: '12px', background: '#2a2a2a', borderRadius: '8px' }}>
          <button
            onClick={() => setSidebarOpen(!sidebarOpen)}
            style={{
              background: '#374151',
              border: 'none',
              color: '#e5e7eb',
              padding: '8px 12px',
              borderRadius: '6px',
              cursor: 'pointer',
              fontSize: '14px',
            }}
          >
            {sidebarOpen ? '←' : '☰'}
          </button>
          <h1 style={{ fontSize: '20px', fontWeight: 600 }}>Voice Gateway</h1>
          {sessionTitle && (
            <span style={{ fontSize: '14px', color: '#9ca3af', padding: '4px 8px', background: '#374151', borderRadius: '4px' }}>
              {sessionTitle}
            </span>
          )}
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <div style={{ width: '10px', height: '10px', borderRadius: '50%', background: connected ? '#4ade80' : '#f87171' }} />
          <span style={{ fontSize: '14px', color: '#9ca3af' }}>{connected ? 'Connected' : 'Disconnected'}</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginLeft: 'auto' }}>
          <div style={{ width: '10px', height: '10px', borderRadius: '50%', background: getStatusColor() }} />
          <span style={{ fontSize: '14px', color: '#9ca3af', textTransform: 'capitalize' }}>{sessionState}</span>
        </div>
      </header>

      {error && (
        <div style={{ padding: '12px', background: '#7f1d1d', borderRadius: '8px', marginBottom: '20px', color: '#fca5a5' }}>
          {error}
        </div>
      )}

      <main style={{ flex: 1, overflow: 'auto', background: '#2a2a2a', borderRadius: '8px', padding: '20px', marginBottom: '20px' }}>
        {messages.length === 0 ? (
          <div style={{ color: '#6b7280', textAlign: 'center', marginTop: '100px' }}>
            Start a conversation by typing or using the microphone
          </div>
        ) : (
          messages.map((msg, i) => (
            <div key={i} style={{ marginBottom: '16px', padding: '12px', borderRadius: '8px', background: msg.role === 'user' ? '#374151' : '#1f2937' }}>
              <div style={{ fontSize: '12px', color: '#9ca3af', marginBottom: '4px' }}>{msg.role === 'user' ? 'You' : 'Agent'}</div>
              <div style={{ whiteSpace: 'pre-wrap' }}>{msg.text}</div>
            </div>
          ))
        )}
      </main>

      <footer style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
        <button
          onClick={toggleRecording}
          style={{
            padding: '12px 20px',
            borderRadius: '8px',
            border: 'none',
            background: isRecording ? '#dc2626' : '#2563eb',
            color: 'white',
            cursor: 'pointer',
            fontWeight: 500,
          }}
        >
          {isRecording ? 'Stop Recording' : 'Start Recording'}
        </button>

        <button
          onClick={stopSpeaking}
          style={{
            padding: '12px 20px',
            borderRadius: '8px',
            border: '1px solid #4b5563',
            background: '#374151',
            color: 'white',
            cursor: 'pointer',
          }}
        >
          Stop Speaking
        </button>

        <button
          onClick={cancelRun}
          style={{
            padding: '12px 20px',
            borderRadius: '8px',
            border: '1px solid #4b5563',
            background: '#374151',
            color: 'white',
            cursor: 'pointer',
          }}
        >
          Cancel Run
        </button>

        <form onSubmit={handleSubmit} style={{ flex: 1, display: 'flex', gap: '8px' }}>
          <input
            type="text"
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            placeholder="Type a message..."
            style={{
              flex: 1,
              padding: '12px',
              borderRadius: '8px',
              border: '1px solid #4b5563',
              background: '#1f2937',
              color: 'white',
              outline: 'none',
            }}
          />
          <button
            type="submit"
            style={{
              padding: '12px 24px',
              borderRadius: '8px',
              border: 'none',
              background: '#2563eb',
              color: 'white',
              cursor: 'pointer',
              fontWeight: 500,
            }}
          >
            Send
          </button>
        </form>
      </footer>
      </div>
    </div>
  );
}