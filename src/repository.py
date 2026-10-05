import json
import sqlite3
from datetime import datetime, timezone

from . import rules
from .audit import audit_hash, canonical_json
from .domain import ConflictError, DomainError, NotFoundError, normalize_utc


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    superseded_at TEXT,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS source_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    replaced_at TEXT NOT NULL,
                    replaced_by TEXT,
                    replace_role TEXT,
                    FOREIGN KEY(source_id) REFERENCES sources(id)
                );
                CREATE TABLE IF NOT EXISTS delegations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    grantee TEXT NOT NULL,
                    region TEXT NOT NULL,
                    item_id INTEGER,
                    expires_at TEXT NOT NULL,
                    note TEXT,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT,
                    revoke_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_delegations_lookup
                    ON delegations(grantee, region, revoked_at, expires_at);
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, record, actor, role):
        """登记来源测量。同一 (source_type, external_id) 的重复回传在单个事务内完成
        取舍、留痕、基准重算和事件版本推进。"""
        source_type = record["source_type"]
        external_id = record["external_id"]
        observed_at = normalize_utc(record["observed_at"], "observed_at")
        strength = float(record["strength_dbm"])
        source_payload = {
            "strength_dbm": strength,
            "region": record.get("region"),
            "station_id": record.get("station_id"),
            "frequency_mhz": record.get("frequency_mhz"),
        }
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item_row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if item_row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            existing = conn.execute(
                "SELECT * FROM sources WHERE item_id=? AND source_type=? AND external_id=?",
                (item_id, source_type, external_id),
            ).fetchone()
            existing_dict = None
            if existing is not None:
                existing_dict = dict(existing)
                existing_dict["payload"] = json.loads(existing["payload"])
            decision = rules.decide_source(
                existing_dict,
                observed_at,
                strength,
            )
            recorded_at = now_iso()
            change = None
            if decision == "insert":
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(source_payload), observed_at, recorded_at),
                )
                source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                self.append_audit(
                    conn, item_id, "source_recorded", actor, role,
                    {"source_id": source_id, "source_type": source_type, "external_id": external_id,
                     "observed_at": observed_at, "strength_dbm": strength},
                )
            elif decision == "ignored":
                source_id = existing["id"]
                self.append_audit(
                    conn, item_id, "source_deduplicated", actor, role,
                    {"source_id": source_id, "source_type": source_type, "external_id": external_id,
                     "observed_at": observed_at, "strength_dbm": strength},
                )
            else:
                source_id = existing["id"]
                old_payload = json.loads(existing["payload"])
                old_observed = existing["observed_at"]
                conn.execute(
                    "INSERT INTO source_revisions(source_id,item_id,payload,observed_at,replaced_at,replaced_by,replace_role) VALUES(?,?,?,?,?,?,?)",
                    (source_id, item_id, canonical_json(old_payload), old_observed, recorded_at, actor, role),
                )
                conn.execute(
                    "UPDATE sources SET payload=?, observed_at=?, superseded_at=? WHERE id=?",
                    (canonical_json(source_payload), observed_at, recorded_at, source_id),
                )
                self.append_audit(
                    conn, item_id, "source_superseded", actor, role,
                    {"source_id": source_id, "source_type": source_type, "external_id": external_id,
                     "old": {"strength_dbm": old_payload.get("strength_dbm"), "observed_at": old_observed},
                     "new": {"strength_dbm": strength, "observed_at": observed_at},
                     "replaced_at": recorded_at},
                )

            # 以全部来源的当前测量重算事件基准
            source_rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            sources = []
            for row in source_rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                sources.append(value)
            payload = json.loads(item_row["payload"])
            change = rules.recompute_baseline(
                payload,
                item_row["status"],
                sources,
                item_row["created_at"],
                recorded_at,
                actor,
                role,
                "来源测量更新（source_type=%s, external_id=%s）" % (source_type, external_id),
            )
            if change is not None:
                version = int(item_row["version"]) + 1
                conn.execute(
                    "UPDATE items SET payload=?, version=?, updated_at=? WHERE id=?",
                    (canonical_json(payload), version, recorded_at, item_id),
                )
                self.append_audit(conn, item_id, "baseline_updated", actor, role, change)
            conn.execute("COMMIT")
            return {
                "id": source_id,
                "item_id": item_id,
                "source_type": source_type,
                "external_id": external_id,
                "payload": source_payload,
                "observed_at": observed_at,
                "outcome": decision,
                "baseline_change": change,
            }
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            revision_rows = conn.execute(
                "SELECT * FROM source_revisions WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
            revisions_by_source = {}
            for row in revision_rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                revisions_by_source.setdefault(value["source_id"], []).append(value)
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                value["revisions"] = revisions_by_source.get(value["id"], [])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def expire_due_delegations(self, at=None):
        """到期代管授权自动收回（惰性执行：查询前先把到期记录标记收回）。"""
        at = at or now_iso()
        conn = self.connect()
        try:
            conn.execute(
                "UPDATE delegations SET revoked_at=?, revoke_reason='expired' "
                "WHERE revoked_at IS NULL AND expires_at<=?",
                (at, at),
            )
        finally:
            conn.close()

    def create_delegation(self, delegation, actor, role):
        self.expire_due_delegations()
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            created_at = now_iso()
            conn.execute(
                "INSERT INTO delegations(grantee,region,item_id,expires_at,note,created_by,created_role,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    delegation["grantee"],
                    delegation["region"],
                    delegation["item_id"],
                    delegation["expires_at"],
                    delegation["note"],
                    actor,
                    role,
                    created_at,
                ),
            )
            delegation_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                delegation["item_id"],
                "delegation_granted",
                actor,
                role,
                {
                    "delegation_id": delegation_id,
                    "grantee": delegation["grantee"],
                    "region": delegation["region"],
                    "expires_at": delegation["expires_at"],
                },
            )
            conn.execute("COMMIT")
            return self.get_delegation(delegation_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_delegation(self, delegation_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM delegations WHERE id=?", (delegation_id,)).fetchone()
            if row is None:
                raise NotFoundError("delegation_not_found", "代管授权不存在")
            return dict(row)
        finally:
            conn.close()

    def find_active_delegation(self, grantee, region, item_id=None, at=None):
        self.expire_due_delegations(at)
        conn = self.connect()
        try:
            at_value = at or now_iso()
            row = conn.execute(
                "SELECT * FROM delegations WHERE grantee=? AND region=? AND revoked_at IS NULL AND expires_at>? "
                "AND (item_id IS NULL OR item_id=?) ORDER BY expires_at ASC, id ASC LIMIT 1",
                (grantee, region, at_value, item_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_delegations(self, grantee=None, region=None):
        self.expire_due_delegations()
        conn = self.connect()
        try:
            sql = "SELECT * FROM delegations WHERE 1=1"
            params = []
            if grantee is not None:
                sql += " AND grantee=?"
                params.append(grantee)
            if region is not None:
                sql += " AND region=?"
                params.append(region)
            sql += " ORDER BY id DESC"
            return [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()
