from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class CreateSessionRequest(BaseModel):
    chat_id: str | None = Field(default=None, min_length=1, max_length=128)
    participant_name: str | None = Field(default=None, min_length=1, max_length=128)
    title: str | None = Field(default=None, min_length=1, max_length=256)
    directory: str | None = Field(default=None, min_length=1, max_length=4096)
    harness: str | None = Field(default=None, min_length=1, max_length=64)
    model_id: str | None = Field(default=None, min_length=1, max_length=256)


class UpdateSessionRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=256)


class CreateSessionResponse(BaseModel):
    session_id: str
    room_name: str
    participant_identity: str
    participant_name: str
    livekit_url: str
    token: str
    expires_in_seconds: int
    harness: str | None = None
    model_id: str | None = None
    title: str | None = None
    directory: str | None = None


class EnsureVoiceAgentRequest(BaseModel):
    room_name: str = Field(min_length=1, max_length=256)


class EnsureVoiceAgentResponse(BaseModel):
    dispatched: bool
    room_name: str


class HarnessInfo(BaseModel):
    id: str
    name: str


class ModelInfo(BaseModel):
    model_ref: str
    provider_id: str
    provider_name: str
    model_id: str
    model_name: str


class ModelsResponse(BaseModel):
    models: list[ModelInfo]
    default: str | None


class SessionSummary(BaseModel):
    id: str
    title: str | None
    directory: str | None
    harness: str
    model_id: str | None = None
    created_at: datetime
    updated_at: datetime


class CurrentModel(BaseModel):
    provider_id: str
    model_id: str


class TranscriptEntry(BaseModel):
    role: str
    text: str
    completed_at: datetime | None
    error: str | None = None
    parts: list[dict[str, Any]] = Field(default_factory=list)
    model: CurrentModel | None = None


class SessionDetailResponse(BaseModel):
    session: SessionSummary
    transcript: list[TranscriptEntry]
    narrator_transcript: list[TranscriptEntry] = Field(default_factory=list)
    current_model: CurrentModel | None
    agent_state: str


class NarratorTurnRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    source: str = Field(default="voice", min_length=1, max_length=64)


class NarratorEventResponse(BaseModel):
    id: int
    type: str
    text: str | None = None
    payload: dict[str, object]
    created_at: datetime


class NarratorEventsResponse(BaseModel):
    events: list[NarratorEventResponse]


class HealthResponse(BaseModel):
    ok: bool
