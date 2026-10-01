"""潮位更正后的已确认行程复核与固定航路改期测试。"""
import pytest

from app.services.rebooking import rebook_fixed_path

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _plan_confirm(client, body):
    r = client.post("/api/trips/plan", json=body)
    assert r.status_code == 200, r.text
    planned = r.json()
    r = client.post("/api/trips/confirm", json=planned)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 纯算法：固定航路改期 DP
# ---------------------------------------------------------------------------

def _mask(pairs, window):
    ok = [False] * (window + 1)
    for d in pairs:
        ok[d] = True
    return ok


def test_rebook_algorithm_keeps_incomparable_pareto_labels():
    # 两个前缀标签互不支配：(changes=1, vec=(0)) 早到，(changes=0, vec=(1)) 晚到。
    # 朴素"同状态只留字典序最小"会丢掉后者，从而漏掉 0 变动解。
    window = 8
    feas0 = _mask({0, 1}, window)
    feas1 = _mask(set(range(window)), window)
    deps, changes = rebook_fixed_path(
        travel_times=[2, 2], baselines=[1, 3],
        feasible_dep=[feas0, feas1], window=window,
    )
    assert deps == (1, 3)
    assert changes == 0


def test_rebook_algorithm_change_count_before_arrival():
    window = 8
    feas0 = _mask({0, 1}, window)
    feas1 = _mask(set(range(window)), window)
    deps, changes = rebook_fixed_path(
        travel_times=[1, 1], baselines=[0, 2],
        feasible_dep=[feas0, feas1], window=window,
    )
    # (0,2) 0 变动抵达 3；(0,1) 与 (1,2) 均为 1 变动，即使更早也不取
    assert deps == (0, 2)
    assert changes == 0


def test_rebook_algorithm_arrival_then_lex():
    window = 6
    feas0 = _mask({0, 1}, window)
    feas1 = _mask({2}, window)
    deps, changes = rebook_fixed_path(
        travel_times=[1, 1], baselines=[5, 5],
        feasible_dep=[feas0, feas1], window=window,
    )
    # 两条解均为 2 变动、均于第 3 分钟抵达：取出发序列字典序小者 (0,2)
    assert deps == (0, 2)
    assert changes == 2


def test_rebook_algorithm_window_boundary_and_no_solution():
    window = 3
    feas = _mask({2}, window)  # dep=2, t=2 -> 抵达 4 > 窗口，不可用
    assert rebook_fixed_path([2], [0], [feas], window) is None

    feas = _mask({1}, window)  # dep=1 -> 抵达 3 == 窗口，允许
    deps, changes = rebook_fixed_path([2], [0], [feas], window)
    assert deps == (1,) and changes == 1


# ---------------------------------------------------------------------------
# API：复核安全 / 首次违规 / 改期
# ---------------------------------------------------------------------------

def _three_leg_world(client):
    """A->B->C->D，各段航时 1；B->C 与 C->D 为浅水（需潮位>=2.5）。"""
    for n in ["A", "B", "C", "D"]:
        add_node(client, n)
    s0 = add_segment(client, "A", "B", 1, depth=99.0)
    s1 = add_segment(client, "B", "C", 1, depth=4.0)
    s2 = add_segment(client, "C", "D", 1, depth=4.0)
    # rev1：第 1..4 分钟低潮，B->C 必须等到第 5 分钟出发
    levels = {0: 3.0, **_level(1, 4, 0.0), **_level(5, 19, 3.0)}
    rev1 = seed_initial_revision(levels)
    return s0, s1, s2, rev1


PLAN_BODY_ACD = {
    "origin": "A", "destination": "D",
    "start_minute": 0, "window_minutes": 20,
    "draft": 6.0, "height": 1.0, "margin": 0.5,
}


def test_review_safe_after_irrelevant_correction(client):
    s0, s1, s2, rev1 = _three_leg_world(client)
    trip = _plan_confirm(client, {**PLAN_BODY_ACD, "revision": rev1})
    # 最早抵达：首段第 4 分钟出发，第 5 分钟随 B->C 续航（在 B 不等待）
    assert [(l["seg_id"], l["start_minute"]) for l in trip["itinerary"]] == \
        [(s0, 4), (s1, 5), (s2, 6)]

    r = client.post("/api/tide/corrections", json={
        "base_revision": rev1, "corrections": {100: 0.0},
    })
    rev2 = r.json()["revision"]

    r = client.post(f"/api/trips/{trip['trip_id']}/review",
                    params={"target_revision": rev2})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["safe"] is True
    assert data["first_violation"] is None
    assert data["rebooked"] is True and data["no_solution"] is False
    assert data["changed_leg_count"] == 0
    assert data["rebooked_arrival_minute"] == 7
    assert [(l["seg_id"], l["start_minute"]) for l in data["rebooked_itinerary"]] == \
        [(s0, 4), (s1, 5), (s2, 6)]


