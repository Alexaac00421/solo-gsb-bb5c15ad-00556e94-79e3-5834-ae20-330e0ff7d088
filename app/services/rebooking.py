"""潮位更正后的已确认行程复核与固定航路改期。

复核：读取不可变确认快照，按快照中的**原占用分钟**，逐航段（保持原航段
编号顺序）、逐占用分钟校验**目标潮位修订**下的水深与桥高约束，返回原行程
是否安全；不安全时给出首次违规的航段、分钟及水深 / 净空原因。

改期：保持
  * 原航段编号顺序（同一条航路，不换航段、不绕路），
  * 原船舶参数（吃水 / 水面以上高度 / 安全余量），
  * 原起始分钟与规划窗口，
仅允许船在节点重新等待（各段出发分钟整体平移）。按以下字典序求最优：
  1. 相对快照出发分钟发生变动的航段**个数**最少；
  2. 抵达分钟最早；
  3. 各段出发分钟序列字典序最小。

算法：沿固定航路分层 DP。第 k 层状态为"完成前 k 段后到达 v_k 的某一分钟"
所携带的标签 (变动航段数 changes, 各段出发分钟序列 vec)。等待不改变标签，
故扫描出发分钟时维护一个"此刻之前（含）已到达"标签的帕累托前沿：
标签 A 支配 B 当且仅当 changes_A ≤ changes_B 且 vec_A ≤ vec_B（未来航段的
可行选择只取决于所在节点与可出发分钟，与历史无关）。changes 取值仅
0..k，故每层前沿标签数不超过航段数；复杂度 O(航段数 × 窗口)。
"""
from dataclasses import dataclass
from typing import Optional

from .. import config
from ..errors import ApiError, tide_missing
from .planning import (
    Segment,
    _build_departure_feasible,
    _load_segments,
    _load_tide,
)


# ---- 快照一致性 ----------------------------------------------------------------

def snapshot_inconsistent(trip_id: int) -> ApiError:
    return ApiError(
        409,
        "SNAPSHOT_INCONSISTENT",
        f"行程 #{trip_id} 的快照与当前航段数据不一致，无法复核（已确认快照不会被修改）",
        {"trip_id": trip_id},
    )


def _validated_legs(trip: dict, segs: dict[int, Segment]) -> list[dict]:
    """校验快照航段链与当前航段表一致，返回原航段列表。"""
    legs = trip["itinerary"]
    node = trip["origin"]
    prev_end: Optional[int] = None
    for leg in legs:
        sid = leg["seg_id"]
        seg = segs.get(sid)
        if seg is None or seg.origin != node or seg.destination != leg["destination"]:
            raise snapshot_inconsistent(trip["trip_id"])
        if leg["origin"] != seg.origin:
            raise snapshot_inconsistent(trip["trip_id"])
        if leg["end_minute"] - leg["start_minute"] != seg.travel_time \
                or leg["travel_time"] != seg.travel_time:
            raise snapshot_inconsistent(trip["trip_id"])
        if prev_end is not None and leg["start_minute"] != prev_end:
            raise snapshot_inconsistent(trip["trip_id"])
        prev_end = leg["end_minute"]
        node = seg.destination
    if node != trip["destination"]:
        raise snapshot_inconsistent(trip["trip_id"])
    return legs


# ---- 原行程安全性校验 -----------------------------------------------------------

