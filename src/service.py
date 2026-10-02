from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, BACKFILL_ROLES, BATCH_ROLES, CREATE_ROLES, ENTITY,
                    QUOTA_ROLES, RECORD_ROLES, TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

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
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ------------------------------------------------------------------
    # 排放批单入账（idempotent batch posting）
    # ------------------------------------------------------------------
    @staticmethod
    def _require_permit_id(value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            from .domain import ValidationError
            raise ValidationError("permit_id必须是正整数")
        return value

    def accept_batch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        permit_id = self._require_permit_id(payload.get("permit_id"))
        quantity = require_number(payload.get("quantity"), "quantity")
        self.repository.get_item(permit_id)
        try:
            result = self.repository.accept_batch(batch_no, permit_id, quantity,
                                                  payload, actor)
        except ConflictError as exc:
            if exc.detail.get("reason") == "batch_no_conflict":
                self.repository.append_audit("batch_conflict", ENTITY, permit_id,
                                             actor, {"batch_no": batch_no,
                                                     "quantity": quantity})
            raise
        batch = result["batch"]
        if result["created"]:
            ledger = self.repository.get_ledger(permit_id)
            self.repository.append_audit("batch_accept", ENTITY, permit_id, actor, {
                "batch_no": batch_no, "quantity": quantity,
                "ledger_version": ledger["version"] if ledger else None,
            })
        elif result["replay"]:
            self.repository.append_audit("batch_replay", ENTITY, permit_id, actor, {
                "batch_no": batch_no,
            })
        return result

    def recover_batch(self, batch_no: str, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_no = require_text(batch_no, "batch_no", 100)
        result = self.repository.recover_batch(batch_no, actor)
        batch = result["batch"]
        self.repository.append_audit("batch_recover", ENTITY, batch["permit_id"],
                                     actor, {"batch_no": batch_no,
                                             "healed": result["healed"],
                                             "replayed": result["replayed"]})
        return result

    def get_batch(self, batch_no: str, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_batch(batch_no)

    def list_batches(self, role: str, permit_id: Optional[int] = None) -> list:
        self._view(role)
        return self.repository.list_batches(permit_id)

    def list_conflicts(self, role: str, batch_no: Optional[str] = None) -> list:
        self._view(role)
        return self.repository.list_conflicts(batch_no)

    # ------------------------------------------------------------------
    # 配额台账（quota ledger with optimistic concurrency）
    # ------------------------------------------------------------------
    def get_ledger(self, permit_id: int, role: str) -> Optional[Dict[str, Any]]:
        self._view(role)
        permit_id = self._require_permit_id(permit_id)
        self.repository.get_item(permit_id)
        return self.repository.get_ledger(permit_id)

    def list_adjustments(self, permit_id: int, role: str) -> list:
        self._view(role)
        permit_id = self._require_permit_id(permit_id)
        self.repository.get_item(permit_id)
        return self.repository.list_adjustments(permit_id)

    def adjust_quota(self, permit_id: int, payload: Dict[str, Any], actor: str,
                     role: str) -> Dict[str, Any]:
        ensure_role(role, QUOTA_ROLES)
        actor = require_text(actor, "actor", 100)
        permit_id = self._require_permit_id(permit_id)
        self.repository.get_item(permit_id)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            from .domain import ValidationError
            raise ValidationError("expected_version必须是正整数")
        new_quantity = require_number(payload.get("new_quantity"), "new_quantity")
        reason = payload.get("reason")
        if reason is not None:
            reason = require_text(reason, "reason", 200)
        result = self.repository.adjust_quota(permit_id, expected_version,
                                              new_quantity, actor, reason)
        adj = result["adjustment"]
        self.repository.append_audit("quota_adjust", ENTITY, permit_id, actor, {
            "from_version": adj["from_version"], "to_version": adj["to_version"],
            "from_quantity": adj["from_quantity"],
            "to_quantity": adj["to_quantity"], "reason": reason,
        })
        return result

    # ------------------------------------------------------------------
    # 台账回填（backfill ledger from approved permit）
    # ------------------------------------------------------------------
    def backfill_ledger(self, permit_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BACKFILL_ROLES)
        actor = require_text(actor, "actor", 100)
        permit_id = self._require_permit_id(permit_id)
        result = self.repository.backfill_ledger(permit_id, actor)
        if result["created"]:
            self.repository.append_audit("ledger_backfill", ENTITY, permit_id, actor, {
                "quantity": result["ledger"]["current_quantity"],
            })
        return result

    def backfill_all(self, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, BACKFILL_ROLES)
        actor = require_text(actor, "actor", 100)
        ids = self.repository.list_approved_permit_ids_without_ledger()
        results = []
        for pid in ids:
            result = self.repository.backfill_ledger(pid, actor)
            if result["created"]:
                self.repository.append_audit("ledger_backfill", ENTITY, pid, actor, {
                    "quantity": result["ledger"]["current_quantity"],
                })
            results.append({"permit_id": pid, "created": result["created"]})
        return {"backfilled": sum(1 for r in results if r["created"]),
                "results": results}

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
