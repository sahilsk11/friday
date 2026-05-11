"""Registry for configured providers and live session ownership."""

from __future__ import annotations

from friday.domain.provider import Provider, SessionNotFound

_PROVIDER_NAMES: dict[str, str] = {
    "opencode": "OpenCode",
    "codex": "Codex",
}


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}
        self._session_map: dict[str, str] = {}

    def add(self, provider: Provider) -> None:
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> Provider | None:
        return self._providers.get(provider_id)

    def all(self) -> list[Provider]:
        return list(self._providers.values())

    def provider_name(self, provider_id: str) -> str:
        return _PROVIDER_NAMES.get(provider_id, provider_id)

    def register_session(self, session_id: str, provider_id: str) -> None:
        self._session_map[session_id] = provider_id

    def lookup_session(self, session_id: str) -> Provider | None:
        provider_id = self._session_map.get(session_id)
        if provider_id is None:
            return None
        return self._providers.get(provider_id)

    async def resolve_for_session(self, session_id: str) -> Provider | None:
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


__all__ = ["ProviderRegistry"]