def test_review_unsafe_rebooks_minimizing_changed_legs_before_arrival(client):
    s0, s1, s2, rev1 = _three_leg_world(client)
    trip = _plan_confirm(client, {**PLAN_BODY_ACD, "revision": rev1})

    # rev2：第 6 分钟低潮（原行程末段占用，违规）；第 1..4 分钟涨潮（可提前走）
    r = client.post("/api/tide/corrections", json={
        "base_revision": rev1,
        "corrections": {1: 3.0, 2: 3.0, 3: 3.0, 4: 3.0, 6: 0.0},
    })
    rev2 = r.json()["revision"]

    r = client.post(f"/api/trips/{trip['trip_id']}/review",
                    params={"target_revision": rev2})
    assert r.status_code == 200, r.text
    data = r.json()

    # 原行程不安全：首次违规为末段（leg_index=2）第 6 分钟水深不足
    assert data["safe"] is False
    v = data["first_violation"]
    assert v["leg_index"] == 2 and v["seg_id"] == s2
    assert v["minute"] == 6 and v["reason"] == "DEPTH"
    assert v["depth_required"] == pytest.approx(6.5)
    assert v["depth_available"] == pytest.approx(4.0)
    assert v["depth_ok"] is False

    # 改期：(4,5,7) 仅末段变动（1 个）第 8 分钟到；
    # 虽然 (0,1,2) 第 3 分钟就到，但 3 个航段均变动，不取
    assert data["rebooked"] is True and data["no_solution"] is False
    assert data["changed_leg_count"] == 1
    assert data["rebooked_arrival_minute"] == 8
    assert [l["start_minute"] for l in data["rebooked_itinerary"]] == [4, 5, 7]


def test_review_first_violation_uses_leg_order_and_minute_order(client):
    for n in ["A", "B", "C"]:
        add_node(client, n)
    sa = add_segment(client, "A", "B", 3, depth=4.0)
    sb = add_segment(client, "B", "C", 1, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    body = {
        "origin": "A", "destination": "C",
        "start_minute": 0, "window_minutes": 20,
        "draft": 6.0, "height": 1.0, "margin": 0.5,
        "revision": rev1,
    }
    trip = _plan_confirm(client, body)
    assert [l["start_minute"] for l in trip["itinerary"]] == [0, 3]

    # 首段第 1 分钟与次段第 3 分钟均低潮：必须报首段、且报该段最早违规分钟
    r = client.post("/api/tide/corrections", json={
        "base_revision": rev1, "corrections": {1: 0.0, 3: 0.0},
    })
    rev2 = r.json()["revision"]
    data = client.post(f"/api/trips/{trip['trip_id']}/review",
                       params={"target_revision": rev2}).json()
    v = data["first_violation"]
    assert v["leg_index"] == 0 and v["seg_id"] == sa and v["minute"] == 1


def test_review_bridge_clearance_violation(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", 1, depth=99.0, clearance=5.0)
    # 低潮时桥孔足够（潮位<=1）；高潮净空不足
    rev1 = seed_initial_revision(_level(0, 19, 0.0))
    body = {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 3.0, "margin": 1.0,
        "revision": rev1,
    }
    trip = _plan_confirm(client, body)
    assert trip["itinerary"][0]["start_minute"] == 0

    r = client.post("/api/tide/corrections",
                    json={"base_revision": rev1, "corrections": {0: 3.0}})
    rev2 = r.json()["revision"]
    data = client.post(f"/api/trips/{trip['trip_id']}/review",
                       params={"target_revision": rev2}).json()
    assert data["safe"] is False
    v = data["first_violation"]
    assert v["seg_id"] == seg and v["minute"] == 0 and v["reason"] == "CLEARANCE"
    assert v["clearance_required"] == pytest.approx(4.0)
    assert v["clearance_available"] == pytest.approx(2.0)
    assert v["clearance_ok"] is False and v["depth_ok"] is True


def test_review_no_rebook_solution_keeps_snapshot(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", 1, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    body = {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 6.0, "height": 0.0, "margin": 0.5,
        "revision": rev1,
    }
    trip = _plan_confirm(client, body)

    r = client.post("/api/tide/corrections",
                    json={"base_revision": rev1, "corrections": _level(0, 19, 0.0)})
    rev2 = r.json()["revision"]

    r = client.post(f"/api/trips/{trip['trip_id']}/review",
                    params={"target_revision": rev2})
    data = r.json()
    assert data["safe"] is False
    assert data["rebooked"] is False and data["no_solution"] is True
    assert data["rebooked_itinerary"] == []
    assert data["rebooked_arrival_minute"] is None
    assert data["first_violation"]["leg_index"] == 0

    # 已确认快照不被修改
    saved = client.get(f"/api/trips/{trip['trip_id']}").json()
    assert saved["revision"] == rev1
    assert saved["itinerary"][0]["start_minute"] == 0
    assert len(client.get("/api/trips").json()) == 1


def test_review_defaults_target_to_latest_revision(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", 1, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 6.0, "height": 0.0, "margin": 0.5,
        "revision": rev1,
    })
    r = client.post("/api/tide/corrections",
                    json={"base_revision": rev1, "corrections": {0: 0.0}})
    rev2 = r.json()["revision"]

    # 不带 target_revision -> 取最新
    data = client.post(f"/api/trips/{trip['trip_id']}/review").json()
    assert data["target_revision"] == rev2
    assert data["snapshot_revision"] == rev1


def test_review_errors_unknown_trip_revision_and_missing_tide(client):
    # 未知行程
    r = client.post("/api/trips/999/review")
    assert r.status_code == 404 and r.json()["error"]["code"] == "TRIP_NOT_FOUND"

    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", 1, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev1,
    })

    # 未知目标修订
    r = client.post(f"/api/trips/{trip['trip_id']}/review",
                    params={"target_revision": 999})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TIDE_REVISION_NOT_FOUND"

    # 稀疏修订：窗口内缺潮位 -> 沿用 MISSING_TIDE_DATA 语义
    sparse = seed_initial_revision(_level(0, 9, 3.0), note="稀疏修订")
    r = client.post(f"/api/trips/{trip['trip_id']}/review",
                    params={"target_revision": sparse})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "MISSING_TIDE_DATA"
    assert err["details"]["total_missing"] == 10


