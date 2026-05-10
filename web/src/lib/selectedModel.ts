import { useCallback, useEffect, useMemo, useState } from 'react';

import { listModels } from './sessions';
import type { ModelRef } from '@/types/api';

// Single source of truth for "which model is the user using right now."
// Persists across reloads via localStorage; seeded from opencode's global
// default the first time the user lands on the app. Pages read + write
// through this hook so a switch in one tab doesn't immediately diverge
// from another (storage events propagate).

const KEY = 'friday.selectedModel';

function keyFor(harness?: string | null): string {
  return harness ? `${KEY}.${harness}` : KEY;
}

function read(key: string): ModelRef | null {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const idx = raw.indexOf('/');
    if (idx < 0) return null;
    return { providerID: raw.slice(0, idx), modelID: raw.slice(idx + 1) };
  } catch {
    return null;
  }
}

function write(key: string, m: ModelRef): void {
  try {
    localStorage.setItem(key, `${m.providerID}/${m.modelID}`);
  } catch {
    // localStorage disabled — we just won't remember next time.
  }
}

export function useSelectedModel(harness?: string | null, serverModel?: ModelRef | null): {
  model: ModelRef | null;
  setModel: (m: ModelRef) => void;
} {
  const storageKey = useMemo(() => keyFor(harness), [harness]);
  const [state, setState] = useState<{ key: string; model: ModelRef | null }>(() => ({
    key: storageKey,
    model: read(storageKey),
  }));
  const model = state.key === storageKey ? state.model : read(storageKey);

  useEffect(() => {
    if (state.key !== storageKey) {
      setState({ key: storageKey, model: read(storageKey) });
    }
  }, [state.key, storageKey]);

  // First mount: if nothing was saved, seed from server-derived current_model
  // if available, otherwise from the harness default.
  useEffect(() => {
    if (model !== null) return;
    if (serverModel) {
      setState({ key: storageKey, model: serverModel });
      write(storageKey, serverModel);
      return;
    }
    let cancelled = false;
    void listModels(harness ?? undefined).then((resp) => {
      if (cancelled || !resp.default) return;
      setState((current) =>
        current.key === storageKey && current.model !== null
          ? current
          : { key: storageKey, model: resp.default },
      );
      if (resp.default) write(storageKey, resp.default);
    });
    return () => {
      cancelled = true;
    };
  }, [harness, model, storageKey, serverModel]);

  // Listen for changes from other tabs so a switch in tab A propagates to
  // tab B without a reload. ``storage`` events only fire in *other* tabs,
  // so the local tab's setModel handles its own update via setLocal.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key !== storageKey) return;
      setState({ key: storageKey, model: read(storageKey) });
    };
    window.addEventListener('storage', onStorage);
    return () => {
      window.removeEventListener('storage', onStorage);
    };
  }, [storageKey]);

  const setModel = useCallback((m: ModelRef): void => {
    write(storageKey, m);
    setState({ key: storageKey, model: m });
  }, [storageKey]);

  return { model, setModel };
}
