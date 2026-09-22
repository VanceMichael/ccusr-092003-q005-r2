"""SQLite 只追加存储：事件日志（哈希链）、冻结的发令决定、签署结果。"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         TEXT NOT NULL UNIQUE,
    race_ref         TEXT NOT NULL,
    leg_ref          TEXT NOT NULL,
    fleet_ref        TEXT,
    event_type       TEXT NOT NULL,
    actor_role       TEXT NOT NULL,
    actor_ref        TEXT NOT NULL,
    occurred_at      TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    source_ref       TEXT,
    payload_json     TEXT NOT NULL,
    prev_hash        TEXT NOT NULL,
    entry_hash       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_leg_time ON events(leg_ref, occurred_at, seq);

CREATE TABLE IF NOT EXISTS start_decisions (
    leg_ref          TEXT PRIMARY KEY,
    race_ref         TEXT NOT NULL,
    open             INTEGER NOT NULL,
    decided_seq      INTEGER NOT NULL,
    commanded_at     TEXT NOT NULL,
    reasons_json     TEXT NOT NULL,
    basis_json       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signed_results (
    leg_ref          TEXT PRIMARY KEY,
    race_ref         TEXT NOT NULL,
    event_seq        INTEGER NOT NULL UNIQUE,
    signed_at        TEXT NOT NULL,
    result_ref       TEXT NOT NULL,
    summary_json     TEXT NOT NULL
);
"""

_EVENT_COLUMNS = (
    "event_id, race_ref, leg_ref, fleet_ref, event_type, actor_role, actor_ref, "
    "occurred_at, received_at, source_ref, payload_json, prev_hash, entry_hash"
)
_EVENT_PLACEHOLDERS = "?,?,?,?,?,?,?,?,?,?,?,?,?"


class Conflict(Exception):
    """唯一性冲突，例如重复 event_id 或赛段已有发令决定。"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 事件日志 -------------------------------------------------------

    def append_event_with_hash(self, event: dict[str, Any], hash_fn) -> int:
        """在同一把锁内完成「取该赛段链头 -> 计算哈希 -> 追加」，保证哈希链连续。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT entry_hash FROM events WHERE leg_ref=? ORDER BY seq DESC LIMIT 1",
                (event["leg_ref"],),
            ).fetchone()
            prev_hash = row["entry_hash"] if row else "GENESIS"
            entry_hash = hash_fn(prev_hash, event)
            try:
                cur = self._conn.execute(
                    f"INSERT INTO events ({_EVENT_COLUMNS}) VALUES ({_EVENT_PLACEHOLDERS})",
                    (
                        event["event_id"],
                        event["race_ref"],
                        event["leg_ref"],
                        event.get("fleet_ref"),
                        event["event_type"],
                        event["actor_role"],
                        event["actor_ref"],
                        event["occurred_at"],
                        event["received_at"],
                        event.get("source_ref"),
                        json.dumps(event["payload"], ensure_ascii=False, sort_keys=True),
                        prev_hash,
                        entry_hash,
                    ),
                )
            except sqlite3.IntegrityError as exc:  # 唯一约束 = 重复投递
                raise Conflict(f"事件已存在：{event['event_id']}") from exc
            return int(cur.lastrowid)

    def head_hash(self, leg_ref: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT entry_hash FROM events WHERE leg_ref=? ORDER BY seq DESC LIMIT 1",
                (leg_ref,),
            ).fetchone()
        return row["entry_hash"] if row else None

    def list_events(self, leg_ref: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE leg_ref=? ORDER BY seq", (leg_ref,)
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
        return self._row_to_event(row) if row else None

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "seq": row["seq"],
            "event_id": row["event_id"],
            "race_ref": row["race_ref"],
            "leg_ref": row["leg_ref"],
            "fleet_ref": row["fleet_ref"],
            "event_type": row["event_type"],
            "actor_role": row["actor_role"],
            "actor_ref": row["actor_ref"],
            "occurred_at": row["occurred_at"],
            "received_at": row["received_at"],
            "source_ref": row["source_ref"],
            "payload": json.loads(row["payload_json"]),
            "prev_hash": row["prev_hash"],
            "entry_hash": row["entry_hash"],
        }

    # ---- 发令决定（冻结） ----------------------------------------------

    def save_decision(self, decision: dict[str, Any]) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO start_decisions (leg_ref, race_ref, open, decided_seq, "
                    "commanded_at, reasons_json, basis_json) VALUES (?,?,?,?,?,?,?)",
                    (
                        decision["leg_ref"],
                        decision["race_ref"],
                        1 if decision["open"] else 0,
                        decision["decided_seq"],
                        decision["commanded_at"],
                        json.dumps(decision["reasons"], ensure_ascii=False),
                        json.dumps(decision["basis"], ensure_ascii=False, sort_keys=True),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"赛段已有发令决定：{decision['leg_ref']}") from exc

    def get_decision(self, leg_ref: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM start_decisions WHERE leg_ref=?", (leg_ref,)
            ).fetchone()
        if not row:
            return None
        return {
            "leg_ref": row["leg_ref"],
            "race_ref": row["race_ref"],
            "open": bool(row["open"]),
            "decided_seq": row["decided_seq"],
            "commanded_at": row["commanded_at"],
            "reasons": json.loads(row["reasons_json"]),
            "basis": json.loads(row["basis_json"]),
        }

    # ---- 签署结果 -------------------------------------------------------

    def save_result(self, result: dict[str, Any]) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO signed_results (leg_ref, race_ref, event_seq, signed_at, "
                    "result_ref, summary_json) VALUES (?,?,?,?,?,?)",
                    (
                        result["leg_ref"],
                        result["race_ref"],
                        result["event_seq"],
                        result["signed_at"],
                        result["result_ref"],
                        json.dumps(result["summary"], ensure_ascii=False, sort_keys=True),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"赛段结果已签署：{result['leg_ref']}") from exc

    def get_result(self, leg_ref: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM signed_results WHERE leg_ref=?", (leg_ref,)
            ).fetchone()
        if not row:
            return None
        return {
            "leg_ref": row["leg_ref"],
            "race_ref": row["race_ref"],
            "event_seq": row["event_seq"],
            "signed_at": row["signed_at"],
            "result_ref": row["result_ref"],
            "summary": json.loads(row["summary_json"]),
        }
