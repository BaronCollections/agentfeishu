from __future__ import annotations

from agentfeishu.config import Settings


def test_settings_creates_runtime_directories(tmp_path):
    settings = Settings(state_dir=tmp_path / "state")

    settings.ensure_directories()

    assert settings.state_dir.is_dir()
    assert settings.browser_profiles_dir == tmp_path / "state" / "browser_profiles"
    assert settings.browser_profiles_dir.is_dir()
    assert settings.downloads_dir == tmp_path / "state" / "downloads"
    assert settings.downloads_dir.is_dir()


def test_settings_loads_environment_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTFEISHU_STATE_DIR", str(tmp_path / "custom-state"))
    monkeypatch.setenv("AGENTFEISHU_CAPABILITY_URL_INGEST_ENABLED", "false")

    settings = Settings.from_env()

    assert settings.state_dir == tmp_path / "custom-state"
    assert settings.capability_enabled("url_ingest") is False
    assert settings.capability_enabled("unknown_future_capability") is True
