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
                CREATE TABLE IF NOT EXISTS quota_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL CHECK(kind IN ('grant','adjustment','usage')),
                    amount REAL NOT NULL,
                    reason TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_ref)
                );
                CREATE INDEX IF NOT EXISTS ix_quota_ledger_item
                    ON quota_ledger(item_id, id);
                CREATE TABLE IF NOT EXISTS emission_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    permit_ref TEXT NOT NULL,
                    item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
                    pollutant TEXT NOT NULL,
                    amount REAL NOT NULL,
                    period TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'received'
                        CHECK(status IN ('received','posted','failed')),
                    received_at TEXT NOT NULL,
                    posted_at TEXT,
                    accepted_by TEXT NOT NULL,
                    ledger_id INTEGER,
                    envelope TEXT
                );
                CREATE TABLE IF NOT EXISTS batch_conflicts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL,
                    permit_ref TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_batch_conflicts_no
                    ON batch_conflicts(batch_no);
            """)
            columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(items)").fetchall()
            }
            if "quota_version" not in columns:
                self.conn.execute(
                    "ALTER TABLE items ADD COLUMN quota_version INTEGER NOT NULL DEFAULT 0"
                )

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

    def find_item_by_external_ref(self, external_ref: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM items WHERE external_ref=?", (external_ref,)
            ).fetchone()
        return dict(row) if row is not None else None

    def grant_initial_quota(self, item_id: int, actor: str,
                            source_ref: str = "approved_permit") -> Dict[str, Any]:
        """许可批准后按已批准额度写入授予台账，仅首次生效，额度版本+1。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT threshold, quota_version FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("项目不存在")
            exists = self.conn.execute(
                "SELECT 1 FROM quota_ledger WHERE item_id=? AND source_ref=?",
                (item_id, source_ref),
            ).fetchone()
            if exists is not None:
                ledger = self.conn.execute(
                    "SELECT * FROM quota_ledger WHERE item_id=? AND source_ref=?",
                    (item_id, source_ref),
                ).fetchone()
                return dict(ledger)
            cur = self.conn.execute(
                """INSERT INTO quota_ledger(item_id, kind, amount, reason, source_ref,
                   created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                (item_id, "grant", float(row["threshold"]), "按已批准许可回填初始额度",
                 source_ref, actor, now),
            )
            ledger_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE items SET quota_version=quota_version+1, updated_at=? WHERE id=?",
                (now, item_id),
            )
            ledger = self.conn.execute(
                "SELECT * FROM quota_ledger WHERE id=?", (ledger_id,)
            ).fetchone()
        return dict(ledger)

    def adjust_quota(self, item_id: int, delta: float, reason: str,
                     expected_version: int, source_ref: str,
                     actor: str) -> Dict[str, Any]:
        """额度调整：先到生效。expected_version不匹配则拒绝且不写台账。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT threshold, quota_version FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("项目不存在")
            if int(row["quota_version"]) != expected_version:
                raise ConflictError("额度版本已变化，请基于当前版本重试")
            cur = self.conn.execute(
                """INSERT INTO quota_ledger(item_id, kind, amount, reason, source_ref,
                   created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                (item_id, "adjustment", float(delta), reason, source_ref, actor, now),
            )
            ledger_id = int(cur.lastrowid)
            self.conn.execute(
                "UPDATE items SET quota_version=quota_version+1, updated_at=? WHERE id=?",
                (now, item_id),
            )
            ledger = self.conn.execute(
                "SELECT * FROM quota_ledger WHERE id=?", (ledger_id,)
            ).fetchone()
        return dict(ledger)

    def list_ledger(self, item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM quota_ledger"
        params: tuple = ()
        if item_id is not None:
            sql += " WHERE item_id=?"
            params = (item_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def quota_summary(self, item_id: int) -> Dict[str, Any]:
        item = self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT kind, COALESCE(SUM(amount),0) AS total FROM quota_ledger "
                "WHERE item_id=? GROUP BY kind",
                (item_id,),
            ).fetchall()
        totals = {row["kind"]: float(row["total"]) for row in rows}
        granted = totals.get("grant", 0.0) + totals.get("adjustment", 0.0)
        emitted = totals.get("usage", 0.0)
        return {
            "item_id": item_id,
            "quota_version": item["quota_version"],
            "granted": granted,
            "adjusted": totals.get("adjustment", 0.0),
            "emitted": emitted,
            "remaining": granted - emitted,
            "exceeded": emitted > granted,
        }

    def accept_batch(self, batch_no: str, permit_ref: str, pollutant: str,
                     amount: float, period: str, content_hash: str,
                     actor: str) -> Dict[str, Any]:
        """接收批单（第一次到达）。同一批单号重复接收触发唯一约束。"""
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO emission_batches(batch_no, permit_ref, pollutant, amount,
                       period, content_hash, status, received_at, accepted_by)
                       VALUES(?,?,?,?,?,?, 'received', ?,?)""",
                    (batch_no, permit_ref, pollutant, amount, period, content_hash, now, actor),
                )
                batch_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("批单号已接收") from exc
        return self.get_batch(batch_id)

    def find_batch(self, batch_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM emission_batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
        return self._batch(row) if row is not None else None

    def get_batch(self, batch_id: Optional[int] = None,
                  batch_no: Optional[str] = None) -> Dict[str, Any]:
        sql = "SELECT * FROM emission_batches"
        params: tuple
        if batch_no is not None:
            sql += " WHERE batch_no=?"
            params = (batch_no,)
        else:
            sql += " WHERE id=?"
            params = (batch_id,)
        with self._lock:
            row = self.conn.execute(sql, params).fetchone()
        if row is None:
            raise NotFoundError("批单不存在")
        return self._batch(row)

    def list_batches(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM emission_batches"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._batch(row) for row in rows]

    @staticmethod
    def _batch(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        if item.get("envelope"):
            item["envelope"] = json.loads(item["envelope"])
        return item

    def add_batch_conflict(self, batch_no: str, permit_ref: str,
                           payload: Dict[str, Any], content_hash: str,
                           actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO batch_conflicts(batch_no, permit_ref, payload, content_hash,
                   created_by, created_at) VALUES(?,?,?,?,?,?)""",
                (batch_no, permit_ref,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 content_hash, actor, now),
            )
            conflict_id = int(cur.lastrowid)
            row = self.conn.execute(
                "SELECT * FROM batch_conflicts WHERE id=?", (conflict_id,)
            ).fetchone()
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def list_batch_conflicts(self, batch_no: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM batch_conflicts"
        params: tuple = ()
        if batch_no is not None:
            sql += " WHERE batch_no=?"
            params = (batch_no,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def post_emission_usage(self, batch_id: int, item_id: int, amount: float,
                            reason: str, actor: str) -> Dict[str, Any]:
        """把批单入账为usage台账并封存结果信封。批单号source_ref保证只入账一次。"""
        now = utc_now()
        source_ref = f"emission_batch:{self.get_batch(batch_id)['batch_no']}"
        with self._lock, self.conn:
            batch = self.conn.execute(
                "SELECT * FROM emission_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise NotFoundError("批单不存在")
            if batch["status"] == "posted":
                raise ConflictError("批单已入账")
            try:
                cur = self.conn.execute(
                    """INSERT INTO quota_ledger(item_id, kind, amount, reason, source_ref,
                       created_by, created_at) VALUES(?, 'usage', ?,?,?,?,?)""",
                    (item_id, amount, reason, source_ref, actor, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("批单已入账") from exc
            ledger_id = int(cur.lastrowid)
            envelope = self._build_envelope(item_id, batch_id, ledger_id)
            self.conn.execute(
                """UPDATE emission_batches SET status='posted', item_id=?, ledger_id=?,
                   posted_at=?, envelope=? WHERE id=?""",
                (item_id, ledger_id, now,
                 json.dumps(envelope, ensure_ascii=False, sort_keys=True), batch_id),
            )
        return envelope

    def mark_batch_failed(self, batch_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE emission_batches SET status='failed' WHERE id=? AND status='received'",
                (batch_id,),
            )

    def _build_envelope(self, item_id: int, batch_id: int,
                        ledger_id: int) -> Dict[str, Any]:
        """入账结果信封：把许可、台账、检查/整改状态链接在一起。"""
        item = self.get_item(item_id)
        summary = self.quota_summary(item_id)
        with self._lock:
            batch_row = self.conn.execute(
                "SELECT * FROM emission_batches WHERE id=?", (batch_id,)
            ).fetchone()
            open_rows = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
            inspection_rows = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND kind='inspection'",
                (item_id,),
            ).fetchone()
        return {
            "batch_no": batch_row["batch_no"],
            "permit_ref": batch_row["permit_ref"],
            "item_id": item_id,
            "permit_status": item["status"],
            "ledger_id": ledger_id,
            "pollutant": batch_row["pollutant"],
            "amount": batch_row["amount"],
            "period": batch_row["period"],
            "posted_at": utc_now(),
            "quota": summary,
            "inspection_records": int(inspection_rows["n"]),
            "open_rectifications": int(open_rows["n"]),
        }

    def backfill_approved_without_ledger(self, actor: str) -> List[Dict[str, Any]]:
        """旧数据缺少台账时，按已批准许可回填初始授予额度（可重复执行）。"""
        with self._lock:
            rows = self.conn.execute(
                """SELECT id, threshold FROM items
                   WHERE status='approved' AND NOT EXISTS (
                       SELECT 1 FROM quota_ledger l
                       WHERE l.item_id=items.id AND l.kind='grant')"""
            ).fetchall()
        backfilled = []
        for row in rows:
            backfilled.append(self.grant_initial_quota(int(row["id"]), actor))
        return backfilled

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

    def close(self) -> None:
        with self._lock:
            self.conn.close()
