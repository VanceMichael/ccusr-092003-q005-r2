"""SQLite 存储层：事件 append-only，配置表小型幂等登记。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .timeutil import now_iso


def connect(database_path: str | Path) -> sqlite3.Connection:
    # HTTP 层用 ThreadingHTTPServer + 全局 RLock 串行化所有访问，
    # 因此允许连接跨线程复用。
    conn = sqlite3.connect(database_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---- 配置登记 -------------------------------------------------------

    def upsert_race(self, race_ref: str, title: str) -> None:
        self.conn.execute(
            """
            INSERT INTO races(race_ref, title, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(race_ref) DO UPDATE SET title = excluded.title
            """,
            (race_ref, title, now_iso()),
        )

    def upsert_leg(
        self,
        leg_ref: str,
        race_ref: str,
        track_ref: str,
        title: str,
        seq: int,
        required_zones: list[str],
        wind_min_kn: float | None,
        wind_max_kn: float | None,
        visibility_min_m: float | None,
        obs_max_age_s: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO legs(leg_ref, race_ref, track_ref, title, seq,
                             required_zones_json, wind_min_kn, wind_max_kn,
                             visibility_min_m, obs_max_age_s, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(leg_ref) DO UPDATE SET
                race_ref = excluded.race_ref,
                track_ref = excluded.track_ref,
                title = excluded.title,
                seq = excluded.seq,
                required_zones_json = excluded.required_zones_json,
                wind_min_kn = excluded.wind_min_kn,
                wind_max_kn = excluded.wind_max_kn,
                visibility_min_m = excluded.visibility_min_m,
                obs_max_age_s = excluded.obs_max_age_s
            """,
            (
                leg_ref, race_ref, track_ref, title, seq,
                json.dumps(required_zones, ensure_ascii=False),
                wind_min_kn, wind_max_kn, visibility_min_m, obs_max_age_s,
                now_iso(),
            ),
        )

    def upsert_entry(self, entry_ref: str, race_ref: str, boat_ref: str) -> None:
        self.conn.execute(
            """
            INSERT INTO entries(entry_ref, race_ref, boat_ref, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(entry_ref) DO UPDATE SET
                race_ref = excluded.race_ref, boat_ref = excluded.boat_ref
            """,
            (entry_ref, race_ref, boat_ref, now_iso()),
        )

    def get_race(self, race_ref: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM races WHERE race_ref = ?", (race_ref,)
        ).fetchone()

    def get_leg(self, leg_ref: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM legs WHERE leg_ref = ?", (leg_ref,)
        ).fetchone()

    def get_entry(self, entry_ref: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM entries WHERE entry_ref = ?", (entry_ref,)
        ).fetchone()

    def list_entries(self, race_ref: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM entries WHERE race_ref = ? ORDER BY entry_ref",
            (race_ref,),
        ))

    # ---- 事件 -----------------------------------------------------------

    def append_event(
        self,
        event_type: str,
        actor_role: str,
        occurred_at: str,
        race_ref: str,
        leg_ref: str | None = None,
        fleet_ref: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO events(event_type, race_ref, leg_ref, fleet_ref,
                               occurred_at, recorded_at, actor_role, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type, race_ref, leg_ref, fleet_ref,
                occurred_at, now_iso(), actor_role,
                json.dumps(payload or {}, ensure_ascii=False),
            ),
        )
        return int(cur.lastrowid)

    def events_for_leg(
        self, leg_ref: str, types: Iterable[str] | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM events WHERE leg_ref = ?"
        params: list[Any] = [leg_ref]
        if types is not None:
            type_list = list(types)
            if not type_list:
                return []
            sql += f" AND event_type IN ({','.join('?' * len(type_list))})"
            params.extend(type_list)
        sql += " ORDER BY occurred_at, id"
        return list(self.conn.execute(sql, params))

    def events_for_fleet(
        self, fleet_ref: str, types: Iterable[str] | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM events WHERE fleet_ref = ?"
        params: list[Any] = [fleet_ref]
        if types is not None:
            type_list = list(types)
            if not type_list:
                return []
            sql += f" AND event_type IN ({','.join('?' * len(type_list))})"
            params.extend(type_list)
        sql += " ORDER BY occurred_at, id"
        return list(self.conn.execute(sql, params))

    # ---- 发令尝试与赛段阶段 ----------------------------------------------

    def record_attempt(
        self, kind: str, leg_ref: str, fired_at: str,
        permitted: bool, basis: dict[str, Any], reasons: list[dict[str, str]],
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO start_attempts(kind, leg_ref, fired_at, permitted,
                                       basis_json, reasons_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                kind, leg_ref, fired_at, 1 if permitted else 0,
                json.dumps(basis, ensure_ascii=False),
                json.dumps(reasons, ensure_ascii=False),
            ),
        )
        return int(cur.lastrowid)

    def latest_attempt(self, leg_ref: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM start_attempts WHERE leg_ref = ? ORDER BY id DESC LIMIT 1",
            (leg_ref,),
        ).fetchone()

    def get_phase(self, leg_ref: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM leg_phases WHERE leg_ref = ?", (leg_ref,)
        ).fetchone()

    def ensure_phase(self, leg_ref: str) -> sqlite3.Row:
        self.conn.execute(
            "INSERT OR IGNORE INTO leg_phases(leg_ref) VALUES (?)", (leg_ref,)
        )
        row = self.get_phase(leg_ref)
        assert row is not None
        return row

    def open_phase(self, leg_ref: str, started_at: str) -> None:
        self.conn.execute(
            "UPDATE leg_phases SET started_at = ? WHERE leg_ref = ? AND started_at IS NULL",
            (started_at, leg_ref),
        )

    def finalize_phase(self, leg_ref: str, finished_at: str, result_ref: str) -> None:
        self.conn.execute(
            """
            UPDATE leg_phases
            SET finished_at = ?, result_json = ?, finalized = 1
            WHERE leg_ref = ?
            """,
            (finished_at, json.dumps({"result_ref": result_ref}, ensure_ascii=False), leg_ref),
        )
