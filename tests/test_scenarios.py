"""赛事故事场景测试：以松花江两天赛程的业务叙述驱动。"""

import unittest

from app.service import JUDGE, PUBLIC, RESCUE
from tests.helpers import assert_error, make_service, satisfy_all, seed_race

# 第一天绕标赛（江段 WW-1），第二天长航赛（江段 LD-2）
WW_START = "2026-09-20T09:00:00+08:00"
WW_T0800 = "2026-09-20T08:00:00+08:00"
WW_T0840 = "2026-09-20T08:40:00+08:00"
WW_T0930 = "2026-09-20T09:30:00+08:00"
WW_T0931 = "2026-09-20T09:31:00+08:00"
WW_T0950 = "2026-09-20T09:50:00+08:00"
WW_T0955 = "2026-09-20T09:55:00+08:00"
WW_T1000 = "2026-09-20T10:00:00+08:00"
WW_T1100 = "2026-09-20T11:00:00+08:00"
WW_T1130 = "2026-09-20T11:30:00+08:00"
LD_START = "2026-09-21T09:00:00+08:00"
LD_T0800 = "2026-09-21T08:00:00+08:00"
LD_T0845 = "2026-09-21T08:45:00+08:00"
LD_T0805 = "2026-09-21T08:05:00+08:00"
LD_T0920 = "2026-09-21T09:20:00+08:00"
LD_T0940 = "2026-09-21T09:40:00+08:00"
LD_T0945 = "2026-09-21T09:45:00+08:00"
LD_T0950 = "2026-09-21T09:50:00+08:00"
LD_T1000 = "2026-09-21T10:00:00+08:00"


class StartGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        seed_race(self.svc)

    def test_all_preconditions_satisfied_permits_start_with_evidence(self) -> None:
        satisfy_all(self.svc, "LEG-WW", t=WW_START, obs_time=WW_T0840, zones=("ZONE-1",))
        result = self.svc.start(JUDGE, "LEG-WW", {"occurred_at": WW_START})
        self.assertTrue(result["permitted"])
        basis = result["basis"]
        # 依据必须可溯源：赛道版本、观测编号、发生/受理时间
        self.assertEqual(basis["course"]["revision"], 3)
        self.assertEqual(basis["course"]["track_ref"], "WW-1")
        self.assertEqual(basis["wind"]["observation_ref"], "OBS-W")
        self.assertEqual(basis["wind"]["value"], 10.0)
        self.assertTrue(basis["wind"]["within_limits"])
        self.assertEqual(basis["rescue_coverage"][0]["zone"], "ZONE-1")
        self.assertIn("occurred_at", basis["course"])
        self.assertIn("recorded_at", basis["course"])

        decision = self.svc.leg_decision("LEG-WW")
        self.assertEqual(decision["status"], "open")
        self.assertTrue(decision["ready"])
        self.assertEqual(decision["updated_at"], WW_START)
        self.assertEqual(decision["basis"]["wind"]["occurred_at"], WW_T0840)

    def test_unconfirmed_course_blocks_start_and_public_sees_not_ready(self) -> None:
        satisfy_all(self.svc, "LEG-WW", t=WW_START, obs_time=WW_T0840, zones=("ZONE-1",))
        # 浮标移位后新赛道版本尚未经裁判确认
        self.svc.store.conn.execute(
            "UPDATE events SET payload_json=json_set(payload_json, '$.confirmed', json('false')) "
            "WHERE event_type='course_revision'"
        )
        err = assert_error(
            lambda: self.svc.start(JUDGE, "LEG-WW", {"occurred_at": WW_START}),
            403, "preconditions_not_met",
        )
        codes = {r["code"] for r in err.extra["reasons"]}
        self.assertIn("course_unconfirmed", codes)
        decision = self.svc.leg_decision("LEG-WW")
        self.assertEqual(decision["status"], "not_ready")
        self.assertFalse(decision["ready"])
        self.assertIn("course_unconfirmed", {r["code"] for r in decision["reasons"]})

    def test_stale_observation_blocks_start(self) -> None:
        # 观测发生在 08:00，发令在 09:00，超过 30 分钟时效
        satisfy_all(self.svc, "LEG-WW", t=WW_START,
                    obs_time=WW_T0800, zones=("ZONE-1",))
        err = assert_error(
            lambda: self.svc.start(JUDGE, "LEG-WW", {"occurred_at": WW_START}),
            403, "preconditions_not_met",
        )
        codes = {r["code"] for r in err.extra["reasons"]}
        self.assertIn("wind_stale", codes)
        self.assertIn("visibility_stale", codes)

    def test_wind_out_of_limits_blocks_start(self) -> None:
        satisfy_all(self.svc, "LEG-WW", t=WW_START, obs_time=WW_T0840, zones=("ZONE-1",))
        self.svc.record_event("observation", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-WW", "occurred_at": WW_T0840,
            "kind": "wind", "value": 25.0, "unit": "kn", "observation_ref": "OBS-W2",
        })
        err = assert_error(
            lambda: self.svc.start(JUDGE, "LEG-WW", {"occurred_at": WW_START}),
            403, "preconditions_not_met",
        )
        self.assertIn("wind_out_of_limits", {r["code"] for r in err.extra["reasons"]})

    def test_per_leg_checkins_required(self) -> None:
        # 在绕标赛全部报到，不代表长航赛已报到
        satisfy_all(self.svc, "LEG-WW", t=WW_START, obs_time=WW_T0840, zones=("ZONE-1",))
        satisfy_all(self.svc, "LEG-LD", t=LD_START, obs_time=LD_T0845,
                    fleets=(), zones=("ZONE-2", "ZONE-3"))
        err = assert_error(
            lambda: self.svc.start(JUDGE, "LEG-LD", {"occurred_at": LD_START}),
            403, "preconditions_not_met",
        )
        codes = {r["code"] for r in err.extra["reasons"]}
        self.assertIn("fleet_not_checked_in", codes)
        # 每个未报到船队各一条
        self.assertEqual(
            sum(1 for r in err.extra["reasons"] if r["code"] == "fleet_not_checked_in"),
            2,
        )


