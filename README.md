# 港口潮汐通航行程规划服务

基于 **Python + FastAPI + SQLite** 的潮汐窗口航路规划服务。录入港口节点、单向航段
（正整数分钟航时、静水深度、可选桥底净空）与逐分钟潮位后，给定船舶吃水、水面以上
高度、安全余量及不超过 1440 分钟的规划窗口，计算**最早抵达**行程；同一抵达时刻按
**航段编号序列字典序**择优。潮位以**修订号**版本化管理，确认行程时在同一数据库事务
中复核修订号与行程，过期即冲突且不保存，已确认行程作为不可变快照长期保留。

---

## 1. 启动

### 方式一：Docker Compose（推荐，一键启动并初始化数据库）

```bash
docker compose up --build -d
```

服务启动时自动：

1. 在命名卷 `tide-data` 中创建 SQLite 数据库 `/data/tide.db`；
2. 建表；
3. 若库为空（`SEED_DEMO=true`，默认），写入演示数据：
   - 节点 `A（外锚地）、B、C、D（内港码头）`
   - 航段 `1:A→B(30min,深6m)`、`2:B→C(40min,深4m,净空8m)`、`3:C→D(20min,深5m)`、`4:B→D(90min,深3.5m)`
   - 潮位修订 `#1`：分钟 `0..1439`，潮位 `2.0 + 1.5·sin(2π·m/720)`（米）

查看日志 / 停止：

```bash
docker compose logs -f
docker compose down            # 停止（保留数据卷）
docker compose down -v         # 停止并删除数据库卷（下次启动重新初始化）
```

### 方式二：Docker

```bash
docker build -t tide-planner .
docker run -d --name tide-planner -p 8000:8000 \
  -v "$PWD/data:/data" -e DB_PATH=/data/tide.db tide-planner
```

### 方式三：本地 Python（3.11+）

```bash
pip install -r requirements.txt
DB_PATH=./tide.db SEED_DEMO=true uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 地址与端口

| 项目 | 值 |
| --- | --- |
| 服务地址 | http://localhost:8000 |
| 健康检查 | http://localhost:8000/health |
| Swagger UI（可在线调用） | http://localhost:8000/docs |
| OpenAPI 文档 | http://localhost:8000/openapi.json |

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/tide.db` | SQLite 数据库文件路径，所在目录自动创建 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `SEED_DEMO` | `true` | 空库启动时是否写入演示节点/航段/初始潮位修订 |
| `MAX_WINDOW` | `1440` | 规划窗口上限（分钟） |

---

## 2. 领域规则

- **节点**：港口位置点。**航段为单向**，航时必须为**正整数**分钟；可设桥底净空（不设表示无桥）。
- **时间轴**：绝对"分钟"（非负整数），与潮位表的 `minute` 对齐。规划窗口为
  `[start_minute, start_minute + window_minutes)`，长度 ≤ 1440。
- **等待**：船可在任一节点的任意整分钟等待（等待不占用水深/净空资源）。
- **航段占用**：于整分钟 `d` 进入、航时 `t`，则占用 `d, d+1, …, d+t-1` 共 `t` 分钟，
  于 `d+t` 抵达。占用的**每一分钟**都必须同时满足：
  - 水深：`静水深度 + 潮位 ≥ 吃水 + 安全余量`
  - 桥高（仅当该航段有桥）：`桥底净空 − 潮位 ≥ 水面以上高度 + 安全余量`
- **择优**：到达分钟最小；同一到达分钟，按航段编号（`seg_id`，自增）组成的序列做
  字典序比较，序列小者优先（先比较首段编号，相同再比较下一段）。
- **潮位修订**：修订号自增、内容不可变。"更正"以某修订为基础全量继承、仅覆盖给定分钟，
  生成一个**新修订**，旧修订永久保留。
- **规划**：默认使用**最新**修订，也可显式指定修订号。窗口内任何一分钟缺少潮位即报错。
- **确认**：请求必须携带规划所据 `revision`。服务在单个 SQLite 事务（`BEGIN IMMEDIATE`）中：
  1. 检查该修订是否仍为最新——否则 `409 REVISION_STALE`，**不保存**；
  2. 依据该修订与请求参数**重新计算**并与提交的行程逐段比对——不一致则
     `409 PLAN_MISMATCH`，**不保存**；
  3. 复核通过后保存**当时的结果快照**（修订号、参数、到达分钟、完整行程 JSON）。
     之后再发生潮位更正不影响已确认行程。

