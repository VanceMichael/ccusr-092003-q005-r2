"""服务编排层：角色权限、事件受理校验、发令/恢复/签署的业务不变量。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import engine
from .store import Store
from .timeutil import now_iso, parse_occurred_at

JUDGE = "judge"
RESCUE = "rescue"
PUBLIC = "public"
ROLES = (JUDGE, RESCUE, PUBLIC)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}


# ---------------------------------------------------------------------------
# 事件受理规格
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EventSpec:
    event_type: str
    role: str
    required: tuple[str, ...]
    leg_scoped: bool = True   # 是否属于某赛段（冻结后拒绝晚于签署时间的写入）


EVENT_SPECS: dict[str, EventSpec] = {
    # 裁判维护
    "checkin":         EventSpec("checkin", JUDGE, ("fleet_ref",)),
    "boat-check":      EventSpec("boat_check", JUDGE, ("fleet_ref", "status"), leg_scoped=False),
    "training":        EventSpec("training", JUDGE, ("fleet_ref", "status"), leg_scoped=False),
    "course-revision": EventSpec("course_revision", JUDGE, ("revision", "track_ref", "confirmed")),
    "observation":     EventSpec("observation", JUDGE, ("kind", "value", "observation_ref")),
    "suspend":         EventSpec("suspend", JUDGE, ("reason",)),
    "checkpoint":      EventSpec("checkpoint", JUDGE, ("fleet_ref", "checkpoint_ref")),
    "withdrawal":      EventSpec("withdrawal", JUDGE, ("fleet_ref", "reason")),
    # 救援维护
    "rescue-coverage": EventSpec("rescue_coverage", RESCUE, ("zone", "active")),
    "safety-incident": EventSpec("safety_incident", RESCUE,
                                 ("incident_ref", "summary_sha256", "severity"),
                                 leg_scoped=False),
}


class Service:
    def __init__(self, store: Store):
        self.store = store

    # ---- 配置登记（裁判） -------------------------------------------------

    def require_role(self, role: str, allowed: str) -> None:
        if role != allowed:
            raise ApiError(403, "forbidden", f"该操作仅允许 {allowed} 角色执行")

    def put_race(self, role: str, body: dict[str, Any]) -> dict:
        self.require_role(role, JUDGE)
        race_ref = _require_str(body, "race_ref")
        title = _require_str(body, "title")
        self.store.upsert_race(race_ref, title)
        return {"race_ref": race_ref, "title": title}

    def put_leg(self, role: str, body: dict[str, Any]) -> dict:
        self.require_role(role, JUDGE)
        leg_ref = _require_str(body, "leg_ref")
        race_ref = _require_str(body, "race_ref")
        if self.store.get_race(race_ref) is None:
            raise ApiError(404, "race_not_found", f"赛事 {race_ref} 尚未登记")
        track_ref = _require_str(body, "track_ref")
        title = _require_str(body, "title")
        seq = body.get("seq")
        if not isinstance(seq, int):
            raise ApiError(400, "invalid_field", "seq 必须是整数")
        required_zones = body.get("required_zones", [])
        if not isinstance(required_zones, list) or not all(isinstance(z, str) for z in required_zones):
            raise ApiError(400, "invalid_field", "required_zones 必须是字符串数组")
        self.store.upsert_leg(
            leg_ref, race_ref, track_ref, title, seq, required_zones,
            _optional_float(body, "wind_min_kn"),
            _optional_float(body, "wind_max_kn"),
            _optional_float(body, "visibility_min_m"),
            int(body.get("obs_max_age_s", 1800)),
        )
        self.store.ensure_phase(leg_ref)
        return {"leg_ref": leg_ref, "track_ref": track_ref, "required_zones": required_zones}

    def put_entry(self, role: str, body: dict[str, Any]) -> dict:
        self.require_role(role, JUDGE)
        entry_ref = _require_str(body, "entry_ref")
        race_ref = _require_str(body, "race_ref")
        boat_ref = _require_str(body, "boat_ref")
        if self.store.get_race(race_ref) is None:
            raise ApiError(404, "race_not_found", f"赛事 {race_ref} 尚未登记")
        self.store.upsert_entry(entry_ref, race_ref, boat_ref)
        return {"entry_ref": entry_ref, "boat_ref": boat_ref}

    # ---- 事件受理 ---------------------------------------------------------

    def record_event(self, kind: str, role: str, body: dict[str, Any]) -> dict:
        try:
            spec = EVENT_SPECS[kind]
        except KeyError:
            raise ApiError(404, "unknown_event", f"未知事件类型 {kind}")
        if role != spec.role:
            raise ApiError(
                403, "forbidden",
                "救援人员只维护救援覆盖与安全事件" if role == RESCUE
                else "该事件仅允许相应责任角色录入",
            )
        race_ref = _require_str(body, "race_ref")
        if self.store.get_race(race_ref) is None:
            raise ApiError(404, "race_not_found", f"赛事 {race_ref} 尚未登记")
        occurred_at = _require_str(body, "occurred_at")
        occurred_dt = parse_occurred_at(occurred_at)

        leg_ref = body.get("leg_ref")
        leg = None
        if spec.leg_scoped:
            if not isinstance(leg_ref, str) or not leg_ref:
                raise ApiError(400, "missing_field", "该事件必须提供 leg_ref")
            leg = self.store.get_leg(leg_ref)
            if leg is None:
                raise ApiError(404, "leg_not_found", f"赛段 {leg_ref} 尚未登记")
            if leg["race_ref"] != race_ref:
                raise ApiError(400, "race_mismatch", "赛段不属于该赛事")
            self._reject_if_leg_closed(leg, occurred_dt)

        fleet_ref = body.get("fleet_ref")
        if "fleet_ref" in spec.required:
            if not isinstance(fleet_ref, str) or not fleet_ref:
                raise ApiError(400, "missing_field", "缺少 fleet_ref")
            entry = self.store.get_entry(fleet_ref)
            if entry is None or entry["race_ref"] != race_ref:
                raise ApiError(404, "fleet_not_found", f"船队 {fleet_ref} 未报名本赛事")

        for field in spec.required:
            if field == "fleet_ref":
                continue
            if field not in body or body[field] in (None, ""):
                raise ApiError(400, "missing_field", f"缺少字段 {field}")

        if kind == "observation" and body["kind"] not in (engine.WIND, engine.VISIBILITY):
            raise ApiError(400, "invalid_field", "观测 kind 必须是 wind 或 visibility")

        if kind == "suspend":
            phase = self.store.get_phase(leg_ref)
            if phase is None or phase["started_at"] is None:
                raise ApiError(409, "not_started",
                               "赛段尚未开放；未开赛前不能暂停（未满足条件本身即会阻止发令）")
            state = engine.evaluate(
                self.store, leg, occurred_at, ignore_suspension=True
            )["basis"]["suspension"]["state"]
            if state == "suspended":
                raise ApiError(409, "already_suspended", "赛段已处于暂停状态")

        payload = {k: v for k, v in body.items()
                   if k not in ("race_ref", "leg_ref", "fleet_ref", "occurred_at")}
        actor_ref = body.get("actor_ref")
        if isinstance(actor_ref, str) and actor_ref:
            payload["actor_ref"] = actor_ref

        event_id = self.store.append_event(
            spec.event_type, spec.role, occurred_at, race_ref,
            leg_ref=leg_ref, fleet_ref=fleet_ref, payload=payload,
        )
        return {
            "event_id": event_id,
            "event_type": spec.event_type,
            "occurred_at": occurred_at,
            "recorded": True,
            "leg_frozen": bool(leg is not None and self.store.get_phase(leg_ref)["finalized"]),
        }

    def _reject_if_leg_closed(self, leg: Any, occurred_dt: Any) -> None:
        phase = self.store.get_phase(leg["leg_ref"])
        if phase is not None and phase["finalized"]:
            finished = parse_occurred_at(phase["finished_at"])
            if occurred_dt > finished:
                raise ApiError(
                    409, "leg_closed",
                    f"赛段已于 {phase['finished_at']} 签署结束，"
                    "发生时间更晚的事实不得再写入（迟到观测不得改写已结束航次）",
                )

    # ---- 发令 / 恢复 / 签署 -----------------------------------------------

    def start(self, role: str, leg_ref: str, body: dict[str, Any]) -> dict:
        self.require_role(role, JUDGE)
        leg = self._require_leg(leg_ref)
        fired_at = _require_str(body, "occurred_at")
        parse_occurred_at(fired_at)
        phase = self.store.ensure_phase(leg_ref)
        if phase["finalized"]:
            raise ApiError(409, "leg_closed", "赛段已签署结束，不能再次发令")
        if phase["started_at"] is not None:
            suspended = engine.evaluate(self.store, leg, fired_at)["basis"]["suspension"]["state"]
            if suspended == "suspended":
                raise ApiError(409, "use_resume",
                               "赛段处于暂停状态，恢复开放必须调用 /resume 并重新确认全部条件")
            raise ApiError(409, "already_open", "赛段已开放发令")

        return self._gate_and_transition("start", leg, fired_at, action_event="start")

    def resume(self, role: str, leg_ref: str, body: dict[str, Any]) -> dict:
        self.require_role(role, JUDGE)
        leg = self._require_leg(leg_ref)
        fired_at = _require_str(body, "occurred_at")
        parse_occurred_at(fired_at)
        phase = self.store.ensure_phase(leg_ref)
        if phase["finalized"]:
            raise ApiError(409, "leg_closed", "赛段已签署结束")
        if phase["started_at"] is None:
            raise ApiError(409, "not_started", "赛段尚未首次开放，应调用 /start")
        state = engine.evaluate(self.store, leg, fired_at)["basis"]["suspension"]["state"]
        if state != "suspended":
            raise ApiError(409, "not_suspended", "赛段当前不处于暂停状态，无需恢复")

        return self._gate_and_transition("resume", leg, fired_at, action_event="resume")

    def _gate_and_transition(self, kind: str, leg: Any, fired_at: str,
                             action_event: str) -> dict:
        """发令瞬间的唯一权威判定路径：评估 → 留快照 → 满足才推进状态。"""
        verdict = engine.evaluate(
            self.store, leg, fired_at, ignore_suspension=(kind == "resume")
        )
        attempt_id = self.store.record_attempt(
            kind, leg["leg_ref"], fired_at,
            verdict["permitted"], verdict["basis"], verdict["reasons"],
        )
        if not verdict["permitted"]:
            raise ApiError(403, "preconditions_not_met",
                           f"{kind} 瞬间存在未满足的前置条件，赛段不予开放",
                           extra={"attempt_id": attempt_id,
                                  "reasons": verdict["reasons"],
                                  "basis": verdict["basis"]})
        if kind == "start":
            self.store.open_phase(leg["leg_ref"], fired_at)
        self.store.append_event(
            action_event, JUDGE, fired_at, leg["race_ref"],
            leg_ref=leg["leg_ref"], payload={"attempt_id": attempt_id},
        )
        return {
            "attempt_id": attempt_id,
            "permitted": True,
            "leg_ref": leg["leg_ref"],
            "decided_at": fired_at,
            "basis": verdict["basis"],
        }

    def sign_result(self, role: str, leg_ref: str, body: dict[str, Any]) -> dict:
        self.require_role(role, JUDGE)
        leg = self._require_leg(leg_ref)
        occurred_at = _require_str(body, "occurred_at")
        result_ref = _require_str(body, "result_ref")
        occurred_dt = parse_occurred_at(occurred_at)
        phase = self.store.ensure_phase(leg_ref)
        if phase["finalized"]:
            raise ApiError(409, "already_finalized", "比赛结果已签署，不可重复签署")
        if phase["started_at"] is None:
            raise ApiError(409, "not_started", "赛段尚未开放，不能签署结果")
        if occurred_dt < parse_occurred_at(phase["started_at"]):
            raise ApiError(400, "invalid_field", "签署时间不得早于开放发令时间")
        state = engine.evaluate(self.store, leg, occurred_at)["basis"]["suspension"]["state"]
        if state == "suspended":
            raise ApiError(409, "leg_suspended", "赛段仍处于暂停状态，不能签署结果")

        self.store.finalize_phase(leg_ref, occurred_at, result_ref)
        event_id = self.store.append_event(
            "result_signed", JUDGE, occurred_at, leg["race_ref"],
            leg_ref=leg_ref, payload={"result_ref": result_ref},
        )
        return {"event_id": event_id, "leg_ref": leg_ref,
                "finished_at": occurred_at, "result_ref": result_ref, "frozen": True}

    # ---- 查询 -------------------------------------------------------------

    def leg_decision(self, leg_ref: str) -> dict:
        leg = self._require_leg(leg_ref)
        return engine.current_decision(self.store, leg, now_iso())

    def race_decision(self, race_ref: str) -> dict:
        if self.store.get_race(race_ref) is None:
            raise ApiError(404, "race_not_found", f"赛事 {race_ref} 尚未登记")
        rows = self.store.conn.execute(
            "SELECT leg_ref FROM legs WHERE race_ref = ? ORDER BY seq", (race_ref,)
        ).fetchall()
        return {
            "race_ref": race_ref,
            "generated_at": now_iso(),
            "legs": [self.leg_decision(row["leg_ref"]) for row in rows],
        }

    def attempts(self, leg_ref: str) -> dict:
        self._require_leg(leg_ref)
        rows = self.store.conn.execute(
            "SELECT * FROM start_attempts WHERE leg_ref = ? ORDER BY id", (leg_ref,)
        ).fetchall()
        return {"leg_ref": leg_ref, "attempts": [
            {
                "attempt_id": row["id"],
                "kind": row["kind"],
                "decided_at": row["fired_at"],
                "permitted": bool(row["permitted"]),
                "basis": json.loads(row["basis_json"]),
                "reasons": json.loads(row["reasons_json"]),
            }
            for row in rows
        ]}

    def safety_events(self, role: str, race_ref: str) -> dict:
        if role not in (JUDGE, RESCUE):
            raise ApiError(403, "forbidden", "安全事件仅对裁判与救援人员开放")
        rows = self.store.conn.execute(
            """
            SELECT * FROM events
            WHERE race_ref = ? AND event_type IN ('safety_incident', 'rescue_coverage')
            ORDER BY occurred_at, id
            """,
            (race_ref,),
        ).fetchall()
        return {"race_ref": race_ref, "events": [
            {
                "event_id": row["id"],
                "event_type": row["event_type"],
                "leg_ref": row["leg_ref"],
                "fleet_ref": row["fleet_ref"],
                "occurred_at": row["occurred_at"],
                "recorded_at": row["recorded_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]}

    # ---- 辅助 -------------------------------------------------------------

    def _require_leg(self, leg_ref: str) -> Any:
        leg = self.store.get_leg(leg_ref)
        if leg is None:
            raise ApiError(404, "leg_not_found", f"赛段 {leg_ref} 尚未登记")
        return leg


def _require_str(body: dict[str, Any], field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value:
        raise ApiError(400, "missing_field", f"缺少字段 {field}")
    return value


def _optional_float(body: dict[str, Any], field: str) -> float | None:
    value = body.get(field)
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise ApiError(400, "invalid_field", f"{field} 必须是数值")
    return float(value)
