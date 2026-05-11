from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

AGENT_RESPONSE_TOPIC = "friday.agent_response"
TurnControlType = Literal[
    "start_turn",
    "end_turn",
    "cancel_turn",
    "set_speaker",
    "submit_text",
]
TURN_CONTROL_RPC_METHODS: dict[TurnControlType, str] = {
    "start_turn": "friday.turn.start",
    "end_turn": "friday.turn.end",
    "cancel_turn": "friday.turn.cancel",
    "set_speaker": "friday.turn.set_speaker",
    "submit_text": "friday.turn.submit_text",
}
AgentResponseType = Literal[
    "transcript",
    "text_delta",
    "text_final",
    "state",
    "tool_start",
    "narration",
    "error",
]


class TurnControlMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: TurnControlType
    speaker_enabled: bool | None = None
    text: str | None = None


class TurnControlResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    type: TurnControlType
    message: str | None = None
    state: str | None = None
    transcript: str | None = None


class AgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: AgentResponseType
    event_id: int | None = None
    text: str | None = None
    state: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    message: str | None = None
