import { useEffect, useState } from 'react';

// Whether the agent should speak each tool start out loud. Off by default —
// the activity feed already shows tool starts, and speaking them is chatty.
// Persisted to localStorage so the choice survives reloads, and the value
// rides on every ``end-turn`` so the server picks up flips immediately.

const KEY = 'friday.narrateTools';

function read(): boolean {
  try {
    return localStorage.getItem(KEY) === 'true';
  } catch {
    return false;
  }
}

function write(v: boolean): void {
  try {
    localStorage.setItem(KEY, v ? 'true' : 'false');
  } catch {
    // localStorage disabled — won't survive reload, that's fine.
  }
}

export function useNarrateTools(): {
  narrateTools: boolean;
  setNarrateTools: (v: boolean) => void;
} {
  const [narrateTools, setLocal] = useState<boolean>(read);

  // Cross-tab sync: a flip in tab A propagates to tab B without a reload.
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

  const setNarrateTools = (v: boolean): void => {
    write(v);
    setLocal(v);
  };

  return { narrateTools, setNarrateTools };
}
