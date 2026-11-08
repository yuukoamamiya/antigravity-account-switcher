import json
import math
import tempfile
import unittest
from pathlib import Path

from config import ConfigError, OAUTH_CLIENT_ID, load_config, save_oauth_client_secret, validate_config


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

    def test_non_finite_and_unreasonably_large_timeout_values_are_rejected(self):
        for value in (math.nan, math.inf, -math.inf, 10**10000):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ConfigError):
                validate_config({"requestTimeoutSeconds": value}, environ={})

    def test_non_finite_json_numbers_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for value in ("NaN", "Infinity", "-Infinity", "1e10000"):
                path.write_text('{"requestTimeoutSeconds": ' + value + "}", encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(ConfigError):
                    load_config(path, environ={})


if __name__ == "__main__":
    unittest.main()
