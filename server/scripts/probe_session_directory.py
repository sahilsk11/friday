"""Probe script to verify opencode session directory behavior.

Hypothesis: opencode's list endpoint only returns sessions from the directory
it was started in, while get_session works for any session.

Run: cd server && python scripts/probe_session_directory.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from friday.core.opencode_provider import OpencodeProvider

BASE_URL = os.environ.get("OPENCODE_BASE_URL", "http://127.0.0.1:4096")


async def main() -> int:
    print(f"[probe] Testing opencode at {BASE_URL}")
    print(f"[probe] Current working dir: {os.getcwd()}")

    async with OpencodeProvider(BASE_URL) as provider:
        print("\n--- LIST SESSIONS ---")
        sessions = await provider.list_sessions()
        print(f"[probe] Listed {len(sessions)} sessions")
        directories = set()
        for s in sessions:
            directories.add(s.directory)
            print(f"  {s.id[:20]}... | dir={s.directory}")

        print(f"\n[probe] Unique directories in list: {directories}")

        print("\n--- GET SPECIFIC SESSION ---")
        target_id = "ses_1f4cb0986ffeCw29ewzwdXptW1"
        try:
            info = await provider.get_session(target_id)
            print(f"[probe] GET {target_id}")
            print(f"  title: {info.title}")
            print(f"  directory: {info.directory}")
            in_list = any(s.id == target_id for s in sessions)
            print(f"  in list results: {in_list}")
        except Exception as e:
            print(f"[probe] GET failed: {e}")

        print("\n--- ANALYSIS ---")
        if target_id not in [s.id for s in sessions]:
            print(f"[probe] BUG CONFIRMED: Session {target_id} not in list but get_session works")
            return 1
        else:
            print("[probe] Session appears in list - no bug")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))