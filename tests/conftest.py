"""Keep default config discovery inside each test's temporary directory."""

import pytest


@pytest.fixture(autouse=True)
def isolate_default_config_candidates(tmp_path, monkeypatch):
    import config

    root = tmp_path / "portable-root"
    monkeypatch.setattr(config, "ROOT", root)
    monkeypatch.setattr(config, "CONFIG_PATH", root / "Data" / "settings" / "account-switcher.config.json")
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", root / "local-data" / "account-switcher.config.json")
    monkeypatch.setattr(config, "LEGACY_CONFIG_PATH", root / "account-switcher.config.json")
