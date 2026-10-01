# 港口潮汐通航行程规划服务

基于 **Python + FastAPI + SQLite** 的潮汐窗口航路规划服务。录入港口节点、单向航段
（正整数分钟航时、静水深度、可选桥底净空）与逐分钟潮位后，给定船舶吃水、水面以上
高度、安全余量及不超过 1440 分钟的规划窗口，计算**最早抵达**行程；同一抵达时刻按
**航段编号序列字典序**择优。潮位以**修订号**版本化管理，确认行程时在同一数据库事务
中复核修订号与行程，过期即冲突且不保存，已确认行程作为不可变快照长期保留。潮位更正
后，可凭已确认行程号与目标修订号**复核**原航路（按原占用分钟逐段校验，给出首次违规
的航段/分钟/水深/净空原因），并在保持原航段顺序、船舶参数、原起始分钟与规划窗口
（仅允许节点重新等待）下求改期行程：变动航段数最少 → 抵达最早 → 出发分钟序列字典序
最优；窗口内无解时返回原行程风险与无解标记，不修改快照。

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
| 复核未知行程 | 404 | `TRIP_NOT_FOUND` |
| 复核目标修订不存在 | 404 | `TIDE_REVISION_NOT_FOUND` |
| 复核窗口内有分钟缺潮位 | 422 | `MISSING_TIDE_DATA`（沿用规划语义） |

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
| POST | `/api/trips/{trip_id}/review?target_revision=` | 潮位更正后复核快照，并求固定航路改期行程 |
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

### 潮位更正后的复核与改期

潮位更正产生新修订后，调度员凭**已确认行程号**（`trip_id`）与**目标潮位修订号**
（`target_revision` 查询参数；缺省取最新修订）调用复核接口。接口为**只读**，
不会修改、覆盖或新建已确认快照。

```
POST /api/trips/{trip_id}/review?target_revision=2
```

服务依次完成：

1. 读取该行程确认时保存的不可变快照（船舶参数、原起始分钟、规划窗口、完整航段）；
2. 按快照中的**原占用分钟**，**保持原航段编号顺序**，逐航段、逐占用分钟校验目标
   修订下的水深与桥高，返回原行程是否安全（`safe`）；不安全时 `first_violation`
   给出**首次**违规的航段序号（`leg_index`，0 基）、`seg_id`、绝对占用 `minute`
   及 `reason`（`DEPTH` / `CLEARANCE` / `DEPTH_AND_CLEARANCE`），并附当分钟潮位、
   需要/可用水深、需要/可用净空与各自是否满足；
3. 同时求**改期行程**：保持原航段编号顺序（不换航段、不绕路）、原船舶参数、
   原起始分钟与规划窗口不变，仅允许船在节点重新等待（各段出发分钟整体平移，
   最后抵达分钟必须仍在窗口内）。择优顺序：
   1. 相对快照**出发分钟发生变动的航段个数**最少（`changed_leg_count`）；
   2. 抵达分钟最早（`rebooked_arrival_minute`）；
   3. 各段出发分钟序列字典序最小。
4. 窗口内存在改期解时 `rebooked=true` 并给出 `rebooked_itinerary`
   （含 `rebooked_total_wait_minutes`）；窗口内无解时 `rebooked=false /
   no_solution=true`，响应仍携带原行程的首次违规风险（`first_violation`），
   已确认快照保持不变。

> 说明：原行程在目标修订下安全时，改期结果即原行程本身——`changed_leg_count=0`。
> 改期只平移出发时间，航段链（首尾节点衔接）仍严格按快照校验。

响应示例（原行程末段低潮，仅末段改期等待）：

