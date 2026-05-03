import { lazy, Suspense } from 'react';
import { Route, Routes } from 'react-router';

// Code-split the voice room — voice-ui-kit + small-webrtc-transport
// is a heavy dependency, no reason to load it on the sessions list.
const SessionsList = lazy(() => import('@/pages/SessionsList'));
const SessionView = lazy(() => import('@/pages/SessionView'));
const VoiceRoom = lazy(() => import('@/pages/VoiceRoom'));

export default function App() {
  return (
    <Suspense fallback={<div className="p-6 text-sm text-neutral-400">loading…</div>}>
      <Routes>
        <Route path="/" element={<SessionsList />} />
        <Route path="/s/:id" element={<VoiceRoom />} />
        <Route path="/s/:id/transcript" element={<SessionView />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Suspense>
  );
}

function NotFound() {
  return (
    <div className="p-6">
      <h1 className="text-lg font-semibold">not found</h1>
      <a href="/" className="text-sm text-neutral-400 hover:text-neutral-200">
        ← sessions
      </a>
    </div>
  );
}