def _first_violation(
    legs: list[dict],
    segs: dict[int, Segment],
    tide: dict[int, float],
    draft: float,
    height: float,
    margin: float,
) -> Optional[dict]:
    """按航段顺序、每段按占用分钟顺序，返回首个违规；全程安全返回 None。"""
    eps = config.EPS
    for idx, leg in enumerate(legs):
        seg = segs[leg["seg_id"]]
        for minute in range(leg["start_minute"], leg["end_minute"]):
            level = tide[minute]
            depth_required = draft + margin
            depth_available = seg.depth + level
            depth_ok = depth_available + eps >= depth_required

            clearance_required: Optional[float] = None
            clearance_available: Optional[float] = None
            clearance_ok: Optional[bool] = None
            if seg.clearance is not None:
                clearance_required = height + margin
                clearance_available = seg.clearance - level
                clearance_ok = clearance_available + eps >= clearance_required

            if not depth_ok or clearance_ok is False:
                if not depth_ok and clearance_ok is False:
                    reason = "DEPTH_AND_CLEARANCE"
                elif not depth_ok:
                    reason = "DEPTH"
                else:
                    reason = "CLEARANCE"
                return {
                    "leg_index": idx,
                    "seg_id": seg.seg_id,
                    "origin": seg.origin,
                    "destination": seg.destination,
                    "minute": minute,
                    "reason": reason,
                    "tide_level": level,
                    "depth_required": depth_required,
                    "depth_available": depth_available,
                    "depth_ok": depth_ok,
                    "clearance_required": clearance_required,
                    "clearance_available": clearance_available,
                    "clearance_ok": clearance_ok,
                }
    return None


# ---- 固定航路改期 DP ------------------------------------------------------------

@dataclass
class _Label:
    changes: int           # 已完成航段中，出发分钟相对快照变动的个数
    vec: tuple             # 已完成航段的出发分钟（局部）序列
    dep: Optional[int]     # 本层航段的出发分钟（局部）
    prev: Optional["_Label"]


def _insert(bucket: list[_Label], cand: _Label) -> list[_Label]:
    """把 cand 并入同一状态的标签桶，剔除被支配标签。

    A 支配 B：changes_A ≤ changes_B 且 vec_A 字典序 ≤ vec_B。
    """
    for b in bucket:
        if b.changes <= cand.changes and b.vec <= cand.vec:
            return bucket  # 已存在不劣于 cand 的标签
    kept = [
        b for b in bucket
        if not (cand.changes <= b.changes and cand.vec <= b.vec)
    ]
    kept.append(cand)
    return kept


def rebook_fixed_path(
    travel_times: list[int],
    baselines: list[int],
    feasible_dep: list[list[bool]],
    window: int,
) -> Optional[tuple[tuple[int, ...], int]]:
    """固定航路改期纯算法。

    travel_times : 各航段航时（分钟）
    baselines    : 快照中各航段出发分钟（相对起始分钟的局部分钟）
    feasible_dep : 每个航段每个局部出发分钟是否全程满足水深 / 净空
    window       : 窗口长度 W；最后抵达分钟必须 ≤ W（占用分钟最晚 W-1）

    返回 (各段局部出发分钟, 变动航段数)；窗口内无解返回 None。
    """
    n = len(travel_times)
    if n == 0:
        return (), 0

    # cur[到达局部分钟] -> 该分钟到达 v_k 的帕累托标签桶
    cur: dict[int, list[_Label]] = {0: [_Label(0, (), None, None)]}

    for k in range(n):
        t = travel_times[k]
        base = baselines[k]
        feas = feasible_dep[k]
        frontier: list[_Label] = []
        nxt: dict[int, list[_Label]] = {}

        for d in range(0, window - t + 1):
            # 恰在 d 到达的标签入前沿；更早到达的标签靠在节点等待同样可于 d 出发
            for lab in cur.get(d, ()):
                frontier = _insert(frontier, lab)
            if frontier and feas[d]:
                j = d + t
                bucket = nxt.setdefault(j, [])
                for lab in frontier:
                    cand = _Label(
                        lab.changes + (1 if d != base else 0),
                        lab.vec + (d,),
                        d,
                        lab,
                    )
                    nxt[j] = _insert(bucket, cand)
        cur = nxt
        if not cur:
            return None

    # 择优：变动个数 -> 抵达分钟 -> 出发分钟序列
    best: Optional[tuple[tuple, _Label]] = None
    for arr, bucket in cur.items():
        for lab in bucket:
            key = (lab.changes, arr, lab.vec)
            if best is None or key < best[0]:
                best = (key, lab)
    assert best is not None

    deps: list[int] = []
    lab = best[1]
    while lab.dep is not None:
        deps.append(lab.dep)
        lab = lab.prev  # type: ignore[assignment]
    deps.reverse()
    return tuple(deps), best[1].changes


