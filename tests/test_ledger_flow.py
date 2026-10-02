import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, NotFoundError
from src.repository import Repository
from src.rules import STATES, TRANSITION_ROLES
from src.service import Service


class LedgerFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _approved_permit(self, ref="P-1", threshold=100.0, quantity=10.0):
        item = self.service.create_item(
            {"title": "permit", "description": "d", "severity": "high",
             "quantity": quantity, "threshold": threshold,
             "external_ref": ref}, "creator", "applicant")
        self.service.add_record(
            item["id"], {"kind": "inspection", "detail": "现场检查",
                         "status": "closed", "external_ref": "INSP-1"},
            "inspector", "inspector")
        current = item
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def test_approval_creates_grant_ledger_once(self):
        item = self._approved_permit()
        summary = self.service.quota_summary(item["id"], "viewer")
        self.assertEqual(summary["granted"], 100.0)
        self.assertEqual(summary["remaining"], 100.0)
        self.assertEqual(summary["quota_version"], 1)
        ledger = self.service.list_ledger(item["id"], "viewer")
        self.assertEqual([l["kind"] for l in ledger], ["grant"])
        # 再次批准路径不应重复授予
        self.repo.grant_initial_quota(item["id"], "x")
        self.assertEqual(len(self.service.list_ledger(item["id"], "viewer")), 1)

    def test_concurrent_adjustment_first_wins_later_sees_version_and_diff(self):
        item = self._approved_permit()
        first = self.service.adjust_quota(
            item["id"], {"delta": 20.0, "reason": "增发",
                         "expected_version": 1, "source_ref": "ADJ-1"},
            "mgr", "compliance_manager")
        self.assertEqual(first["quota"]["granted"], 120.0)
        self.assertEqual(first["quota"]["quota_version"], 2)

        # 后到者仍基于版本1：拒绝且不覆盖
        with self.assertRaises(ConflictError) as ctx:
            self.service.adjust_quota(
                item["id"], {"delta": -5.0, "reason": "核减",
                             "expected_version": 1, "source_ref": "ADJ-2"},
                "mgr2", "compliance_manager")
        details = ctx.exception.details
        self.assertEqual(details["current_version"], 2)
        self.assertEqual(details["current_granted"], 120.0)
        self.assertEqual(details["your_projected_granted"], 115.0)
        self.assertEqual(details["version_gap"], 1)
        # 台账里只有grant + 第一条调整，后到者未写入
        ledger = self.service.list_ledger(item["id"], "viewer")
        self.assertEqual([l["kind"] for l in ledger], ["grant", "adjustment"])

        # 基于当前版本重试可成功
        again = self.service.adjust_quota(
            item["id"], {"delta": -5.0, "reason": "核减",
                         "expected_version": 2, "source_ref": "ADJ-2"},
            "mgr2", "compliance_manager")
        self.assertEqual(again["quota"]["granted"], 115.0)

    def test_adjustment_cannot_go_below_emitted(self):
        item = self._approved_permit()
        self.service.post_emission_batch(
            {"batch_no": "B-1", "permit_ref": "P-1", "pollutant": "SO2",
             "amount": 80.0, "period": "2026-09"}, "park", "applicant")
        with self.assertRaises(ConflictError):
            self.service.adjust_quota(
                item["id"], {"delta": -50.0, "reason": "超额核减",
                             "expected_version": 1, "source_ref": "ADJ-X"},
                "mgr", "compliance_manager")

    def test_first_batch_posted_identical_redelivery_is_replay(self):
        self._approved_permit()
        payload = {"batch_no": "B-1", "permit_ref": "P-1", "pollutant": "SO2",
                   "amount": 30.0, "period": "2026-09"}
        first = self.service.post_emission_batch(payload, "park", "applicant")
        self.assertFalse(first["replay"])
        self.assertEqual(first["envelope"]["quota"]["emitted"], 30.0)
        self.assertEqual(first["envelope"]["permit_status"], "approved")
        self.assertEqual(first["envelope"]["inspection_records"], 1)

        replay = self.service.post_emission_batch(dict(payload), "park", "applicant")
        self.assertTrue(replay["replay"])
        # 只入账一次
        usage = [l for l in self.service.list_ledger(None, "viewer")
                 if l["kind"] == "usage"]
        self.assertEqual(len(usage), 1)

    def test_late_different_content_becomes_conflict_and_does_not_overwrite(self):
        self._approved_permit()
        self.service.post_emission_batch(
            {"batch_no": "B-2", "permit_ref": "P-1", "pollutant": "NOx",
             "amount": 10.0, "period": "2026-09"}, "park", "applicant")
        with self.assertRaises(ConflictError) as ctx:
            self.service.post_emission_batch(
                {"batch_no": "B-2", "permit_ref": "P-1", "pollutant": "NOx",
                 "amount": 99.0, "period": "2026-09"}, "park-late", "applicant")
        self.assertIn("留作冲突", str(ctx.exception))
        conflicts = self.service.list_conflicts("viewer", "B-2")
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["payload"]["amount"], 99.0)
        # 已入账数据未被覆盖
        batch = self.repo.find_batch("B-2")
        self.assertEqual(batch["envelope"]["amount"], 10.0)

    def test_write_failure_then_recover_by_batch_no(self):
        self._approved_permit()
        payload = {"batch_no": "B-3", "permit_ref": "P-1", "pollutant": "COD",
                   "amount": 12.0, "period": "2026-09"}
        self.service._post_failure = True
        with self.assertRaises(RuntimeError):
            self.service.post_emission_batch(payload, "park", "applicant")
        self.service._post_failure = False
        # 失败批单保留，台账无usage
        self.assertEqual(self.repo.find_batch("B-3")["status"], "failed")
        self.assertEqual(
            len([l for l in self.service.list_ledger(None, "viewer")
                 if l["kind"] == "usage"]), 0)
        # 按批单号恢复
        recovered = self.service.recover_batch("B-3", "park", "applicant")
        self.assertTrue(recovered["replay"])
        self.assertEqual(recovered["envelope"]["amount"], 12.0)
        self.assertEqual(
            len([l for l in self.service.list_ledger(None, "viewer")
                 if l["kind"] == "usage"]), 1)
        # 再次重投相同内容幂等
        again = self.service.recover_batch("B-3", "park", "applicant")
        self.assertTrue(again["replay"])

    def test_missing_permit_batch_recoverable_after_permit_exists(self):
        payload = {"batch_no": "B-4", "permit_ref": "P-FUTURE",
                   "pollutant": "SO2", "amount": 5.0, "period": "2026-09"}
        with self.assertRaises(NotFoundError):
            self.service.post_emission_batch(payload, "park", "applicant")
        self.assertEqual(self.repo.find_batch("B-4")["status"], "failed")
        # 不同内容的晚到仍进冲突，不能顶替第一次
        other = dict(payload, amount=999.0)
        with self.assertRaises(ConflictError):
            self.service.post_emission_batch(other, "park", "applicant")
        # 许可建立并批准后恢复
        self._approved_permit(ref="P-FUTURE")
        recovered = self.service.recover_batch("B-4", "park", "applicant")
        self.assertEqual(recovered["envelope"]["item_id"],
                         self.repo.find_item_by_external_ref("P-FUTURE")["id"])

    def test_backfill_old_approved_permits_without_ledger(self):
        # 模拟旧数据：直接把一条许可置为approved，没有任何台账
        item = self.service.create_item(
            {"title": "old", "description": "legacy", "severity": "low",
             "quantity": 1, "threshold": 50.0, "external_ref": "OLD-1"},
            "creator", "applicant")
        self.repo.conn.execute(
            "UPDATE items SET status='approved' WHERE id=?", (item["id"],))
        self.repo.conn.commit()
        result = self.service.backfill_ledger("mgr", "compliance_manager")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["backfilled"][0]["amount"], 50.0)
        # 回填幂等
        again = self.service.backfill_ledger("mgr", "compliance_manager")
        self.assertEqual(again["count"], 0)

    def test_historical_inspection_records_still_queryable(self):
        item = self._approved_permit()
        self.service.add_record(
            item["id"], {"kind": "inspection", "detail": "复查历史记录",
                         "status": "closed", "external_ref": "INSP-2"},
            "inspector", "inspector")
        records = self.service.list_records(item["id"], "viewer", kind="inspection")
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r["kind"] == "inspection" for r in records))

    def test_audit_chain_links_all_actions(self):
        item = self._approved_permit()
        self.service.adjust_quota(
            item["id"], {"delta": 5.0, "reason": "r", "expected_version": 1,
                         "source_ref": "ADJ-A"}, "mgr", "compliance_manager")
        self.service.post_emission_batch(
            {"batch_no": "B-9", "permit_ref": "P-1", "pollutant": "SO2",
             "amount": 7.0, "period": "2026-09"}, "park", "applicant")
        actions = {e["action"] for e in self.repo.list_audit()}
        self.assertIn("quota_grant", actions)
        self.assertIn("quota_adjust", actions)
        self.assertIn("batch_posted", actions)
        self.assertTrue(self.repo.verify_audit_chain())


