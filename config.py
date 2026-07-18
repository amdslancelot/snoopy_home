from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    discord_token: str
    gemini_api_key: str
    discord_guild_id: Optional[int] = None

    @field_validator("discord_guild_id", mode="before")
    @classmethod
    def empty_str_to_none(cls, v):
        return None if v == "" else v

    bot_name: str = "Snoopy"
    db_path: str = "snoopy_home.db"
    timezone: str = "UTC"

    model_low: str = "gemini-2.5-flash-lite"
    model_medium: str = "gemini-2.5-flash"
    model_high: str = "gemini-2.5-pro"

    complexity_medium_threshold: int = 4
    complexity_high_threshold: int = 8

    context_window: int = 20
    cache_ttl_seconds: int = 3600

    # Google Calendar (optional) — service account + shared household calendar
    google_service_account_json: Optional[str] = None
    household_calendar_id: Optional[str] = None

    # Discord voice TTS (optional) — fallback channel when target user is not in voice
    default_voice_channel_id: Optional[int] = None

    # Bot personality ("default" = neutral assistant, "snoopy" = Snoopy the beagle)
    bot_personality: str = "default"

    # Observability
    metrics_port: int = 8080
    log_format: str = "console"  # "console" (dev) | "json" (production)
    log_level: str = "INFO"

    # USD per 1M tokens, used for the llm_cost_usd_total metric. Approximate —
    # verify against https://ai.google.dev/gemini-api/docs/pricing and override
    # via the MODEL_PRICES env var (JSON) when Google reprices.
    model_prices: dict = {
        "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40, "cached": 0.025},
        "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cached": 0.075},
        "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "cached": 0.31},
    }


settings = Settings()
