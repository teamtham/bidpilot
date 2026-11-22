"""The sample-deal seed covers every dashboard stage and removes cleanly."""
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import db
import seed_sample_deals as seed
import server


class SeedSampleDealsTests(unittest.TestCase):
    def test_seed_shows_every_stage_and_remove_leaves_real_deals(self):
        tmp = Path(tempfile.mkdtemp(prefix="bidpilot-seed-"))
        try:
            path = tmp / "t.db"
            srv, _ = server.make_server(str(path), port=0)
            srv.server_close()
            conn = db.connect(path)
            conn.execute("INSERT INTO deals (id, source, source_id, client, raw_inquiry, status, sample_data, created_at)"
                         " VALUES ('real','hashmeta','LEAD-REAL','Real Client','brief','new',1,0)")
            conn.commit()
            seed.seed(conn)
            seed.seed(conn)                                    # idempotent
            deals = {d["client"]: d for d in server.state(conn)["deals"] if d.get("is_sample")}
            self.assertEqual(len(deals), 8)
            phases = {d["phase"] for d in deals.values()}
            for expected in ("counter_ready", "ready_for_review", "needs_estimate",
                             "sent", "booked", "rejected"):
                self.assertIn(expected, phases)
            self.assertTrue(deals["Orchard Fitness"]["needs_fresh_estimate"])
            self.assertFalse(any(d["is_test"] for d in deals.values()))
            self.assertNotIn("needs_info", phases)
            seed.remove(conn, "bakery")                         # one sample only
            left = {d["client"] for d in server.state(conn)["deals"] if d.get("is_sample")}
            self.assertEqual(len(left), 7)
            self.assertNotIn("Sample Bakery", left)
            seed.remove(conn)
            left = [r[0] for r in conn.execute("SELECT source_id FROM deals")]
            self.assertEqual(left, ["LEAD-REAL"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 0)
            conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
