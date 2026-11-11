"""Typed runtime settings, loaded from environment variables prefixed ``WRITER_``."""

from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EXAMPLE_INGEST_KEY = "dev-ingest-key-change-me"  # the value shipped in .env.example

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

    # Auth (ADR-0011)
    ingest_api_key: SecretStr | None = None  # unset = ingestion refused (503)
    admin_jwt_secret: SecretStr | None = None  # unset in dev = random per process
    session_ttl_minutes: int = Field(default=480, ge=5, le=7 * 24 * 60)
    cookie_secure: bool = False  # True behind HTTPS (always in prod)

    @model_validator(mode="after")
    def _prod_needs_real_secrets(self) -> "Settings":
        if self.app_env == "prod":
            secret = self.admin_jwt_secret.get_secret_value() if self.admin_jwt_secret else ""
            if len(secret) < 32:
                raise ValueError("WRITER_ADMIN_JWT_SECRET must be at least 32 characters in prod")
            if not self.cookie_secure:
                raise ValueError("WRITER_COOKIE_SECURE must be true in prod")
            if self.ingest_api_key is not None:
                key = self.ingest_api_key.get_secret_value()
                if len(key) < 32 or key == EXAMPLE_INGEST_KEY:
                    raise ValueError(
                        "WRITER_INGEST_API_KEY must be at least 32 characters (and not the "
                        "example value) in prod"
                    )
        return self


def get_settings() -> Settings:
    return Settings()
