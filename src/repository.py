from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS emission_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    permit_id INTEGER NOT NULL REFERENCES items(id),
                    quantity REAL NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('accepted','failed')),
                    result TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batch_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL,
                    permit_id INTEGER,
                    quantity REAL,
                    payload TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_batch_conflicts_no
                    ON batch_conflicts(batch_no);
                CREATE TABLE IF NOT EXISTS quota_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    permit_id INTEGER NOT NULL UNIQUE REFERENCES items(id),
                    approved_quantity REAL NOT NULL,
                    current_quantity REAL NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    source_batch_no TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quota_adjustments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    permit_id INTEGER NOT NULL REFERENCES items(id),
                    batch_no TEXT,
                    from_version INTEGER NOT NULL,
                    to_version INTEGER NOT NULL,
                    from_quantity REAL NOT NULL,
                    to_quantity REAL NOT NULL,
                    actor TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_quota_adjustments_permit
                    ON quota_adjustments(permit_id, id)
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ------------------------------------------------------------------
    # 排放批单（idempotent batch intake）
    # ------------------------------------------------------------------
    @staticmethod
    def _batch(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        if item.get("result"):
            item["result"] = json.loads(item["result"])
        return item

    @staticmethod
    def _same_payload(stored: Dict[str, Any], permit_id: int, quantity: float,
                      payload: Dict[str, Any]) -> bool:
        return (stored["permit_id"] == permit_id
                and float(stored["quantity"]) == float(quantity)
                and stored["payload"] == payload)

    def _post_batch_to_ledger(self, cur: sqlite3.Cursor, permit_id: int,
                              quantity: float, batch_no: str, actor: str) -> None:
        now = utc_now()
        row = cur.execute(
            "SELECT * FROM quota_ledger WHERE permit_id=?", (permit_id,)
        ).fetchone()
        if row is None:
            cur.execute(
                """INSERT INTO quota_ledger(permit_id, approved_quantity, current_quantity,
                   version, source_batch_no, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (permit_id, quantity, quantity, 1, batch_no, now, now),
            )
            cur.execute(
                """INSERT INTO quota_adjustments(permit_id, batch_no, from_version, to_version,
                   from_quantity, to_quantity, actor, reason, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (permit_id, batch_no, 0, 1, 0.0, quantity, actor, "batch_posting", now),
            )
        else:
            old = dict(row)
            new_version = int(old["version"]) + 1
            cur.execute(
                """UPDATE quota_ledger SET current_quantity=?, version=?, source_batch_no=?,
                   updated_at=? WHERE permit_id=?""",
                (quantity, new_version, batch_no, now, permit_id),
            )
            cur.execute(
                """INSERT INTO quota_adjustments(permit_id, batch_no, from_version, to_version,
                   from_quantity, to_quantity, actor, reason, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (permit_id, batch_no, old["version"], new_version,
                 old["current_quantity"], quantity, actor, "batch_posting", now),
            )

    def _record_conflict(self, cur: sqlite3.Cursor, batch_no: str, permit_id: int,
                         quantity: float, payload: Dict[str, Any], reason: str,
                         actor: str) -> None:
        cur.execute(
            """INSERT INTO batch_conflicts(batch_no, permit_id, quantity, payload,
               reason, created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
            (batch_no, permit_id, quantity, json.dumps(payload, ensure_ascii=False,
             sort_keys=True), reason, actor, utc_now()),
        )

    def accept_batch(self, batch_no: str, permit_id: int, quantity: float,
                     payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        """Accept a batch posting. Idempotent by batch_no.

        Returns a dict with keys: batch (the stored batch), created (bool),
        replay (bool). Raises ConflictError when a different payload arrives
        for an already-accepted batch_no (late content kept as conflict).
        """
        conflict = None
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM emission_batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
            if row is not None:
                stored = self._batch(row)
                if self._same_payload(stored, permit_id, quantity, payload):
                    return {"batch": stored, "created": False, "replay": True}
                conflict = stored
            else:
                try:
                    cur = self.conn.execute(
                        """INSERT INTO emission_batches(batch_no, permit_id, quantity, payload,
                           status, result, created_by, created_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (batch_no, permit_id, quantity,
                         json.dumps(payload, ensure_ascii=False, sort_keys=True),
                         "accepted", None, actor, utc_now()),
                    )
                    batch_id = int(cur.lastrowid)
                    self._post_batch_to_ledger(self.conn, permit_id, quantity,
                                              batch_no, actor)
                except sqlite3.IntegrityError:
                    # Concurrent inserter won; fetch and compare.
                    row = self.conn.execute(
                        "SELECT * FROM emission_batches WHERE batch_no=?", (batch_no,)
                    ).fetchone()
                    stored = self._batch(row)
                    if self._same_payload(stored, permit_id, quantity, payload):
                        return {"batch": stored, "created": False, "replay": True}
                    conflict = stored
        if conflict is not None:
            # Record the late content in its own committed transaction so the
            # conflict survives the rollback of the batch transaction.
            with self._lock, self.conn:
                self._record_conflict(self.conn, batch_no, permit_id, quantity,
                                      payload, "batch_no已存在且内容不一致", actor)
            raise ConflictError(
                "批单号已存在且内容不一致，晚到内容已留作冲突",
                detail={"batch_no": batch_no, "reason": "batch_no_conflict",
                        "stored": conflict},
            )
        return {"batch": self.get_batch(batch_no), "created": True, "replay": False}

    def recover_batch(self, batch_no: str, actor: str) -> Dict[str, Any]:
        """Recover a batch by batch_no.

        Accepted batches are a no-op (idempotent replay). Failed batches are
        re-processed: the ledger is filled if missing and the batch is marked
        accepted. Missing batches raise NotFoundError.
        """
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM emission_batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
            if row is None:
                raise NotFoundError("批单不存在，无法恢复")
            stored = self._batch(row)
            healed = False
            if stored["status"] == "accepted":
                # Heal partial state: ledger may be missing for an accepted batch.
                ledger = self.conn.execute(
                    "SELECT * FROM quota_ledger WHERE permit_id=?",
                    (stored["permit_id"],),
                ).fetchone()
                if ledger is None:
                    self._post_batch_to_ledger(self.conn, stored["permit_id"],
                                               stored["quantity"], batch_no, actor)
                    healed = True
                return {"batch": stored, "replayed": True, "healed": healed}
            # status == 'failed': re-process.
            self._post_batch_to_ledger(self.conn, stored["permit_id"],
                                       stored["quantity"], batch_no, actor)
            self.conn.execute(
                "UPDATE emission_batches SET status='accepted' WHERE batch_no=?",
                (batch_no,),
            )
            healed = True
        return {"batch": self.get_batch(batch_no), "replayed": False, "healed": healed}

    def get_batch(self, batch_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM emission_batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
        if row is None:
            raise NotFoundError("批单不存在")
        return self._batch(row)

    def list_batches(self, permit_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM emission_batches"
        params: tuple = ()
        if permit_id is not None:
            sql += " WHERE permit_id=?"
            params = (permit_id,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._batch(row) for row in rows]

    def list_conflicts(self, batch_no: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM batch_conflicts"
        params: tuple = ()
        if batch_no is not None:
            sql += " WHERE batch_no=?"
            params = (batch_no,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    # ------------------------------------------------------------------
    # 配额台账（quota ledger with optimistic concurrency）
    # ------------------------------------------------------------------
    def get_ledger(self, permit_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM quota_ledger WHERE permit_id=?", (permit_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def require_ledger(self, permit_id: int) -> Dict[str, Any]:
        ledger = self.get_ledger(permit_id)
        if ledger is None:
            raise ConflictError(
                "配额台账不存在，请先按已批准许可回填",
                detail={"reason": "ledger_missing", "permit_id": permit_id},
            )
        return ledger

    def adjust_quota(self, permit_id: int, expected_version: int,
                     new_quantity: float, actor: str,
                     reason: Optional[str] = None) -> Dict[str, Any]:
        """Adjust quota with optimistic concurrency (compare-and-swap).

        First writer wins. A stale expected_version raises ConflictError with
        the current version and diff; already-entered data is never overwritten.
        """
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM quota_ledger WHERE permit_id=?", (permit_id,)
            ).fetchone()
            if row is None:
                raise ConflictError(
                    "配额台账不存在，请先按已批准许可回填",
                    detail={"reason": "ledger_missing", "permit_id": permit_id},
                )
            old = dict(row)
            if int(old["version"]) != expected_version:
                diff = float(new_quantity) - float(old["current_quantity"])
                raise ConflictError(
                    "版本冲突，额度已被他人调整，请刷新后重试",
                    detail={
                        "reason": "version_conflict",
                        "expected_version": expected_version,
                        "current_version": int(old["version"]),
                        "current_quantity": float(old["current_quantity"]),
                        "proposed_quantity": float(new_quantity),
                        "diff": diff,
                    },
                )
            now = utc_now()
            new_version = int(old["version"]) + 1
            cur = self.conn.execute(
                """UPDATE quota_ledger SET current_quantity=?, version=?, updated_at=?
                   WHERE permit_id=? AND version=?""",
                (new_quantity, new_version, now, permit_id, expected_version),
            )
            if cur.rowcount == 0:
                # Lost the race; re-read the true current state.
                current = dict(self.conn.execute(
                    "SELECT * FROM quota_ledger WHERE permit_id=?", (permit_id,)
                ).fetchone())
                diff = float(new_quantity) - float(current["current_quantity"])
                raise ConflictError(
                    "版本冲突，额度已被他人调整，请刷新后重试",
                    detail={
                        "reason": "version_conflict",
                        "expected_version": expected_version,
                        "current_version": int(current["version"]),
                        "current_quantity": float(current["current_quantity"]),
                        "proposed_quantity": float(new_quantity),
                        "diff": diff,
                    },
                )
            self.conn.execute(
                """INSERT INTO quota_adjustments(permit_id, batch_no, from_version, to_version,
                   from_quantity, to_quantity, actor, reason, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (permit_id, None, expected_version, new_version,
                 old["current_quantity"], new_quantity, actor, reason, now),
            )
        return {"ledger": self.get_ledger(permit_id),
                "adjustment": {
                    "permit_id": permit_id, "from_version": expected_version,
                    "to_version": new_version,
                    "from_quantity": float(old["current_quantity"]),
                    "to_quantity": float(new_quantity),
                    "actor": actor, "reason": reason,
                }}

    def backfill_ledger(self, permit_id: int, actor: str) -> Dict[str, Any]:
        """Backfill a missing ledger from the approved permit.

        Approved permits without a ledger get one seeded from the permit's
        approved threshold. Existing ledgers are returned untouched.
        """
        with self._lock, self.conn:
            permit = self.conn.execute(
                "SELECT * FROM items WHERE id=?", (permit_id,)
            ).fetchone()
            if permit is None:
                raise NotFoundError("项目不存在")
            if permit["status"] != "approved":
                raise ConflictError(
                    "许可未批准，不能回填台账",
                    detail={"reason": "permit_not_approved",
                            "status": permit["status"]},
                )
            existing = self.conn.execute(
                "SELECT * FROM quota_ledger WHERE permit_id=?", (permit_id,)
            ).fetchone()
            if existing is not None:
                return {"ledger": dict(existing), "created": False}
            now = utc_now()
            approved = float(permit["threshold"])
            self.conn.execute(
                """INSERT INTO quota_ledger(permit_id, approved_quantity, current_quantity,
                   version, source_batch_no, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (permit_id, approved, approved, 1, None, now, now),
            )
            self.conn.execute(
                """INSERT INTO quota_adjustments(permit_id, batch_no, from_version, to_version,
                   from_quantity, to_quantity, actor, reason, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (permit_id, None, 0, 1, 0.0, approved, actor, "backfill", now),
            )
        return {"ledger": self.get_ledger(permit_id), "created": True}

    def list_approved_permit_ids_without_ledger(self) -> List[int]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT i.id FROM items i
                   LEFT JOIN quota_ledger q ON q.permit_id=i.id
                   WHERE i.status='approved' AND q.id IS NULL
                   ORDER BY i.id""",
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def list_adjustments(self, permit_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM quota_adjustments WHERE permit_id=? ORDER BY id",
                (permit_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