### 错误响应格式

```json
{ "error": { "code": "NODE_NOT_FOUND", "message": "未知节点: ZZ", "details": {"node_id": "ZZ"} } }
```

| 场景 | HTTP | code |
| --- | --- | --- |
| 起点或终点未知 | 404 | `NODE_NOT_FOUND` |
| 指定潮位修订不存在 | 404 | `TIDE_REVISION_NOT_FOUND` |
| 窗口内有分钟缺潮位 | 422 | `MISSING_TIDE_DATA`（`details` 给出缺失分钟列表与数量） |
| 窗口内无可行行程 | 404 | `NO_FEASIBLE_ITINERARY` |
| 参数校验失败（窗口>1440、航时非正等） | 422 | `VALIDATION_ERROR` |
| 确认时修订已过期 | 409 | `REVISION_STALE` |
| 确认时行程复核不一致 | 409 | `PLAN_MISMATCH` |

---

## 3. HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/nodes` | 录入节点 |
| GET | `/api/nodes` | 节点列表 |
| POST | `/api/segments` | 录入单向航段 |
| GET | `/api/segments` | 航段列表（按编号排序） |
| GET | `/api/tide/revisions` | 潮位修订列表 |
| GET | `/api/tide/revisions/{rev}?from_minute=&to_minute=` | 取某修订逐分钟潮位 |
| POST | `/api/tide/corrections` | 潮位更正，生成新修订 |
| POST | `/api/trips/plan` | 规划最早抵达行程 |
| POST | `/api/trips/confirm` | 携带修订号确认行程（事务复核） |
| GET | `/api/trips` | 已确认行程列表 |
| GET | `/api/trips/{trip_id}` | 已确认行程快照详情 |
| GET | `/health` | 健康检查 |

### 规划请求 / 响应示例

请求：

```json
POST /api/trips/plan
{
  "origin": "A",
  "destination": "D",
  "start_minute": 0,
  "window_minutes": 1440,
  "draft": 5.0,
  "height": 2.0,
  "margin": 0.5,
  "revision": 1
}
```

响应（每段给出 `seg_id` 与起止分钟；`end_minute` 为抵达下一节点的分钟）：

```json
{
  "revision": 1,
  "origin": "A",
  "destination": "D",
  "start_minute": 0,
  "window_minutes": 1440,
  "draft": 5.0,
  "height": 2.0,
  "margin": 0.5,
  "arrival_minute": 90,
  "total_wait_minutes": 0,
  "itinerary": [
    {"seg_id": 1, "origin": "A", "destination": "B", "start_minute": 0,  "end_minute": 30, "travel_time": 30},
    {"seg_id": 2, "origin": "B", "destination": "C", "start_minute": 30, "end_minute": 70, "travel_time": 40},
    {"seg_id": 3, "origin": "C", "destination": "D", "start_minute": 70, "end_minute": 90, "travel_time": 20}
  ]
}
```

### 确认请求 / 响应示例

将规划响应**原样回填**（含 `revision` 与每段 `seg_id/起止分钟`）即可：

```json
POST /api/trips/confirm
{
  "revision": 1, "origin": "A", "destination": "D",
  "start_minute": 0, "window_minutes": 1440,
  "draft": 5.0, "height": 2.0, "margin": 0.5,
  "itinerary": [
    {"seg_id": 1, "origin": "A", "destination": "B", "start_minute": 0,  "end_minute": 30, "travel_time": 30},
    {"seg_id": 2, "origin": "B", "destination": "C", "start_minute": 30, "end_minute": 70, "travel_time": 40},
    {"seg_id": 3, "origin": "C", "destination": "D", "start_minute": 70, "end_minute": 90, "travel_time": 20}
  ]
}
```

成功返回 `201`，响应为含 `trip_id / revision / arrival_minute / itinerary / confirmed_at`
的不可变快照；过期返回 `409 REVISION_STALE`。

---

## 4. 调用示例（curl）