# ---- 复核入口（读操作，不修改快照） ----------------------------------------------

def review_trip(conn, trip: dict, target_revision: Optional[int]) -> dict:
    """对已确认行程快照在目标潮位修订下做安全复核与固定航路改期。

    未知修订 -> TIDE_REVISION_NOT_FOUND (404)
    窗口内缺潮位 -> MISSING_TIDE_DATA (422)
    本函数不执行任何写库操作。
    """
    start = trip["start_minute"]
    window = trip["window_minutes"]
    horizon = start + window
    draft, height, margin = trip["draft"], trip["height"], trip["margin"]

    # 1) 目标修订：显式指定或取最新（语义同规划接口）
    if target_revision is None:
        row = conn.execute(
            "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ApiError(404, "TIDE_REVISION_NOT_FOUND",
                           "系统中不存在任何潮位修订，无法复核")
        target_revision = row["revision"]
    else:
        row = conn.execute(
            "SELECT 1 FROM tide_revisions WHERE revision = ?", (target_revision,)
        ).fetchone()
        if row is None:
            raise ApiError(404, "TIDE_REVISION_NOT_FOUND",
                           f"潮位修订不存在: {target_revision}",
                           {"revision": target_revision})

    # 2) 快照航段链与当前航段表一致（航段不可经接口删除，此处为防御性校验）
    segs = _load_segments(conn)
    legs = _validated_legs(trip, segs)

    # 3) 窗口潮位：改期可能在窗口内任意分钟出发，缺任一分钟沿用规划错误语义
    tide = _load_tide(conn, target_revision, start, horizon - 1)
    missing = [m for m in range(start, horizon) if m not in tide]
    if missing:
        raise tide_missing(missing)

    # 4) 按原占用分钟逐段校验目标修订
    violation = _first_violation(legs, segs, tide, draft, height, margin)
    safe = violation is None

    # 5) 固定航路改期：原航段顺序、船舶参数、起始分钟与窗口不变，允许节点等待
    seg_list = [segs[l["seg_id"]] for l in legs]
    feasible = _build_departure_feasible(
        seg_list, tide, start, window, draft, height, margin
    )
    travel_times = [s.travel_time for s in seg_list]
    baselines = [l["start_minute"] - start for l in legs]
    feasible_dep = [feasible[s.seg_id] for s in seg_list]

    solved = rebook_fixed_path(travel_times, baselines, feasible_dep, window)

    rebooked_itinerary: list = []
    changed_leg_count = 0
    rebooked_arrival: Optional[int] = None
    rebooked_wait: Optional[int] = None
    no_solution = solved is None

    if solved is not None:
        deps, changed_leg_count = solved
        leg_dicts = []
        for leg, seg, dep in zip(legs, seg_list, deps):
            dep_abs = start + dep
            leg_dicts.append({
                "seg_id": seg.seg_id,
                "origin": seg.origin,
                "destination": seg.destination,
                "start_minute": dep_abs,
                "end_minute": dep_abs + seg.travel_time,
                "travel_time": seg.travel_time,
            })
        rebooked_itinerary = leg_dicts
        rebooked_arrival = (start + deps[-1] + travel_times[-1]) if legs else start
        sailing = sum(travel_times)
        rebooked_wait = rebooked_arrival - start - sailing

    return {
        "trip_id": trip["trip_id"],
        "snapshot_revision": trip["revision"],
        "target_revision": target_revision,
        "safe": safe,
        "first_violation": violation,
        "rebooked": solved is not None,
        "no_solution": no_solution,
        "changed_leg_count": changed_leg_count,
        "rebooked_arrival_minute": rebooked_arrival,
        "rebooked_total_wait_minutes": rebooked_wait,
        "rebooked_itinerary": rebooked_itinerary,
        "original_itinerary": legs,
    }