class BuoyDriftStoryTest(unittest.TestCase):
    """浮标移位：裁判台已暂停，直播端不得仍显示可以出发；恢复必须重新确认。"""

    def setUp(self) -> None:
        self.svc = make_service()
        seed_race(self.svc)
        satisfy_all(self.svc, "LEG-WW", t=WW_START, obs_time=WW_T0840, zones=("ZONE-1",))
        self.svc.start(JUDGE, "LEG-WW", {"occurred_at": WW_START})

    def test_suspension_immediately_marks_public_decision_not_startable(self) -> None:
        # 09:30 发现浮标移位：新版本尚未确认，09:31 裁判台暂停
        self.svc.record_event("course-revision", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-WW", "occurred_at": WW_T0930,
            "revision": 4, "track_ref": "WW-1", "confirmed": False,
            "note_ref": "BUOY-DRIFT-7",
        })
        self.svc.record_event("suspend", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-WW", "occurred_at": WW_T0931,
            "reason": "浮标移位，赛道待复核",
        })
        decision = self.svc.leg_decision("LEG-WW")
        self.assertEqual(decision["status"], "suspended")
        self.assertFalse(decision["ready"])
        self.assertIn("leg_suspended", {r["code"] for r in decision["reasons"]})

        # 暂停期间再次发令必须被拒绝，并引导走恢复确认
        assert_error(
            lambda: self.svc.start(JUDGE, "LEG-WW", {"occurred_at": WW_T1000}),
            409, "use_resume",
        )

    def test_resume_requires_reconfirmation_then_opens_again(self) -> None:
        self.svc.record_event("course-revision", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-WW", "occurred_at": WW_T0930,
            "revision": 4, "track_ref": "WW-1", "confirmed": False,
        })
        self.svc.record_event("suspend", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-WW", "occurred_at": WW_T0931,
            "reason": "浮标移位",
        })
        # 恢复瞬间赛道版本仍未确认 → 拒绝，但快照必须留档
        err = assert_error(
            lambda: self.svc.resume(JUDGE, "LEG-WW", {"occurred_at": WW_T0950}),
            403, "preconditions_not_met",
        )
        self.assertIn("course_unconfirmed", {r["code"] for r in err.extra["reasons"]})
        attempts = self.svc.attempts("LEG-WW")["attempts"]
        failed_resume = attempts[-1]
        self.assertEqual(failed_resume["kind"], "resume")
        self.assertFalse(failed_resume["permitted"])

        # 复核完成：新赛道版本确认 + 新观测（在恢复的 30 分钟窗口内）
        self.svc.record_event("course-revision", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-WW", "occurred_at": WW_T0950,
            "revision": 5, "track_ref": "WW-1", "confirmed": True,
        })
        self.svc.record_event("observation", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-WW", "occurred_at": WW_T0955,
            "kind": "wind", "value": 9.0, "unit": "kn", "observation_ref": "OBS-W-R",
        })
        self.svc.record_event("observation", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-WW", "occurred_at": WW_T0955,
            "kind": "visibility", "value": 4000.0, "unit": "m", "observation_ref": "OBS-V-R",
        })
        result = self.svc.resume(JUDGE, "LEG-WW", {"occurred_at": WW_T1000})
        self.assertTrue(result["permitted"])
        self.assertEqual(result["basis"]["course"]["revision"], 5)

        decision = self.svc.leg_decision("LEG-WW")
        self.assertEqual(decision["status"], "open")
        self.assertTrue(decision["ready"])

    def test_resume_without_suspend_is_rejected(self) -> None:
        assert_error(
            lambda: self.svc.resume(JUDGE, "LEG-WW", {"occurred_at": WW_T0930}),
            409, "not_suspended",
        )


