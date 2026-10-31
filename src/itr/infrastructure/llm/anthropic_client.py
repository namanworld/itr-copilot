"""The only module that talks to Claude.

AnthropicLLM implements the LLMClient port with structured outputs: the SDK
turns the Pydantic model into a JSON schema, Claude replies in that shape and
the SDK validates the reply. Two caches sit in front of the API:

- Anthropic's prompt cache: the system prompt is marked cache_control, so
  repeated calls pay the cache-read price for it. Only kicks in once the cached
  prefix reaches the model's minimum (512 tokens for Haiku 5.5).
- A disk cache of whole responses, keyed by everything that affects the reply,
  so re-running an eval on the same documents makes no calls at all.
"""

import hashlib
import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from anthropic.types import TextBlockParam, UserMessageParam
from pydantic import BaseModel, ValidationError

from itr.application.ports import LLMOutputError, LLMResult, LLMUsage
from itr.settings import Settings

logger = logging.getLogger(__name__)

MILLION = Decimal(1_000_000)


# Add user message block
def create_user_message(message: str) -> UserMessageParam:
    return {"role": "user", "content": message}


def call_cost(
    settings: Settings,
    input_tokens: int,
    output_tokens: int,
    cache_write_tokens: int,
    cache_read_tokens: int,
) -> Decimal:
    """USD for one call. The four token counts don't overlap, so they add up."""
    return (
        input_tokens * settings.price_input_per_mtok
        + output_tokens * settings.price_output_per_mtok
        + cache_write_tokens * settings.price_cache_write_per_mtok
        + cache_read_tokens * settings.price_cache_read_per_mtok
    ) / MILLION


def cache_key(
    model: str, max_tokens: int, system: str, prompt: str, schema: dict[str, Any]
) -> str:
    """A hash of everything that changes the reply."""
    payload = json.dumps(
        {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "prompt": prompt,
            "schema": schema,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AnthropicLLM:
    def __init__(
        self,
        settings: Settings,
        client: Anthropic | None = None,
        cache_dir: Path | None = None,
    ):
        """cache_dir=None turns the disk cache off."""
        self._settings = settings
        self._client = client or Anthropic(api_key=settings.anthropic_api_key)
        self._cache_dir = cache_dir

    def structured[T: BaseModel](
        self, system: str, prompt: str, output_model: type[T]
    ) -> LLMResult[T]:
        model = self._settings.claude_model
        max_tokens = self._settings.claude_max_tokens
        key = cache_key(
            model, max_tokens, system, prompt, output_model.model_json_schema()
        )

        saved = self._load(key, output_model)
        if saved is not None:
            self._log(saved.usage, key)
            return saved

        system_blocks: list[TextBlockParam] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]

        try:
            response = self._client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=system_blocks,
                messages=[create_user_message(prompt)],
                output_format=output_model,
            )
        except ValidationError as e:
            raise LLMOutputError(
                f"Response didn't match {output_model.__name__}: {e}"
            ) from e

        output = response.parsed_output
        if output is None:
            raise LLMOutputError(
                f"no {output_model.__name__} in the reply "
                f"(stop_reason={response.stop_reason})"
            )

        u = response.usage
        cache_write = u.cache_creation_input_tokens or 0
        cache_read = u.cache_read_input_tokens or 0
        usage = LLMUsage(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_write_tokens=cache_write,
            cache_read_tokens=cache_read,
            cost_usd=call_cost(
                self._settings, u.input_tokens, u.output_tokens, cache_write, cache_read
            ),
        )

        result = LLMResult(output=output, usage=usage)
        self._save(key, result)
        self._log(usage, key)
        return result

    def _path(self, key: str) -> Path | None:
        return None if self._cache_dir is None else self._cache_dir / f"{key}.json"

    def _load[T: BaseModel](
        self, key: str, output_model: type[T]
    ) -> LLMResult[T] | None:
        path = self._path(key)
        if path is None or not path.exists():
            return None
        saved = json.loads(path.read_text(encoding="utf-8"))
        usage = saved["usage"]
        return LLMResult(
            # Validated again, so a saved reply can't bypass the model's checks.
            output=output_model.model_validate(saved["output"]),
            usage=LLMUsage(
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cache_write_tokens=usage["cache_write_tokens"],
                cache_read_tokens=usage["cache_read_tokens"],
                cost_usd=Decimal(usage["cost_usd"]),
                from_disk_cache=True,
            ),
        )

    def _save(self, key: str, result: LLMResult[Any]) -> None:
        path = self._path(key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        u = result.usage
        saved = {
            "output": result.output.model_dump(mode="json"),
            "usage": {
                "input_tokens": u.input_tokens,
                "output_tokens": u.output_tokens,
                "cache_write_tokens": u.cache_write_tokens,
                "cache_read_tokens": u.cache_read_tokens,
                "cost_usd": str(u.cost_usd),
            },
        }
        # Write then rename, so an interrupted run never leaves half a file.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(saved, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _log(self, usage: LLMUsage, key: str) -> None:
        logger.info(
            "claude %s: in=%d out=%d cache_write=%d cache_read=%d cost=$%s%s",
            key[:12],
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_write_tokens,
            usage.cache_read_tokens,
            f"{usage.cost_usd:.6f}",
            " (disk cache hit, not charged)" if usage.from_disk_cache else "",
        )
