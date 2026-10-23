import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import ConfigError, normalize_proxy_setting, save_proxy_setting


class ProxySettingsTests(unittest.TestCase):
    def test_local_port_boundaries_and_disable(self):
        self.assertEqual(normalize_proxy_setting("1"), "http://127.0.0.1:1")
        self.assertEqual(normalize_proxy_setting(" 65535 "), "http://127.0.0.1:65535")
        self.assertEqual(normalize_proxy_setting(""), "")
        for value in ("0", "65536", "-1", "1.5"):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                normalize_proxy_setting(value)

    def test_http_url_allowed_and_unsupported_or_invalid_urls_rejected(self):
        self.assertEqual(normalize_proxy_setting("http://proxy.example:8080"), "http://proxy.example:8080")
        for value in (
            "socks5://127.0.0.1:1080", "https://proxy.example:443", "http://proxy.example",
            "http://:80", "http://proxy.example:80/path", "http://bad host:80",
            "http://proxy.example:80\n", "http://999.999.1.1:80", "http://-bad.example:80",
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "SOCKS|代理地址无效"):
                normalize_proxy_setting(value)

    def test_save_merges_current_file_and_preserves_file_secret_without_materializing_env(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            original = {"proxy": "http://old.example:8080", "oauthClientSecret": "simulated-file-secret",
                        "futureOption": {"enabled": True}}
            path.write_text(json.dumps(original), encoding="utf-8")
            with patch.dict("os.environ", {"ANTIGRAVITY_OAUTH_CLIENT_SECRET": "simulated-environment-secret"}):
                saved = save_proxy_setting("7890", path)
            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved, "http://127.0.0.1:7890")
            self.assertEqual(result["proxy"], saved)
            self.assertEqual(result["oauthClientSecret"], "simulated-file-secret")
            self.assertEqual(result["futureOption"], {"enabled": True})
            self.assertNotIn("simulated-environment-secret", path.read_text(encoding="utf-8"))

    def test_missing_portable_config_updates_legacy_file_in_place_without_losing_fields(self):
        import config

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private_path = root / "Data" / "settings" / "account-switcher.config.json"
            legacy_path = root / "account-switcher.config.json"
            legacy = {"oauthClientSecret": "simulated-legacy-secret", "credentialsDirectory": "legacy-creds",
                      "customPath": "preserve-me", "proxy": "http://old.example:8080"}
            legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
            with patch.object(config, "CONFIG_PATH", private_path), patch.object(config, "LOCAL_CONFIG_PATH", root / "local-data" / "account-switcher.config.json"), patch.object(config, "LEGACY_CONFIG_PATH", legacy_path):
                result = config.save_proxy_setting("8123")
            saved = json.loads(legacy_path.read_text(encoding="utf-8"))
            self.assertEqual(result, "http://127.0.0.1:8123")
            self.assertEqual(saved["proxy"], result)
            self.assertEqual(saved["oauthClientSecret"], "simulated-legacy-secret")
            self.assertEqual(saved["credentialsDirectory"], "legacy-creds")
            self.assertEqual(saved["customPath"], "preserve-me")
            self.assertFalse(private_path.exists())

    def test_failed_write_does_not_change_runtime_config(self):
        runtime = {"proxy": "http://old.example:8080"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with patch("config.atomic_save_json", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(ConfigError, "无法保存代理设置"):
                    proxy = save_proxy_setting("8888", path)
                    runtime["proxy"] = proxy
        self.assertEqual(runtime["proxy"], "http://old.example:8080")


if __name__ == "__main__":
    unittest.main()
