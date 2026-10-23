import contextlib
import io
import unittest

import antigravity_account_switcher as entrypoint


class EntrypointTests(unittest.TestCase):
    def test_help_is_available_without_windows_services_or_gui(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            entrypoint.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--quota-only", output.getvalue())

    def test_legacy_imports_remain_available(self):
        for name in ("Account", "AccountCore", "CredentialManager", "SwitcherApp", "load_config", "load_cache", "run_self_test"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(entrypoint, name))


if __name__ == "__main__":
    unittest.main()
