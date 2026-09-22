# 松花江帆船赛安全判定

绕标赛与长航赛使用不同江段，赛道浮标、船只检修、天气观测和救援覆盖共同决定能否发令。

本服务是赛事安全状态的**唯一权威来源**：所有事实按实际发生时间追加写入，赛段只在发令瞬间全部前置条件有效时才开放；暂停后的恢复必须重新确认，迟到观测不得改写已结束航次。通过 HTTP 接口交换业务记录，使用 SQLite 文件保存状态。`PORT` 指定监听端口，`DATABASE_PATH` 指定数据文件；`contracts/entities.json` 记录字段约定，`docs/domain.md` 说明领域规则。

## 本地开发

```bash
make migrate   # 初始化/升级数据文件
make test      # 执行自动化检查
make run       # 启动服务（默认 :8080）
```

也可以使用 `docker compose up --build` 在隔离容器中运行，宿主机端口由 `APP_PORT` 调整。

## 角色

请求头 `X-Role` 声明角色：`judge`（裁判）、`rescue`（救援）、`public`（公众，默认）。
服务只按角色授权，不做身份认证，生产部署应置于认证网关之后。

## 接口速览

所有时间字段使用带偏移量的 ISO 8601，如 `2026-09-20T09:00:00+08:00`。

### 配置（judge）

- `PUT /races` — 登记赛事：`race_ref`、`title`
- `PUT /legs` — 登记赛段：`leg_ref`、`race_ref`、`track_ref`（江段）、`title`、`seq`、
  `required_zones`、`wind_min_kn`、`wind_max_kn`、`visibility_min_m`、`obs_max_age_s`
- `PUT /entries` — 船队报名：`entry_ref`、`race_ref`、`boat_ref`

### 事实上报（append-only）

`POST /events/{type}`，通用字段 `race_ref`、`occurred_at`，按类型带 `leg_ref`/`fleet_ref`：

| 事件 | 角色 | 关键字段 |
| --- | --- | --- |
| `checkin` | judge | `leg_ref`、`fleet_ref` |
| `boat-check` | judge | `fleet_ref`、`status=passed` |
| `training` | judge | `fleet_ref`、`status=completed` |
| `course-revision` | judge | `leg_ref`、`revision`、`track_ref`、`confirmed` |
| `observation` | judge | `leg_ref`、`kind=wind\|visibility`、`value`、`observation_ref` |
| `rescue-coverage` | rescue | `leg_ref`、`zone`、`active` |
| `safety-incident` | rescue | `incident_ref`、`summary_sha256`、`severity` |
| `suspend` | judge | `leg_ref`、`reason` |
| `checkpoint` | judge | `leg_ref`、`fleet_ref`、`checkpoint_ref` |
| `withdrawal` | judge | `leg_ref`、`fleet_ref`、`reason` |

### 发令与签署（judge）

- `POST /legs/{leg_ref}/start` — 首次发令，body 仅需 `occurred_at`；
  服务以该瞬间点时间评估全部前置条件，不满足返回 `403 preconditions_not_met`，
  响应中带逐项 `reasons` 与完整 `basis`。
- `POST /legs/{leg_ref}/resume` — 暂停后恢复，在恢复瞬间重新确认全部前置条件。
- `POST /legs/{leg_ref}/sign` — 签署结果：`occurred_at`、`result_ref`，签署后赛段冻结。

### 查询

- `GET /legs/{leg_ref}/decision` — 公众当前权威决定：
  `status`（`not_ready` / `ready_to_start` / `open` / `suspended` / `finished`）、
  `ready`、`reasons`、`basis`（依据版本、观测编号、发生与受理时间）、
  `updated_at`、`progress`（检查点/退赛）。
- `GET /races/{race_ref}/decision` — 赛事下各赛段决定。
- `GET /legs/{leg_ref}/attempts` — （judge）历次发令/恢复判定快照，含被拒绝的尝试。
- `GET /races/{race_ref}/safety-events` — （judge/rescue）安全事件与救援覆盖。
- `GET /health` — 健康检查。

## 一次完整流程

```bash
# 登记赛事、绕标赛江段 WW-1、船队
curl -X PUT localhost:8080/races -H 'X-Role: judge' -d '{"race_ref":"RACE-1","title":"绕标赛"}'
curl -X PUT localhost:8080/legs  -H 'X-Role: judge' -d '{
  "leg_ref":"LEG-WW","race_ref":"RACE-1","track_ref":"WW-1","title":"绕标赛","seq":1,
  "required_zones":["ZONE-1"],"wind_min_kn":3,"wind_max_kn":20,"visibility_min_m":1000}'
curl -X PUT localhost:8080/entries -H 'X-Role: judge' -d '{"entry_ref":"TEAM-A","race_ref":"RACE-1","boat_ref":"BOAT-A1"}'

# 赛道版本、观测、救援、报到/检修/培训齐备后发令
curl -X POST localhost:8080/legs/LEG-WW/start -H 'X-Role: judge' \
  -d '{"occurred_at":"2026-09-20T09:00:00+08:00"}'

# 浮标移位：暂停立即对所有查询方生效
curl -X POST localhost:8080/events/suspend -H 'X-Role: judge' -d '{
  "race_ref":"RACE-1","leg_ref":"LEG-WW","occurred_at":"2026-09-20T09:31:00+08:00","reason":"浮标移位"}'
curl localhost:8080/legs/LEG-WW/decision   # status=suspended, ready=false
```

`fixtures/example.json` 提供不含真实身份的本地示例请求序列。
