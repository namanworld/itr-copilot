from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    anthropic_api_key: str = ""
    claude_model: str = "claude-haiku-5-5"
    rules_dir: Path = Path(__file__).resolve().parents[2] / "rules"


@lru_cache
def get_settings() -> Settings:
    return Settings()
