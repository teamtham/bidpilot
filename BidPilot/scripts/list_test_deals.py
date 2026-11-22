"""Read-only inventory, without schema initialization or database writes."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deal_logic


def inventory(path):
    with sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = []
        for d in conn.execute("SELECT * FROM deals ORDER BY created_at"):
            versions = []
            for v in conn.execute("SELECT v.*,e.verdict,e.result_json FROM proposal_versions v LEFT JOIN evaluations e ON e.offer_id=v.offer_id WHERE v.deal_id=? ORDER BY v.version_no", (d["id"],)):
                versions.append({**dict(v), "options": (json.loads(v["result_json"]) if v["result_json"] else {}).get("options", [])})
            offers = [{"round": o["round"], "evaluated": bool(o["evaluated"])} for o in conn.execute("SELECT o.round, e.id AS evaluated FROM offers o LEFT JOIN evaluations e ON e.offer_id=o.id WHERE o.deal_id=? ORDER BY o.round", (d["id"],))]
            rows.append({"deal_id": d["id"], "source": d["source"], "source_id": d["source_id"], "client": d["client"], "phase": deal_logic.phase(d["status"], versions, offers), "created_at": d["created_at"], "is_test": deal_logic.is_test_deal(d["source"], d["source_id"], d["raw_inquiry"])})
        return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    parser.add_argument("--json", action="store_true")
    print(json.dumps(inventory(parser.parse_args().database), indent=2))
