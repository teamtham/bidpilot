"""Overnight hardening regressions — one test per fixed defect.

Covers: release-integrity revalidation on accept/confirm (tamper after check),
other-live-deal client names in the outbound gate, confirmed client links
staying live (and rotating), concurrent duplicate import, request_id payload
conflicts + reserved namespace, malformed owner payloads, import source
allowlist, reject/close state guards, anonymous body cap, privacy headers and
the sample badge, and seed-tamper invalidation of the client page.

Run: python3 -B -m unittest -v test_overnight_regressions
"""
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import server


@contextmanager
def _db(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


INQUIRY = {
    "pricing_mode": "legacy_replay",
    "as_of": "2026-11-23",
    "comparable_ids": ["P-031", "P-036", "P-041", "P-044"],
    "offer": {
        "client": "Juniper Cafe", "format": "product_promo",
        "production_mode": "one_shoot_day", "shoot_days": 1, "locations": 1,
        "interviewees": 0, "scripting": "light", "deliverables": 3,
        "editing_level": "standard", "graphics": "basic_titles",
        "revision_rounds": 2, "caption_versions": 3, "deadline": "2026-12-11",
        "budget_sgd": 2700, "direct_costs_sgd": 0,
    },
}
INQUIRY2 = {
    "as_of": "2026-11-23",
    "comparable_ids": ["P-031", "P-036"],
    "offer": {
        "client": "Harbor Fitness", "format": "social_reel",
        "production_mode": "edit_only", "shoot_days": 0, "locations": 0,
        "interviewees": 0, "scripting": "none", "deliverables": 2,
        "editing_level": "simple", "graphics": "none",
        "revision_rounds": 1, "caption_versions": 2, "deadline": "2026-12-18",
        "budget_sgd": 1500, "direct_costs_sgd": 0,
    },
}
RAW1 = "three 30-second Instagram promo videos, one location, captions, by 11 Dec, budget S$2,700"
COUNTER_TERMS = {"count": 5, "captions": 5, "deadline": "2026-12-04", "budget": 2700}
COUNTER_BODY = "five videos, five captioned versions by 4 Dec, same S$2,700"


def request(base, method, path, data=None, headers=None, raw=None):
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
    req = urllib.request.Request(base + path, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if body is not None and raw is None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode(), dict(err.headers)


class OvernightRegressions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="bidpilot-overnight-")
        cls.db_path = str(Path(cls.tmp) / "test.db")
        cls.srv, cls.owner = server.make_server(cls.db_path, port=0)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    # -- helpers ------------------------------------------------------------
    @classmethod
    def post(cls, path, data=None):
        return request(cls.base, "POST", path, data,
                       {"X-Owner-Token": cls.owner, "Origin": cls.base})

    @classmethod
    def client_post(cls, path, data=None, raw=None):
        headers = {"Origin": cls.base}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        return request(cls.base, "POST", path, data, headers, raw=raw)

    @classmethod
    def get(cls, path, owner=False):
        headers = {"X-Owner-Token": cls.owner} if owner else {}
        return request(cls.base, "GET", path, None, headers)

    @classmethod
    def flow(cls, source_id, inquiry=None):
        status, text, _ = cls.post("/api/import", {
            "source": "hashmeta", "source_id": source_id, "raw_inquiry": RAW1,
            "offer_request": inquiry or INQUIRY})
        assert status == 200, text
        imp = json.loads(text)
        _, text, _ = cls.post("/api/evaluate", {"deal_id": imp["deal_id"],
                                                "offer_id": imp["offer_id"]})
        version_id = json.loads(text)["version_id"]
        _, text, _ = cls.post("/api/check", {"version_id": version_id})
        assert json.loads(text)["ok"], text
        _, text, _ = cls.post("/api/approve", {"version_id": version_id})
        res = json.loads(text)
        assert res["ok"], text
        return {"deal_id": imp["deal_id"], "offer_id": imp["offer_id"],
                "version_id": version_id, "token": res["token"], "url": res["url"]}

    def db(self):
        return _db(self.db_path)

    def deal_state(self, deal_id):
        _, text, _ = self.get("/api/state", owner=True)
        return next(d for d in json.loads(text)["deals"] if d["deal_id"] == deal_id)

    def accept_own_option(self, f):
        with self.db() as conn:
            option_id = conn.execute(
                "SELECT value FROM json_each((SELECT public_option_ids_json"
                " FROM proposal_versions WHERE id=?))", (f["version_id"],)
            ).fetchone()["value"]
        status, text, _ = self.client_post(f["url"] + "/accept", {"option_id": option_id})
        self.assertEqual(status, 200, text)
        self.assertTrue(json.loads(text)["ok"], text)
        return option_id

    # -- 1: tamper after check invalidates accept AND confirm ----------------
    def test_tampered_draft_blocks_accept(self):
        f = self.flow("ov-tamper-accept")
        with self.db() as conn:
            conn.execute("UPDATE proposal_versions SET draft_body=? WHERE id=?",
                         ("Our price floor is S$2,454.90.", f["version_id"]))
            conn.commit()
        status, _, _ = self.get(f["url"])
        self.assertEqual(status, 404)  # page already enforced this
        with self.db() as conn:
            option_id = conn.execute(
                "SELECT value FROM json_each((SELECT public_option_ids_json"
                " FROM proposal_versions WHERE id=?))", (f["version_id"],)
            ).fetchone()["value"]
        status, text, _ = self.client_post(f["url"] + "/accept", {"option_id": option_id})
        self.assertEqual(status, 404)  # accept must enforce it too
        self.assertFalse(json.loads(text)["ok"])

    def test_tampered_option_terms_block_accept(self):
        f = self.flow("ov-tamper-terms")
        with self.db() as conn:
            row = conn.execute("SELECT id, public_terms_json FROM options WHERE evaluation_id="
                               "(SELECT id FROM evaluations WHERE offer_id=?)",
                               (f["offer_id"],)).fetchone()
            terms = json.loads(row["public_terms_json"])
            terms["price_sgd"] += 1
            conn.execute("UPDATE options SET public_terms_json=? WHERE id=?",
                         (json.dumps(terms), row["id"]))
            conn.commit()
        status, _, _ = self.get(f["url"])
        self.assertEqual(status, 404)
        with self.db() as conn:
            option_id = conn.execute(
                "SELECT value FROM json_each((SELECT public_option_ids_json"
                " FROM proposal_versions WHERE id=?))", (f["version_id"],)
            ).fetchone()["value"]
        status, _, _ = self.client_post(f["url"] + "/accept", {"option_id": option_id})
        self.assertEqual(status, 404)

    def test_tampered_draft_blocks_confirm_after_acceptance(self):
        f = self.flow("ov-tamper-confirm")
        self.accept_own_option(f)
        with self.db() as conn:
            conn.execute("UPDATE proposal_versions SET draft_body=? WHERE id=?",
                         ("Edited after acceptance.", f["version_id"]))
            conn.commit()
        status, text, _ = self.post("/api/confirm-booking", {"version_id": f["version_id"]})
        self.assertEqual(status, 409)
        res = json.loads(text)
        self.assertFalse(res["ok"])
        self.assertIn("digest_mismatch", res["reasons"])
        with self.db() as conn:
            self.assertIsNone(conn.execute("SELECT * FROM confirmations WHERE version_id=?",
                                           (f["version_id"],)).fetchone())

    # -- 2: outbound gate covers other live deals' clients --------------------
    def test_outbound_blocks_other_live_deal_client_name(self):
        self.flow("ov-name-juniper")
        harbor = json.loads(json.dumps(INQUIRY))
        harbor["offer"]["client"] = "Harbor Fitness"
        status, text, _ = self.post("/api/import", {
            "source": "hashmeta", "source_id": "ov-name-harbor",
            "raw_inquiry": RAW1, "offer_request": harbor})
        self.assertEqual(status, 200, text)
        imp = json.loads(text)
        _, text, _ = self.post("/api/evaluate", {"deal_id": imp["deal_id"],
                                                 "offer_id": imp["offer_id"]})
        version_id = json.loads(text)["version_id"]
        _, text, _ = self.post("/api/draft", {
            "version_id": version_id,
            "body": "Thanks Harbor Fitness — this draft wrongly names Juniper Cafe."})
        new_version = json.loads(text)["version_id"]
        _, text, _ = self.post("/api/check", {"version_id": new_version})
        res = json.loads(text)
        self.assertFalse(res["ok"])
        codes = [v["code"] for v in res["result"]["violations"]]
        self.assertIn("OTHER_CLIENT_NAME", codes)

    # -- 3: confirmed client link stays live, rotates, refuses changes --------
    def test_confirmed_link_live_rotates_and_refuses_change(self):
        f = self.flow("ov-confirmed")
        self.accept_own_option(f)
        status, text, _ = self.post("/api/confirm-booking", {"version_id": f["version_id"]})
        self.assertEqual(status, 200, text)
        self.assertTrue(json.loads(text)["ok"])
        # the client's own page still resolves and shows the confirmed state
        status, text, headers = self.get(f["url"])
        self.assertEqual(status, 200)
        self.assertIn("booking is confirmed", text)
        self.assertNotIn("Request a change", text)
        # no private values on the confirmed page
        self.assertNotIn("2454.9", text)
        self.assertNotIn("35.07", text)
        # changes on a confirmed booking are refused generically
        status, _, _ = self.client_post(f["url"] + "/request-change",
                                        {"body": COUNTER_BODY, "terms": COUNTER_TERMS,
                                         "request_id": "ov-after-confirm"})
        self.assertEqual(status, 404)
        # rotating a confirmed version issues a WORKING link; old one dies
        status, text, _ = self.post("/api/rotate-token", {"version_id": f["version_id"]})
        self.assertEqual(status, 200, text)
        res = json.loads(text)
        self.assertTrue(res["ok"])
        status, _, _ = self.get(res["url"])
        self.assertEqual(status, 200)
        status, _, _ = self.get(f["url"])
        self.assertEqual(status, 404)

    # -- 4: concurrent duplicate HTTP import ----------------------------------
    def test_concurrent_duplicate_import_creates_one_deal(self):
        results = []

        def do_import():
            results.append(self.post("/api/import", {
                "source": "hashmeta", "source_id": "ov-race-1",
                "raw_inquiry": RAW1, "offer_request": INQUIRY}))

        threads = [threading.Thread(target=do_import) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertEqual(len(results), 2)
        for status, text, _ in results:
            self.assertEqual(status, 200, text)  # no dropped connection / traceback
        payloads = [json.loads(text) for _, text, _ in results]
        self.assertEqual(payloads[0]["deal_id"], payloads[1]["deal_id"])
        self.assertEqual(payloads[0]["offer_id"], payloads[1]["offer_id"])
        self.assertEqual(sorted(p["fresh"] for p in payloads), [False, True])
        deal = self.deal_state(payloads[0]["deal_id"])
        self.assertEqual(len(deal["offers"]), 1)

    # -- 5: request_id payload conflicts + reserved namespace -----------------
    def test_request_id_reuse_with_different_terms_is_conflict(self):
        f = self.flow("ov-conflict-terms")
        status, text, _ = self.post("/api/client-change", {
            "deal_id": f["deal_id"], "request_id": "ov-shared-1",
            "body": COUNTER_BODY, "terms": COUNTER_TERMS})
        self.assertEqual(status, 200, text)
        first_offer = json.loads(text)["offer_id"]
        # same id, same payload -> idempotent resume
        status, text, _ = self.post("/api/client-change", {
            "deal_id": f["deal_id"], "request_id": "ov-shared-1",
            "body": COUNTER_BODY, "terms": COUNTER_TERMS})
        self.assertEqual(status, 200, text)
        self.assertEqual(json.loads(text)["offer_id"], first_offer)
        # same id, DIFFERENT terms -> 409, never a silent no-op
        status, text, _ = self.post("/api/client-change", {
            "deal_id": f["deal_id"], "request_id": "ov-shared-1",
            "body": "two videos by 4 Dec for S$2,700",
            "terms": {**COUNTER_TERMS, "count": 2, "captions": 2}})
        self.assertEqual(status, 409, text)
        self.assertEqual(json.loads(text)["error"], "request_id_conflict")
        self.assertEqual(len(self.deal_state(f["deal_id"])["offers"]), 2)

    def test_request_id_reuse_across_deals_is_conflict(self):
        f1 = self.flow("ov-cross-a")
        f2 = self.flow("ov-cross-b")
        status, _, _ = self.post("/api/client-change", {
            "deal_id": f1["deal_id"], "request_id": "ov-cross-id",
            "body": COUNTER_BODY, "terms": COUNTER_TERMS})
        self.assertEqual(status, 200)
        status, text, _ = self.post("/api/client-change", {
            "deal_id": f2["deal_id"], "request_id": "ov-cross-id",
            "body": COUNTER_BODY, "terms": COUNTER_TERMS})
        self.assertEqual(status, 409, text)
        self.assertEqual(json.loads(text)["error"], "request_id_conflict")

    def test_reserved_accept_namespace_cannot_be_squatted(self):
        f = self.flow("ov-squat")
        status, text, _ = self.post("/api/client-change", {
            "deal_id": f["deal_id"], "request_id": f"accept:{f['version_id']}",
            "body": COUNTER_BODY, "terms": COUNTER_TERMS})
        self.assertEqual(status, 409, text)
        # acceptance still works afterwards
        self.accept_own_option(f)

    def test_client_route_payload_conflict_is_409(self):
        f = self.flow("ov-client-conflict")
        status, _, _ = self.client_post(f["url"] + "/request-change",
                                        {"body": COUNTER_BODY, "terms": COUNTER_TERMS,
                                         "request_id": "ov-client-1"})
        self.assertEqual(status, 200)
        status, text, _ = self.client_post(f["url"] + "/request-change",
                                           {"body": "two videos by 4 Dec for S$2,700",
                                            "terms": {**COUNTER_TERMS, "count": 2},
                                            "request_id": "ov-client-1"})
        self.assertEqual(status, 409, text)

    # -- 6: malformed owner payloads are clean 400s ---------------------------
    def test_malformed_owner_payloads_return_json_400(self):
        cases = [
            ("/api/import", {}),
            ("/api/import", {"source": "hashmeta", "source_id": "x"}),
            ("/api/evaluate", {"deal_id": "nope", "offer_id": "nope"}),
            ("/api/evaluate", {}),
            ("/api/check", {}),
            ("/api/check", {"version_id": None}),
            ("/api/draft", {"version_id": None, "body": "x"}),
            ("/api/approve", {}),
            ("/api/reject", {}),
            ("/api/rotate-token", {}),
            ("/api/confirm-booking", {}),
            ("/api/close-deal", {}),
            ("/api/client-change", {}),
        ]
        for path, payload in cases:
            status, text, _ = self.post(path, payload)
            self.assertEqual(status, 400, f"{path} {payload} -> {status} {text}")
            self.assertFalse(json.loads(text)["ok"], text)

    # -- 7: import source allowlist -------------------------------------------
    def test_import_source_allowlist(self):
        for source in ("change:ov-x", "bogus", ""):
            status, text, _ = self.post("/api/import", {
                "source": source, "source_id": "r2", "raw_inquiry": RAW1,
                "offer_request": INQUIRY})
            self.assertEqual(status, 400, text)
            self.assertEqual(json.loads(text)["error"], "invalid_source")
        status, text, _ = self.post("/api/import-run", {
            "source": "change:ov-x", "source_id": "r2", "draft": "x"})
        self.assertEqual(status, 400, text)
        self.assertEqual(json.loads(text)["error"], "invalid_source")

    # -- 8: reject / close state guards ---------------------------------------
    def test_reject_refused_on_published_and_confirmed(self):
        f = self.flow("ov-reject")
        status, text, _ = self.post("/api/reject", {"version_id": f["version_id"]})
        self.assertEqual(status, 409, text)
        self.assertIn("status=published", json.loads(text)["reasons"])
        self.accept_own_option(f)
        status, text, _ = self.post("/api/reject", {"version_id": f["version_id"]})
        self.assertEqual(status, 409, text)
        _, _, _ = self.post("/api/confirm-booking", {"version_id": f["version_id"]})
        status, text, _ = self.post("/api/reject", {"version_id": f["version_id"]})
        self.assertEqual(status, 409, text)
        self.assertIn("status=confirmed", json.loads(text)["reasons"])
        deal = self.deal_state(f["deal_id"])
        self.assertEqual(deal["status"], "confirmed")
        self.assertEqual(deal["versions"][0]["status"], "confirmed")

    def test_reject_still_works_on_checked_draft(self):
        status, text, _ = self.post("/api/import", {
            "source": "hashmeta", "source_id": "ov-reject-ok",
            "raw_inquiry": RAW1, "offer_request": INQUIRY})
        imp = json.loads(text)
        _, text, _ = self.post("/api/evaluate", {"deal_id": imp["deal_id"],
                                                 "offer_id": imp["offer_id"]})
        version_id = json.loads(text)["version_id"]
        self.post("/api/check", {"version_id": version_id})
        status, text, _ = self.post("/api/reject", {"version_id": version_id})
        self.assertEqual(status, 200, text)
        self.assertTrue(json.loads(text)["ok"])

    def test_concurrent_close_writes_one_audit_row(self):
        f = self.flow("ov-close")
        self.accept_own_option(f)
        _, text, _ = self.post("/api/confirm-booking", {"version_id": f["version_id"]})
        self.assertTrue(json.loads(text)["ok"], text)
        results = []

        def do_close():
            results.append(self.post("/api/close-deal", {"deal_id": f["deal_id"]}))

        threads = [threading.Thread(target=do_close) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        self.assertEqual(len(results), 2)
        for status, text, _ in results:
            self.assertEqual(status, 200, text)
            self.assertTrue(json.loads(text)["ok"])
        with self.db() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) AS n FROM approvals a JOIN proposal_versions v"
                " ON v.id=a.version_id WHERE v.deal_id=? AND a.action='close_deal'",
                (f["deal_id"],)).fetchone()["n"]
        self.assertEqual(rows, 1)

    # -- 9: anonymous body cap + nonfinite JSON -------------------------------
    def test_anonymous_change_body_cap(self):
        f = self.flow("ov-bodycap")
        big = json.dumps({"body": "x" * (16 * 1024), "terms": COUNTER_TERMS,
                          "request_id": "ov-big"}).encode()
        status, text, _ = self.client_post(f["url"] + "/request-change", raw=big)
        self.assertEqual(status, 413, text)
        self.assertEqual(json.loads(text)["error"], "body_too_large")

    def test_nonfinite_json_rejected_on_client_and_owner_routes(self):
        f = self.flow("ov-nan")
        status, text, _ = self.client_post(f["url"] + "/accept",
                                           raw=b'{"option_id": NaN}')
        self.assertEqual(status, 400, text)
        self.assertEqual(json.loads(text)["error"], "bad_json")
        # owner route: send NaN via raw request with owner headers
        req = urllib.request.Request(
            self.base + "/api/check", data=b'{"version_id": Infinity}', method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Owner-Token", self.owner)
        req.add_header("Origin", self.base)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status, text = resp.status, resp.read().decode()
        except urllib.error.HTTPError as err:
            status, text = err.code, err.read().decode()
        self.assertEqual(status, 400, text)
        self.assertEqual(json.loads(text)["error"], "bad_json")

    # -- 10: privacy headers + sample badge -----------------------------------
    def test_privacy_headers_and_badge(self):
        f = self.flow("ov-headers")
        for path, owner in (("/quote", False), (f["url"], False), ("/", True)):
            status, text, headers = self.get(path, owner=owner)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers.get("Cache-Control"), "no-store", path)
            self.assertIn("noindex", headers.get("X-Robots-Tag", ""), path)
            if owner:   # the founder dashboard no longer shows the sample-data pill
                self.assertNotIn("Sample data · 23 Nov 2026 · Asia/Singapore", text, path)
            else:
                self.assertIn("Sample data · 23 Nov 2026 · Asia/Singapore", text, path)

    # -- 11: seed tamper invalidates the client page ---------------------------
    def test_seed_tamper_invalidates_client_page(self):
        f = self.flow("ov-seed")
        with self.db() as conn:
            conn.execute("UPDATE seed_rate_card SET price_sgd=price_sgd+1 WHERE item='promo_video'")
            conn.commit()
        try:
            status, _, _ = self.get(f["url"])
            self.assertEqual(status, 404)
            with self.db() as conn:
                option_id = conn.execute(
                    "SELECT value FROM json_each((SELECT public_option_ids_json"
                    " FROM proposal_versions WHERE id=?))", (f["version_id"],)
                ).fetchone()["value"]
            status, _, _ = self.client_post(f["url"] + "/accept", {"option_id": option_id})
            self.assertEqual(status, 404)
        finally:
            with self.db() as conn:
                conn.execute("UPDATE seed_rate_card SET price_sgd=price_sgd-1 WHERE item='promo_video'")
                conn.commit()


if __name__ == "__main__":
    unittest.main(verbosity=2)
