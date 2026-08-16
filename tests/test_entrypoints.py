# -*- coding: utf-8 -*-
import ast, json, os, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

class EntrypointTests(unittest.TestCase):
    def test_entrypoints_share_runtime_and_have_no_secret(self):
        paths = [os.path.join(ROOT, "08196542-1ba7-11f1-8c86-107c6107318f", "main.py"),
                 os.path.join(ROOT, "a9c2a169-41ee-11f1-a1ab-107c6107318f", "main.py")]
        for path in paths:
            with open(path, encoding="utf-8") as handle: text = handle.read()
            ast.parse(text)
            self.assertIn("from gm_runtime import", text)
            self.assertNotIn("fdb4c9", text)
            self.assertNotIn("6c16e60a", text)

    def test_live_entry_uses_live_account_and_separate_state(self):
        path = os.path.join(ROOT, "a9c2a169-41ee-11f1-a1ab-107c6107318f", "main.py")
        with open(path, encoding="utf-8") as handle: text = handle.read()
        self.assertIn('user_environment("GOLDMINER_LIVE_ACCOUNT")', text)
        self.assertIn('initialize(context, "live")', text)
        self.assertNotIn('GOLDMINER_SIM_ACCOUNT', text)

    def test_backtest_defaults_to_2025_gate(self):
        path = os.path.join(ROOT, "08196542-1ba7-11f1-8c86-107c6107318f", "main.py")
        with open(path, encoding="utf-8") as handle: text = handle.read()
        self.assertIn('user_environment("GM_BACKTEST_START", "2025-01-01 09:00:00")', text)
        self.assertIn('user_environment("GM_BACKTEST_END", "2025-12-31 15:00:00")', text)

    def test_pending_validation_keeps_live_entries_disabled(self):
        with open(os.path.join(ROOT, "config", "config.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        self.assertFalse(config["deployment"]["live_new_entries_enabled"])
        self.assertIn("pending_", config["deployment"]["validation_status"])

if __name__ == "__main__": unittest.main()

