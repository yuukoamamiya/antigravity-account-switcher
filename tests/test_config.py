import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from config import ConfigError, OAUTH_CLIENT_ID, load_config, save_config_flag, save_oauth_client_secret, validate_config


class ConfigTests(unittest.TestCase):
    def test_defaults_and_environment_secret_precedence(self):
        config = validate_config({"oauthClientId": "", "oauthClientSecret": "simulated-file-secret"}, environ={
            "ANTIGRAVITY_OAUTH_CLIENT_SECRET": "simulated-environment-secret"
        })
        self.assertEqual(config["oauthClientId"], OAUTH_CLIENT_ID)
        self.assertEqual(config["oauthClientSecret"], "simulated-environment-secret")
        self.assertEqual(config["quotaDeadlineSeconds"], 45)
        self.assertEqual(validate_config({}, environ={})["oauthClientSecret"], "")

    def test_runtime_client_secret_is_saved_locally_and_preserves_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"proxy": "http://127.0.0.1:10809", "keep": 1}), encoding="utf-8")
            client_value = "GOCSPX-" + "A" * 28
            save_oauth_client_secret(client_value, path)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["oauthClientSecret"], client_value)
            self.assertEqual(stored["proxy"], "http://127.0.0.1:10809")
            self.assertEqual(stored["keep"], 1)
            with self.assertRaises(ConfigError):
                save_oauth_client_secret("not-a-client-value", path)

    def test_file_parse_and_schema_errors_are_actionable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for contents, expected in (("{", "无法读取配置文件"), ("[]", "必须是 JSON 对象"),
                                       ('{"queryQuota": "yes"}', "queryQuota"),
                                       ('{"credentialsDirectory": ""}', "不能为空")):
                path.write_text(contents, encoding="utf-8")
                with self.subTest(contents=contents), self.assertRaisesRegex(ConfigError, expected):
                    load_config(path, environ={})

    def test_backup_directory_defaults_and_must_not_be_empty(self):
        self.assertEqual(validate_config({}, environ={})["backupDirectory"], "Data/backup")
        self.assertEqual(validate_config({"backupDirectory": "Other/backup"}, environ={})["backupDirectory"], "Other/backup")
        for value in ("", "   "):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "backupDirectory 不能为空"):
                validate_config({"backupDirectory": value}, environ={})
        with self.assertRaisesRegex(ConfigError, "backupDirectory 必须是字符串"):
            validate_config({"backupDirectory": None}, environ={})

    def test_non_finite_and_unreasonably_large_timeout_values_are_rejected(self):
        for value in (math.nan, math.inf, -math.inf, 10**10000):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ConfigError):
                validate_config({"requestTimeoutSeconds": value}, environ={})

    def test_refresh_on_startup_defaults_to_false_and_requires_bool(self):
        self.assertIs(validate_config({}, environ={})["refreshOnStartup"], False)
        for value in ("yes", 1, 0, None, []):
            with self.subTest(value=value), self.assertRaisesRegex(ConfigError, "refreshOnStartup"):
                validate_config({"refreshOnStartup": value}, environ={})

    def test_save_config_flag_writes_flag_and_preserves_unknown_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"proxy": "http://127.0.0.1:10809", "futureOption": {"enabled": True}}), encoding="utf-8")
            self.assertIs(save_config_flag("refreshOnStartup", True, path), True)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertIs(stored["refreshOnStartup"], True)
            self.assertEqual(stored["proxy"], "http://127.0.0.1:10809")
            self.assertEqual(stored["futureOption"], {"enabled": True})
            self.assertIs(load_config(path, environ={})["refreshOnStartup"], True)

    def test_save_config_flag_rejects_unknown_fields_and_non_bool_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            original = {"keep": "unchanged"}
            path.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "不支持的配置项：proxy。"):
                save_config_flag("proxy", True, path)
            with self.assertRaisesRegex(ConfigError, "refreshOnStartup 必须是 true 或 false"):
                save_config_flag("refreshOnStartup", "true", path)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)

    def test_save_config_flag_writes_only_active_config_path_in_temp_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "Data" / "settings" / "account-switcher.config.json"
            legacy = root / "account-switcher.config.json"
            with patch.object(config, "CONFIG_PATH", primary), \
                 patch.object(config, "LOCAL_CONFIG_PATH", root / "local-data" / "account-switcher.config.json"), \
                 patch.object(config, "LEGACY_CONFIG_PATH", legacy):
                self.assertIs(config.save_config_flag("queryQuota", False), False)
            self.assertTrue(primary.exists())
            self.assertFalse(legacy.exists())
            self.assertIs(json.loads(primary.read_text(encoding="utf-8"))["queryQuota"], False)

    def test_save_config_flag_wraps_write_failure_with_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            with patch("config.atomic_save_json", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(ConfigError, "无法保存配置到") as raised:
                    save_config_flag("refreshOnStartup", True, path)
            self.assertIn(str(path), str(raised.exception))

    def test_non_finite_json_numbers_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for value in ("NaN", "Infinity", "-Infinity", "1e10000"):
                path.write_text('{"requestTimeoutSeconds": ' + value + "}", encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(ConfigError):
                    load_config(path, environ={})


if __name__ == "__main__":
    unittest.main()
