"""安全判定服务测试：以赛事故事为主线覆盖全部权威规则。"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app import domain
from app.domain import ValidationError
from app.store import Conflict, Store


def event(event_id: str, event_type: str, role: str, occurred_at: str,
          payload: dict | None = None, *, leg: str = "LEG-BUOY-01",
          race: str = "RACE-2026", fleet: str | None = None,
          actor: str | None = None, source_ref: str | None = None) -> dict:
    return {
        "event_id": event_id,
        "race_ref": race,
        "leg_ref": leg,
        "fleet_ref": fleet,
        "event_type": event_type,
        "actor_role": role,
        "actor_ref": actor or f"{role.upper()}-1",
        "occurred_at": occurred_at,
        "source_ref": source_ref,
        "payload": payload or {},
    }


class StoreFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "test.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def put(self, ev: dict) -> dict:
        return domain.ingest(self.store, ev)

    def decision(self, leg: str = "LEG-BUOY-01") -> dict:
        return domain.current_decision(self.store, leg)


def ready_leg(t: StoreFixture, *, revision: int = 1, zones=("ZONE-1",),
              wind: float = 8.0, vis: float = 3000.0,
              fleets=("TEAM-A",), leg: str = "LEG-BUOY-01",
              base: str = "2026-09-22T07:00:00+08:00") -> None:
    """布置一个在 base 时刻全部前置条件均有效的赛段。"""
    seq = 0
    def nid() -> str:
        nonlocal seq
        seq += 1
        return f"EV{seq:03d}"

    t.put(event(nid(), "course_published", "race_office", base,
                {"course_revision": revision, "buoy_refs": [f"BUOY-{z}" for z in zones],
                 "rescue_zones": list(zones)}, leg=leg))
    t.put(event(nid(), "weather_wind", "weather_observer", base,
                {"wind_speed_kt": wind, "limit_kt": 15}, leg=leg))
    t.put(event(nid(), "weather_visibility", "weather_observer", base,
                {"visibility_m": vis, "minimum_m": 1000}, leg=leg))
    for zone in zones:
        t.put(event(nid(), "rescue_coverage", "rescue_coordinator", base,
                    {"rescue_zone": zone, "course_revision": revision, "covered": True},
                    leg=leg))
    for fleet in fleets:
        t.put(event(nid(), "fleet_checkin", "registrar", base, {"fleet_ref": fleet},
                    fleet=fleet, leg=leg))
        t.put(event(nid(), "boat_inspection", "inspector", base, {"passed": True},
                    fleet=fleet, leg=leg))
        t.put(event(nid(), "training_completed", "trainer", base, {"fleet_ref": fleet},
                    fleet=fleet, leg=leg))


class HappyPathTest(StoreFixture):
    def test_all_preconditions_valid_at_command_moment_opens(self) -> None:
        ready_leg(self)
        result = self.put(event(
            "CMD-1", "start_commanded", "race_referee", "2026-09-22T09:00:00+08:00", {}))
        self.assertEqual(result["status"], "open_frozen")
        self.assertTrue(result["decision"]["open"])

        current = self.decision()
        self.assertEqual(current["state"], "open")
        self.assertTrue(current["open"])
        self.assertEqual(current["updated_at"], "2026-09-22T09:00:00+08:00")
        basis = current["decision"]["basis"]
        self.assertTrue(basis["open"])
        self.assertTrue(basis["checks"]["fleet_ready"]["satisfied"])
        self.assertEqual(
            basis["checks"]["fleet_ready"]["start_list"][0]["fleet_ref"], "TEAM-A")

    def test_missing_evidence_fails_while_silence_is_not_suspension(self) -> None:
        # 无事件时：缺赛道/观测/覆盖/船队必须拒绝；但「未暂停」「无活跃事件」
        # 在没有相反证据时视为满足（沉默不构成暂停或事故）。
        verdict = domain.evaluate([], datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc))
        self.assertFalse(verdict["open"])
        codes = {r["code"] for r in verdict["reasons"]}
        self.assertEqual(
            codes,
            {"course_current", "wind_within_limit", "visibility_adequate",
             "rescue_covered", "fleet_ready"})
        self.assertTrue(verdict["checks"]["not_suspended"]["satisfied"])
        self.assertTrue(verdict["checks"]["no_active_safety_incident"]["satisfied"])

    def test_wind_over_limit_denies(self) -> None:
        ready_leg(self, wind=18.0)
        result = self.put(event(
            "CMD-2", "start_commanded", "race_referee", "2026-09-22T09:00:00+08:00", {}))
        self.assertEqual(result["status"], "denied")
        self.assertFalse(result["decision"]["open"])
        self.assertIn("wind_within_limit", {r["code"] for r in result["decision"]["reasons"]})
        # 拒绝发令不冻结，赛段仍可被查询为未开放
        self.assertEqual(self.decision()["state"], "not_open")

    def test_low_visibility_denies(self) -> None:
        ready_leg(self, vis=500.0)
        result = self.put(event(
            "CMD-3", "start_commanded", "race_referee", "2026-09-22T09:00:00+08:00", {}))
        self.assertEqual(result["status"], "denied")
        self.assertIn("visibility_adequate",
                      {r["code"] for r in result["decision"]["reasons"]})


class BuoyShiftSuspensionTest(StoreFixture):
    """松花江绕标赛核心事故：浮标移位，裁判台暂停后直播端不得继续显示可出发。"""

    def test_suspension_blocks_live_start_and_resume_requires_reconfirmation(self) -> None:
        ready_leg(self)

        # 08:30 裁判台发现浮标移位，暂停赛段
        self.put(event("SUS-1", "leg_suspended", "race_referee",
                       "2026-09-22T08:30:00+08:00", {"reason": "浮标 BUOY-ZONE-1 移位"}))
        live = self.decision()
        self.assertEqual(live["state"], "suspended")
        self.assertFalse(live["open"], "暂停期间直播端不得显示可出发")

        # 暂停期间尝试发令 → 拒绝
        denied = self.put(event("CMD-X", "start_commanded", "race_referee",
                                "2026-09-22T08:45:00+08:00", {}))
        self.assertEqual(denied["status"], "denied")
        self.assertIn("not_suspended", {r["code"] for r in denied["decision"]["reasons"]})

        # 恢复时未重新确认 → 服务端拒绝写入
        with self.assertRaises(ValidationError) as ctx:
            self.put(event("RES-BAD", "leg_resumed", "race_referee",
                           "2026-09-22T08:50:00+08:00", {"reconfirmed": False}))
        self.assertEqual(ctx.exception.code, "reconfirmation_required")

        # 08:55 裁判重新确认全部前置条件后恢复
        self.put(event("RES-1", "leg_resumed", "race_referee",
                       "2026-09-22T08:55:00+08:00", {"reconfirmed": True}))
        self.assertEqual(self.decision()["state"], "not_open")

        # 09:00 发令瞬间条件仍全部有效 → 开放，且依据记录了暂停/恢复链
        opened = self.put(event("CMD-OK", "start_commanded", "race_referee",
                                "2026-09-22T09:00:00+08:00", {}))
        self.assertEqual(opened["status"], "open_frozen")
        basis = self.decision()["decision"]["basis"]
        self.assertEqual(basis["checks"]["not_suspended"]["last_control"], "leg_resumed")
        self.assertTrue(basis["ever_suspended"])

    def test_reconfirmation_evaluates_current_conditions_not_old_ones(self) -> None:
        ready_leg(self)
        self.put(event("SUS-2", "leg_suspended", "race_referee",
                       "2026-09-22T08:30:00+08:00", {"reason": "天气转差观察"}))
        # 暂停期间风速超限（08:40 观测）
        self.put(event("WX-GALE", "weather_wind", "weather_observer",
                       "2026-09-22T08:40:00+08:00",
                       {"wind_speed_kt": 22, "limit_kt": 15}))
        self.put(event("RES-2", "leg_resumed", "race_referee",
                       "2026-09-22T08:55:00+08:00", {"reconfirmed": True}))
        # 恢复不等于可发令：发令瞬间仍按当下条件求值
        denied = self.put(event("CMD-GALE", "start_commanded", "race_referee",
                                "2026-09-22T09:00:00+08:00", {}))
        self.assertEqual(denied["status"], "denied")
        self.assertIn("wind_within_limit",
                      {r["code"] for r in denied["decision"]["reasons"]})


class CourseRevisionAndRescueTest(StoreFixture):
    """长航赛拿到旧江段信息：救援覆盖必须绑定赛道版本。"""

    def test_rescue_coverage_for_old_revision_does_not_cover_new_course(self) -> None:
        ready_leg(self, revision=1)
        # 次日长航赛发布新版本赛道（江段集合变化），救援队仍只有 rev1 的覆盖声明
        self.put(event("COURSE-2", "course_published", "race_office",
                       "2026-09-23T06:30:00+08:00",
                       {"course_revision": 2, "buoy_refs": ["BUOY-ZONE-1", "BUOY-ZONE-2"],
                        "rescue_zones": ["ZONE-1", "ZONE-2"]},
                       leg="LEG-LONG-02"))
        self.put(event("WX-2", "weather_wind", "weather_observer",
                       "2026-09-23T06:30:00+08:00", {"wind_speed_kt": 6, "limit_kt": 15},
                       leg="LEG-LONG-02"))
        self.put(event("VIS-2", "weather_visibility", "weather_observer",
                       "2026-09-23T06:30:00+08:00", {"visibility_m": 4000, "minimum_m": 1000},
                       leg="LEG-LONG-02"))
        # 救援队按旧江段 rev1 申报（错误信息源）
        self.put(event("COV-OLD", "rescue_coverage", "rescue_coordinator",
                       "2026-09-23T06:31:00+08:00",
                       {"rescue_zone": "ZONE-1", "course_revision": 1, "covered": True},
                       leg="LEG-LONG-02"))
        for fleet in ("TEAM-A",):
            self.put(event(f"CI-{fleet}", "fleet_checkin", "registrar",
                           "2026-09-23T06:30:00+08:00", {"fleet_ref": fleet},
                           fleet=fleet, leg="LEG-LONG-02"))
            self.put(event(f"BI-{fleet}", "boat_inspection", "inspector",
                           "2026-09-23T06:30:00+08:00", {"passed": True},
                           fleet=fleet, leg="LEG-LONG-02"))
            self.put(event(f"TR-{fleet}", "training_completed", "trainer",
                           "2026-09-23T06:30:00+08:00", {"fleet_ref": fleet},
                           fleet=fleet, leg="LEG-LONG-02"))

        denied = self.put(event(
            "CMD-LONG-1", "start_commanded", "race_referee",
            "2026-09-23T07:00:00+08:00", {}, leg="LEG-LONG-02"))
        self.assertEqual(denied["status"], "denied")
        codes = {r["code"] for r in denied["decision"]["reasons"]}
        self.assertIn("rescue_covered", codes)
        basis = denied["decision"]["reasons"]
        rescue_reason = next(r for r in basis if r["code"] == "rescue_covered")
        self.assertIn("未覆盖江段", rescue_reason["detail"])

    def test_new_revision_coverage_then_opens(self) -> None:
        self.test_rescue_coverage_for_old_revision_does_not_cover_new_course()
        # 救援队补报 rev2 两个江段覆盖
        for zone in ("ZONE-1", "ZONE-2"):
            self.put(event(f"COV-2-{zone}", "rescue_coverage", "rescue_coordinator",
                           "2026-09-23T06:45:00+08:00",
                           {"rescue_zone": zone, "course_revision": 2, "covered": True},
                           leg="LEG-LONG-02"))
        opened = self.put(event("CMD-LONG-2", "start_commanded", "race_referee",
                                "2026-09-23T07:05:00+08:00", {}, leg="LEG-LONG-02"))
        self.assertEqual(opened["status"], "open_frozen")


class LateObservationTest(StoreFixture):
    """迟到观测不得改写已结束航次。"""

    def test_late_observation_after_open_does_not_change_frozen_decision(self) -> None:
        ready_leg(self)
        self.put(event("CMD-F", "start_commanded", "race_referee",
                       "2026-09-22T09:00:00+08:00", {}))
        # 09:10 收到一份标注发生于 08:50 的狂风观测（迟到归位）
        late = self.put(event("WX-LATE", "weather_wind", "weather_observer",
                              "2026-09-22T08:50:00+08:00",
                              {"wind_speed_kt": 30, "limit_kt": 15}))
        self.assertEqual(late["status"], "recorded")
        current = self.decision()
        self.assertTrue(current["open"], "冻结的开放决定不得被迟到观测改写")
        # 依据快照仍是发令瞬间的风速 8kt
        self.assertEqual(
            current["decision"]["basis"]["checks"]["wind_within_limit"]["wind_speed_kt"], 8.0)
        # 重复发令被拒绝
        with self.assertRaises(Conflict):
            self.put(event("CMD-F2", "start_commanded", "race_referee",
                           "2026-09-22T09:20:00+08:00", {}))

    def test_events_after_signing_are_logged_but_not_authoritative(self) -> None:
        ready_leg(self)
        self.put(event("CMD-S", "start_commanded", "race_referee",
                       "2026-09-22T09:00:00+08:00", {}))
        self.put(event("CP-1", "checkpoint_recorded", "race_referee",
                       "2026-09-22T10:00:00+08:00",
                       {"checkpoint_ref": "MK-1", "rank_hint": 1}, fleet="TEAM-A"))
        signed = self.put(event("SIGN-1", "result_signed", "race_referee",
                                "2026-09-22T11:00:00+08:00",
                                {"result_ref": "RESULT-2026-001"}))
        self.assertEqual(signed["status"], "result_signed_frozen")

        after = self.put(event("WX-AFTER", "weather_wind", "weather_observer",
                               "2026-09-22T11:30:00+08:00",
                               {"wind_speed_kt": 40, "limit_kt": 15}))
        self.assertEqual(after["status"], "recorded_not_authoritative")
        current = self.decision()
        self.assertEqual(current["state"], "result_signed")
        self.assertFalse(current["open"])
        self.assertEqual(current["result_ref"], "RESULT-2026-001")

        with self.assertRaises(Conflict):
            self.put(event("SIGN-2", "result_signed", "race_referee",
                           "2026-09-22T12:00:00+08:00", {"result_ref": "RESULT-OTHER"}))


class WithdrawalTest(StoreFixture):
    def test_withdrawn_fleet_keeps_prior_checkpoints_but_leaves_start_list(self) -> None:
        ready_leg(self, fleets=("TEAM-A", "TEAM-B"))
        self.put(event("CMD-W", "start_commanded", "race_referee",
                       "2026-09-22T09:00:00+08:00", {}))
        # TEAM-B 完成两个检查点后退赛
        self.put(event("CP-B1", "checkpoint_recorded", "race_referee",
                       "2026-09-22T09:30:00+08:00",
                       {"checkpoint_ref": "MK-1", "rank_hint": 2}, fleet="TEAM-B"))
        self.put(event("CP-B2", "checkpoint_recorded", "race_referee",
                       "2026-09-22T10:00:00+08:00",
                       {"checkpoint_ref": "MK-2", "rank_hint": 2}, fleet="TEAM-B"))
        self.put(event("WD-B", "withdrawal", "registrar",
                       "2026-09-22T10:30:00+08:00", {"fleet_ref": "TEAM-B"}, fleet="TEAM-B"))
        self.put(event("CP-A1", "checkpoint_recorded", "race_referee",
                       "2026-09-22T10:35:00+08:00",
                       {"checkpoint_ref": "MK-3", "rank_hint": 1}, fleet="TEAM-A"))

        signed = self.put(event("SIGN-W", "result_signed", "race_referee",
                                "2026-09-22T11:00:00+08:00",
                                {"result_ref": "RESULT-W"}))
        self.assertEqual(signed["status"], "result_signed_frozen")
        summary = self.decision()["summary"]
        by_fleet = {row["fleet_ref"]: row for row in summary["standings"]}
        # 退赛前两个检查点保留
        self.assertEqual(len(by_fleet["TEAM-B"]["checkpoints"]), 2)
        self.assertEqual(by_fleet["TEAM-B"]["withdrawn_at"], "2026-09-22T10:30:00+08:00")
        # 发令名单来自冻结时刻，包含 TEAM-B
        self.assertEqual(summary["start_list"], ["TEAM-A", "TEAM-B"])

    def test_withdrawal_before_start_does_not_block_leg(self) -> None:
        ready_leg(self, fleets=("TEAM-A", "TEAM-B"))
        self.put(event("WD-EARLY", "withdrawal", "registrar",
                       "2026-09-22T08:00:00+08:00", {"fleet_ref": "TEAM-B"}, fleet="TEAM-B"))
        opened = self.put(event("CMD-W2", "start_commanded", "race_referee",
                                "2026-09-22T09:00:00+08:00", {}))
        self.assertEqual(opened["status"], "open_frozen")
        basis = self.decision()["decision"]["basis"]
        self.assertEqual(
            [f["fleet_ref"] for f in basis["checks"]["fleet_ready"]["start_list"]],
            ["TEAM-A"])
        self.assertEqual(
            [f["fleet_ref"] for f in basis["checks"]["fleet_ready"]["withdrawn"]],
            ["TEAM-B"])

    def test_uninspected_or_untrained_fleet_blocks(self) -> None:
        ready_leg(self, fleets=("TEAM-A", "TEAM-B"))
        # TEAM-B 检修不合格
        self.put(event("BI-FAIL", "boat_inspection", "inspector",
                       "2026-09-22T08:30:00+08:00", {"passed": False}, fleet="TEAM-B"))
        denied = self.put(event("CMD-Q", "start_commanded", "race_referee",
                                "2026-09-22T09:00:00+08:00", {}))
        self.assertEqual(denied["status"], "denied")
        self.assertIn("fleet_ready", {r["code"] for r in denied["decision"]["reasons"]})


class RoleMatrixTest(StoreFixture):
    def _base(self, etype: str, role: str, payload: dict) -> dict:
        return event("X", etype, role, "2026-09-22T08:00:00+08:00", payload,
                     fleet="TEAM-A")

    def test_rescue_cannot_command_or_suspend_or_sign(self) -> None:
        for etype, payload in (("start_commanded", {}),
                               ("leg_suspended", {"reason": "x"}),
                               ("result_signed", {"result_ref": "R"})):
            with self.subTest(etype=etype):
                with self.assertRaises(ValidationError) as ctx:
                    self.put(self._base(etype, "rescue_coordinator", payload))
                self.assertEqual(ctx.exception.code, "role_forbidden")

    def test_referee_cannot_maintain_rescue_or_weather(self) -> None:
        for etype, payload in (
                ("rescue_coverage", {"rescue_zone": "Z1", "course_revision": 1, "covered": True}),
                ("safety_incident", {"rescue_zone": "Z1", "severity": "high", "active": True}),
                ("weather_wind", {"wind_speed_kt": 1, "limit_kt": 2}),
                ("boat_inspection", {"passed": True})):
            with self.subTest(etype=etype):
                with self.assertRaises(ValidationError) as ctx:
                    self.put(self._base(etype, "race_referee", payload))
                self.assertEqual(ctx.exception.code, "role_forbidden")

    def test_active_safety_incident_blocks_start(self) -> None:
        ready_leg(self)
        self.put(event("INC-1", "safety_incident", "rescue_coordinator",
                       "2026-09-22T08:40:00+08:00",
                       {"rescue_zone": "ZONE-1", "severity": "high", "active": True}))
        denied = self.put(event("CMD-I", "start_commanded", "race_referee",
                                "2026-09-22T09:00:00+08:00", {}))
        self.assertEqual(denied["status"], "denied")
        self.assertIn("no_active_safety_incident",
                      {r["code"] for r in denied["decision"]["reasons"]})
        # 事件解除后可以发令
        self.put(event("INC-CLR", "safety_incident", "rescue_coordinator",
                       "2026-09-22T08:55:00+08:00",
                       {"rescue_zone": "ZONE-1", "severity": "high", "active": False}))
        opened = self.put(event("CMD-I2", "start_commanded", "race_referee",
                                "2026-09-22T09:01:00+08:00", {}))
        self.assertEqual(opened["status"], "open_frozen")


class EventIntegrityTest(StoreFixture):
    def test_naive_datetime_rejected(self) -> None:
        bad = event("BAD-T", "course_published", "race_office",
                    "2026-09-22T08:00:00",
                    {"course_revision": 1, "buoy_refs": ["B"], "rescue_zones": ["Z"]})
        with self.assertRaises(ValidationError) as ctx:
            self.put(bad)
        self.assertEqual(ctx.exception.code, "invalid_time")

    def test_duplicate_event_id_is_conflict(self) -> None:
        ready_leg(self)
        ev1 = event("DUP-1", "weather_wind", "weather_observer",
                    "2026-09-22T08:10:00+08:00", {"wind_speed_kt": 9, "limit_kt": 15})
        ev2 = dict(ev1, payload={"wind_speed_kt": 10, "limit_kt": 15})
        self.put(ev1)
        with self.assertRaises(Conflict):
            self.put(ev2)

    def test_sign_requires_open_leg(self) -> None:
        ready_leg(self)
        with self.assertRaises(ValidationError) as ctx:
            self.put(event("SIGN-NEVER", "result_signed", "race_referee",
                           "2026-09-22T11:00:00+08:00", {"result_ref": "R"}))
        self.assertEqual(ctx.exception.code, "leg_not_open")

    def test_hash_chain_links_events(self) -> None:
        ready_leg(self)
        events = self.store.list_events("LEG-BUOY-01")
        self.assertGreater(len(events), 1)
        self.assertEqual(events[0]["prev_hash"], "GENESIS")
        for prev, cur in zip(events, events[1:]):
            self.assertEqual(cur["prev_hash"], prev["entry_hash"])

    def test_denied_start_does_not_freeze_and_may_be_retried(self) -> None:
        ready_leg(self, wind=20.0)
        first = self.put(event("CMD-R1", "start_commanded", "race_referee",
                               "2026-09-22T09:00:00+08:00", {}))
        self.assertEqual(first["status"], "denied")
        # 风恢复后再次发令成功
        self.put(event("WX-OK", "weather_wind", "weather_observer",
                       "2026-09-22T09:05:00+08:00", {"wind_speed_kt": 7, "limit_kt": 15}))
        second = self.put(event("CMD-R2", "start_commanded", "race_referee",
                                "2026-09-22T09:10:00+08:00", {}))
        self.assertEqual(second["status"], "open_frozen")


if __name__ == "__main__":
    unittest.main()
