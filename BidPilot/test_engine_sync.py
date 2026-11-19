"""Require exact parity between the runnable and reference engine folders."""
import unittest
from pathlib import Path


class EngineSyncTests(unittest.TestCase):
    def test_engine_copies_are_byte_identical(self):
        app = Path(__file__).resolve().parent
        primary = app / "bidpilot-engine"
        reference = app.parent / "bidpilot-build-guide" / "bidpilot-engine"

        def files(root):
            return {path.relative_to(root) for path in root.rglob("*")
                    if path.is_file() and "__pycache__" not in path.parts}

        primary_files = files(primary)
        self.assertTrue(primary_files)
        self.assertEqual(primary_files, files(reference))
        for relative in sorted(primary_files):
            with self.subTest(path=relative):
                self.assertEqual((primary / relative).read_bytes(),
                                 (reference / relative).read_bytes())
