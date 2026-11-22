import os
import tempfile
import unittest

from home_handler import config


class ConfigTest(unittest.TestCase):
    def test_config_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config.toml")
            with open(p, "w") as f:
                f.write("[caps]\nevening = 2\n")
            cfg = config.load(p)
        self.assertEqual(cfg["caps"]["evening"], 2)
        self.assertEqual(cfg["caps"]["triage"], 12)
        self.assertEqual(cfg["caps"]["job"], 4)

    def test_missing_file_gives_defaults(self):
        cfg = config.load("/nonexistent/config.toml")
        self.assertEqual(cfg["caps"], {"triage": 12, "job": 4, "evening": 1, "ask": 30})


if __name__ == "__main__":
    unittest.main()
