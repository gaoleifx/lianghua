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
        self.assertIn('user_environment("GM_LIVE_STRATEGY_ID", "a9c2a169-41ee-11f1-a1ab-107c6107318f")', text)
        self.assertNotIn('GOLDMINER_SIM_ACCOUNT', text)

    def test_simulation_entry_uses_simulation_account_and_mode(self):
        path = os.path.join(ROOT, "08196542-1ba7-11f1-8c86-107c6107318f", "main.py")
        with open(path, encoding="utf-8") as handle: text = handle.read()
        self.assertIn('user_environment("GOLDMINER_SIM_ACCOUNT")', text)
        self.assertIn('initialize(context, "simulation")', text)
        self.assertIn('user_environment("GM_SIM_STRATEGY_ID", "08196542-1ba7-11f1-8c86-107c6107318f")', text)
        self.assertIn('mode=MODE_LIVE', text)

    def test_manual_live_authorization_enables_live_entries(self):
        with open(os.path.join(ROOT, "config", "config.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        self.assertTrue(config["deployment"]["live_new_entries_enabled"])
        self.assertEqual("manual_live_authorization_20260903", config["deployment"]["validation_status"])

if __name__ == "__main__": unittest.main()
