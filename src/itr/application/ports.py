"""What the application needs from the outside world, as protocols.

The application layer depends on these, never on a vendor SDK. The real
implementations live in infrastructure/; tests use fakes.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class LLMUsage:
    """Tokens and cost of one call.

    On a disk-cache hit no request is made and nothing is charged; the numbers
    are those of the original call, so an eval can still report what a fresh
    run would cost.
    """

    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_read_tokens: int
    cost_usd: Decimal
    from_disk_cache: bool = False


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    output: T
    usage: LLMUsage


class LLMOutputError(Exception):
    """The model's reply couldn't be parsed into the requested model, for
    example because it was cut off at max_tokens or the model refused."""


class LLMClient(Protocol):
    def structured[T: BaseModel](
        self, system: str, prompt: str, output_model: type[T]
    ) -> LLMResult[T]:
        """Ask for a reply matching output_model's JSON schema, parsed and
        validated. Raises LLMOutputError if the reply doesn't fit."""
        ...