```json
{
  "trip_id": 1,
  "snapshot_revision": 1,
  "target_revision": 2,
  "safe": false,
  "first_violation": {
    "leg_index": 2,
    "seg_id": 3,
    "origin": "C", "destination": "D",
    "minute": 70,
    "reason": "DEPTH",
    "tide_level": 0.0,
    "depth_required": 5.5,
    "depth_available": 5.0,
    "depth_ok": false,
    "clearance_required": null,
    "clearance_available": null,
    "clearance_ok": null
  },
  "rebooked": true,
  "no_solution": false,
  "changed_leg_count": 1,
  "rebooked_arrival_minute": 110,
  "rebooked_total_wait_minutes": 20,
  "rebooked_itinerary": [
    {"seg_id": 1, "origin": "A", "destination": "B", "start_minute": 0,  "end_minute": 30,  "travel_time": 30},
    {"seg_id": 2, "origin": "B", "destination": "C", "start_minute": 30, "end_minute": 70,  "travel_time": 40},
    {"seg_id": 3, "origin": "C", "destination": "D", "start_minute": 90, "end_minute": 110, "travel_time": 20}
  ],
  "original_itinerary": [ "...快照原行程，逐字段同上..." ]
}
```

窗口内无改期解时：`"rebooked": false, "no_solution": true,
"rebooked_itinerary": [], "rebooked_arrival_minute": null`，并保留
`first_violation` 供调度员评估原行程风险。

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

# 5) 先确认一个行程（需在更正之前，依据最新修订 #1）
curl -s -X POST $BASE/api/trips/confirm \
  -H 'Content-Type: application/json' -d @/tmp/plan.json    # -> trip_id=1

# 6) 潮位更正后复核已确认行程（缺省目标修订=最新；只读，不改快照）
curl -s -X POST "$BASE/api/trips/1/review"                 # 等价于 ?target_revision=<最新>
curl -s -X POST "$BASE/api/trips/1/review?target_revision=1"   # 指定修订号复核
```

复核演练（让已确认行程末段原占用分钟变成低潮，再看首次违规与改期）：

```bash
# 生成 70..89 分钟全部低潮的更正
python3 - <<'PY'
import json, urllib.request
corr = {str(m): 0.0 for m in range(70, 90)}
req = urllib.request.Request(
    "http://localhost:8000/api/tide/corrections",
    data=json.dumps({"corrections": corr, "note": "C->D 原窗口低潮"}).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
print(json.load(urllib.request.urlopen(req)))
PY

# safe=false，first_violation 指向航段 3 第 70 分钟水深不足；
# 改期仅末段后移（changed_leg_count=1），110 分钟抵达
curl -s -X POST "$BASE/api/trips/1/review" | python3 -m json.tool
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

### 复核与固定航路改期（`app/services/rebooking.py`）

- **复核**：直接按快照航段的原占用区间逐分钟套用同一水深 / 净空判定
（容差 `EPS` 与规划一致），按航段顺序、每段按分钟顺序取**首个**违规。
- **改期**：航路固定为快照中的航段序列 `v0→v1→…→vk`，沿航路做分层 DP。
  第 `k` 层状态为"完成前 `k` 段、到达 `vk` 的某一分钟"所携带的标签
  `(变动航段数 changes, 各段出发分钟序列 vec)`；等待不改变标签，因此扫描出发
  分钟时维护"此前（含）已到达标签"的**帕累托前沿**：标签 A 支配 B 当且仅当
  `changes_A ≤ changes_B` 且 `vec_A ≤ vec_B`（字典序）——后续航段的可行选择只
  取决于所在节点与可出发分钟，与历史无关。`changes` 取值仅 `0..k`，前沿标签数
  不超过航段数，复杂度 O(航段数 × W)。最后按
  `(changes, 抵达分钟, 出发分钟序列)` 取最小者，父指针回溯各段出发分钟。
  注意此处**不能**像规划那样在同状态只留字典序最小序列：变动数才是首要目标，
  `(1, (0,…))` 与 `(0, (1,…))` 互不支配，后者恰是更优的"少改"解。

---

## 6. 项目结构

```
app/
  config.py              # 环境变量配置
  database.py            # SQLite 连接、建表、演示数据初始化
  errors.py              # 统一业务错误
  schemas.py             # Pydantic 模型与入参校验（窗口<=1440、航时>0 等）
  services/planning.py   # 潮汐约束与时间展开 DP 规划算法
  services/rebooking.py  # 快照复核（原占用分钟逐段校验）与固定航路改期 DP
  main.py                # FastAPI 路由（节点/航段/潮位/规划/确认/复核）
tests/                   # pytest 测试（28 个，含择优、缺潮位、事务冲突、复核改期等）
Dockerfile
docker-compose.yml
requirements.txt
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest -q
```
