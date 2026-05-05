"""ElevenLabs STT subclass that force-commits in VAD mode on tap-to-send.

Pipecat's stock ``ElevenLabsRealtimeSTTService`` only sends a manual commit
to ElevenLabs when ``commit_strategy == CommitStrategy.MANUAL`` (see line
663 in pipecat/services/elevenlabs/stt.py). We need VAD strategy for
"commit early and often" segmentation, but we *also* need to be able to
force a commit when the user taps Send — otherwise the trailing audio
(everything after the last natural pause) sits in ElevenLabs' buffer until
the next pause or the 90s ceiling.

This shim overrides ``process_frame`` to send the same ``{commit: True}``
websocket message on ``VADUserStoppedSpeakingFrame`` regardless of strategy.
"""

from __future__ import annotations

import json
from typing import override

from loguru import logger
from pipecat.frames.frames import Frame, VADUserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.elevenlabs.stt import CommitStrategy, ElevenLabsRealtimeSTTService
from websockets.protocol import State


class ElevenLabsRealtimeSTTServiceForceCommit(ElevenLabsRealtimeSTTService):
    """Adds force-commit on ``VADUserStoppedSpeakingFrame`` in VAD mode."""

    @override
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # Parent already commits in MANUAL — only act in VAD mode where it
        # would otherwise skip. Without this gate we'd double-commit in
        # MANUAL.
        if (
            isinstance(frame, VADUserStoppedSpeakingFrame)
            and self._commit_strategy == CommitStrategy.VAD
            and self._websocket
            and self._websocket.state is State.OPEN
        ):
            try:
                commit_message = {
                    "message_type": "input_audio_chunk",
                    "audio_base_64": "",
                    "commit": True,
                    "sample_rate": self.sample_rate,
                }
                await self._websocket.send(json.dumps(commit_message))
                logger.debug("elevenlabs_force_commit: sent commit (VAD mode tap-to-send)")
            except Exception:
                logger.exception("elevenlabs_force_commit: commit failed")
