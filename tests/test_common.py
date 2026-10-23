import unittest

from common import safe_text


class SafeTextTests(unittest.TestCase):
    def test_redacts_bearer_and_json_token_fields(self):
        source = 'Bearer simulated-access-token {"access_token": "simulated-json-token", "client_secret": "simulated-secret"}'
        result = safe_text(source)
        for value in ("simulated-access-token", "simulated-json-token", "simulated-secret"):
            self.assertNotIn(value, result)
        self.assertIn("[已隐藏]", result)


if __name__ == "__main__":
    unittest.main()
