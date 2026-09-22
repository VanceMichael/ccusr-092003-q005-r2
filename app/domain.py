"""领域核心：角色矩阵、事件校验、按业务时间的前置条件求值、发令/签署终局。

关键原则：
- 事件只追加，occurred_at 为实际发生时间（带偏移量），求值只看 occurred_at <= 判定瞬间 的事件；
- 发令瞬间对全部前置条件做一次性快照求值，开放决定冻结，迟到观测永不改写；
- 暂停后的恢复必须携带 reconfirmed=true，且发令时重新评估当下全部条件；
- 退赛船队保留此前检查点，但不进入发令名单、不阻塞赛段。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .store import Conflict, Store


# ---- 角色矩阵：每类事件唯一允许的写入角色 ---------------------------------
EVENT_ROLES: dict[str, str] = {
    "fleet_checkin": "registrar",
    "boat_inspection": "inspector",
    "training_completed": "trainer",
    "withdrawal": "registrar",
    "course_published": "race_office",
    "weather_wind": "weather_observer",
    "weather_visibility": "weather_observer",
    "rescue_coverage": "rescue_coordinator",
    "safety_incident": "rescue_coordinator",
    "leg_suspended": "race_referee",
    "leg_resumed": "race_referee",
    "checkpoint_recorded": "race_referee",
    "start_commanded": "race_referee",
    "result_signed": "race_referee",
}

FLEET_SCOPED = {
    "fleet_checkin",
    "boat_inspection",
    "training_completed",
    "withdrawal",
    "checkpoint_recorded",
}

PRECONDITIONS = [
    "course_current",
    "wind_within_limit",
    "visibility_adequate",
    "rescue_covered",
    "no_active_safety_incident",
    "not_suspended",
    "fleet_ready",
]


class ValidationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---- 时间与信封校验 -------------------------------------------------------

def parse_time(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("invalid_time", f"时间不是合法 ISO 8601：{value!r}") from exc
    if dt.tzinfo is None:
        raise ValidationError("invalid_time", "occurred_at 必须带时区偏移量")
    return dt


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise ValidationError(code, message)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    _require(isinstance(envelope, dict), "bad_request", "请求体必须是事件对象")
    required = ["event_id", "race_ref", "leg_ref", "event_type", "actor_role", "actor_ref", "occurred_at"]
    for key in required:
        _require(isinstance(envelope.get(key), str) and envelope[key].strip(),
                 "missing_field", f"缺少必填字段：{key}")
    event_type = envelope["event_type"]
    _require(event_type in EVENT_ROLES, "unknown_event_type", f"未知事件类型：{event_type}")

    expected_role = EVENT_ROLES[event_type]
    _require(envelope["actor_role"] == expected_role, "role_forbidden",
             f"{event_type} 只能由 {expected_role} 写入，收到角色 {envelope['actor_role']}")

    parse_time(envelope["occurred_at"])

    payload = envelope.get("payload") or {}
    _require(isinstance(payload, dict), "invalid_payload", "payload 必须是对象")
    fleet_ref = envelope.get("fleet_ref") or payload.get("fleet_ref")
    if event_type in FLEET_SCOPED:
        _require(isinstance(fleet_ref, str) and fleet_ref.strip(),
                 "missing_field", f"{event_type} 需要 fleet_ref")
    _validate_payload(event_type, payload)

    return {
        "event_id": envelope["event_id"].strip(),
        "race_ref": envelope["race_ref"].strip(),
        "leg_ref": envelope["leg_ref"].strip(),
        "fleet_ref": fleet_ref if event_type in FLEET_SCOPED else envelope.get("fleet_ref"),
        "event_type": event_type,
        "actor_role": envelope["actor_role"],
        "actor_ref": envelope["actor_ref"].strip(),
        "occurred_at": envelope["occurred_at"],
        "source_ref": envelope.get("source_ref"),
        "payload": payload,
    }


def _validate_payload(event_type: str, p: dict[str, Any]) -> None:
    if event_type == "boat_inspection":
        _require(isinstance(p.get("passed"), bool), "invalid_payload", "passed 必须为布尔值")
    elif event_type == "course_published":
        _require(isinstance(p.get("course_revision"), int) and p["course_revision"] >= 1,
                 "invalid_payload", "course_revision 必须为 >=1 的整数")
        _require(isinstance(p.get("buoy_refs"), list) and all(isinstance(x, str) for x in p["buoy_refs"]),
                 "invalid_payload", "buoy_refs 必须为字符串数组")
        _require(isinstance(p.get("rescue_zones"), list) and all(isinstance(x, str) for x in p["rescue_zones"]),
                 "invalid_payload", "rescue_zones 必须为字符串数组")
    elif event_type == "weather_wind":
        _require(_is_number(p.get("wind_speed_kt")) and p["wind_speed_kt"] >= 0,
                 "invalid_payload", "wind_speed_kt 必须为 >=0 的数值")
        _require(_is_number(p.get("limit_kt")) and p["limit_kt"] > 0,
                 "invalid_payload", "limit_kt 必须为 >0 的数值")
    elif event_type == "weather_visibility":
        _require(_is_number(p.get("visibility_m")) and p["visibility_m"] >= 0,
                 "invalid_payload", "visibility_m 必须为 >=0 的数值")
        _require(_is_number(p.get("minimum_m")) and p["minimum_m"] > 0,
                 "invalid_payload", "minimum_m 必须为 >0 的数值")
    elif event_type == "rescue_coverage":
        _require(isinstance(p.get("rescue_zone"), str) and p["rescue_zone"].strip(),
                 "missing_field", "rescue_coverage 需要 rescue_zone")
        _require(isinstance(p.get("course_revision"), int) and p["course_revision"] >= 1,
                 "invalid_payload", "course_revision 必须为 >=1 的整数")
        _require(isinstance(p.get("covered"), bool), "invalid_payload", "covered 必须为布尔值")
    elif event_type == "safety_incident":
        _require(isinstance(p.get("rescue_zone"), str) and p["rescue_zone"].strip(),
                 "missing_field", "safety_incident 需要 rescue_zone")
        _require(p.get("severity") in {"low", "moderate", "high"},
                 "invalid_payload", "severity 必须为 low/moderate/high")
        _require(isinstance(p.get("active"), bool), "invalid_payload", "active 必须为布尔值")
    elif event_type == "leg_suspended":
        _require(isinstance(p.get("reason"), str) and p["reason"].strip(),
                 "missing_field", "leg_suspended 需要 reason")
    elif event_type == "leg_resumed":
        _require(p.get("reconfirmed") is True,
                 "reconfirmation_required",
                 "暂停后的恢复必须重新确认：reconfirmed 必须为 true")
    elif event_type == "checkpoint_recorded":
        _require(isinstance(p.get("checkpoint_ref"), str) and p["checkpoint_ref"].strip(),
                 "missing_field", "checkpoint_recorded 需要 checkpoint_ref")
        if "rank_hint" in p:
            _require(isinstance(p["rank_hint"], int), "invalid_payload", "rank_hint 必须为整数")
    elif event_type == "result_signed":
        _require(isinstance(p.get("result_ref"), str) and p["result_ref"].strip(),
                 "missing_field", "result_signed 需要 result_ref")


# ---- 时点求值 -------------------------------------------------------------

@dataclass
class FleetState:
    checkin_at: str | None = None
    inspection: dict[str, Any] | None = None
    trained_at: str | None = None
    withdrawn_at: str | None = None
    checkpoints: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.checkpoints is None:
            self.checkpoints = []


def _eligible_events(events: list[dict[str, Any]], at: datetime) -> list[dict[str, Any]]:
    out = []
    for e in events:
        if parse_time(e["occurred_at"]) <= at:
            out.append(e)
    out.sort(key=lambda e: (parse_time(e["occurred_at"]), e["seq"]))
    return out


def evaluate(events: list[dict[str, Any]], at: datetime) -> dict[str, Any]:
    """在判定瞬间 at 对全部前置条件求值。只使用 occurred_at <= at 的事件。"""
    eligible = _eligible_events(events, at)

    course: dict[str, Any] | None = None
    wind: dict[str, Any] | None = None
    visibility: dict[str, Any] | None = None
    coverage: dict[tuple[str, int], dict[str, Any]] = {}
    incidents: dict[str, dict[str, Any]] = {}
    suspension: dict[str, Any] | None = None  # 最新的暂停/恢复事件
    ever_suspended = False
    fleets: dict[str, FleetState] = {}

    for e in eligible:
        t = e["event_type"]
        p = e["payload"]
        if t == "course_published":
            course = e
        elif t == "weather_wind":
            wind = e
        elif t == "weather_visibility":
            visibility = e
        elif t == "rescue_coverage":
            coverage[(p["rescue_zone"], p["course_revision"])] = e
        elif t == "safety_incident":
            incidents[p["rescue_zone"]] = e
        elif t in ("leg_suspended", "leg_resumed"):
            suspension = e
            if t == "leg_suspended":
                ever_suspended = True
        elif t == "fleet_checkin":
            fleets.setdefault(e["fleet_ref"], FleetState()).checkin_at = e["occurred_at"]
        elif t == "boat_inspection":
            fleets.setdefault(e["fleet_ref"], FleetState()).inspection = e
        elif t == "training_completed":
            fleets.setdefault(e["fleet_ref"], FleetState()).trained_at = e["occurred_at"]
        elif t == "withdrawal":
            fleets.setdefault(e["fleet_ref"], FleetState()).withdrawn_at = e["occurred_at"]
        elif t == "checkpoint_recorded":
            fs = fleets.setdefault(e["fleet_ref"], FleetState())
            fs.checkpoints.append({
                "checkpoint_ref": p["checkpoint_ref"],
                "recorded_at": e["occurred_at"],
                "rank_hint": p.get("rank_hint"),
            })

    reasons: list[dict[str, str]] = []
    checks: dict[str, dict[str, Any]] = {}

    # 1. 赛道版本（浮标配布）
    if course is None:
        reasons.append({"code": "course_current", "detail": "发令瞬间尚无已发布赛道版本"})
        checks["course_current"] = {"satisfied": False}
    else:
        checks["course_current"] = {
            "satisfied": True,
            "course_revision": course["payload"]["course_revision"],
            "buoy_refs": course["payload"]["buoy_refs"],
            "published_at": course["occurred_at"],
            "evidence_event": course["event_id"],
        }

    # 2. 风速
    if wind is None:
        reasons.append({"code": "wind_within_limit", "detail": "发令瞬间无风速观测"})
        checks["wind_within_limit"] = {"satisfied": False}
    else:
        ok = wind["payload"]["wind_speed_kt"] <= wind["payload"]["limit_kt"]
        if not ok:
            reasons.append({"code": "wind_within_limit",
                            "detail": f"风速 {wind['payload']['wind_speed_kt']}kt 超过限值 {wind['payload']['limit_kt']}kt"})
        checks["wind_within_limit"] = {
            "satisfied": ok,
            "wind_speed_kt": wind["payload"]["wind_speed_kt"],
            "limit_kt": wind["payload"]["limit_kt"],
            "observed_at": wind["occurred_at"],
            "evidence_event": wind["event_id"],
        }

    # 3. 能见度
    if visibility is None:
        reasons.append({"code": "visibility_adequate", "detail": "发令瞬间无能见度观测"})
        checks["visibility_adequate"] = {"satisfied": False}
    else:
        ok = visibility["payload"]["visibility_m"] >= visibility["payload"]["minimum_m"]
        if not ok:
            reasons.append({"code": "visibility_adequate",
                            "detail": f"能见度 {visibility['payload']['visibility_m']}m 低于最低 {visibility['payload']['minimum_m']}m"})
        checks["visibility_adequate"] = {
            "satisfied": ok,
            "visibility_m": visibility["payload"]["visibility_m"],
            "minimum_m": visibility["payload"]["minimum_m"],
            "observed_at": visibility["occurred_at"],
            "evidence_event": visibility["event_id"],
        }

    # 4. 救援覆盖（必须逐江段匹配当前赛道版本；旧江段信息不适用）
    zones = course["payload"]["rescue_zones"] if course else []
    revision = course["payload"]["course_revision"] if course else None
    zone_basis: list[dict[str, Any]] = []
    rescue_ok = course is not None
    for zone in zones:
        ev = coverage.get((zone, revision))
        covered = bool(ev and ev["payload"]["covered"])
        rescue_ok = rescue_ok and covered
        item = {"rescue_zone": zone, "course_revision": revision, "covered": covered}
        if ev:
            item.update({"declared_at": ev["occurred_at"], "evidence_event": ev["event_id"]})
        else:
            item["detail"] = "当前赛道版本缺少该江段救援覆盖声明（可能仍为旧江段信息）"
        zone_basis.append(item)
    if not rescue_ok:
        reasons.append({"code": "rescue_covered",
                        "detail": "当前赛道版本存在未覆盖江段或无发布赛道"})
    checks["rescue_covered"] = {
        "satisfied": rescue_ok,
        "current_course_revision": revision,
        "zones": zone_basis,
    }

    # 5. 活跃安全事件（仅统计当前赛道江段；无赛道时统计全部江段）
    active_zones = []
    for zone, ev in incidents.items():
        if ev["payload"]["active"] and (course is None or zone in zones):
            active_zones.append({"rescue_zone": zone,
                                 "severity": ev["payload"]["severity"],
                                 "reported_at": ev["occurred_at"]})
    if active_zones:
        reasons.append({"code": "no_active_safety_incident",
                        "detail": f"存在 {len(active_zones)} 起活跃安全事件"})
    checks["no_active_safety_incident"] = {"satisfied": not active_zones, "active": active_zones}

    # 6. 暂停状态
    suspended = suspension is not None and suspension["event_type"] == "leg_suspended"
    if suspended:
        reasons.append({"code": "not_suspended",
                        "detail": f"赛段处于暂停状态：{suspension['payload'].get('reason', '')}"})
    suspension_basis: dict[str, Any] = {"satisfied": not suspended, "suspended": suspended}
    if suspension is not None:
        suspension_basis.update({
            "last_control": suspension["event_type"],
            "at": suspension["occurred_at"],
            "reconfirmed": suspension["payload"].get("reconfirmed"),
            "evidence_event": suspension["event_id"],
        })
    checks["not_suspended"] = suspension_basis

    # 7. 船队就绪（已报到且未退赛者必须检修合格且完成培训）
    start_list: list[dict[str, Any]] = []
    withdrawn: list[dict[str, Any]] = []
    fleet_problems: list[str] = []
    for fleet_ref, fs in fleets.items():
        if fs.withdrawn_at:
            withdrawn.append({"fleet_ref": fleet_ref, "withdrawn_at": fs.withdrawn_at,
                              "checkpoints_preserved": len(fs.checkpoints)})
            continue
        if fs.checkin_at is None:
            continue  # 未报到船队不进入发令名单
        problems = []
        if fs.inspection is None or not fs.inspection["payload"].get("passed"):
            problems.append("船只检修未合格")
        if fs.trained_at is None:
            problems.append("安全培训未完成")
        if problems:
            fleet_problems.append(f"{fleet_ref}: {', '.join(problems)}")
        else:
            start_list.append({
                "fleet_ref": fleet_ref,
                "checked_in_at": fs.checkin_at,
                "inspection_passed_at": fs.inspection["occurred_at"],
                "trained_at": fs.trained_at,
            })
    if not start_list:
        fleet_problems.append("没有可发令的已报到船队")
    fleet_ok = not fleet_problems
    if not fleet_ok:
        reasons.append({"code": "fleet_ready", "detail": "；".join(fleet_problems)})
    checks["fleet_ready"] = {
        "satisfied": fleet_ok,
        "start_list": sorted(start_list, key=lambda x: x["fleet_ref"]),
        "withdrawn": sorted(withdrawn, key=lambda x: x["fleet_ref"]),
    }

    satisfied = all(c["satisfied"] for c in checks.values())
    return {
        "open": satisfied,
        "reasons": reasons,
        "checks": checks,
        "evaluated_at": at.isoformat(),
        "events_considered": len(eligible),
        "ever_suspended": ever_suspended,
    }


# ---- 事件写入与终局副作用 -------------------------------------------------

def _entry_hash(prev_hash: str, event: dict[str, Any]) -> str:
    canonical = json.dumps(
        {k: event[k] for k in
         ("event_id", "race_ref", "leg_ref", "fleet_ref", "event_type",
          "actor_role", "actor_ref", "occurred_at", "source_ref", "payload")},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


def _result_summary(events: list[dict[str, Any]], decision: dict[str, Any],
                    signed_at: str) -> dict[str, Any]:
    """签署快照：发令名单取自冻结决定；检查点证据取至签署时刻。

    航次中的检查点发生在发令之后、签署之前；退赛船队此前检查点仍保留。
    """
    started = {x["fleet_ref"] for x in decision["basis"]["checks"]["fleet_ready"]["start_list"]}
    cutoff = parse_time(signed_at)

    per_fleet: dict[str, dict[str, Any]] = {}
    for e in _eligible_events(events, cutoff):
        if e["event_type"] == "checkpoint_recorded":
            per_fleet.setdefault(e["fleet_ref"], {"checkpoints": [], "withdrawn_at": None})
        elif e["event_type"] == "withdrawal":
            per_fleet.setdefault(e["fleet_ref"], {"checkpoints": [], "withdrawn_at": None})

    for e in _eligible_events(events, cutoff):
        if e["event_type"] == "withdrawal":
            per_fleet[e["fleet_ref"]]["withdrawn_at"] = e["occurred_at"]
        elif e["event_type"] == "checkpoint_recorded":
            info = per_fleet[e["fleet_ref"]]
            # 退赛之后的打卡不成立，只保留退赛之前的检查点
            if info["withdrawn_at"] and parse_time(info["withdrawn_at"]) <= parse_time(e["occurred_at"]):
                continue
            info["checkpoints"].append({
                "checkpoint_ref": e["payload"]["checkpoint_ref"],
                "recorded_at": e["occurred_at"],
                "rank_hint": e["payload"].get("rank_hint"),
            })

    standings = []
    for fleet_ref, info in per_fleet.items():
        cps = sorted(info["checkpoints"], key=lambda c: (c["rank_hint"] is None, c["rank_hint"] or 0))
        standings.append({
            "fleet_ref": fleet_ref,
            "started": fleet_ref in started,
            "withdrawn_at": info.get("withdrawn_at"),
            "checkpoints": info["checkpoints"],
            "last_rank_hint": cps[-1]["rank_hint"] if cps else None,
        })
    standings.sort(key=lambda s: (s["last_rank_hint"] is None, s["last_rank_hint"] or 0, s["fleet_ref"]))
    return {
        "commanded_at": decision["commanded_at"],
        "signed_at": signed_at,
        "start_list": sorted(started),
        "standings": standings,
    }


def ingest(store: Store, raw: dict[str, Any], received_at: str | None = None) -> dict[str, Any]:
    """校验并追加事件，处理发令/签署的终局冻结。"""
    event = validate_envelope(raw)
    event["received_at"] = received_at or now_iso()

    # 终局赛段：事件仍按只追加原则留痕，但不再产生权威副作用
    signed = store.get_result(event["leg_ref"])
    decision = store.get_decision(event["leg_ref"])

    event_type = event["event_type"]
    if event_type == "start_commanded":
        if signed:
            raise Conflict("赛段结果已签署，发令决定不可变更")
        if decision is not None:
            raise Conflict(f"赛段已于 {decision['commanded_at']} 开放发令，决定已冻结")
    if event_type == "result_signed":
        if signed:
            raise Conflict("赛段结果已签署，不可重复签署")
        if decision is None or not decision["open"]:
            raise ValidationError("leg_not_open", "赛段尚未经发令开放，不能签署结果")

    seq = store.append_event_with_hash(event, _entry_hash)
    response: dict[str, Any] = {"status": "recorded", "seq": seq, "event_id": event["event_id"]}

    if signed is not None:
        response["status"] = "recorded_not_authoritative"
        response["note"] = "赛段已结束并签署，迟到记录留痕但不改写结果"
        return response

    if event_type == "start_commanded":
        verdict = evaluate(store.list_events(event["leg_ref"]), parse_time(event["occurred_at"]))
        response["decision"] = {"open": verdict["open"], "reasons": verdict["reasons"]}
        if verdict["open"]:
            frozen = {
                "leg_ref": event["leg_ref"],
                "race_ref": event["race_ref"],
                "open": True,
                "decided_seq": seq,
                "commanded_at": event["occurred_at"],
                "reasons": [],
                "basis": verdict,
            }
            store.save_decision(frozen)
            response["status"] = "open_frozen"
        else:
            response["status"] = "denied"
            response["note"] = "发令瞬间前置条件未全部有效，赛段不开放，未冻结决定，可在条件满足后重新发令"

    elif event_type == "result_signed":
        fresh_decision = store.get_decision(event["leg_ref"])
        summary = _result_summary(store.list_events(event["leg_ref"]), fresh_decision,
                                  event["occurred_at"])
        result = {
            "leg_ref": event["leg_ref"],
            "race_ref": event["race_ref"],
            "event_seq": seq,
            "signed_at": event["occurred_at"],
            "result_ref": event["payload"]["result_ref"],
            "summary": summary,
        }
        store.save_result(result)
        response["status"] = "result_signed_frozen"
        response["result"] = {"result_ref": result["result_ref"], "signed_at": result["signed_at"]}

    return response


def current_decision(store: Store, leg_ref: str, at: datetime | None = None) -> dict[str, Any]:
    """公众/直播端读取的唯一权威当前决定：决定 + 依据版本 + 更新时间。"""
    events = store.list_events(leg_ref)
    signed = store.get_result(leg_ref)
    frozen = store.get_decision(leg_ref)

    if signed is not None:
        return {
            "leg_ref": leg_ref,
            "race_ref": signed["race_ref"],
            "state": "result_signed",
            "open": False,
            "updated_at": signed["signed_at"],
            "result_ref": signed["result_ref"],
            "summary": signed["summary"],
        }

    if frozen is not None:
        return {
            "leg_ref": leg_ref,
            "race_ref": frozen["race_ref"],
            "state": "open",
            "open": True,
            "updated_at": frozen["commanded_at"],
            "decision": {
                "commanded_at": frozen["commanded_at"],
                "reasons": frozen["reasons"],
                "basis": frozen["basis"],
                "frozen": True,
            },
        }

    at = at or datetime.now(timezone.utc)
    verdict = evaluate(events, at)
    latest_received = max((e["received_at"] for e in events), default=None)
    state = "suspended" if verdict["checks"]["not_suspended"]["suspended"] else "not_open"
    return {
        "leg_ref": leg_ref,
        "race_ref": events[0]["race_ref"] if events else None,
        "state": state,
        "open": False,
        "updated_at": latest_received,
        "decision": {
            "evaluated_at": verdict["evaluated_at"],
            "reasons": verdict["reasons"],
            "basis": verdict,
            "frozen": False,
        },
    }
