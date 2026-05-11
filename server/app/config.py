from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

OPENROUTER_CHAT_COMPLETIONS_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_NARRATOR_LLM_MODEL = "openai/gpt-4o-mini"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    livekit_url: str = Field(default="ws://localhost:7880", alias="LIVEKIT_URL")
    livekit_internal_url: str = Field(default="", alias="LIVEKIT_INTERNAL_URL")
    livekit_public_url: str = Field(default="", alias="LIVEKIT_PUBLIC_URL")
    livekit_api_key: str = Field(default="devkey", alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(default="secret", alias="LIVEKIT_API_SECRET")
    livekit_agent_name: str = Field(default="friday", alias="FRIDAY_LIVEKIT_AGENT_NAME")
    friday_token_ttl_seconds: int = Field(default=3600, alias="FRIDAY_TOKEN_TTL_SECONDS")
    friday_cors_origins: str = Field(
        default="http://localhost:5173",
        alias="FRIDAY_CORS_ORIGINS",
    )
    opencode_base_url: str = Field(
        default="http://127.0.0.1:4096",
        alias="OPENCODE_BASE_URL",
    )
    friday_db_path: str = Field(default=".friday/friday.sqlite3", alias="FRIDAY_DB_PATH")
    friday_web_dist: str = Field(default="web/dist", alias="FRIDAY_WEB_DIST")
    friday_narrator_brain: str = Field(
        default="openai_compatible",
        alias="FRIDAY_NARRATOR_BRAIN",
    )
    friday_narrator_llm_provider: str = Field(
        default="openai_compatible",
        alias="FRIDAY_NARRATOR_LLM_PROVIDER",
    )
    friday_narrator_llm_base_url: str = Field(
        default=OPENROUTER_CHAT_COMPLETIONS_BASE_URL,
        alias="FRIDAY_NARRATOR_LLM_BASE_URL",
    )
    friday_narrator_llm_api_key: str = Field(
        default="",
        alias="FRIDAY_NARRATOR_LLM_API_KEY",
    )
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    friday_narrator_llm_model: str = Field(
        default=DEFAULT_NARRATOR_LLM_MODEL,
        alias="FRIDAY_NARRATOR_LLM_MODEL",
    )
    friday_narrator_opencode_base_url: str = Field(
        default="",
        alias="FRIDAY_NARRATOR_OPENCODE_BASE_URL",
    )
    friday_narrator_opencode_model: str = Field(
        default="",
        alias="FRIDAY_NARRATOR_OPENCODE_MODEL",
    )
    friday_narrator_opencode_agent: str = Field(
        default="",
        alias="FRIDAY_NARRATOR_OPENCODE_AGENT",
    )
    friday_narrator_opencode_directory: str = Field(
        default="",
        alias="FRIDAY_NARRATOR_OPENCODE_DIRECTORY",
    )
    friday_narrator_opencode_timeout_secs: float = Field(
        default=15.0,
        alias="FRIDAY_NARRATOR_OPENCODE_TIMEOUT_SECS",
    )
    friday_narrator_opencode_disable_tools: bool = Field(
        default=True,
        alias="FRIDAY_NARRATOR_OPENCODE_DISABLE_TOOLS",
    )
    friday_narrator_opencode_delete_sessions: bool = Field(
        default=True,
        alias="FRIDAY_NARRATOR_OPENCODE_DELETE_SESSIONS",
    )
    friday_narrator_progress_initial_delay_secs: float = Field(
        default=2.0,
        alias="FRIDAY_NARRATOR_PROGRESS_INITIAL_DELAY_SECS",
    )
    friday_narrator_progress_cooldown_secs: float = Field(
        default=6.0,
        alias="FRIDAY_NARRATOR_PROGRESS_COOLDOWN_SECS",
    )

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.friday_cors_origins.split(",") if origin.strip()]

    @property
    def livekit_server_url(self) -> str:
        return self.livekit_internal_url or self.livekit_url

    @property
    def livekit_client_url(self) -> str:
        return self.livekit_public_url or self.livekit_url

    @property
    def narrator_llm_api_key(self) -> str:
        return self.friday_narrator_llm_api_key or self.openrouter_api_key

    @property
    def narrator_opencode_base_url(self) -> str:
        return self.friday_narrator_opencode_base_url or self.opencode_base_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
