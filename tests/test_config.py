from __future__ import annotations

from agentfeishu.config import load_settings


def test_load_settings_defaults_to_project_state(tmp_path):
    settings = load_settings(tmp_path)
    assert settings.project_root == tmp_path
    assert settings.state_dir == tmp_path / "state"
    assert settings.browser_profiles_dir == tmp_path / "state" / "browser_profiles"
    assert settings.task_store_path == tmp_path / "state" / "tasks.jsonl"
    assert settings.state_dir.exists()


def test_settings_masks_sensitive_values(tmp_path):
    (tmp_path / "agentfeishu.toml").write_text(
        '[feishu]\napp_id = "app"\napp_secret = "secret"\n',
        encoding="utf-8",
    )
    settings = load_settings(tmp_path)
    masked = settings.masked_configuration()
    assert masked["feishu_app_id"] == "app"
    assert masked["feishu_app_secret"] == "***"
    assert "secret" not in str(list(masked.values()))


def test_enabled_capabilities_from_config(tmp_path):
    (tmp_path / "agentfeishu.toml").write_text(
        '[capabilities]\nenabled = ["url_ingest", "research"]\n',
        encoding="utf-8",
    )
    settings = load_settings(tmp_path)
    assert settings.enabled_capabilities == ("url_ingest", "research")
