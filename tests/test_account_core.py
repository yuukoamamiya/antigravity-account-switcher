import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from account_core import AccountCore
from config import validate_config


class FakeCredentialManager:
    def __init__(self):
        self.value = None
        self.writes = []

    def read(self, _target):
        return self.value

    def write(self, _target, data):
        self.writes.append(data)
        self.value = data


class AccountCoreTests(unittest.TestCase):
    def test_switch_waits_for_save_and_writes_matching_latest_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            credential_path = folder / "account-simulated.json"
            initial = {"token": {"access_token": "simulated-old", "refresh_token": "simulated-refresh"}}
            credential_path.write_text(json.dumps(initial), encoding="utf-8")
            config = validate_config({"credentialsDirectory": directory}, environ={})
            fake_credentials = FakeCredentialManager()
            with patch("account_core.CredentialManager", return_value=fake_credentials):
                core = AccountCore(config)
            account = core.scan_accounts()[0]
            account.payload["token"]["access_token"] = "simulated-new"
            save_entered = threading.Event()
            allow_save = threading.Event()
            switch_done = threading.Event()
            errors = []

            def pause_metadata_save(_path, _value):
                save_entered.set()
                if not allow_save.wait(2):
                    raise TimeoutError("simulated test gate timed out")

            def save():
                try:
                    core.save_account(account)
                except Exception as exc:
                    errors.append(exc)

            def switch():
                try:
                    core.switch(account)
                except Exception as exc:
                    errors.append(exc)
                finally:
                    switch_done.set()

            core.stop_antigravity = lambda: False
            core.antigravity_running = lambda: False
            with patch("account_core.atomic_save_json", side_effect=pause_metadata_save):
                saver = threading.Thread(target=save)
                saver.start()
                self.assertTrue(save_entered.wait(1))
                switcher = threading.Thread(target=switch)
                switcher.start()
                time.sleep(0.05)
                self.assertFalse(switch_done.is_set())
                self.assertEqual(fake_credentials.writes, [])
                allow_save.set()
                saver.join(2)
                switcher.join(2)

            self.assertFalse(saver.is_alive())
            self.assertFalse(switcher.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(fake_credentials.writes, [account.data])
            self.assertEqual(account.digest, __import__("hashlib").sha256(account.data).hexdigest())
            self.assertEqual(fake_credentials.value, account.path.read_bytes())
            self.assertEqual(fake_credentials.value, account.data)


if __name__ == "__main__":
    unittest.main()