class NextDayLongRaceTest(unittest.TestCase):
    """次日长航赛：救援队若仍拿着旧江段（WW-1/ZONE-1）信息，覆盖判定不得通过。"""

    def setUp(self) -> None:
        self.svc = make_service()
        seed_race(self.svc)

    def test_old_river_section_coverage_does_not_satisfy_long_race(self) -> None:
        # 除救援外全部满足；救援队只拿着第一天旧江段的信息在 ZONE-1 就位
        satisfy_all(self.svc, "LEG-LD", t=LD_START, obs_time=LD_T0845,
                    fleets=("TEAM-A", "TEAM-B"), zones=())
        self.svc.record_event("rescue-coverage", RESCUE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-LD", "occurred_at": LD_T0845,
            "zone": "ZONE-1", "active": True, "unit_ref": "R-OLD",
        })
        err = assert_error(
            lambda: self.svc.start(JUDGE, "LEG-LD", {"occurred_at": LD_START}),
            403, "preconditions_not_met",
        )
        uncovered = {r["message"] for r in err.extra["reasons"]
                     if r["code"] == "rescue_zone_uncovered"}
        self.assertTrue(any("ZONE-2" in m for m in uncovered))
        self.assertTrue(any("ZONE-3" in m for m in uncovered))
        self.assertFalse(any("ZONE-1" in m for m in uncovered))

    def test_course_revision_for_wrong_track_is_rejected(self) -> None:
        satisfy_all(self.svc, "LEG-LD", t=LD_START, obs_time=LD_T0845,
                    fleets=("TEAM-A", "TEAM-B"), zones=("ZONE-2", "ZONE-3"))
        # 误把绕标赛江段的赛道版本发到长航赛
        self.svc.record_event("course-revision", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-LD", "occurred_at": LD_T0845,
            "revision": 3, "track_ref": "WW-1", "confirmed": True,
        })
        err = assert_error(
            lambda: self.svc.start(JUDGE, "LEG-LD", {"occurred_at": LD_START}),
            403, "preconditions_not_met",
        )
        self.assertIn(
            "course_track_mismatch", {r["code"] for r in err.extra["reasons"]}
        )


