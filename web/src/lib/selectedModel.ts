import { useEffect, useState } from 'react';

import { listModels } from './sessions';
import type { ModelRef } from '@/types/api';

// Single source of truth for "which model is the user using right now."
// Persists across reloads via localStorage; seeded from opencode's global
// default the first time the user lands on the app. Pages read + write
// through this hook so a switch in one tab doesn't immediately diverge
// from another (storage events propagate).

const KEY = 'friday.selectedModel';

function read(): ModelRef | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const idx = raw.indexOf('/');
    if (idx < 0) return null;
    return { providerID: raw.slice(0, idx), modelID: raw.slice(idx + 1) };
  } catch {
    return null;
  }
}

function write(m: ModelRef): void {
  try {
    localStorage.setItem(KEY, `${m.providerID}/${m.modelID}`);
  } catch {
    // localStorage disabled — we just won't remember next time.
  }
}

export function useSelectedModel(): {
  model: ModelRef | null;
  setModel: (m: ModelRef) => void;
} {
  const [model, setLocal] = useState<ModelRef | null>(read);

  // First mount: if nothing was saved, seed from opencode's global default.
  // We only fire this when ``model`` is still null so the user's choice is
  // never overwritten.
  useEffect(() => {
    if (model !== null) return;
    let cancelled = false;
    void listModels().then((resp) => {
      if (cancelled || !resp.default) return;
      setLocal((current) => current ?? resp.default);
      if (resp.default) write(resp.default);
    });
    return () => {
      cancelled = true;
    };
  }, [model]);

  // Listen for changes from other tabs so a switch in tab A propagates to
  // tab B without a reload. ``storage`` events only fire in *other* tabs,
  // so the local tab's setModel handles its own update via setLocal.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== KEY) return;
      setLocal(read());
    };
    window.addEventListener('storage', onStorage);
    return () => {
      window.removeEventListener('storage', onStorage);
    };
  }, []);

  const setModel = (m: ModelRef): void => {
    write(m);
    setLocal(m);
  };

  return { model, setModel };
}
