import tempfile, unittest, threading
from pathlib import Path
from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, NotFoundError
from src.rules import STATES, TRANSITION_ROLES


class LedgerFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _make_permit(self, title="ledger permit", threshold=100.0, quantity=10.0):
        return self.service.create_item(
            {"title": title, "description": "d", "severity": "high",
             "quantity": quantity, "threshold": threshold},
            "creator", "applicant")

    def _approve(self, item):
        self.service.add_record(
            item["id"],
            {"kind": "evidence", "detail": "ok", "status": "closed",
             "external_ref": "EV-%d" % item["id"]},
            "recorder", "applicant")
        current = item
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"],
                "reviewer", TRANSITION_ROLES[target][0])
        return current

    def test_batch_idempotency_first_wins_late_conflict(self):
        permit = self._make_permit()
        body = {"batch_no": "B-001", "permit_id": permit["id"],
                "quantity": 50.0, "note": "first"}
        r1 = self.service.accept_batch(body, "clerk", "inspector")
        self.assertTrue(r1["created"])
        # Replay with the same payload is an idempotent no-op.
        r2 = self.service.accept_batch(body, "clerk", "inspector")
        self.assertFalse(r2["created"])
        self.assertTrue(r2["replay"])
        self.assertEqual(r2["batch"]["id"], r1["batch"]["id"])
        # Late content with a different payload is kept as a conflict.
        late = dict(body, note="late")
        with self.assertRaises(ConflictError) as ctx:
            self.service.accept_batch(late, "clerk", "inspector")
        self.assertEqual(ctx.exception.detail["reason"], "batch_no_conflict")
        conflicts = self.service.list_conflicts("viewer", "B-001")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["reason"], "batch_no已存在且内容不一致")

    def test_quota_adjustment_optimistic_concurrency(self):
        permit = self._make_permit()
        self.service.accept_batch(
            {"batch_no": "B-002", "permit_id": permit["id"], "quantity": 50.0},
            "clerk", "inspector")
        ledger = self.service.get_ledger(permit["id"], "viewer")
        self.assertEqual(ledger["version"], 1)
        self.assertEqual(ledger["current_quantity"], 50.0)
        # First adjuster wins.
        r1 = self.service.adjust_quota(
            permit["id"], {"expected_version": 1, "new_quantity": 60.0},
            "mgr", "compliance_manager")
        self.assertEqual(r1["ledger"]["version"], 2)
        self.assertEqual(r1["ledger"]["current_quantity"], 60.0)
        # Second adjuster with a stale version sees the current version and diff.
        with self.assertRaises(ConflictError) as ctx:
            self.service.adjust_quota(
                permit["id"], {"expected_version": 1, "new_quantity": 70.0},
                "mgr2", "compliance_manager")
        detail = ctx.exception.detail
        self.assertEqual(detail["reason"], "version_conflict")
        self.assertEqual(detail["expected_version"], 1)
        self.assertEqual(detail["current_version"], 2)
        self.assertEqual(detail["current_quantity"], 60.0)
        self.assertEqual(detail["proposed_quantity"], 70.0)
        self.assertEqual(detail["diff"], 10.0)
        # Already-entered data is not overwritten.
        ledger = self.service.get_ledger(permit["id"], "viewer")
        self.assertEqual(ledger["current_quantity"], 60.0)
        self.assertEqual(ledger["version"], 2)
        # The adjustment history is append-only.
        adj = self.service.list_adjustments(permit["id"], "viewer")
        self.assertEqual(len(adj), 2)
        self.assertEqual(adj[-1]["to_quantity"], 60.0)

    def test_concurrent_adjustments_first_wins(self):
        permit = self._make_permit()
        self.service.accept_batch(
            {"batch_no": "B-003", "permit_id": permit["id"], "quantity": 50.0},
            "clerk", "inspector")
        results, errors = [], []

        def adjust(new_q):
            try:
                results.append(self.service.adjust_quota(
                    permit["id"], {"expected_version": 1, "new_quantity": new_q},
                    "mgr", "compliance_manager"))
            except ConflictError as exc:
                errors.append(exc)

        t1 = threading.Thread(target=adjust, args=(60.0,))
        t2 = threading.Thread(target=adjust, args=(70.0,))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(results[0]["ledger"]["current_quantity"],
                         errors[0].detail["current_quantity"])

    def test_recovery_by_batch_no(self):
        permit = self._make_permit()
        body = {"batch_no": "B-004", "permit_id": permit["id"], "quantity": 50.0}
        self.service.accept_batch(body, "clerk", "inspector")
        # Recovering an accepted batch is a no-op.
        r = self.service.recover_batch("B-004", "clerk", "inspector")
        self.assertTrue(r["replayed"])
        # A missing batch cannot be recovered.
        with self.assertRaises(NotFoundError):
            self.service.recover_batch("NO-SUCH", "clerk", "inspector")

    def test_recovery_heals_missing_ledger(self):
        permit = self._make_permit()
        body = {"batch_no": "B-007", "permit_id": permit["id"], "quantity": 50.0}
        self.service.accept_batch(body, "clerk", "inspector")
        # Simulate a partial write: batch accepted but ledger missing.
        with self.repo._lock, self.repo.conn:
            self.repo.conn.execute(
                "DELETE FROM quota_ledger WHERE permit_id=?", (permit["id"],))
        self.assertIsNone(self.service.get_ledger(permit["id"], "viewer"))
        r = self.service.recover_batch("B-007", "clerk", "inspector")
        self.assertTrue(r["healed"])
        ledger = self.service.get_ledger(permit["id"], "viewer")
        self.assertIsNotNone(ledger)
        self.assertEqual(ledger["current_quantity"], 50.0)

    def test_backfill_from_approved_permit(self):
        permit = self._make_permit(threshold=120.0)
        approved = self._approve(permit)
        self.assertIsNone(self.service.get_ledger(approved["id"], "viewer"))
        r = self.service.backfill_ledger(approved["id"], "mgr", "compliance_manager")
        self.assertTrue(r["created"])
        self.assertEqual(r["ledger"]["current_quantity"], 120.0)
        self.assertEqual(r["ledger"]["approved_quantity"], 120.0)
        # Backfill is idempotent.
        r2 = self.service.backfill_ledger(approved["id"], "mgr", "compliance_manager")
        self.assertFalse(r2["created"])

    def test_backfill_requires_approved_permit(self):
        permit = self._make_permit()
        with self.assertRaises(ConflictError) as ctx:
            self.service.backfill_ledger(permit["id"], "mgr", "compliance_manager")
        self.assertEqual(ctx.exception.detail["reason"], "permit_not_approved")

    def test_backfill_all(self):
        p1 = self._make_permit(threshold=100.0, title="p1")
        p2 = self._make_permit(threshold=200.0, title="p2")
        self._approve(p1)
        self._approve(p2)
        r = self.service.backfill_all("mgr", "compliance_manager")
        self.assertEqual(r["backfilled"], 2)
        self.assertIsNotNone(self.service.get_ledger(p1["id"], "viewer"))
        self.assertIsNotNone(self.service.get_ledger(p2["id"], "viewer"))

    def test_historical_records_still_queryable(self):
        permit = self._make_permit()
        self.service.add_record(
            permit["id"],
            {"kind": "inspection", "detail": "on-site check", "status": "open",
             "external_ref": "REC-1"},
            "inspector", "inspector")
        records = self.service.list_records(permit["id"], "viewer")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["detail"], "on-site check")

    def test_late_conflict_does_not_overwrite(self):
        permit = self._make_permit()
        self.service.accept_batch(
            {"batch_no": "B-006", "permit_id": permit["id"], "quantity": 50.0},
            "clerk", "inspector")
        with self.assertRaises(ConflictError):
            self.service.accept_batch(
                {"batch_no": "B-006", "permit_id": permit["id"], "quantity": 999.0},
                "clerk", "inspector")
        ledger = self.service.get_ledger(permit["id"], "viewer")
        self.assertEqual(ledger["current_quantity"], 50.0)

    def test_audit_chain_linked(self):
        permit = self._make_permit()
        self.service.accept_batch(
            {"batch_no": "B-005", "permit_id": permit["id"], "quantity": 50.0},
            "clerk", "inspector")
        self.service.adjust_quota(
            permit["id"], {"expected_version": 1, "new_quantity": 55.0},
            "mgr", "compliance_manager")
        events = self.service.audit("viewer", permit["id"])
        actions = [e["action"] for e in events]
        self.assertIn("batch_accept", actions)
        self.assertIn("quota_adjust", actions)
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
