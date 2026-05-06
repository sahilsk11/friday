"""Focused tests for the Pipecat turn-taking migration.

The old TurnAccumulator owned commit buffering and turn finalization. These
tests pin the replacement contract: Pipecat's
SpeechTimeoutUserTurnStopStrategy decides when the user turn has stopped, and
only then do we forward the aggregated transcript into
ProviderSessionProcessor.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.turns.user_stop.base_user_turn_stop_strategy import UserTurnStoppedParams
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams
from pytest_httpx import HTTPXMock

from friday.core.opencode_provider import OpencodeProvider, OpencodeSession
from friday.voice.pipecat_adapter import ProviderSessionProcessor

OPENCODE_URL = "http://opencode.test"
SESSION_ID = "ses_turns"
_TEST_SYSTEM_PROMPT = "TEST_SYS_PROMPT"


@pytest.fixture(autouse=True)
def _no_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:  # pyright: ignore[reportUnusedFunction]
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


@pytest.fixture
async def session() -> AsyncIterator[OpencodeSession]:
    client = OpencodeProvider(OPENCODE_URL)
    yield client.attach(SESSION_ID)
    await client.aclose()


def _transcription(text: str, *, finalized: bool = True) -> TranscriptionFrame:
    return TranscriptionFrame(
        text=text,
        user_id="u1",
        timestamp="2026-01-01T00:00:00Z",
        finalized=finalized,
    )


def _vad_stopped() -> VADUserStoppedSpeakingFrame:
    return VADUserStoppedSpeakingFrame(stop_secs=0.2)


def _prompt_requests(httpx_mock: HTTPXMock) -> list[httpx.Request]:
    return [r for r in httpx_mock.get_requests() if r.url.path.endswith("/prompt_async")]


def _abort_requests(httpx_mock: HTTPXMock) -> list[httpx.Request]:
    return [r for r in httpx_mock.get_requests() if r.url.path.endswith("/abort")]


class _PipecatTurnHarness:
    """Minimal integration seam between Pipecat turn stop and friday provider dispatch."""

    def __init__(self, session: OpencodeSession, *, user_speech_timeout: float = 0.03) -> None:
        self.pushed: list[Frame] = []
        self.processor = ProviderSessionProcessor(session, system_prompt=_TEST_SYSTEM_PROMPT)
        self.processor.tts_enabled = False
        self.strategy = SpeechTimeoutUserTurnStopStrategy(
            user_speech_timeout=user_speech_timeout
        )
        self._task_manager = TaskManager()
        self._turn_parts: list[str] = []

        async def capture(
            frame: Frame, _direction: FrameDirection = FrameDirection.DOWNSTREAM
        ) -> None:
            self.pushed.append(frame)

        self.processor.push_frame = capture  # type: ignore[method-assign]

        async def on_user_turn_stopped(
            _event_source: SpeechTimeoutUserTurnStopStrategy,
            _params: UserTurnStoppedParams,
        ) -> None:
            text = " ".join(self._turn_parts).strip()
            if not text:
                return
            await self.processor.send_user_turn(text)
            self._turn_parts = []
            await self.strategy.reset()

        self.strategy.add_event_handler("on_user_turn_stopped", on_user_turn_stopped)

    async def setup(self) -> None:
        loop = asyncio.get_running_loop()
        self._task_manager.setup(TaskManagerParams(loop=loop))
        await self.strategy.setup(self._task_manager)

    async def process(self, frame: Frame) -> None:
        if isinstance(frame, TranscriptionFrame) and frame.finalized:
            text = frame.text.strip()
            if text:
                self._turn_parts.append(text)
        await self.strategy.process_frame(frame)

    async def interrupt(self) -> None:
        self._turn_parts = []
        await self.strategy.reset()
        await self.processor.cancel_current_turn()
        await self.processor.push_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

    async def cleanup(self) -> None:
        await self.strategy.cleanup()
        await self.processor.cleanup()


async def test_transcription_then_vad_restart_before_timeout_does_not_dispatch_provider(
    httpx_mock: HTTPXMock, session: OpencodeSession
) -> None:
    harness = _PipecatTurnHarness(session)
    await harness.setup()

    await harness.process(_vad_stopped())
    await harness.process(_transcription("keep listening"))
    await harness.process(VADUserStartedSpeakingFrame())
    await asyncio.sleep(0.08)

    assert _prompt_requests(httpx_mock) == []
    await harness.cleanup()


async def test_finalized_transcript_reaches_provider_once_after_vad_stop_timeout(
    httpx_mock: HTTPXMock, session: OpencodeSession
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/{SESSION_ID}/prompt_async",
        status_code=204,
    )
    harness = _PipecatTurnHarness(session)
    await harness.setup()

    await harness.process(_vad_stopped())
    await harness.process(_transcription("send this once", finalized=True))
    await asyncio.sleep(0.08)

    prompts = _prompt_requests(httpx_mock)
    assert len(prompts) == 1
    assert b"send this once" in httpx.Request.read(prompts[0])
    await harness.cleanup()


async def test_post_stop_timeout_is_cancelled_when_user_resumes_speaking(
    httpx_mock: HTTPXMock, session: OpencodeSession
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/{SESSION_ID}/prompt_async",
        status_code=204,
    )
    harness = _PipecatTurnHarness(session)
    await harness.setup()

    await harness.process(_vad_stopped())
    await harness.process(_transcription("first fragment"))
    await harness.process(VADUserStartedSpeakingFrame())
    await asyncio.sleep(0.08)
    assert _prompt_requests(httpx_mock) == []

    await harness.process(_vad_stopped())
    await asyncio.sleep(0.08)

    prompts = _prompt_requests(httpx_mock)
    assert len(prompts) == 1
    assert b"first fragment" in httpx.Request.read(prompts[0])
    await harness.cleanup()


async def test_interruption_frame_still_aborts_provider_through_processor(
    httpx_mock: HTTPXMock, session: OpencodeSession
) -> None:
    httpx_mock.add_response(
        method="POST",
        url=f"{OPENCODE_URL}/session/{SESSION_ID}/abort",
        status_code=204,
    )
    harness = _PipecatTurnHarness(session)
    await harness.setup()

    await harness.process(_vad_stopped())
    await harness.process(_transcription("do not send"))
    await harness.interrupt()
    await asyncio.sleep(0.08)

    assert _prompt_requests(httpx_mock) == []
    assert len(_abort_requests(httpx_mock)) == 1
    assert any(isinstance(frame, InterruptionFrame) for frame in harness.pushed)
    await harness.cleanup()
