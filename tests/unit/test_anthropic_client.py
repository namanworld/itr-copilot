"""AnthropicLLM, with the SDK replaced by a stub: no network, no key."""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from itr.application.ports import LLMClient, LLMOutputError
from itr.infrastructure.llm.anthropic_client import AnthropicLLM, call_cost
from itr.settings import Settings
from tests.fakes import FakeLLM


class Answer(BaseModel):
    pan: str
    gross: str


class OtherAnswer(BaseModel):
    pan: str


@dataclass
class StubMessages:
    """Stands in for client.messages; returns one canned reply per call."""

    output: BaseModel | None
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_creation_input_tokens": 600,
            "cache_read_input_tokens": 0,
        }
    )
    stop_reason: str = "end_turn"
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def parse(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            parsed_output=self.output,
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(**self.usage),
        )


SETTINGS = Settings(anthropic_api_key="test", claude_model="claude-haiku-5-5")
ANSWER = Answer(pan="ABCPS1234Z", gross="1200000")


def make(messages: StubMessages, cache_dir: Path | None = None) -> AnthropicLLM:
    client: Any = SimpleNamespace(messages=messages)
    return AnthropicLLM(SETTINGS, client=client, cache_dir=cache_dir)


def test_returns_the_parsed_output_and_usage() -> None:
    llm = make(StubMessages(ANSWER))
    result = llm.structured("system", "prompt", Answer)

    assert result.output == ANSWER
    assert result.usage.input_tokens == 1000
    assert result.usage.output_tokens == 200
    assert result.usage.cache_write_tokens == 600
    assert result.usage.cache_read_tokens == 0
    assert result.usage.from_disk_cache is False


def test_cost_uses_all_four_prices() -> None:
    # Haiku 5.5: $0.10 in, $0.50 out, $0.125 cache write, $0.01 cache read per MTok
    cost = call_cost(SETTINGS, 1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert cost == Decimal("0.735")
    assert call_cost(SETTINGS, 1000, 200, 600, 0) == Decimal("0.000275")


def test_request_uses_settings_and_caches_the_system_prompt() -> None:
    messages = StubMessages(ANSWER)
    make(messages).structured("You extract Form 16s.", "the text", Answer)

    [call] = messages.calls
    assert call["model"] == "claude-haiku-5-5"
    assert call["max_tokens"] == SETTINGS.claude_max_tokens
    assert call["output_format"] is Answer
    assert call["system"] == [
        {
            "type": "text",
            "text": "You extract Form 16s.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert call["messages"] == [{"role": "user", "content": "the text"}]


def test_disk_cache_hit_makes_no_second_call(tmp_path: Path) -> None:
    messages = StubMessages(ANSWER)
    first = make(messages, tmp_path).structured("s", "p", Answer)
    # A new client with the same cache folder, as on the next eval run.
    second = make(messages, tmp_path).structured("s", "p", Answer)

    assert len(messages.calls) == 1
    assert second.output == first.output
    assert second.usage.from_disk_cache is True
    assert second.usage.cost_usd == first.usage.cost_usd


@pytest.mark.parametrize(
    ("system", "prompt", "model"),
    [("other system", "p", Answer), ("s", "other prompt", Answer)],
)
def test_changing_any_input_misses_the_cache(
    tmp_path: Path, system: str, prompt: str, model: type[BaseModel]
) -> None:
    messages = StubMessages(ANSWER)
    make(messages, tmp_path).structured("s", "p", Answer)
    make(messages, tmp_path).structured(system, prompt, model)
    assert len(messages.calls) == 2


def test_changing_the_schema_misses_the_cache(tmp_path: Path) -> None:
    make(StubMessages(ANSWER), tmp_path).structured("s", "p", Answer)
    messages = StubMessages(OtherAnswer(pan="ABCPS1234Z"))
    make(messages, tmp_path).structured("s", "p", OtherAnswer)
    assert len(messages.calls) == 1


def test_changing_the_model_misses_the_cache(tmp_path: Path) -> None:
    messages = StubMessages(ANSWER)
    make(messages, tmp_path).structured("s", "p", Answer)
    other = Settings(anthropic_api_key="test", claude_model="claude-sonnet-5-5")
    client: Any = SimpleNamespace(messages=messages)
    AnthropicLLM(other, client=client, cache_dir=tmp_path).structured("s", "p", Answer)
    assert len(messages.calls) == 2


def test_no_cache_dir_means_every_call_goes_out() -> None:
    messages = StubMessages(ANSWER)
    llm = make(messages, cache_dir=None)
    llm.structured("s", "p", Answer)
    llm.structured("s", "p", Answer)
    assert len(messages.calls) == 2


def test_unparseable_reply_raises_llm_output_error() -> None:
    try:
        Answer.model_validate_json('{"pan": "ABC')  # cut off at max_tokens
    except ValidationError as error:
        truncated = error
    llm = make(StubMessages(None, error=truncated))
    with pytest.raises(LLMOutputError):
        llm.structured("s", "p", Answer)


def test_missing_output_raises_llm_output_error(tmp_path: Path) -> None:
    llm = make(StubMessages(None, stop_reason="refusal"), tmp_path)
    with pytest.raises(LLMOutputError, match="refusal"):
        llm.structured("s", "p", Answer)
    assert list(tmp_path.iterdir()) == []  # failures are never cached


def test_logs_usage_and_cost(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    caplog.set_level(logging.INFO, logger="itr.infrastructure.llm.anthropic_client")
    make(StubMessages(ANSWER), tmp_path).structured("s", "p", Answer)
    make(StubMessages(ANSWER), tmp_path).structured("s", "p", Answer)

    first, second = caplog.messages
    assert "in=1000 out=200 cache_write=600 cache_read=0 cost=$0.000275" in first
    assert "disk cache hit" in second


def test_fake_llm_returns_canned_answers_in_order() -> None:
    llm: LLMClient = FakeLLM([ANSWER])
    result = llm.structured("s", "p", Answer)
    assert result.output == ANSWER
    with pytest.raises(AssertionError, match="no answer left"):
        llm.structured("s", "p", Answer)


def test_fake_llm_rejects_the_wrong_answer_type() -> None:
    with pytest.raises(AssertionError, match="asked for OtherAnswer"):
        FakeLLM([ANSWER]).structured("s", "p", OtherAnswer)
