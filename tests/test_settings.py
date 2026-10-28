import pytest

from itr.settings import Settings


def test_settings_reads_claude_model_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_MODEL", "test-model")

    settings = Settings()

    assert settings.claude_model == "test-model"
