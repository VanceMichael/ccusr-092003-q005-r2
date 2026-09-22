# 松花江帆船赛安全判定

绕标赛与长航赛使用不同江段，赛道浮标、船只检修、天气观测和救援覆盖共同决定能否发令。本服务是赛事的**唯一权威状态源**：所有业务事实按实际发生时间以只追加事件写入，某赛段只有在**发令瞬间**全部前置条件有效才开放；开放决定连同依据冻结，迟到观测不改写已结束航次。详细规则见 [`docs/domain.md`](docs/domain.md)，字段约定见 [`contracts/entities.json`](contracts/entities.json)。

本服务通过 HTTP 接口交换业务记录，并使用 SQLite 文件保存状态。`PORT` 指定监听端口，`DATABASE_PATH` 指定数据文件；`fixtures/example.json` 提供不含真实身份的本地示例。

## 本地开发

```bash
make migrate   # 初始化/升级数据文件
make test      # 自动化测试
make run       # 启动服务（默认 :8080）
```

也可以使用 `docker compose up --build` 在隔离容器中运行，宿主机端口由 `APP_PORT` 调整。

## HTTP 接口

### 写入事件 `POST /v1/events`

请求体为事件信封，`actor_role` 必须与事件类型匹配（角色矩阵见契约文件），`occurred_at` 为带偏移量 ISO 8601 的实际发生时间：

```json
{
  "event_id": "EV-WIND-1",
  "race_ref": "RACE-2026",
  "leg_ref": "LEG-BUOY-1",
  "fleet_ref": "TEAM-A",
  "event_type": "weather_wind",
  "actor_role": "weather_observer",
  "actor_ref": "WX-1",
  "occurred_at": "2026-09-22T07:00:00+08:00",
  "source_ref": "sha256:...",
  "payload": {"wind_speed_kt": 8, "limit_kt": 15}
}
```

事件类型：`fleet_checkin` / `boat_inspection` / `training_completed` / `withdrawal`（报到方）、`course_published`（赛道方）、`weather_wind` / `weather_visibility`（气象方）、`rescue_coverage` / `safety_incident`（救援方）、`leg_suspended` / `leg_resumed` / `checkpoint_recorded` / `start_commanded` / `result_signed`（裁判）。

响应语义：

| HTTP / status | 含义 |
| --- | --- |
| `200 recorded` | 事件已追加留痕 |
| `201 open_frozen` | 发令成功，开放决定已冻结 |
| `409 denied` | 发令瞬间前置条件未全部有效，响应 `decision.reasons` 给出逐项原因；未冻结，可补齐后重新发令 |
| `201 result_signed_frozen` | 裁判签署完成，赛段终局 |
| `200 recorded_not_authoritative` | 赛段签署后到达的事件：留痕但不产生权威效力 |
| `422 role_forbidden` / `reconfirmation_required` / 其他校验码 | 写入被拒 |
| `409 conflict` | 重复 `event_id`、重复发令/签署等 |

暂停后的恢复事件必须 `"payload": {"reconfirmed": true}`。

### 查询当前决定 `GET /v1/legs/<leg_ref>/decision`

直播端与公众使用的唯一权威视图：

- 未开放：`state=not_open`（或暂停时 `suspended`），`open=false`，`decision.basis` 为按当前时间实时求值的逐项依据；
- 已发令：`state=open`，`open=true`，`updated_at` 为发令时刻，`decision.frozen=true`，`basis` 是发令瞬间冻结的依据版本；
- 已签署：`state=result_signed`，含 `result_ref` 与结果摘要（发令名单、各船队检查点、退赛时间），`updated_at` 为签署时刻。

### 审计流 `GET /v1/legs/<leg_ref>/events`

返回该赛段全部事件（含接收时间、哈希链字段），供核对与追责。

### 健康检查 `GET /health`
