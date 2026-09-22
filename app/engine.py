"""发令瞬间判定引擎。

核心原则：赛段是否开放，只取决于发令瞬间 T 各前置条件的有效性。
所有事实按 occurred_at 追加保存，评估时只取 occurred_at <= T 的最新一条，
因此迟到受理（recorded_at 晚）不会改变 T 时刻已经作出的判定。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .store import Store
from .timeutil import parse_occurred_at

WIND = "wind"
VISIBILITY = "visibility"


@dataclass
class GateContext:
    leg: Any  # sqlite3.Row
    T: datetime
    T_iso: str


def _row_dt(row: Any) -> datetime:
    return parse_occurred_at(row["occurred_at"])


def _sorted(rows: list[Any]) -> list[Any]:
    """按实际时间排序（SQL 字符串排序无法正确处理不同 UTC 偏移量）。"""
    return sorted(rows, key=lambda r: (_row_dt(r), r["id"]))


def _payload(row: Any) -> dict[str, Any]:
    return json.loads(row["payload_json"])


def _latest_as_of(rows: list[Any], t: datetime) -> Any | None:
    """取发生时间不晚于 t 的最新一条（按实际时间，不依赖存储排序）。"""
    eligible = [row for row in rows if _row_dt(row) <= t]
    if not eligible:
        return None
    return max(eligible, key=lambda row: (_row_dt(row), row["id"]))


def _evidence(row: Any, **extra: Any) -> dict[str, Any]:
    ev = {
        "event_id": row["id"],
        "occurred_at": row["occurred_at"],
        "recorded_at": row["recorded_at"],
    }
    ev.update(_payload(row))
    ev.update(extra)
    return ev


def evaluate(
    store: Store, leg: Any, t_iso: str, *, ignore_suspension: bool = False
) -> dict[str, Any]:
    """评估赛段在 T 时刻全部前置条件。返回 {permitted, reasons, basis}。

    ignore_suspension 用于恢复确认：此时赛段按定义正处于暂停，
    暂停状态本身不算未满足项——恢复动作经其余全部条件确认后清除暂停。
    """
    t = parse_occurred_at(t_iso)
    ctx = GateContext(leg=leg, T=t, T_iso=t_iso)
    reasons: list[dict[str, str]] = []

    course = _course_gate(store, ctx, reasons)
    wind = _wind_gate(store, ctx, reasons)
    visibility = _visibility_gate(store, ctx, reasons)
    rescue = _rescue_gate(store, ctx, reasons)
    fleets = _fleet_gates(store, ctx, reasons)
    suspension = _suspension_gate(store, ctx, [] if ignore_suspension else reasons)
    if ignore_suspension:
        suspension["resume_would_clear"] = suspension["state"] == "suspended"

    basis = {
        "evaluated_at": t_iso,
        "leg_ref": leg["leg_ref"],
        "track_ref": leg["track_ref"],
        "course": course,
        "wind": wind,
        "visibility": visibility,
        "rescue_coverage": rescue,
        "fleets": fleets,
        "suspension": suspension,
    }
    return {"permitted": not reasons, "reasons": reasons, "basis": basis}


# ---- 各前置条件 -----------------------------------------------------------

def _course_gate(store: Store, ctx: GateContext, reasons: list[dict[str, str]]) -> Any:
    rows = store.events_for_leg(ctx.leg["leg_ref"], ["course_revision"])
    row = _latest_as_of(rows, ctx.T)
    if row is None:
        reasons.append({
            "code": "course_missing",
            "message": "发令前尚未发布任何经确认的赛道版本",
        })
        return None
    data = _payload(row)
    ok = bool(data.get("confirmed")) and data.get("track_ref") == ctx.leg["track_ref"]
    if not data.get("confirmed"):
        reasons.append({
            "code": "course_unconfirmed",
            "message": f"赛道版本 {data.get('revision')} 尚未经裁判确认（如浮标移位后未复核）",
        })
    if data.get("track_ref") != ctx.leg["track_ref"]:
        reasons.append({
            "code": "course_track_mismatch",
            "message": "赛道版本对应的江段与本赛段江段不一致，可能使用了旧江段资料",
        })
    return _evidence(row, satisfied=ok)


def _observation_gate(
    store: Store, ctx: GateContext, kind: str,
) -> tuple[Any | None, dict[str, str] | None]:
    rows = [
        r for r in store.events_for_leg(ctx.leg["leg_ref"], ["observation"])
        if _payload(r).get("kind") == kind
    ]
    row = _latest_as_of(rows, ctx.T)
    if row is None:
        return None, {
            "code": f"{kind}_missing",
            "message": f"发令瞬间没有可依据的{('风速' if kind == WIND else '能见度')}观测",
        }
    age_s = (ctx.T - _row_dt(row)).total_seconds()
    data = _payload(row)
    fresh = age_s <= ctx.leg["obs_max_age_s"]
    evidence = _evidence(row, age_s=round(age_s, 1), fresh=fresh)
    failure: dict[str, str] | None = None
    if not fresh:
        failure = {
            "code": f"{kind}_stale",
            "message": (
                f"最新{('风速' if kind == WIND else '能见度')}观测已过时 "
                f"({round(age_s / 60)} 分钟前，限值 "
                f"{ctx.leg['obs_max_age_s'] // 60} 分钟)"
            ),
        }
    return evidence, failure


def _wind_gate(store: Store, ctx: GateContext, reasons: list[dict[str, str]]) -> Any:
    evidence, failure = _observation_gate(store, ctx, WIND)
    if failure:
        reasons.append(failure)
        return evidence
    value = evidence["value"]
    lo, hi = ctx.leg["wind_min_kn"], ctx.leg["wind_max_kn"]
    within = (lo is None or value >= lo) and (hi is None or value <= hi)
    evidence["within_limits"] = within
    if not within:
        reasons.append({
            "code": "wind_out_of_limits",
            "message": f"风速 {value} kn 超出赛段允许范围 [{lo}, {hi}] kn",
        })
    return evidence


def _visibility_gate(store: Store, ctx: GateContext, reasons: list[dict[str, str]]) -> Any:
    evidence, failure = _observation_gate(store, ctx, VISIBILITY)
    if failure:
        reasons.append(failure)
        return evidence
    value = evidence["value"]
    minimum = ctx.leg["visibility_min_m"]
    within = minimum is None or value >= minimum
    evidence["within_limits"] = within
    if not within:
        reasons.append({
            "code": "visibility_below_minimum",
            "message": f"能见度 {value} m 低于赛段下限 {minimum} m",
        })
    return evidence


def _rescue_gate(store: Store, ctx: GateContext, reasons: list[dict[str, str]]) -> list[Any]:
    required_zones: list[str] = json.loads(ctx.leg["required_zones_json"])
    rows = _sorted(store.events_for_leg(ctx.leg["leg_ref"], ["rescue_coverage"]))
    by_zone: dict[str, Any] = {}
    for row in rows:
        data = _payload(row)
        zone = data.get("zone")
        if zone in required_zones and _row_dt(row) <= ctx.T:
            by_zone[zone] = row  # 升序遍历，后者覆盖
    result: list[Any] = []
    for zone in required_zones:
        row = by_zone.get(zone)
        if row is None:
            reasons.append({
                "code": "rescue_zone_uncovered",
                "message": f"救援区 {zone} 在本赛段（江段 {ctx.leg['track_ref']}）无覆盖记录",
            })
            result.append({"zone": zone, "active": False, "satisfied": False})
            continue
        data = _payload(row)
        active = bool(data.get("active"))
        result.append(_evidence(row, satisfied=active))
        if not active:
            reasons.append({
                "code": "rescue_zone_inactive",
                "message": f"救援区 {zone} 最新状态为未就位",
            })
    return result


def _fleet_gates(store: Store, ctx: GateContext, reasons: list[dict[str, str]]) -> list[Any]:
    leg_rows = store.events_for_leg(
        ctx.leg["leg_ref"], ["checkin", "withdrawal"]
    )
    result: list[Any] = []
    for entry in store.list_entries(ctx.leg["race_ref"]):
        fleet_ref = entry["entry_ref"]
        withdrawal = _latest_as_of(
            [r for r in leg_rows if r["fleet_ref"] == fleet_ref
             and r["event_type"] == "withdrawal"],
            ctx.T,
        )
        checkin = _latest_as_of(
            [r for r in leg_rows if r["fleet_ref"] == fleet_ref
             and r["event_type"] == "checkin"],
            ctx.T,
        )
        checks = store.events_for_fleet(fleet_ref, ["boat_check", "training"])
        boat = _latest_as_of(
            [r for r in checks if r["event_type"] == "boat_check"], ctx.T
        )
        training = _latest_as_of(
            [r for r in checks if r["event_type"] == "training"], ctx.T
        )

        item: dict[str, Any] = {
            "fleet_ref": fleet_ref,
            "boat_ref": entry["boat_ref"],
            "withdrawn": withdrawal is not None,
            "withdrawal": _evidence(withdrawal) if withdrawal is not None else None,
        }
        if withdrawal is not None:
            # 退赛船队不再构成发令前置条件，但其历史记录全部保留。
            result.append(item)
            continue

        item["checkin"] = _evidence(checkin) if checkin is not None else None
        item["boat_check"] = (
            _evidence(boat, passed=_payload(boat).get("status") == "passed")
            if boat is not None else None
        )
        item["training"] = (
            _evidence(training, completed=_payload(training).get("status") == "completed")
            if training is not None else None
        )
        result.append(item)

        if checkin is None:
            reasons.append({
                "code": "fleet_not_checked_in",
                "message": f"船队 {fleet_ref} 尚未在本赛段报到",
            })
        if boat is None or _payload(boat).get("status") != "passed":
            reasons.append({
                "code": "boat_check_not_passed",
                "message": f"船队 {fleet_ref} 的船只 {entry['boat_ref']} 检修未通过或无记录",
            })
        if training is None or _payload(training).get("status") != "completed":
            reasons.append({
                "code": "training_not_completed",
                "message": f"船队 {fleet_ref} 未完成规定培训",
            })
    return result


def _suspension_gate(store: Store, ctx: GateContext, reasons: list[dict[str, str]]) -> Any:
    rows = store.events_for_leg(ctx.leg["leg_ref"], ["suspend", "resume"])
    latest = _latest_as_of(rows, ctx.T)
    if latest is None:
        return {"state": "normal", "latest_event": None, "satisfied": True}
    suspended = latest["event_type"] == "suspend"
    if suspended:
        reasons.append({
            "code": "leg_suspended",
            "message": "赛段处于暂停状态；恢复开放必须重新确认全部前置条件",
        })
    return {
        "state": "suspended" if suspended else "resumed",
        "latest_event": _evidence(latest),
        "satisfied": not suspended,
    }


# ---- 公众当前决定 ----------------------------------------------------------

def current_decision(store: Store, leg: Any, now_iso_value: str) -> dict[str, Any]:
    """聚合当前权威决定。

    - 未开放：以当前时刻重算全部前置条件，得到 ready_to_start / not_ready；
    - 进行中：锚定最近一次成功发令或恢复确认时留存的依据快照，
      暂停/恢复事件只改变状态（suspended/open），不拿现在的时刻去重算历史观测；
    - 已签署：返回冻结结果，迟到事实永不改写。
    """
    phase = store.get_phase(leg["leg_ref"])
    progress = _leg_progress(store, leg["leg_ref"])

    if phase is not None and phase["finalized"]:
        frozen = json.loads(phase["result_json"])
        return {
            "leg_ref": leg["leg_ref"],
            "race_ref": leg["race_ref"],
            "track_ref": leg["track_ref"],
            "title": leg["title"],
            "status": "finished",
            "updated_at": phase["finished_at"],
            "started_at": phase["started_at"],
            "signed_result": frozen,
            "progress": progress,
            "frozen": True,
        }

    last_gate = store.conn.execute(
        """
        SELECT * FROM start_attempts
        WHERE leg_ref = ? AND permitted = 1
        ORDER BY id DESC LIMIT 1
        """,
        (leg["leg_ref"],),
    ).fetchone()

    if last_gate is None:
        gate = evaluate(store, leg, now_iso_value)
        return {
            "leg_ref": leg["leg_ref"],
            "race_ref": leg["race_ref"],
            "track_ref": leg["track_ref"],
            "title": leg["title"],
            "status": "ready_to_start" if gate["permitted"] else "not_ready",
            "updated_at": _basis_updated_at(store, leg, gate),
            "computed_at": now_iso_value,
            "started_at": None,
            "ready": gate["permitted"],
            "reasons": gate["reasons"],
            "basis": gate["basis"],
            "progress": progress,
            "frozen": False,
        }

    # 航次进行中：依据是最近一次成功判定（发令或恢复）的不可变快照。
    basis = json.loads(last_gate["basis_json"])
    suspension = _suspension_gate(store, GateContext(leg, parse_occurred_at(now_iso_value),
                                                     now_iso_value), [])
    suspended = suspension["state"] == "suspended"
    return {
        "leg_ref": leg["leg_ref"],
        "race_ref": leg["race_ref"],
        "track_ref": leg["track_ref"],
        "title": leg["title"],
        "status": "suspended" if suspended else "open",
        "updated_at": _timeline_updated_at(last_gate["fired_at"], suspension, progress),
        "computed_at": now_iso_value,
        "started_at": phase["started_at"] if phase is not None else None,
        "ready": not suspended,
        "reasons": ([] if not suspended
                    else [{"code": "leg_suspended",
                           "message": "赛段暂停中；恢复开放必须重新确认全部前置条件"}]),
        "last_gate": {
            "kind": last_gate["kind"],
            "attempt_id": last_gate["id"],
            "decided_at": last_gate["fired_at"],
        },
        "basis": basis,
        "suspension": suspension,
        "progress": progress,
        "frozen": False,
    }


def _timeline_updated_at(gate_fired_at: str, suspension: Any,
                         progress: list[dict[str, Any]]) -> str:
    latest_dt = parse_occurred_at(gate_fired_at)
    latest_iso = gate_fired_at

    def consider(ts: str) -> None:
        nonlocal latest_dt, latest_iso
        dt = parse_occurred_at(ts)
        if dt > latest_dt:
            latest_dt, latest_iso = dt, ts

    event = suspension.get("latest_event")
    if event:
        consider(event["occurred_at"])
    for item in progress:
        consider(item["occurred_at"])
    return latest_iso


def _leg_progress(store: Store, leg_ref: str) -> list[dict[str, Any]]:
    """已保留的航次进展（检查点、退赛）。退赛后此前检查点仍在此可见。"""
    rows = _sorted(store.events_for_leg(leg_ref, ["checkpoint", "withdrawal"]))
    return [
        {
            "event_id": row["id"],
            "type": row["event_type"],
            "fleet_ref": row["fleet_ref"],
            "occurred_at": row["occurred_at"],
            "payload": _payload(row),
        }
        for row in rows
    ]


def _basis_updated_at(store: Store, leg: Any, gate: dict[str, Any]) -> str:
    """未开放赛段：决定所依据事实中的最新实际发生时间。"""
    latest_dt = parse_occurred_at(gate["basis"]["evaluated_at"])
    latest_iso = gate["basis"]["evaluated_at"]

    def consider(ts: str) -> None:
        nonlocal latest_dt, latest_iso
        dt = parse_occurred_at(ts)
        if dt > latest_dt:
            latest_dt, latest_iso = dt, ts

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            ts = obj.get("occurred_at")
            if isinstance(ts, str):
                consider(ts)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(gate["basis"])
    return latest_iso
