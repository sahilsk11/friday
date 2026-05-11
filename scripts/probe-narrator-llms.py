from __future__ import annotations

# ruff: noqa: E402,I001

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.app.config import get_settings
from server.app.narrator_brain import (
    ChatMessage,
    JsonChatClient,
    _NARRATOR_DECISION_SCHEMA,
    _NARRATOR_DECISION_SYSTEM,
)
from server.app.narrator_llm import (
    OpenAICompatibleJsonChatClient,
    OpenCodeServerJsonChatClient,
)


@dataclass(frozen=True, slots=True)
class ProbeCase:
    name: str
    snapshot: dict[str, Any]
    expected_action: str | None = None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    backend: str
    case: str
    trial: int
    elapsed_ms: float
    parsed: dict[str, Any] | None
    error: str | None
    expected_action: str | None

    @property
    def completed(self) -> bool:
        if self.error is not None or self.parsed is None:
            return False
        return isinstance(self.parsed.get("action"), str)

    @property
    def expected_match(self) -> bool:
        if not self.completed:
            return False
        if self.expected_action is None:
            return True
        return self.parsed.get("action") == self.expected_action


def _cases() -> list[ProbeCase]:
    return [
        ProbeCase(
            name="final-concise",
            expected_action="speak",
            snapshot={
                "decision_type": "final_response",
                "session_state": {
                    "provider_state": "idle",
                    "elapsed_since_user_seconds": 18.4,
                    "elapsed_since_last_speech_seconds": None,
                    "has_spoken_this_user_turn": False,
                },
                "spoken_context": [
                    {
                        "role": "user",
                        "text": "Can you add the OpenCode narrator backend and test it?",
                    }
                ],
                "latest_user_message": "Can you add the OpenCode narrator backend and test it?",
                "provider_context": {
                    "recent_events": [
                        {"type": "tool_started", "summary": "Using apply_patch"},
                        {"type": "tool_started", "summary": "Using bash"},
                        {"type": "final", "summary": "Implemented and verified with mypy."},
                    ],
                    "partial_assistant_text": "",
                    "final_text": (
                        "Implemented the OpenCode narrator backend and verified it with "
                        "mypy, ruff, and a live schema probe."
                    ),
                },
            },
        ),
        ProbeCase(
            name="progress-tool",
            expected_action="speak",
            snapshot={
                "decision_type": "progress_check",
                "session_state": {
                    "provider_state": "thinking",
                    "elapsed_since_user_seconds": 7.2,
                    "elapsed_since_last_speech_seconds": None,
                    "has_spoken_this_user_turn": False,
                },
                "spoken_context": [
                    {"role": "user", "text": "Run the tests and fix whatever breaks."}
                ],
                "latest_user_message": "Run the tests and fix whatever breaks.",
                "provider_context": {
                    "recent_events": [{"type": "tool_started", "summary": "Using bash"}],
                    "partial_assistant_text": "",
                    "final_text": None,
                },
            },
        ),
        ProbeCase(
            name="avoid-repeat",
            expected_action="silent",
            snapshot={
                "decision_type": "progress_check",
                "session_state": {
                    "provider_state": "thinking",
                    "elapsed_since_user_seconds": 11.0,
                    "elapsed_since_last_speech_seconds": 3.1,
                    "has_spoken_this_user_turn": True,
                },
                "spoken_context": [
                    {"role": "user", "text": "Check the failing import."},
                    {"role": "friday", "text": "I'm running the command now."},
                ],
                "latest_user_message": "Check the failing import.",
                "provider_context": {
                    "recent_events": [{"type": "tool_started", "summary": "Using bash"}],
                    "partial_assistant_text": "",
                    "final_text": None,
                },
            },
        ),
        ProbeCase(
            name="trap-no-inventing",
            expected_action="silent",
            snapshot={
                "decision_type": "progress_check",
                "session_state": {
                    "provider_state": "thinking",
                    "elapsed_since_user_seconds": 4.5,
                    "elapsed_since_last_speech_seconds": None,
                    "has_spoken_this_user_turn": False,
                },
                "spoken_context": [
                    {"role": "user", "text": "Why is the deploy failing?"}
                ],
                "latest_user_message": "Why is the deploy failing?",
                "provider_context": {
                    "recent_events": [],
                    "partial_assistant_text": "I need to inspect the logs before I know.",
                    "final_text": None,
                },
            },
        ),
    ]


def _messages(snapshot: dict[str, Any]) -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content=_NARRATOR_DECISION_SYSTEM),
        ChatMessage(
            role="user",
            content=(
                "Here is the current narration decision snapshot:\n"
                f"{json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))}"
            ),
        ),
    ]


async def _discover_opencode_flash_model(base_url: str) -> str:
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=10.0) as http:
        response = await http.get("/config/providers")
        response.raise_for_status()
        payload = response.json()

    candidates: list[str] = []
    for provider in payload.get("providers", []):
        provider_id = provider.get("id")
        if not isinstance(provider_id, str):
            continue
        for model_id, model in (provider.get("models") or {}).items():
            if not isinstance(model_id, str) or not isinstance(model, dict):
                continue
            name = str(model.get("name") or "")
            status = model.get("status")
            haystack = f"{provider_id} {model_id} {name}".lower()
            if status == "active" and "deepseek" in haystack and "flash" in haystack:
                candidates.append(f"{provider_id}/{model_id}")

    preferred = "opencode-go/deepseek-v4-flash"
    if preferred in candidates:
        return preferred
    for candidate in candidates:
        if candidate.startswith("opencode-go/"):
            return candidate
    if candidates:
        return candidates[0]
    raise RuntimeError("No active DeepSeek Flash model found in OpenCode server catalog")