class HttpConcurrencyTest(unittest.TestCase):
    """两人同时提交同一许可的额度调整，只有先到者生效。"""

    def test_parallel_adjustments_only_one_wins(self):
        import json as _json
        from http.server import ThreadingHTTPServer
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError

        from src.http_api import make_handler

        tmp = tempfile.TemporaryDirectory()
        repo = Repository(str(Path(tmp.name) / "test.db"))
        service = Service(repo)
        item = service.create_item(
            {"title": "p", "description": "d", "severity": "high",
             "quantity": 1, "threshold": 100.0, "external_ref": "PC-1"},
            "creator", "applicant")
        record = service.add_record(
            item["id"], {"kind": "inspection", "detail": "ok",
                         "status": "closed"}, "i", "inspector")
        del record
        for target in STATES[1:]:
            item = service.transition(
                item["id"], target, item["version"], "r",
                TRANSITION_ROLES[target][0])

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(service, str(Path("static").resolve())))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def submit(src):
            payload = _json.dumps({"delta": 1.0, "reason": src,
                                   "expected_version": 1,
                                   "source_ref": src}).encode()
            req = Request(f"{base}/api/items/{item['id']}/quota/adjust",
                          data=payload, method="POST",
                          headers={"Content-Type": "application/json",
                                   "X-Actor": src, "X-Role": "compliance_manager"})
            try:
                with urlopen(req) as resp:
                    return resp.status
            except HTTPError as exc:
                return exc.code

        threads = [threading.Thread(target=lambda s=s: results.append(submit(s)))
                   for s in ("ADJ-T1", "ADJ-T2")]
        results = []
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertIn(200, results)
        self.assertIn(409, results)
        adjustments = [l for l in service.list_ledger(item["id"], "viewer")
                       if l["kind"] == "adjustment"]
        self.assertEqual(len(adjustments), 1)
        summary = service.quota_summary(item["id"], "viewer")
        self.assertEqual(summary["quota_version"], 2)
        server.shutdown(); server.server_close(); repo.close(); tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