```bash
BASE=http://localhost:8000

# 健康检查
curl -s $BASE/health

# 查看演示航段
curl -s $BASE/api/segments

# 1) 规划
curl -s -X POST $BASE/api/trips/plan \
  -H 'Content-Type: application/json' \
  -d '{"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,
       "draft":5.0,"height":2.0,"margin":0.5,"revision":1}' | tee /tmp/plan.json

# 2) 确认（规划响应可直接作为确认请求体）
curl -s -X POST $BASE/api/trips/confirm \
  -H 'Content-Type: application/json' -d @/tmp/plan.json

# 3) 潮位更正：把第 100、101 分钟改为低潮 0.2m（继承最新修订，生成新修订）
curl -s -X POST $BASE/api/tide/corrections \
  -H 'Content-Type: application/json' \
  -d '{"corrections":{"100":0.2,"101":0.2},"note":"人工低潮更正"}'

# 4) 用旧修订号再次确认 -> 409 REVISION_STALE
curl -i -X POST $BASE/api/trips/confirm \
  -H 'Content-Type: application/json' -d @/tmp/plan.json
```

Python 示例（规划 → 确认 → 模拟过期冲突）：

```python
import requests

B = "http://localhost:8000"

plan = requests.post(f"{B}/api/trips/plan", json={
    "origin": "A", "destination": "D",
    "start_minute": 0, "window_minutes": 1440,
    "draft": 5.0, "height": 2.0, "margin": 0.5,
}).json()
print("依据修订", plan["revision"], "第", plan["arrival_minute"], "分钟抵达")

r = requests.post(f"{B}/api/trips/confirm", json=plan)   # 规划响应即确认请求体
r.raise_for_status()
print("已确认行程 trip_id =", r.json()["trip_id"])

# 期间潮位被更正 -> 用旧结果再次确认
requests.post(f"{B}/api/tide/corrections", json={"corrections": {100: 0.2}})
r = requests.post(f"{B}/api/trips/confirm", json=plan)
assert r.status_code == 409 and r.json()["error"]["code"] == "REVISION_STALE"
print("过期冲突：", r.json()["error"]["message"])
```

录入自定义数据：

```bash
curl -s -X POST $BASE/api/nodes    -H 'Content-Type: application/json' -d '{"node_id":"E","name":"新码头"}'
curl -s -X POST $BASE/api/segments -H 'Content-Type: application/json' \
  -d '{"origin":"D","destination":"E","travel_time":15,"depth":4.2,"clearance":7.0}'
```

---

## 5. 算法说明

窗口不超过 1440 分钟，采用**时间展开动态规划**（实现见 `app/services/planning.py`）：

1. 校验节点与修订；取窗口内全部潮位，缺任一分钟立即报 `MISSING_TIDE_DATA`；
2. 对每条航段用"不可行分钟前缀和"预处理：任意进入分钟对应的占用区间是否全程满足
   水深/净空，查询为 O(1)；
3. 状态 `(节点, 分钟)` 只保留一个值：到达该状态的航段编号序列中**字典序最小**者。
   转移有两种：
   - 等待：同一节点序列不变推进到下一分钟；
   - 出发：在该节点的整分钟进入可行航段，序列追加该航段编号；
4. 按分钟递增处理，终点第一次出现的分钟即最早抵达，其序列即同到达时刻字典序最优；
5. 回溯父指针，输出每段的进入/抵达分钟。

剪枝依据：若序列 `s1 < s2`（字典序），则对任意后缀 `x` 恒有 `s1+x < s2+x`，
故同一 `(节点, 分钟)` 只需保留最小序列，不会丢失最优解。复杂度 O(W·E)（W≤1440）。

---

## 6. 项目结构

```
app/
  config.py             # 环境变量配置
  database.py           # SQLite 连接、建表、演示数据初始化
  errors.py             # 统一业务错误
  schemas.py            # Pydantic 模型与入参校验（窗口<=1440、航时>0 等）
  services/planning.py  # 潮汐约束与时间展开 DP 规划算法
  main.py               # FastAPI 路由（节点/航段/潮位/规划/确认）
tests/                  # pytest 测试（15 个，含择优、缺潮位、事务冲突等）
Dockerfile
docker-compose.yml
requirements.txt
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest -q
```
