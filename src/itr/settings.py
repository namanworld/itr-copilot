from decimal import Decimal
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

    # USD per million tokens for claude_model, from
    # https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-10-30).
    # Haiku 5.5's rates for prompts up to 100K tokens; longer prompts cost 5x.
    # Change these together with CLAUDE_MODEL.
    price_input_per_mtok: Decimal = Decimal("0.10")
    price_output_per_mtok: Decimal = Decimal("0.50")
    price_cache_write_per_mtok: Decimal = Decimal("0.125")  # 5-minute cache
    price_cache_read_per_mtok: Decimal = Decimal("0.01")

    claude_max_tokens: int = 4096
    # Saved Claude responses, so re-running an eval doesn't pay twice.
    llm_cache_dir: Path = Path(__file__).resolve().parents[2] / "data" / "llm-cache"
    prompts_dir: Path = Path(__file__).resolve().parents[2] / "prompts"


@lru_cache
def get_settings() -> Settings:
    return Settings()
