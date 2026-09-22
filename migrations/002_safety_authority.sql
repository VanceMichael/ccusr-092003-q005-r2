-- 安全判定服务：唯一权威状态源
-- 所有事实按实际发生时间(occurred_at)追加写入；服务端另记受理时间(recorded_at)。
-- 任何事件不得物理删除或就地改写，更正以新事件表达。

PRAGMA foreign_keys = ON;

-- 赛事（一次赛事含多个赛段）
CREATE TABLE IF NOT EXISTS races (
    race_ref       TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

-- 赛段：绕标赛与长航赛使用不同江段(track_ref)，各自要求不同救援区与安全限值。
CREATE TABLE IF NOT EXISTS legs (
    leg_ref        TEXT PRIMARY KEY,
    race_ref       TEXT NOT NULL REFERENCES races(race_ref),
    track_ref      TEXT NOT NULL,          -- 江段引用
    title          TEXT NOT NULL,
    seq            INTEGER NOT NULL,       -- 赛程顺序（次日长航赛 seq 更大）
    required_zones_json TEXT NOT NULL DEFAULT '[]',
    wind_min_kn    REAL,                   -- 风速允许下限（可空=不限）
    wind_max_kn    REAL,                   -- 风速允许上限
    visibility_min_m REAL,                 -- 能见度允许下限（米）
    obs_max_age_s  INTEGER NOT NULL DEFAULT 1800,  -- 发令瞬间观测可接受的最大时效
    created_at     TEXT NOT NULL,
    UNIQUE(race_ref, seq)
);

-- 船队报名：entry_ref 即事件中的 fleet_ref；boat_ref 为受控船只编号
CREATE TABLE IF NOT EXISTS entries (
    entry_ref      TEXT PRIMARY KEY,
    race_ref       TEXT NOT NULL REFERENCES races(race_ref),
    boat_ref       TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

-- 通用事件表：事实的唯一存储，append-only。
--   judge  : 报到、检修、培训、赛道版本、风/能见度观测、暂停/恢复、
--            发令、检查点、退赛、成绩签署
--   rescue : 救援覆盖、安全事件
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type     TEXT NOT NULL,
    race_ref       TEXT NOT NULL,
    leg_ref        TEXT,                   -- 赛段级事实必填；船只检修等赛事级事实可空
    fleet_ref      TEXT,                   -- 船队级事实指向 entry_ref
    occurred_at    TEXT NOT NULL,          -- 实际发生时间（带偏移量 ISO 8601）
    recorded_at    TEXT NOT NULL,          -- 服务端受理时间
    actor_role     TEXT NOT NULL,
    payload_json   TEXT NOT NULL DEFAULT '{}',
    CHECK (actor_role IN ('judge', 'rescue'))
);

CREATE INDEX IF NOT EXISTS idx_events_leg_time
    ON events(leg_ref, event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_fleet_time
    ON events(fleet_ref, event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_race_time
    ON events(race_ref, event_type, occurred_at);

-- 发令尝试与恢复确认：记录判定瞬间 T 的完整依据快照（不可变）。
-- 即使判定为拒绝，或之后有迟到观测，本快照都不改变。
CREATE TABLE IF NOT EXISTS start_attempts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL CHECK (kind IN ('start', 'resume')),
    leg_ref        TEXT NOT NULL REFERENCES legs(leg_ref),
    fired_at       TEXT NOT NULL,          -- 判定瞬间 T（= 请求中的实际发生时间）
    permitted      INTEGER NOT NULL,       -- 1 满足 / 0 不满足
    basis_json     TEXT NOT NULL,          -- 每项前置条件在 T 时刻的证据（含版本/观测编号/时间）
    reasons_json   TEXT NOT NULL           -- 未满足项（permitted=0 时非空）
);

-- 赛段阶段：开放后记录航次；签署结果后冻结，迟到事实一律不得改写。
CREATE TABLE IF NOT EXISTS leg_phases (
    leg_ref        TEXT PRIMARY KEY REFERENCES legs(leg_ref),
    started_at     TEXT,                  -- 首次成功开放时间（航次起点，不可变）
    finished_at    TEXT,                  -- 裁判签署结果时间
    result_json    TEXT,                  -- 签署的成绩材料（受控引用/摘要）
    finalized      INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('002_safety_authority');
