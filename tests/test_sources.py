import os
import tempfile
import time
import unittest
from datetime import datetime

from home_handler import sources

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "status.json")


class SourcesTest(unittest.TestCase):
    def test_read_status_ok(self):
        data, age = sources.read_status(FIX, time.time())
        self.assertIsInstance(data["hosts"]["failover-host"]["disk_root_free_gb"], int)

    def test_read_status_truncated(self):
        with open(FIX) as f:
            head = f.read(100)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as t:
            t.write(head)
        try:
            self.assertEqual(sources.read_status(t.name, time.time()), (None, None))
        finally:
            os.unlink(t.name)

    def test_missing_file(self):
        self.assertEqual(sources.read_status("/nonexistent.json", time.time()), (None, None))

    def test_age(self):
        data, _ = sources.read_status(FIX, time.time())
        gen = datetime.fromisoformat(data["generated"]).timestamp()
        _, age = sources.read_status(FIX, gen + 123)
        self.assertAlmostEqual(age, 123, places=3)

    def test_ssh_timeout(self):
        t = time.time()
        rc, out, err = sources.ssh("127.0.0.2", "true", timeout=1)
        self.assertNotEqual(rc, 0)
        self.assertLess(time.time() - t, 5)


if __name__ == "__main__":
    unittest.main()
