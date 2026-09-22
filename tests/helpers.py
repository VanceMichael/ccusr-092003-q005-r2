"""测试辅助：内存数据库 + 直接构造 Service。"""

import sqlite3

from app.service import JUDGE, RESCUE, ApiError, Service
from app.store import Store
from scripts.migrate import MIGRATIONS_DIR


def make_service() -> Service:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 直接在内存库上按序执行真实迁移脚本，避免测试与生产 schema 漂移。
    for script in sorted(MIGRATIONS_DIR.glob("*.sql")):
        conn.executescript(script.read_text(encoding="utf-8"))
    return Service(Store(conn))


def seed_race(svc: Service) -> None:
    """登记一次赛事：绕标赛 LEG-WW（江段 WW-1）+ 长航赛 LEG-LD（江段 LD-2）。"""
    svc.put_race(JUDGE, {"race_ref": "RACE-2026", "title": "松花江帆船赛 2026"})
    svc.put_leg(JUDGE, {
        "leg_ref": "LEG-WW", "race_ref": "RACE-2026", "track_ref": "WW-1",
        "title": "绕标赛", "seq": 1, "required_zones": ["ZONE-1"],
        "wind_min_kn": 3.0, "wind_max_kn": 20.0,
        "visibility_min_m": 1000.0, "obs_max_age_s": 1800,
    })
    svc.put_leg(JUDGE, {
        "leg_ref": "LEG-LD", "race_ref": "RACE-2026", "track_ref": "LD-2",
        "title": "长航赛", "seq": 2, "required_zones": ["ZONE-2", "ZONE-3"],
        "wind_min_kn": 3.0, "wind_max_kn": 18.0,
        "visibility_min_m": 1500.0, "obs_max_age_s": 1800,
    })
    svc.put_entry(JUDGE, {
        "entry_ref": "TEAM-A", "race_ref": "RACE-2026", "boat_ref": "BOAT-A1",
    })
    svc.put_entry(JUDGE, {
        "entry_ref": "TEAM-B", "race_ref": "RACE-2026", "boat_ref": "BOAT-B2",
    })


def satisfy_all(svc: Service, leg_ref: str, *, t: str, obs_time: str | None = None,
                fleets=("TEAM-A", "TEAM-B"), zones=("ZONE-1",)) -> None:
    """在给定赛段补齐全部前置条件。"""
    obs_time = obs_time or t
    track = "WW-1" if leg_ref == "LEG-WW" else "LD-2"
    svc.record_event("course-revision", JUDGE, {
        "race_ref": "RACE-2026", "leg_ref": leg_ref, "occurred_at": obs_time,
        "revision": 3, "track_ref": track, "confirmed": True,
        "buoys_sha256": "abc123",
    })
    svc.record_event("observation", JUDGE, {
        "race_ref": "RACE-2026", "leg_ref": leg_ref, "occurred_at": obs_time,
        "kind": "wind", "value": 10.0, "unit": "kn", "observation_ref": "OBS-W",
    })
    svc.record_event("observation", JUDGE, {
        "race_ref": "RACE-2026", "leg_ref": leg_ref, "occurred_at": obs_time,
        "kind": "visibility", "value": 3000.0, "unit": "m", "observation_ref": "OBS-V",
    })
    for zone in zones:
        svc.record_event("rescue-coverage", RESCUE, {
            "race_ref": "RACE-2026", "leg_ref": leg_ref, "occurred_at": obs_time,
            "zone": zone, "active": True, "unit_ref": f"R-{zone}",
        })
    for fleet in fleets:
        svc.record_event("checkin", JUDGE, {
            "race_ref": "RACE-2026", "leg_ref": leg_ref,
            "fleet_ref": fleet, "occurred_at": obs_time,
        })
        svc.record_event("boat-check", JUDGE, {
            "race_ref": "RACE-2026", "fleet_ref": fleet, "occurred_at": obs_time,
            "status": "passed", "inspection_ref": f"INSP-{fleet}",
        })
        svc.record_event("training", JUDGE, {
            "race_ref": "RACE-2026", "fleet_ref": fleet, "occurred_at": obs_time,
            "status": "completed", "course_ref": "TR-2026",
        })


def assert_error(fn, status: int, code: str) -> ApiError:
    try:
        fn()
    except ApiError as err:
        assert err.status == status, f"expected {status}, got {err.status}"
        assert err.code == code, f"expected {code}, got {err.code}"
        return err
    raise AssertionError(f"expected ApiError {code}, nothing raised")
