from pathlib import Path

import pytest

from itr.settings import Settings, get_settings


def test_settings_reads_claude_model_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_MODEL", "test-model")

    settings = Settings()

    assert settings.claude_model == "test-model"


def test_settings_reads_rules_dir_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RULES_DIR", str(tmp_path))

    settings = Settings()

    assert settings.rules_dir == tmp_path


def test_get_settings_caches_environment_backed_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLAUDE_MODEL", "cached-test-model")
    get_settings.cache_clear()

    try:
        settings = get_settings()

        assert settings.claude_model == "cached-test-model"
        assert get_settings() is settings
    finally:
        get_settings.cache_clear()
