from __future__ import annotations

import json
from pathlib import Path

import pytest

from bot.utils.config import HistoryTodayConfig, PromptConfig, Settings
from bot.utils.config_paths import ConfigPaths


@pytest.fixture
def config_file(tmp_path: Path):
    def write(**overrides) -> Path:
        data = {
            "guild_id": 0,
            "welcome_channel_id": 1,
            "ticket_category_id": 2,
            "ticket_panel_channel_id": 3,
            "transcript_dir": str(tmp_path / "transcripts"),
            "extensions": ["bot.cogs.welcome"],
        }
        data.update(overrides)
        path = tmp_path / "bot.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    return write


def test_legacy_settings_use_history_defaults(config_file):
    settings = Settings.from_file(config_file())

    assert settings.history_today == HistoryTodayConfig(enabled=True, timezone="Asia/Taipei")
    assert settings.extensions == ["bot.cogs.welcome"]
    assert settings.welcome_channel_id == 1
    assert settings.ticket_category_id == 2


@pytest.mark.parametrize("history_settings", [{}, None])
def test_empty_history_settings_use_defaults(config_file, history_settings):
    settings = Settings.from_file(config_file(history_today=history_settings))

    assert settings.history_today == HistoryTodayConfig()


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        (" YES ", True),
        ("on", True),
        ("1", True),
        ("false", False),
        ("off", False),
        ("0", False),
        ("no", False),
    ],
)
def test_history_enabled_flags(config_file, enabled, expected):
    settings = Settings.from_file(config_file(history_today={"enabled": enabled}))

    assert settings.history_today.enabled is expected


@pytest.mark.parametrize("timezone", ["UTC", "Asia/Taipei", "America/New_York"])
def test_history_timezone_accepts_iana_names(config_file, timezone):
    settings = Settings.from_file(config_file(history_today={"timezone": timezone}))

    assert settings.history_today.timezone == timezone


@pytest.mark.parametrize("timezone", ["Not/AZone", "../Asia/Taipei", "/etc/passwd", ""])
def test_history_timezone_rejects_invalid_names(config_file, timezone):
    with pytest.raises(ValueError, match="history_today.timezone 無效"):
        Settings.from_file(config_file(history_today={"timezone": timezone}))


def test_history_timezone_trims_whitespace(config_file):
    settings = Settings.from_file(config_file(history_today={"timezone": " Asia/Taipei "}))

    assert settings.history_today.timezone == "Asia/Taipei"


def test_legacy_settings_constructors_have_independent_history_config(tmp_path):
    arguments = {
        "guild_id": 0,
        "welcome_channel_id": 1,
        "ticket_category_id": 2,
        "ticket_panel_channel_id": 3,
        "support_role_ids": [],
        "transcript_dir": tmp_path / "transcripts",
        "llm_model": "existing-model",
        "prompt_config": PromptConfig("", "", "", ""),
    }
    first = Settings(**arguments)
    second = Settings(**arguments)

    first.history_today.enabled = False

    assert second.history_today.enabled is True


def test_history_config_rejects_non_object_settings(config_file):
    with pytest.raises(ValueError, match="history_today 必須是 JSON 物件"):
        Settings.from_file(config_file(history_today="invalid"))


def test_history_database_path_is_in_persistent_data_directory():
    assert ConfigPaths.HISTORY_DATABASE == ConfigPaths.DATABASE_DIR / "history_today.db"
    assert ConfigPaths.HISTORY_DATABASE.parent == ConfigPaths.ROOT_DIR / "data" / "database"
