from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import zipfile

import db
import operations


class OperationsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registry = operations.StudioRegistry(self.root / "runtime")
        self.paths = self.registry.onboard(
            "north-star", operations.RetentionPolicy(media_days=2, inquiry_link_days=3),
            {"worker-token": "only-for-this-studio"},
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_onboarding_isolates_state_and_credentials(self):
        other = self.registry.onboard("south-star", credentials={"worker-token": "different"})
        self.assertNotEqual(self.paths.workspace, other.workspace)
        self.assertNotEqual(self.paths.app_db, other.app_db)
        first = self.registry.credential_path(self.paths.studio_id, "worker-token")
        second = self.registry.credential_path(other.studio_id, "worker-token")
        self.assertEqual(first.read_text(), "only-for-this-studio")
        self.assertEqual(second.read_text(), "different")
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.paths.workspace.stat().st_mode), 0o700)
        with sqlite3.connect(self.paths.app_db) as conn:
            self.assertTrue(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deals'"
            ).fetchone())

    def test_queue_idempotency_concurrent_claim_and_stale_lease(self):
        store = operations.OperationsStore(self.paths)
        event_id, created = store.enqueue(
            "conversation_turn", {"inquiry_id": "inquiry-1", "through_sequence": 2},
            "inquiry-1:2", now=100,
        )
        self.assertTrue(created)
        self.assertEqual(store.enqueue(
            "conversation_turn", {"inquiry_id": "inquiry-1", "through_sequence": 2},
            "inquiry-1:2", now=101,
        ), (event_id, False))
        with self.assertRaises(operations.RetryConflict):
            store.enqueue("conversation_turn", {"inquiry_id": "different"},
                          "inquiry-1:2", now=101)

        barrier = threading.Barrier(3)
        claimed = []

        def claim(worker):
            barrier.wait()
            claimed.append(store.claim(worker, lease_seconds=5, now=101))

        threads = [threading.Thread(target=claim, args=("one",)),
                   threading.Thread(target=claim, args=("two",))]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        leases = [item for item in claimed if item]
        self.assertEqual(len(leases), 1)
        first = leases[0]
        second = store.claim("recovery", lease_seconds=5, now=107)
        self.assertEqual(second.event_id, first.event_id)
        self.assertNotEqual(second.lease_token, first.lease_token)
        with self.assertRaises(operations.LeaseLost):
            store.complete(first.event_id, first.lease_token, now=108)
        store.complete(second.event_id, second.lease_token, now=108)
        self.assertEqual(store.metrics(now=110)["queue"]["status_counts"]["completed"], 1)

    def test_dispatch_uses_explicit_command_and_sends_no_conversation_body(self):
        store = operations.OperationsStore(self.paths)
        event_id, _ = store.enqueue(
            "conversation_turn",
            {"inquiry_id": "i-1", "private_body": "a private customer message"},
            "turn:i-1:1",
        )
        adapter = (
            sys.executable, "-c",
            "import json,sys; x=json.load(sys.stdin); "
            "assert 'payload' not in x and 'private_body' not in str(x); "
            "print(json.dumps({'ok':True,'event_id':x['event_id']}))",
        )
        dispatcher = operations.TurnDispatcher(
            store, operations.DispatchConfig(command=adapter), "test-worker"
        )
        self.assertTrue(dispatcher.dispatch_one())
        self.assertFalse(dispatcher.dispatch_one())
        summary = store.metrics()
        self.assertEqual(summary["queue"]["status_counts"]["completed"], 1)
        self.assertEqual(summary["metrics"]["dispatch_seconds"]["n"], 1)
        with self.assertRaises(ValueError):
            operations.DispatchConfig(url="http://example.com/turn",
                                      token_file=self.registry.credential_path(
                                          self.paths.studio_id, "worker-token"))
        self.assertTrue(event_id)

    def test_discovers_real_app_work_with_least_exposure_payloads(self):
        conn = db.connect(str(self.paths.app_db))
        try:
            conn.execute("INSERT INTO inquiries(id,token_hash,created_at) VALUES (?,?,?)",
                         ("inquiry-1", "hash", 1))
            conn.execute("INSERT INTO inquiry_jobs VALUES (?,?,?,?,?,?)",
                         ("job-1", "inquiry-1", 4, "queued", None, None))
            asset_path = self.paths.media_dir / "asset.video"
            asset_path.write_bytes(b"media body must not enter operations")
            conn.execute("INSERT INTO reference_assets VALUES (?,?,?,?,?,?,?)",
                         ("asset-1", "inquiry-1", "secret-name.mp4", "video/mp4",
                          "digest", str(asset_path), 1))
            conn.execute("INSERT INTO reference_runs(id,asset_id,status,created_at) VALUES (?,?,?,?)",
                         ("analysis-1", "asset-1", "done", 2))
            conn.execute("INSERT INTO media_notifications VALUES (?,?,?)",
                         ("analysis-1", "inquiry-1", "queued"))
            conn.execute("INSERT INTO messages(id,deal_id,version_id,direction,body,option_ids_json,request_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
                         ("message-1", "deal-1", "version-1", "inbound",
                          "private creative instructions", "[]", "creative:req-1", 3))
            conn.execute("INSERT INTO commerce_reference_requests VALUES (?,?,?)",
                         ("creative:req-1", "option-1", '{"action":"replace"}'))
            conn.execute("INSERT INTO deals(id,source,source_id,client,status,sample_data,created_at) VALUES (?,?,?,?,?,?,?)",
                         ("deal-1", "site", "source-1", "Client", "open", 1, 1))
            conn.execute("INSERT INTO proposal_versions(id,deal_id,version_no,status,created_at) VALUES (?,?,?,?,?)",
                         ("version-1", "deal-1", 1, "published", 1))
            conn.execute("INSERT INTO portfolio_search_requests VALUES (?,?,?,?,?)",
                         ("portfolio-1", "version-1", "private search text", None, 4))
            conn.commit()
        finally:
            conn.close()

        store = operations.OperationsStore(self.paths)
        self.assertEqual(store.discover_app_work(now=10), {
            "conversation_turn": 1, "media_update": 1, "creative_request": 1,
            "portfolio_search_request": 1,
        })
        self.assertEqual(store.discover_app_work(now=11), {
            "conversation_turn": 0, "media_update": 0, "creative_request": 0,
            "portfolio_search_request": 0,
        })
        with sqlite3.connect(self.paths.operations_db) as check:
            rows = check.execute(
                "SELECT kind,payload_json FROM work_events ORDER BY kind"
            ).fetchall()
        payloads = {kind: json.loads(payload) for kind, payload in rows}
        self.assertEqual(payloads["conversation_turn"],
                         {"inquiry_id": "inquiry-1", "through_sequence": 4})
        self.assertEqual(payloads["media_update"],
                         {"analysis_id": "analysis-1", "inquiry_id": "inquiry-1",
                          "status": "done"})
        self.assertEqual(payloads["creative_request"],
                         {"deal_id": "deal-1", "request_id": "creative:req-1",
                          "version_id": "version-1"})
        self.assertEqual(payloads["portfolio_search_request"],
                         {"request_id": "portfolio-1", "version_id": "version-1"})
        serialized = json.dumps(payloads)
        self.assertNotIn("private creative", serialized)
        self.assertNotIn("secret-name", serialized)
        self.assertNotIn("option-1", serialized)
        self.assertNotIn("private search", serialized)

    def test_lease_validation_before_queue_mutation(self):
        store = operations.OperationsStore(self.paths)
        event_id, _ = store.enqueue("conversation_turn", {}, "lease-test", now=100)
        config = operations.DispatchConfig(command=(sys.executable,))
        invalid = (True, False, 0, -1, 3601, float("nan"), float("inf"), "60", None)
        for value in invalid:
            with self.subTest(value=value):
                with mock.patch.object(store, "_conn") as connect:
                    with self.assertRaises(ValueError):
                        store.claim("worker", value, now=100)
                    with self.assertRaises(ValueError):
                        store.extend(event_id, "token", value, now=100)
                    connect.assert_not_called()
                if value is not None:
                    with self.assertRaises(ValueError):
                        operations.TurnDispatcher(store, config, "worker", value)
        for value in (1, 30):
            with self.assertRaises(ValueError):
                operations.TurnDispatcher(store, config, "worker", value)
        self.assertEqual(operations.TurnDispatcher(store, config, "worker").lease_seconds, 45)
        event = store.claim("worker", 1, now=100)
        self.assertEqual(store.extend(event_id, event.lease_token, 3600, now=100.5), 3700.5)

    def test_authenticated_http_redirects_are_refused(self):
        received = []
        redirect = {"code": 302}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(redirect["code"])
                self.send_header("Location", "/target")
                self.end_headers()

            def do_GET(self):
                received.append((self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            store = operations.OperationsStore(self.paths)
            config = operations.DispatchConfig(
                url=f"http://127.0.0.1:{server.server_port}/dispatch",
                token_file=self.registry.credential_path(self.paths.studio_id, "worker-token"),
            )
            dispatcher = operations.TurnDispatcher(store, config, "worker")
            for code in (301, 302, 303, 307, 308):
                with self.subTest(code=code):
                    redirect["code"] = code
                    received.clear()
                    with self.assertRaises(operations.OperationsError):
                        dispatcher._invoke({"event_id": "redirect-test"})
                    self.assertEqual(received, [("/dispatch", "Bearer only-for-this-studio")])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_worker_validates_configuration_before_registry(self):
        from scripts import operations_worker

        cases = (
            ["--command-json", "not-json"],
            ["--command-json", "[]"],
            ["--command-json", ""],
            ["--command-json", '["relative"]'],
            ["--command-json", json.dumps([sys.executable]), "--timeout", "0"],
            ["--command-json", json.dumps([sys.executable]), "--token-file", "unused"],
            ["--url", "http://example.com/dispatch", "--token-file", "unused"],
            ["--url", "https://example.com/dispatch"],
            ["--url", "https://example.com/dispatch", "--token-file", "missing"],
        )
        root = self.root / "must-not-be-created"
        for target in cases:
            with self.subTest(target=target):
                with mock.patch.object(operations, "StudioRegistry") as registry:
                    with self.assertRaises((ValueError, SystemExit, operations.OperationsError)):
                        operations_worker.main(["--root", str(root), "--studio", "unused"] + target)
                    registry.assert_not_called()
                self.assertFalse(root.exists())

    def test_failed_dispatch_requeues_then_dead_letters(self):
        store = operations.OperationsStore(self.paths)
        store.enqueue("conversation_turn", {"inquiry_id": "i-2"}, "turn:i-2:1",
                      max_attempts=1)
        dispatcher = operations.TurnDispatcher(
            store,
            operations.DispatchConfig(command=(sys.executable, "-c", "raise SystemExit(7)")),
            "failed-worker",
        )
        self.assertTrue(dispatcher.dispatch_one())
        summary = store.metrics()
        self.assertEqual(summary["queue"]["status_counts"]["dead"], 1)
        self.assertEqual(summary["metrics"]["dispatch_failures"]["total"], 1)

    def test_lane_serializes_events_for_one_inquiry(self):
        store = operations.OperationsStore(self.paths)
        first, _ = store.enqueue("conversation_turn", {"inquiry_id": "i", "through_sequence": 1},
                                 "conversation:i:1", lane="i", now=100)
        second, _ = store.enqueue("media_update", {"inquiry_id": "i", "analysis_id": "a", "status": "done"},
                                  "media:i:a:done", lane="i", now=101)
        other, _ = store.enqueue("conversation_turn", {"inquiry_id": "j", "through_sequence": 1},
                                 "conversation:j:1", lane="j", now=102)
        claimed = store.claim("worker-one", lease_seconds=30, now=103)
        self.assertEqual(claimed.event_id, first)
        # Same lane is pinned to the first worker; a different lane proceeds.
        parallel = store.claim("worker-two", lease_seconds=30, now=103)
        self.assertEqual(parallel.event_id, other)
        self.assertIsNone(store.claim("worker-three", lease_seconds=30, now=103))
        store.complete(first, claimed.lease_token, now=104)
        nxt = store.claim("worker-two", lease_seconds=30, now=105)
        self.assertEqual(nxt.event_id, second)

    def test_completion_verification_rejects_self_attested_success(self):
        store = operations.OperationsStore(self.paths)
        store.enqueue("conversation_turn", {"inquiry_id": "i", "through_sequence": 1},
                      "conversation:i:1", max_attempts=1)
        adapter = (sys.executable, "-c",
                   "import json,sys; x=json.load(sys.stdin); "
                   "print(json.dumps({'ok':True,'event_id':x['event_id']}))")
        config = operations.DispatchConfig(command=adapter)
        unverified = operations.TurnDispatcher(store, config, "worker",
                                               verifier=lambda event: False)
        self.assertTrue(unverified.dispatch_one())
        self.assertIsNone(store.metrics()["queue"]["status_counts"].get("completed"))
        self.assertEqual(store.dead_letters()[0]["last_error"], "adapter_unverified")
        store.requeue(store.dead_letters()[0]["event_id"])
        verified = operations.TurnDispatcher(store, config, "worker",
                                             verifier=lambda event: True)
        self.assertTrue(verified.dispatch_one())
        self.assertEqual(store.metrics()["queue"]["status_counts"]["completed"], 1)

    def test_adapter_script_waits_for_authorized_consumer(self):
        import conversation
        conn = db.connect(str(self.paths.app_db))
        try:
            created = conversation.create_inquiry(conn, {"text": "Three reels please", "request_id": "m1"})
        finally:
            conn.close()
        store = operations.OperationsStore(self.paths)
        self.assertEqual(store.discover_app_work()["conversation_turn"], 1)
        adapter = (sys.executable, str(Path(__file__).parent / "scripts" / "qwenwork_adapter.py"),
                   "--app-db", str(self.paths.app_db))
        dispatcher = operations.TurnDispatcher(
            store, operations.DispatchConfig(command=adapter, timeout_seconds=15),
            "worker", verifier=operations.AppWorkVerifier(self.paths.app_db))
        # The authorized QwenWork consumer has not replied yet: the adapter
        # must not self-attest completion.
        self.assertTrue(dispatcher.dispatch_one())
        self.assertIsNone(store.metrics()["queue"]["status_counts"].get("completed"))
        conn = db.connect(str(self.paths.app_db))
        try:
            conversation.commit(conn, {"inquiry_id": created["inquiry_id"], "through_sequence": 1,
                                       "expected_revision": 0, "request_id": "reply-1",
                                       "reply": "Thanks for the brief.", "fact_changes": []})
        finally:
            conn.close()
        self.assertTrue(dispatcher.dispatch_one(now=time.time() + 10))
        self.assertEqual(store.metrics()["queue"]["status_counts"]["completed"], 1)

    def test_failed_dispatch_requeues_across_attempts_then_dead_letters(self):
        store = operations.OperationsStore(self.paths)
        store.enqueue("conversation_turn", {"inquiry_id": "i-3"}, "turn:i-3:1",
                      max_attempts=2)
        dispatcher = operations.TurnDispatcher(
            store,
            operations.DispatchConfig(command=(sys.executable, "-c", "raise SystemExit(7)")),
            "retry-worker",
        )
        self.assertTrue(dispatcher.dispatch_one())
        self.assertEqual(store.metrics()["queue"]["status_counts"]["queued"], 1)
        self.assertTrue(dispatcher.dispatch_one(now=time.time() + 10))
        summary = store.metrics()
        self.assertEqual(summary["queue"]["status_counts"]["dead"], 1)
        self.assertEqual(summary["metrics"]["dispatch_failures"]["total"], 2)
        dead = store.dead_letters()
        self.assertEqual(dead[0]["last_error"], "adapter_exit")
        self.assertEqual(dead[0]["attempts"], 2)
        store.requeue(dead[0]["event_id"])
        self.assertEqual(store.metrics()["queue"]["status_counts"]["queued"], 1)
        with self.assertRaises(operations.OperationsError):
            store.requeue(dead[0]["event_id"])

    def test_metrics_reject_sensitive_labels(self):
        store = operations.OperationsStore(self.paths)
        store.record_metric("analysis_seconds", 3.5,
                            {"provider": "local", "result": "partial"}, now=10)
        with self.assertRaises(ValueError):
            store.record_metric("analysis_seconds", 1, {"token": "secret"})
        metrics = store.metrics(since=0, now=20)
        self.assertEqual(metrics["metrics"]["analysis_seconds"]["average"], 3.5)

    def test_snapshot_is_verified_and_restore_preserves_credentials(self):
        with sqlite3.connect(self.paths.app_db) as conn:
            conn.execute("INSERT INTO learnings VALUES (?,?,?,?,?)",
                         ("before", None, "test", "snapshot value", 1))
        media = self.paths.media_dir / "asset.video"
        media.write_bytes(b"private-media")
        archive = self.root / "backups" / "north-star.zip"
        result = operations.create_snapshot(self.paths, archive, now=123)
        self.assertTrue(result["verified"])
        self.assertTrue(result["archive_sha256"])

        with sqlite3.connect(self.paths.app_db) as conn:
            conn.execute("DELETE FROM learnings")
        media.write_bytes(b"changed")
        operations.restore_snapshot(self.paths, archive)
        with sqlite3.connect(self.paths.app_db) as conn:
            self.assertEqual(conn.execute("SELECT body FROM learnings").fetchone()[0],
                             "snapshot value")
        self.assertEqual(media.read_bytes(), b"private-media")
        self.assertEqual(self.registry.credential_path(
            self.paths.studio_id, "worker-token").read_text(), "only-for-this-studio")

        tampered = self.root / "tampered.zip"
        with zipfile.ZipFile(archive) as src, zipfile.ZipFile(tampered, "w") as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                dst.writestr(info, b"corrupt" if info.filename == "app.db" else data)
        with self.assertRaises(operations.OperationsError):
            operations.verify_snapshot(tampered, self.paths.studio_id)

    def test_retention_dry_run_apply_and_revoke(self):
        old = 100.0
        conn = db.connect(str(self.paths.app_db))
        try:
            asset = "asset-old"
            inquiry = "inquiry-old"
            conn.execute("INSERT INTO inquiries(id,token_hash,created_at) VALUES (?,?,?)",
                         (inquiry, "hash-old", old))
            path = self.paths.media_dir / "asset-old.video"
            path.write_bytes(b"old")
            conn.execute("INSERT INTO reference_assets VALUES (?,?,?,?,?,?,?)",
                         (asset, inquiry, "old.mp4", "video/mp4", "digest", str(path), old))
            conn.execute("INSERT INTO reference_runs(id,asset_id,status,created_at) VALUES (?,?,?,?)",
                         ("run-old", asset, "done", old))
            derived = self.paths.media_dir / "run-old"
            derived.mkdir()
            (derived / "frame-1.jpg").write_bytes(b"frame")
            conn.execute("INSERT INTO proposal_versions(id,deal_id,version_no,status,token_hash,"
                         "published_at,created_at) VALUES (?,?,?,?,?,?,?)",
                         ("version-old", "deal", 1, "published", "proposal-hash", old, old))
            conn.commit()
        finally:
            conn.close()
        now = old + 10 * 86400
        preview = operations.apply_retention(self.registry, self.paths.studio_id,
                                             now=now, dry_run=True)
        self.assertEqual(preview["media_assets"], 1)
        self.assertTrue(path.exists())
        applied = operations.apply_retention(self.registry, self.paths.studio_id,
                                             now=now, dry_run=False)
        self.assertFalse(path.exists())
        self.assertFalse(derived.exists())
        self.assertEqual(applied["inquiry_links"], 1)
        with sqlite3.connect(self.paths.app_db) as check:
            self.assertEqual(check.execute(
                "SELECT revoked FROM inquiries WHERE id=?", (inquiry,)).fetchone()[0], 1)
            self.assertIsNone(check.execute(
                "SELECT token_hash FROM proposal_versions WHERE id='version-old'").fetchone()[0])

        result = self.registry.revoke(self.paths.studio_id, now=now)
        self.assertEqual(result["credentials"], 1)
        with self.assertRaises(operations.OperationsError):
            self.registry.paths(self.paths.studio_id)


if __name__ == "__main__":
    unittest.main()