class LateObservationTest(unittest.TestCase):
    """迟到观测不得改写已结束航次；历史发令快照不可变。"""

    def setUp(self) -> None:
        self.svc = make_service()
        seed_race(self.svc)
        satisfy_all(self.svc, "LEG-WW", t=WW_START, obs_time=WW_T0840, zones=("ZONE-1",))
        self.svc.start(JUDGE, "LEG-WW", {"occurred_at": WW_START})
        self.svc.sign_result(JUDGE, "LEG-WW", {
            "occurred_at": WW_T1100, "result_ref": "RESULT-WW-1",
        })

    def test_finished_leg_is_frozen_and_rejects_later_facts(self) -> None:
        # 签署之后才发生的任何赛段事实一律拒收
        assert_error(
            lambda: self.svc.record_event("observation", JUDGE, {
                "race_ref": "RACE-2026", "leg_ref": "LEG-WW",
                "occurred_at": WW_T1130, "kind": "wind",
                "value": 30.0, "observation_ref": "OBS-LATE",
            }),
            409, "leg_closed",
        )
        assert_error(
            lambda: self.svc.resume(JUDGE, "LEG-WW", {"occurred_at": WW_T1130}),
            409, "leg_closed",
        )
        assert_error(
            lambda: self.svc.sign_result(JUDGE, "LEG-WW", {
                "occurred_at": WW_T1130, "result_ref": "RESULT-OTHER",
            }),
            409, "already_finalized",
        )

    def test_backdated_fact_within_window_is_kept_but_decision_stays_frozen(self) -> None:
        # 受理很晚、但实际发生在签署之前的观测允许补录，冻结决定不被改写
        self.svc.record_event("observation", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-WW",
            "occurred_at": WW_T0950, "kind": "wind",
            "value": 30.0, "observation_ref": "OBS-BACKDATED",
        })
        decision = self.svc.leg_decision("LEG-WW")
        self.assertTrue(decision["frozen"])
        self.assertEqual(decision["status"], "finished")
        self.assertEqual(decision["updated_at"], WW_T1100)
        self.assertEqual(decision["signed_result"]["result_ref"], "RESULT-WW-1")

        # 发令瞬间的依据快照也不受补录影响
        start_attempt = self.svc.attempts("LEG-WW")["attempts"][0]
        self.assertTrue(start_attempt["permitted"])
        self.assertEqual(start_attempt["basis"]["wind"]["observation_ref"], "OBS-W")

    def test_cannot_sign_while_suspended(self) -> None:
        svc = make_service()
        seed_race(svc)
        satisfy_all(svc, "LEG-LD", t=LD_START, obs_time=LD_T0845,
                    fleets=("TEAM-A", "TEAM-B"), zones=("ZONE-2", "ZONE-3"))
        svc.start(JUDGE, "LEG-LD", {"occurred_at": LD_START})
        svc.record_event("suspend", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-LD",
            "occurred_at": "2026-09-21T09:30:00+08:00", "reason": "雷雨",
        })
        assert_error(
            lambda: svc.sign_result(JUDGE, "LEG-LD", {
                "occurred_at": "2026-09-21T11:00:00+08:00",
                "result_ref": "R-X",
            }),
            409, "leg_suspended",
        )


class WithdrawalTest(unittest.TestCase):
    """退赛仍保留此前检查点，且退赛船队不再阻塞发令。"""

    def test_withdrawn_fleet_keeps_checkpoints_and_exits_gate(self) -> None:
        svc = make_service()
        seed_race(svc)
        satisfy_all(svc, "LEG-LD", t=LD_START, obs_time=LD_T0845,
                    fleets=("TEAM-A", "TEAM-B"), zones=("ZONE-2", "ZONE-3"))
        svc.start(JUDGE, "LEG-LD", {"occurred_at": LD_START})

        # 航次中：TEAM-B 过检查点后于 09:40 退赛
        svc.record_event("checkpoint", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-LD", "fleet_ref": "TEAM-B",
            "occurred_at": LD_T0920, "checkpoint_ref": "CP-B-1",
        })
        svc.record_event("withdrawal", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-LD", "fleet_ref": "TEAM-B",
            "occurred_at": LD_T0940, "reason": "桅杆受损",
        })

        # 09:45 赛段暂停；恢复确认时 TEAM-B 已退赛，不应再要求其检修/培训
        svc.record_event("suspend", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-LD",
            "occurred_at": LD_T0945, "reason": "短时雷暴",
        })
        svc.record_event("course-revision", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-LD", "occurred_at": LD_T0945,
            "revision": 2, "track_ref": "LD-2", "confirmed": True,
        })
        svc.record_event("observation", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-LD", "occurred_at": LD_T0950,
            "kind": "wind", "value": 8.0, "unit": "kn", "observation_ref": "OBS-LD-W2",
        })
        svc.record_event("observation", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": "LEG-LD", "occurred_at": LD_T0950,
            "kind": "visibility", "value": 3000.0, "unit": "m",
            "observation_ref": "OBS-LD-V2",
        })
        for zone in ("ZONE-2", "ZONE-3"):
            svc.record_event("rescue-coverage", RESCUE, {
                "race_ref": "RACE-2026", "leg_ref": "LEG-LD",
                "occurred_at": LD_T0950,
                "zone": zone, "active": True,
            })
        result = svc.resume(JUDGE, "LEG-LD", {"occurred_at": LD_T1000})
        self.assertTrue(result["permitted"])
        fleets = {f["fleet_ref"]: f for f in result["basis"]["fleets"]}
        self.assertTrue(fleets["TEAM-B"]["withdrawn"])
        self.assertNotIn("checkin", fleets["TEAM-B"])

        # 退赛后此前检查点仍保留在公众可见的进展里
        decision = svc.leg_decision("LEG-LD")
        progress = [(p["type"], p["fleet_ref"], p["payload"].get("checkpoint_ref"))
                    for p in decision["progress"]]
        self.assertIn(("checkpoint", "TEAM-B", "CP-B-1"), progress)
        self.assertIn(("withdrawal", "TEAM-B", None), progress)


class RoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = make_service()
        seed_race(self.svc)

    def test_rescue_only_maintains_safety_domain(self) -> None:
        # 救援不能发令、不能签署、不能录赛道/天气
        assert_error(
            lambda: self.svc.start(RESCUE, "LEG-WW", {"occurred_at": WW_START}),
            403, "forbidden",
        )
        assert_error(
            lambda: self.svc.record_event("course-revision", RESCUE, {
                "race_ref": "RACE-2026", "leg_ref": "LEG-WW", "occurred_at": WW_T0840,
                "revision": 1, "track_ref": "WW-1", "confirmed": True,
            }),
            403, "forbidden",
        )
        # 裁判不录入安全事件；救援可以
        assert_error(
            lambda: self.svc.record_event("safety-incident", JUDGE, {
                "race_ref": "RACE-2026", "occurred_at": WW_T0930,
                "incident_ref": "INC-1", "summary_sha256": "x" * 64,
                "severity": "minor",
            }),
            403, "forbidden",
        )
        created = self.svc.record_event("safety-incident", RESCUE, {
            "race_ref": "RACE-2026", "occurred_at": WW_T0930,
            "incident_ref": "INC-1", "summary_sha256": "a" * 64,
            "severity": "minor",
        })
        self.assertTrue(created["recorded"])

    def test_public_reads_decision_but_not_internal_records(self) -> None:
        satisfy_all(self.svc, "LEG-WW", t=WW_START, obs_time=WW_T0840, zones=("ZONE-1",))
        decision = self.svc.leg_decision("LEG-WW")  # 无角色即公众
        self.assertIn("status", decision)
        self.assertIn("basis", decision)
        self.assertIn("updated_at", decision)
        # 公众视图不暴露内部受理通道以外的内容：安全事件走专门接口
        # （HTTP 层会拦截，这里验证服务层角色矩阵）
        assert_error(
            lambda: self.svc.safety_events(PUBLIC, "RACE-2026"),
            403, "forbidden",
        )

        events = self.svc.safety_events(RESCUE, "RACE-2026")
        # 安全视图汇总救援覆盖与安全事件（此时只有 satisfy_all 写入的 ZONE-1 覆盖）
        self.assertTrue(events["events"])
        self.assertTrue(all(
            e["event_type"] in ("rescue_coverage", "safety_incident")
            for e in events["events"]
        ))

    def test_judge_signs_results(self) -> None:
        satisfy_all(self.svc, "LEG-WW", t=WW_START, obs_time=WW_T0840, zones=("ZONE-1",))
        assert_error(
            lambda: self.svc.sign_result(JUDGE, "LEG-WW", {
                "occurred_at": WW_T0840, "result_ref": "R-EARLY",
            }),
            409, "not_started",
        )
        self.svc.start(JUDGE, "LEG-WW", {"occurred_at": WW_START})
        signed = self.svc.sign_result(JUDGE, "LEG-WW", {
            "occurred_at": WW_T1100, "result_ref": "RESULT-WW-1",
        })
        self.assertTrue(signed["frozen"])


if __name__ == "__main__":
    unittest.main()