def test_review_zero_leg_trip(client):
    add_node(client, "A")
    seed_initial_revision(_level(0, 19, 0.0))
    trip = _plan_confirm(client, {
        "origin": "A", "destination": "A",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
    })
    data = client.post(f"/api/trips/{trip['trip_id']}/review").json()
    assert data["safe"] is True and data["rebooked"] is True
    assert data["rebooked_itinerary"] == []
    assert data["changed_leg_count"] == 0
    assert data["rebooked_arrival_minute"] == 0


# ---------------------------------------------------------------------------
# 演示数据端到端：确认 -> 更正 -> 复核 -> 改期
# ---------------------------------------------------------------------------

def test_review_demo_data_end_to_end(tmp_path, monkeypatch):
    db_file = tmp_path / "seed.db"
    from app import config
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "SEED_DEMO", True)

    from app.database import connect, init_db
    from fastapi.testclient import TestClient
    from app.main import app

    conn = connect()
    init_db(conn)
    conn.close()

    with TestClient(app) as c:
        planned = c.post("/api/trips/plan", json={
            "origin": "A", "destination": "D",
            "start_minute": 0, "window_minutes": 1440,
            "draft": 5.0, "height": 2.0, "margin": 0.5,
        }).json()
        assert planned["revision"] == 1
        assert [l["seg_id"] for l in planned["itinerary"]] == [1, 2, 3]

        trip = c.post("/api/trips/confirm", json=planned).json()
        trip_id = trip["trip_id"]

        # 把航段 3（C->D，深 5m）原占用分钟改为低潮
        occ = list(range(70, 90))
        r = c.post("/api/tide/corrections", json={
            "corrections": {m: 0.0 for m in occ},
            "note": "复核演练：C->D 原占用窗口低潮",
        })
        rev2 = r.json()["revision"]

        r = c.post(f"/api/trips/{trip_id}/review",
                   params={"target_revision": rev2})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["safe"] is False
        assert data["first_violation"]["seg_id"] == 3
        assert data["first_violation"]["minute"] == 70
        assert data["rebooked"] is True
        assert data["no_solution"] is False
        # 原航段顺序不变；前两段可不动，仅 C->D 延后
        assert [l["seg_id"] for l in data["rebooked_itinerary"]] == [1, 2, 3]
        deps = [l["start_minute"] for l in data["rebooked_itinerary"]]
        assert deps[0] == 0 and deps[1] == 30
        assert deps[2] >= 90
