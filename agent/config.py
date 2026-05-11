from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    elevenlabs_api_key: str = Field(default="", alias="ELEVENLABS_API_KEY")
    eleven_api_key: str = Field(default="", alias="ELEVEN_API_KEY")
    elevenlabs_stt_model: str = Field(default="scribe_v2_realtime", alias="ELEVENLABS_STT_MODEL")
    elevenlabs_language_code: str = Field(default="en", alias="ELEVENLABS_LANGUAGE_CODE")
    elevenlabs_vad_silence_threshold_secs: float = Field(
        default=0.3,
        alias="ELEVENLABS_VAD_SILENCE_THRESHOLD_SECS",
    )
    elevenlabs_vad_threshold: float = Field(default=0.4, alias="ELEVENLABS_VAD_THRESHOLD")
    elevenlabs_min_speech_duration_ms: int = Field(
        default=100,
        alias="ELEVENLABS_MIN_SPEECH_DURATION_MS",
    )
    elevenlabs_min_silence_duration_ms: int = Field(
        default=300,
        alias="ELEVENLABS_MIN_SILENCE_DURATION_MS",
    )
    friday_commit_transcript_timeout_secs: float = Field(
        default=1.0,
        alias="FRIDAY_COMMIT_TRANSCRIPT_TIMEOUT_SECS",
    )
    friday_commit_stt_flush_duration_secs: float = Field(
        default=0.3,
        alias="FRIDAY_COMMIT_STT_FLUSH_DURATION_SECS",
    )
    friday_api_base_url: str = Field(
        default="http://127.0.0.1:8000",
        alias="FRIDAY_API_BASE_URL",
    )
    livekit_agent_name: str = Field(default="friday", alias="FRIDAY_LIVEKIT_AGENT_NAME")
    friday_narrator_poll_interval_secs: float = Field(
        default=0.5,
        alias="FRIDAY_NARRATOR_POLL_INTERVAL_SECS",
    )
    elevenlabs_tts_model: str = Field(
        default="eleven_turbo_v2_5",
        alias="ELEVENLABS_TTS_MODEL",
    )
    elevenlabs_tts_voice_id: str = Field(
        default="fOnNRYVB7V3x1eGuzh7v",
        alias="ELEVENLABS_TTS_VOICE_ID",
    )

    @property
    def api_key(self) -> str:
        return self.eleven_api_key or self.elevenlabs_api_key


@lru_cache
def get_agent_settings() -> AgentSettings:
    return AgentSettings()
