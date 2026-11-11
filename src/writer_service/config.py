"""Typed runtime settings, loaded from environment variables prefixed ``WRITER_``."""

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProvider = Literal["anthropic", "fake"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WRITER_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    app_env: Literal["dev", "test", "prod"] = "dev"

    # LLM (provider-agnostic; see ADR-0003)
    llm_provider: LLMProvider = "anthropic"
    llm_model: str = "claude-sonnet-5-5"
    anthropic_api_key: SecretStr | None = None
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    llm_fallbacks: bool = True  # Anthropic server-side fallback on safety refusals
    llm_max_attempts: int = Field(default=2, ge=1, le=5)  # attempts on unusable/invalid output
    llm_cache_dir: str = "data/cache/llm"
    self_consistency_runs: int = Field(default=3, ge=1, le=10)

    # Stores
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = SecretStr("change-me-neo4j")
    governance_db_url: str = "sqlite:///./governance.sqlite"


def get_settings() -> Settings:
    return Settings()
