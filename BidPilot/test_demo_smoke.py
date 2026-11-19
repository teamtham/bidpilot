"""Exercise the demo replay as a separate process."""
import subprocess
import sys
import unittest
from pathlib import Path


class DemoSmokeTest(unittest.TestCase):
    def test_fixture_replay(self):
        script = Path(__file__).resolve().parent / "scripts" / "demo_smoke.py"
        result = subprocess.run([sys.executable, "-B", str(script)], capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fixture replay, not the QwenWork agents", result.stdout)
        self.assertIn("PASS    Founder confirms phased booking", result.stdout)


if __name__ == "__main__":
    unittest.main()