async def _run_one(
    *,
    backend: str,
    client: JsonChatClient,
    case: ProbeCase,
    trial: int,
) -> ProbeResult:
    started = time.perf_counter()
    try:
        parsed = await client.complete_json(
            messages=_messages(case.snapshot),
            schema_name="narrator_decision",
            json_schema=_NARRATOR_DECISION_SCHEMA,
            temperature=0.4,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        return ProbeResult(
            backend=backend,
            case=case.name,
            trial=trial,
            elapsed_ms=elapsed_ms,
            parsed=parsed,
            error=None,
            expected_action=case.expected_action,
        )
    except Exception as err:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return ProbeResult(
            backend=backend,
            case=case.name,
            trial=trial,
            elapsed_ms=elapsed_ms,
            parsed=None,
            error=f"{type(err).__name__}: {err}",
            expected_action=case.expected_action,
        )


async def _run_backend(
    *,
    backend: str,
    client: JsonChatClient,
    cases: list[ProbeCase],
    trials: int,
) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    try:
        for trial in range(1, trials + 1):
            for case in cases:
                result = await _run_one(
                    backend=backend,
                    client=client,
                    case=case,
                    trial=trial,
                )
                results.append(result)
                _print_result(result)
    finally:
        await client.aclose()
    return results


def _print_result(result: ProbeResult) -> None:
    if result.error:
        status = "ERROR"
    elif result.expected_match:
        status = "MATCH"
    else:
        status = "DIFF"
    action = result.parsed.get("action") if result.parsed else None
    text = result.parsed.get("text") if result.parsed else None
    if isinstance(text, str) and len(text) > 100:
        text = text[:97] + "..."
    print(
        f"{status:5} {result.backend:16} {result.case:18} "
        f"trial={result.trial} latency={result.elapsed_ms:7.1f}ms "
        f"action={action!r} text={text!r}"
    )
    if result.error:
        print(f"  error={result.error}")


def _print_summary(results: list[ProbeResult]) -> None:
    print("\nSummary")
    by_backend: dict[str, list[ProbeResult]] = {}
    for result in results:
        by_backend.setdefault(result.backend, []).append(result)
    for backend, rows in by_backend.items():
        latencies = [row.elapsed_ms for row in rows if row.error is None]
        completed_count = sum(1 for row in rows if row.completed)
        expected_matches = sum(1 for row in rows if row.expected_match)
        if latencies:
            mean = statistics.mean(latencies)
            median = statistics.median(latencies)
            p95 = sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)]
            print(
                f"{backend:16} completed={completed_count}/{len(rows)} "
                f"expected_match={expected_matches}/{len(rows)} "
                f"mean={mean:.1f}ms median={median:.1f}ms p95={p95:.1f}ms"
            )
        else:
            print(f"{backend:16} completed={completed_count}/{len(rows)} no successful calls")


async def _amain() -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Compare narrator JSON LLM backends using identical sample snapshots."
    )
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--openrouter-model", default=settings.friday_narrator_llm_model)
    parser.add_argument("--openrouter-base-url", default=settings.friday_narrator_llm_base_url)
    parser.add_argument("--skip-openrouter", action="store_true")
    parser.add_argument("--opencode-base-url", default=settings.narrator_opencode_base_url)
    parser.add_argument("--opencode-model", default="")
    parser.add_argument(
        "--opencode-agent",
        default=settings.friday_narrator_opencode_agent or "build",
    )
    parser.add_argument("--opencode-timeout", type=float, default=30.0)
    parser.add_argument("--skip-opencode", action="store_true")
    parser.add_argument("--json-output", default="")
    args = parser.parse_args()

    if args.trials < 1:
        raise ValueError("--trials must be >= 1")

    cases = _cases()
    results: list[ProbeResult] = []

    if not args.skip_openrouter:
        api_key = settings.narrator_llm_api_key
        if not api_key:
            print("Skipping openrouter: OPENROUTER_API_KEY/FRIDAY_NARRATOR_LLM_API_KEY is not set")
        else:
            print(f"OpenRouter-compatible model: {args.openrouter_model}")
            results.extend(
                await _run_backend(
                    backend="openrouter",
                    client=OpenAICompatibleJsonChatClient(
                        base_url=args.openrouter_base_url,
                        api_key=api_key,
                        model=args.openrouter_model,
                        timeout_secs=args.opencode_timeout,
                    ),
                    cases=cases,
                    trials=args.trials,
                )
            )

    if not args.skip_opencode:
        opencode_model = args.opencode_model or await _discover_opencode_flash_model(
            args.opencode_base_url
        )
        print(f"\nOpenCode server model: {opencode_model}")
        results.extend(
            await _run_backend(
                backend="opencode-server",
                client=OpenCodeServerJsonChatClient(
                    base_url=args.opencode_base_url,
                    model=opencode_model,
                    agent=args.opencode_agent,
                    timeout_secs=args.opencode_timeout,
                    disable_tools=True,
                    delete_sessions=True,
                ),
                cases=cases,
                trials=args.trials,
            )
        )

    _print_summary(results)
    if args.json_output:
        with open(args.json_output, "w", encoding="utf-8") as handle:
            json.dump(
                [
                    {
                        "backend": result.backend,
                        "case": result.case,
                        "trial": result.trial,
                        "elapsed_ms": result.elapsed_ms,
                        "parsed": result.parsed,
                        "error": result.error,
                        "expected_action": result.expected_action,
                        "completed": result.completed,
                        "expected_match": result.expected_match,
                    }
                    for result in results
                ],
                handle,
                indent=2,
            )
            handle.write("\n")

    return 0 if all(result.error is None for result in results) else 1


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
