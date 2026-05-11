from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Sequence
from typing import Any

import httpx

from server.app.narrator_brain import ChatMessage

logger = logging.getLogger("friday.narrator_llm")


class OpenAICompatibleJsonChatClient:
    """Minimal OpenAI-compatible JSON chat client.

    The narrator uses this narrow interface so provider wiring, response parsing,
    and HTTP concerns stay out of the narration policy code.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_secs: float = 15.0,
    ) -> None:
        self._model = model
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout_secs,
        )

    async def complete_json(
        self,
        *,
        messages: Sequence[ChatMessage],
        schema_name: str,
        json_schema: dict[str, Any],
        temperature: float,
    ) -> dict[str, Any]:
        body = {
            "model": self._model,
            "temperature": temperature,
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": json_schema,
                },
            },
        }
        response = await self._http.post("/chat/completions", json=body)
        payload = self._parse_response_json(response)
        if response.status_code >= 400:
            raise RuntimeError(
                f"chat completion failed: status={response.status_code} "
                f"{self._format_error(payload)}"
            )
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(f"chat completion failed: {self._format_error(payload)}")
        if not isinstance(payload, dict):
            raise RuntimeError("chat completion response must be a JSON object")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError(
                "chat completion response missing choices: "
                f"keys={sorted(str(key) for key in payload.keys())}"
            )
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise RuntimeError("chat completion choice missing message")
        content = message.get("content")
        if not isinstance(content, str):
            raise ValueError("chat completion content must be a string")
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("chat completion JSON content must be an object")
        return parsed

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _parse_response_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as err:
            raise RuntimeError(
                "chat completion returned non-JSON response: "
                f"status={response.status_code} body={response.text[:300]!r}"
            ) from err

    @staticmethod
    def _format_error(payload: Any) -> str:
        if not isinstance(payload, dict):
            return f"body={str(payload)[:300]!r}"
        error = payload.get("error")
        if not isinstance(error, dict):
            return f"body={json.dumps(payload, ensure_ascii=False)[:500]}"
        parts: list[str] = []
        message = error.get("message")
        code = error.get("code")
        metadata = error.get("metadata")
        if message:
            parts.append(f"message={message!r}")
        if code is not None:
            parts.append(f"code={code!r}")
        if isinstance(metadata, dict):
            provider = metadata.get("provider_name")
            raw = metadata.get("raw")
            if provider:
                parts.append(f"provider={provider!r}")
            if raw:
                parts.append(f"raw={str(raw)[:300]!r}")
        return " ".join(parts) if parts else f"error={json.dumps(error, ensure_ascii=False)[:500]}"


class OpenCodeServerJsonChatClient:
    """JSON chat client backed by a running OpenCode server.

    This intentionally exposes the same narrow interface as the OpenAI-compatible
    client: the narrator brain gives us chat messages plus a JSON schema, and we
    return a parsed JSON object. OpenCode sessions are created only for the
    individual narrator decision and are deleted afterward by default.
    """

    _KNOWN_TOOL_IDS = frozenset(
        {
            "bash",
            "edit",
            "grep",
            "glob",
            "list",
            "read",
            "todowrite",
            "webfetch",
            "websearch",
            "write",
        }
    )

    def __init__(
        self,
        *,
        base_url: str,
        model: str = "",
        agent: str = "",
        directory: str = "",
        timeout_secs: float = 15.0,
        disable_tools: bool = True,
        delete_sessions: bool = True,
    ) -> None:
        self._model = model.strip()
        self._agent = agent.strip()
        self._directory = directory.strip()
        self._disable_tools = disable_tools
        self._delete_sessions = delete_sessions
        self._tool_ids: set[str] | None = None
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_secs)

    async def complete_json(
        self,
        *,
        messages: Sequence[ChatMessage],
        schema_name: str,
        json_schema: dict[str, Any],
        temperature: float,
    ) -> dict[str, Any]:
        session_id = await self._create_session()
        try:
            payload = await self._prompt_session(
                session_id=session_id,
                messages=messages,
                schema_name=schema_name,
                json_schema=json_schema,
                temperature=temperature,
            )
            content = self._response_text(payload)
            return _parse_json_object(content)
        finally:
            if self._delete_sessions:
                await self._delete_session(session_id)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _create_session(self) -> str:
        response = await self._http.post(
            "/session",
            params=self._directory_query(),
            json={"title": "Friday narrator"},
        )
        payload = self._parse_response_json(response)
        if response.status_code >= 400:
            raise RuntimeError(
                f"opencode session create failed: status={response.status_code} "
                f"{self._format_error(payload)}"
            )
        if not isinstance(payload, dict):
            raise RuntimeError("opencode session create response missing id")
        session_id = payload.get("id")
        if not isinstance(session_id, str):
            raise RuntimeError("opencode session create response missing id")
        return session_id

    async def _prompt_session(
        self,
        *,
        session_id: str,
        messages: Sequence[ChatMessage],
        schema_name: str,
        json_schema: dict[str, Any],
        temperature: float,
    ) -> Any:
        body: dict[str, Any] = {
            "parts": [
                {
                    "type": "text",
                    "text": _format_json_prompt(
                        messages=messages,
                        schema_name=schema_name,
                        json_schema=json_schema,
                    ),
                }
            ],
        }
        model = self._model_choice()
        if model is not None:
            body["model"] = model
        if self._agent:
            body["agent"] = self._agent
        if self._disable_tools:
            body["tools"] = await self._disabled_tools()
        _ = temperature

        response = await self._http.post(
            f"/session/{session_id}/message",
            params=self._directory_query(),
            json=body,
        )
        payload = self._parse_response_json(response)
        if response.status_code >= 400:
            raise RuntimeError(
                f"opencode prompt failed: status={response.status_code} "
                f"{self._format_error(payload)}"
            )
        return payload

    async def _delete_session(self, session_id: str) -> None:
        with contextlib.suppress(Exception):
            await self._http.delete(
                f"/session/{session_id}",
                params=self._directory_query(),
            )

    async def _disabled_tools(self) -> dict[str, bool]:
        tool_ids = await self._fetch_tool_ids()
        return {tool_id: False for tool_id in sorted(tool_ids)}

    async def _fetch_tool_ids(self) -> set[str]:
        if self._tool_ids is not None:
            return self._tool_ids
        tool_ids = set(self._KNOWN_TOOL_IDS)
        try:
            response = await self._http.get("/experimental/tool/ids")
            payload = self._parse_response_json(response)
            if response.status_code >= 400:
                raise RuntimeError(f"status={response.status_code} {self._format_error(payload)}")
            if isinstance(payload, list):
                tool_ids.update(item for item in payload if isinstance(item, str))
        except Exception as err:
            logger.warning("unable to list opencode tools; using built-in denylist | err=%s", err)
        self._tool_ids = tool_ids
        return tool_ids

    def _model_choice(self) -> dict[str, str] | None:
        if not self._model:
            return None
        provider_id, sep, model_id = self._model.partition("/")
        if not sep or not provider_id or not model_id:
            raise ValueError(
                f"OpenCode narrator model must use provider/model format, got {self._model!r}"
            )
        return {"providerID": provider_id, "modelID": model_id}

    def _directory_query(self) -> dict[str, str]:
        return {"directory": self._directory} if self._directory else {}

    @staticmethod
    def _response_text(payload: Any) -> str:
        if not isinstance(payload, dict):
            raise RuntimeError("opencode prompt response must be a JSON object")
        parts = payload.get("parts")
        if not isinstance(parts, list):
            raise RuntimeError("opencode prompt response missing parts")
        texts: list[str] = []
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
        content = "".join(texts).strip()
        if not content:
            raise RuntimeError("opencode prompt response did not contain text")
        return content

    @staticmethod
    def _parse_response_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as err:
            raise RuntimeError(
                "opencode returned non-JSON response: "
                f"status={response.status_code} body={response.text[:300]!r}"
            ) from err

    @staticmethod
    def _format_error(payload: Any) -> str:
        if isinstance(payload, dict):
            return f"body={json.dumps(payload, ensure_ascii=False)[:500]}"
        return f"body={str(payload)[:300]!r}"


def _format_json_prompt(
    *,
    messages: Sequence[ChatMessage],
    schema_name: str,
    json_schema: dict[str, Any],
) -> str:
    rendered_messages = "\n\n".join(
        f"{message.role.upper()}:\n{message.content}" for message in messages
    )
    return (
        "Complete the following chat request.\n"
        "Return only one valid JSON object. Do not include markdown fences, "
        "commentary, or any text outside the JSON object.\n\n"
        f"JSON schema name: {schema_name}\n"
        f"JSON schema:\n{json.dumps(json_schema, ensure_ascii=False, separators=(',', ':'))}\n\n"
        f"Messages:\n{rendered_messages}"
    )


def _parse_json_object(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(content[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("chat completion JSON content must be an object")
    return parsed
