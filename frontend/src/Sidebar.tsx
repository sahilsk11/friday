interface Session {
  id: string;
  title: string;
  created: number;
}

interface SidebarProps {
  sessions: Session[];
  onSelect?: (sessionId: string) => void;
  onNew?: () => void;
  open?: boolean;
}

export function Sidebar({ sessions, onSelect, onNew, open }: SidebarProps) {
  return (
    <div
      className="sidebar"
      style={{
        width: open ? '280px' : '0px',
        overflow: 'hidden',
        flexShrink: 0,
        transition: 'width 0.2s ease',
      }}
    >
      <div style={{
        width: '280px',
        height: '100%',
        background: '#1f2937',
        borderRadius: '0 8px 8px 0',
        padding: '16px',
        display: 'flex',
        flexDirection: 'column',
        gap: '12px',
      }}>
        <h2 style={{ fontSize: '14px', fontWeight: 600, color: '#9ca3af', marginBottom: '8px' }}>
          Prior Sessions
        </h2>
        <button
          onClick={onNew}
          style={{
            background: '#2563eb',
            border: 'none',
            color: 'white',
            padding: '10px 16px',
            borderRadius: '6px',
            cursor: 'pointer',
            fontSize: '14px',
            fontWeight: 500,
          }}
        >
          + New Session
        </button>
        {sessions.length === 0 ? (
          <div style={{ color: '#6b7280', fontSize: '13px' }}>No sessions</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px', overflow: 'auto' }}>
            {sessions.slice(0, 20).map(s => (
              <div
                key={s.id}
                onClick={() => onSelect?.(s.id)}
                style={{
                  padding: '10px',
                  background: '#374151',
                  borderRadius: '6px',
                  cursor: 'pointer',
                }}
                title={s.id}
              >
                <div style={{ fontSize: '13px', color: '#e5e7eb', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                  {s.title}
                </div>
                <div style={{ fontSize: '11px', color: '#6b7280', marginTop: '4px' }}>
                  {s.created ? new Date(s.created).toLocaleDateString() : ''}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}