-- 安全判定服务：只追加事件日志 + 发令决定冻结快照 + 签署结果
CREATE TABLE IF NOT EXISTS events (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         TEXT NOT NULL UNIQUE,
    race_ref         TEXT NOT NULL,
    leg_ref          TEXT NOT NULL,
    fleet_ref        TEXT,
    event_type       TEXT NOT NULL,
    actor_role       TEXT NOT NULL,
    actor_ref        TEXT NOT NULL,
    occurred_at      TEXT NOT NULL,           -- 带偏移量 ISO 8601，业务实际发生时间
    received_at      TEXT NOT NULL,           -- 服务接收时间
    source_ref       TEXT,                    -- 受控引用或 sha256 摘要
    payload_json     TEXT NOT NULL,
    prev_hash        TEXT NOT NULL,
    entry_hash       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_leg_time ON events(leg_ref, occurred_at, seq);
CREATE INDEX IF NOT EXISTS idx_events_race     ON events(race_ref, leg_ref);

-- 发令瞬间的权威决定：一经写入永不更新，迟到观测无法改写
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

INSERT OR IGNORE INTO schema_migrations(version) VALUES ('002_safety_decision');
