from __future__ import annotations

from agent.protocol import AgentResponse
from friday.application.voice import VoiceAgentMessage


def agent_response_from_voice_message(message: VoiceAgentMessage) -> AgentResponse:
    return AgentResponse(
        type=message.type,
        event_id=message.event_id,
        text=message.text,
        state=message.state,
        name=message.name,
        input=message.input,
        message=message.message,
    )


__all__ = ["agent_response_from_voice_message"]
