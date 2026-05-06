"""ProviderRegistry — maps provider IDs and session IDs to live Provider instances.

The registry is created at startup and stored on ``app.state.registry``.
It has two jobs:
  1. Keep all available providers (opencode, claude-code, …) in one place so
     any layer (API, voice WS) can look up the right one without knowing the
     full provider list.
  2. Remember which provider owns each session so session-scoped endpoints
     can route to the right backend without embedding provider info in session IDs.

The session→provider mapping is in-memory only. On a cold restart the map is
empty; ``resolve_for_session`` probes each provider with ``get_session()`` as a
fallback so routing still works for sessions that pre-date the restart.
"""

from __future__ import annotations

from friday.core.provider import Provider, SessionNotFound

_PROVIDER_NAMES: dict[str, str] = {
    "opencode": "OpenCode",
    "claude-code": "Claude Code",
    "codex": "Codex",
}


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}
        self._session_map: dict[str, str] = {}

    # ── Provider management ────────────────────────────────────────────

    def add(self, provider: Provider) -> None:
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> Provider | None:
        return self._providers.get(provider_id)

    def all(self) -> list[Provider]:
        return list(self._providers.values())

    def provider_name(self, provider_id: str) -> str:
        return _PROVIDER_NAMES.get(provider_id, provider_id)

    # ── Session ownership ──────────────────────────────────────────────

    def register_session(self, session_id: str, provider_id: str) -> None:
        self._session_map[session_id] = provider_id

    def lookup_session(self, session_id: str) -> Provider | None:
        """Fast in-memory lookup — returns None on miss (registry cold)."""
        provider_id = self._session_map.get(session_id)
        if provider_id is None:
            return None
        return self._providers.get(provider_id)

    async def resolve_for_session(self, session_id: str) -> Provider | None:
        """Return the provider that owns ``session_id``.

        Checks the in-memory map first. On a miss (cold restart), probes each
        provider's ``get_session()`` and caches the result for next time.
        Returns ``None`` if no provider recognises the session.
        """
        provider = self.lookup_session(session_id)
        if provider is not None:
            return provider
        for p in self._providers.values():
            try:
                await p.get_session(session_id)
                self._session_map[session_id] = p.provider_id
                return p
            except (SessionNotFound, Exception):
                continue
        return None
