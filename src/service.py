from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from .domain import (ConflictError, NotFoundError, ensure_role,
                     normalize_severity, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BATCH_NOTE, CONFLICT_VIEW_ROLES, CREATE_ROLES,
                    ENTITY, GRANT_SOURCE, LEDGER_ROLES, LEDGER_VIEW_ROLES,
                    POST_ROLES, RECOVER_ROLES, RECORD_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository
        # 测试用故障注入点：入账阶段抛错以验证按批单号恢复
        self._post_failure = False

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        if target == "approved":
            # 许可批准：以批准额度建立配额台账起点，已存在则不重复入账
            self.repository.grant_initial_quota(item_id, actor, GRANT_SOURCE)
            self.repository.append_audit("quota_grant", "配额台账", item_id, actor, {
                "item_id": item_id, "threshold": updated["threshold"],
                "source_ref": GRANT_SOURCE,
            })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str, kind: Optional[str] = None) -> list:
        self._view(role)
        records = self.repository.list_records(item_id)
        if kind:
            records = [r for r in records if r["kind"] == kind]
        return records

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ------------------------------------------------------------------
    # 配额台账：调整先到生效，后到者看到当前版本和差异，不能覆盖已入账数据
    # ------------------------------------------------------------------
    def quota_summary(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        self.repository.get_item(item_id)
        return self.repository.quota_summary(item_id)

    def list_ledger(self, item_id: Optional[int], role: str) -> List[Dict[str, Any]]:
        ensure_role(role, LEDGER_VIEW_ROLES)
        if item_id is not None:
            self.repository.get_item(item_id)
        return self.repository.list_ledger(item_id)

    def adjust_quota(self, item_id: int, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, LEDGER_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        delta = require_number(payload.get("delta"), "delta", -1e15)
        reason = require_text(payload.get("reason"), "reason")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 0:
            raise ValueError("expected_version必须是非负整数")
        if item["quota_version"] != expected_version:
            # 后到者：返回当前版本、当前额度与本次差异，但不写入、不覆盖
            current = self.repository.quota_summary(item_id)
            raise ConflictError("额度调整冲突：已有先到调整生效，请基于当前版本提交", {
                "item_id": item_id,
                "expected_version": expected_version,
                "current_version": current["quota_version"],
                "current_granted": current["granted"],
                "current_remaining": current["remaining"],
                "your_delta": delta,
                "your_projected_granted": current["granted"] + delta,
                "version_gap": current["quota_version"] - expected_version,
            })
        if not any(l["kind"] == "grant"
                   for l in self.repository.list_ledger(item_id)):
            raise ConflictError("许可尚未批准，无授予台账，不能调整额度")
        summary = self.repository.quota_summary(item_id)
        if summary["granted"] + delta < summary["emitted"]:
            raise ConflictError(
                "调整后授予额度不能低于已排放量，不能覆盖已入账数据", {
                    "granted": summary["granted"], "emitted": summary["emitted"],
                    "delta": delta,
                })
        source_ref = require_text(payload.get("source_ref"), "source_ref", 100)
        ledger = self.repository.adjust_quota(
            item_id, delta, reason, expected_version, source_ref, actor)
        after = self.repository.quota_summary(item_id)
        self.repository.append_audit("quota_adjust", "配额台账", item_id, actor, {
            "ledger_id": ledger["id"], "delta": delta, "reason": reason,
            "from_version": expected_version,
            "to_version": after["quota_version"],
        })
        return {"ledger": ledger, "quota": after}

    # ------------------------------------------------------------------
    # 排放批单：同一批单号只接收第一次结果，晚到内容留作冲突；可按批单号恢复
    # ------------------------------------------------------------------
    @staticmethod
    def _content_hash(permit_ref: str, pollutant: str, amount: float,
                      period: str) -> str:
        canonical = json.dumps(
            {"permit_ref": permit_ref, "pollutant": pollutant,
             "amount": amount, "period": period},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _parse_batch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        permit_ref = require_text(payload.get("permit_ref"), "permit_ref", 100)
        pollutant = require_text(payload.get("pollutant"), "pollutant", 100)
        amount = require_number(payload.get("amount"), "amount")
        period = require_text(payload.get("period"), "period", 100)
        content_hash = self._content_hash(permit_ref, pollutant, amount, period)
        return {"batch_no": batch_no, "permit_ref": permit_ref,
                "pollutant": pollutant, "amount": amount, "period": period,
                "content_hash": content_hash}

    def post_emission_batch(self, payload: Dict[str, Any], actor: str,
                            role: str) -> Dict[str, Any]:
        ensure_role(role, POST_ROLES)
        actor = require_text(actor, "actor", 100)
        parsed = self._parse_batch(payload)
        batch_no = parsed["batch_no"]

        existing = self.repository.find_batch(batch_no)
        if existing is not None:
            if existing["content_hash"] == parsed["content_hash"]:
                # 完全相同的重投：幂等返回，不重复入账
                if existing["status"] == "posted":
                    envelope = dict(existing["envelope"])
                    envelope["replay"] = True
                    return {"status": "posted", "batch": existing,
                            "envelope": envelope, "replay": True}
                # 首次写入失败或进程中断：相同内容按批单号恢复入账
                return self._recover(existing, parsed["amount"], actor)
            # 晚到且内容不同：留作冲突，不覆盖第一次结果
            conflict = self.repository.add_batch_conflict(
                batch_no, parsed["permit_ref"], parsed, parsed["content_hash"], actor)
            self.repository.append_audit("batch_conflict", "排放批单",
                                         existing["id"], actor, {
                "batch_no": batch_no, "conflict_id": conflict["id"],
                "accepted_content_hash": existing["content_hash"],
                "late_content_hash": parsed["content_hash"], "note": BATCH_NOTE})
            raise ConflictError("批单号已有第一次结果，晚到内容留作冲突", {
                "batch_no": batch_no, "accepted_status": existing["status"],
                "accepted_content_hash": existing["content_hash"],
                "late_content_hash": parsed["content_hash"],
                "conflict_id": conflict["id"], "note": BATCH_NOTE})

        batch = self.repository.accept_batch(
            batch_no, parsed["permit_ref"], parsed["pollutant"], parsed["amount"],
            parsed["period"], parsed["content_hash"], actor)
        return self._post_accepted(batch, parsed["amount"], actor)

    def _post_accepted(self, batch: Dict[str, Any], amount: float,
                       actor: str) -> Dict[str, Any]:
        permit = self.repository.find_item_by_external_ref(batch["permit_ref"])
        if permit is None:
            # 许可缺失是可恢复失败：批单保留为failed，待许可建立后按批单号恢复
            self.repository.mark_batch_failed(batch["id"])
            self.repository.append_audit("batch_failed", "排放批单", batch["id"],
                                         actor, {"batch_no": batch["batch_no"],
                                                 "permit_ref": batch["permit_ref"],
                                                 "reason": "引用的许可不存在"})
            raise NotFoundError(
                f"批单{batch['batch_no']}引用的许可{batch['permit_ref']}不存在，"
                "批单已保留为失败，可在许可建立后按批单号恢复")
        try:
            if self._post_failure:  # 故障注入：模拟写入失败
                raise RuntimeError("injected post failure")
            envelope = self.repository.post_emission_usage(
                batch["id"], permit["id"], amount,
                f"园区排放批单{batch['batch_no']}入账", actor)
        except Exception:
            # 写入失败：标记失败，台账未提交，可按批单号重放
            self.repository.mark_batch_failed(batch["id"])
            raise
        self.repository.append_audit("batch_posted", "排放批单", batch["id"], actor, {
            "batch_no": batch["batch_no"], "item_id": permit["id"],
            "ledger_id": envelope["ledger_id"], "amount": amount,
            "remaining": envelope["quota"]["remaining"],
            "exceeded": envelope["quota"]["exceeded"]})
        return {"status": "posted", "batch": self.repository.get_batch(batch["id"]),
                "envelope": envelope, "replay": False}

    def _recover(self, batch: Dict[str, Any], amount: float,
                 actor: str) -> Dict[str, Any]:
        permit = self.repository.find_item_by_external_ref(batch["permit_ref"])
        if permit is None:
            self.repository.mark_batch_failed(batch["id"])
            raise NotFoundError(
                f"批单{batch['batch_no']}引用的许可{batch['permit_ref']}仍不存在，"
                "继续保留为失败")
        if batch["status"] == "posted":
            envelope = dict(batch["envelope"])
            envelope["replay"] = True
            return {"status": "posted", "batch": batch,
                    "envelope": envelope, "replay": True}
        envelope = self.repository.post_emission_usage(
            batch["id"], permit["id"], amount,
            f"园区排放批单{batch['batch_no']}恢复入账", actor)
        self.repository.append_audit("batch_recovered", "排放批单", batch["id"],
                                     actor, {"batch_no": batch["batch_no"],
                                             "item_id": permit["id"],
                                             "ledger_id": envelope["ledger_id"]})
        return {"status": "posted", "batch": self.repository.get_batch(batch["id"]),
                "envelope": envelope, "replay": True}

    def recover_batch(self, batch_no: str, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECOVER_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_no = require_text(batch_no, "batch_no", 100)
        batch = self.repository.get_batch(batch_no=batch_no)
        return self._recover(batch, batch["amount"], actor)

    def list_batches(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return self.repository.list_batches(status)

    def list_conflicts(self, role: str, batch_no: Optional[str] = None) -> list:
        ensure_role(role, CONFLICT_VIEW_ROLES)
        return self.repository.list_batch_conflicts(batch_no)

    def backfill_ledger(self, actor: str, role: str) -> Dict[str, Any]:
        """旧数据缺少台账：按已批准许可回填初始额度。"""
        ensure_role(role, LEDGER_ROLES)
        actor = require_text(actor, "actor", 100)
        backfilled = self.repository.backfill_approved_without_ledger(actor)
        for ledger in backfilled:
            self.repository.append_audit("quota_backfill", "配额台账",
                                         ledger["item_id"], actor, {
                "ledger_id": ledger["id"], "amount": ledger["amount"],
                "source_ref": ledger["source_ref"]})
        return {"backfilled": backfilled, "count": len(backfilled)}

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
