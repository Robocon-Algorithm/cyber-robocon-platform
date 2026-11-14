"""Pure legal-action calculator shared by SDK and authoritative engine.

Segments use integer milliseconds relative to the action start. Intermediate
positions remain floats; only time is rounded up. Each straight edge starts and
ends at rest, so corners include real braking and a separate heading turn.
"""
from __future__ import annotations

from copy import deepcopy
import heapq
import math
from typing import Any

from .config import DEFAULT_PARAMETERS
from .map import tower_access_error

Pose = dict[str, Any]
_ACTION_FIELDS: dict[str, dict[str, type]] = {
    "move_to": {"point_id": str},
    "traverse": {"connection_id": str, "destination_layer": str},
    "pick_stack": {"stack_id": str, "expected_version": int},
    "build_stack": {"site_id": str, "stack_id": str, "expected_version": int},
    "put_down_stack": {"stack_id": str, "bay_id": str, "expected_version": int},
    "pick": {"item_ids": list},
    "build": {"site_id": str, "item_id": str, "level": str, "expected_version": int},
    "flip_cap": {"site_id": str, "expected_version": int},
    "receive_ball": {},
    "place_ball": {},
    "put_down": {"item_id": str, "slot_id": str, "expected_version": int},
    "take_from": {"item_id": str, "source_id": str, "expected_version": int},
}


def travel_profile(distance_mm: float, speed_mm_s: float, acceleration_mm_s2: float,
                   braking_mm_s2: float | None = None) -> dict[str, float | int]:
    """Rest-to-rest triangular/trapezoidal profile, with no fixed start fee."""
    brake = acceleration_mm_s2 if braking_mm_s2 is None else braking_mm_s2
    if not all(math.isfinite(v) and v > 0 for v in (speed_mm_s, acceleration_mm_s2, brake)):
        raise ValueError("speed and accelerations must be finite and positive")
    if not math.isfinite(distance_mm) or distance_mm < 0:
        raise ValueError("distance must be finite and non-negative")
    peak = min(speed_mm_s, math.sqrt(2 * distance_mm / (1 / acceleration_mm_s2 + 1 / brake)))
    ta, td = peak / acceleration_mm_s2, peak / brake
    cruise_distance = max(0., distance_mm - peak * (ta + td) / 2)
    tc = cruise_distance / peak if peak else 0.
    return {"distance_mm": distance_mm, "max_speed_mm_s": peak,
            "acceleration_mm_s2": acceleration_mm_s2, "braking_mm_s2": brake,
            "acceleration_ms": ta * 1000, "cruise_ms": tc * 1000,
            "deceleration_ms": td * 1000, "duration_ms": math.ceil((ta + tc + td) * 1000 - 1e-9)}


def _heading_delta(a: float, b: float) -> float:
    return (b - a + math.pi * 1000) % (math.tau * 1000) - math.pi * 1000


def point_pose(point: dict[str, Any]) -> Pose:
    return {key: point.get(key, 0) for key in ("x_mm", "y_mm", "z_mm", "heading_mrad", "layer")} | {
        "workpoint_id": point.get("id", point.get("workpoint_id"))}


def _available(value: dict[str, Any], team: str) -> bool:
    return value.get("owner") in (None, team)


def _distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    return math.hypot(b["x_mm"] - a["x_mm"], b["y_mm"] - a["y_mm"])


def _on_edge(pose: Pose, a: Pose, b: Pose) -> bool:
    dx, dy = b["x_mm"] - a["x_mm"], b["y_mm"] - a["y_mm"]
    px, py = pose["x_mm"] - a["x_mm"], pose["y_mm"] - a["y_mm"]
    length_squared = dx * dx + dy * dy
    if not length_squared:
        return _distance(pose, a) < 0.01
    along = (px * dx + py * dy) / length_squared
    return -1e-9 <= along <= 1 + 1e-9 and abs(dx * py - dy * px) < 0.01 * math.sqrt(length_squared)


def _crosses_l2(a: Pose, b: Pose, map_data: dict[str, Any]) -> bool:
    """Clip against the raised platform's open footprint on the L1 plane."""
    bounds = map_data.get("layers", {}).get("L2", {}).get("bounds")
    if a["layer"] != "L1" or not bounds:
        return False
    enter, leave = 0., 1.
    for axis, low, high in (("x_mm", bounds[0], bounds[2]), ("y_mm", bounds[1], bounds[3])):
        start = a[axis]
        delta = b[axis] - start
        if not delta:
            if not low < start < high:
                return False
            continue
        x, y = sorted(((low - start) / delta, (high - start) / delta))
        enter, leave = max(enter, x), min(leave, y)
        if enter >= leave:
            return False
    return enter < leave


def route(map_data: dict[str, Any], pose: Pose, target: str, team: str) -> list[Pose] | None:
    """Shortest same-layer graph route; stopped positions must lie on legal edges."""
    points = map_data.get("points", map_data.get("workpoints", {}))
    if target not in points or not _available(points[target], team):
        return None
    layer = pose["layer"]
    allowed = {pid: p for pid, p in points.items() if p["layer"] == layer and _available(p, team)}
    if target not in allowed:
        return None
    graph: dict[str, list[tuple[str, float]]] = {pid: [] for pid in allowed}
    edges: list[tuple[str, str, bool]] = []
    for edge in map_data.get("edges", []):
        a, b = edge["a"], edge["b"]
        if a in graph and b in graph and _available(edge, team) and not _crosses_l2(points[a], points[b], map_data):
            graph[a].append((b, _distance(points[a], points[b])))
            bidirectional = edge.get("bidirectional", True)
            if bidirectional:
                graph[b].append((a, _distance(points[a], points[b])))
            edges.append((a, b, bidirectional))
    start = "__current_pose__"
    graph[start] = []
    declared = pose.get("workpoint_id")
    if declared in allowed and _distance(pose, allowed[declared]) < 0.01:
        graph[start].append((declared, 0.))
    else:
        # No nearest-node shortcut: a cancelled robot reconnects to its actual edge.
        for a, b, bidirectional in edges:
            if _on_edge(pose, points[a], points[b]):
                graph[start].append((b, _distance(pose, points[b])))
                if bidirectional:
                    graph[start].append((a, _distance(pose, points[a])))
        for pid, point in allowed.items():
            if _distance(pose, point) < 0.01:
                graph[start].append((pid, 0.))
    queue = [(0., start)]
    distances = {start: 0.}
    previous: dict[str, str] = {}
    while queue:
        cost, node = heapq.heappop(queue)
        if cost != distances.get(node):
            continue
        if node == target:
            ids = [node]
            while ids[-1] != start:
                ids.append(previous[ids[-1]])
            return [deepcopy(pose)] + [point_pose(points[pid]) for pid in reversed(ids[:-1])]
        for neighbor, dist in graph[node]:
            new = cost + dist
            if new < distances.get(neighbor, math.inf):
                distances[neighbor] = new
                previous[neighbor] = node
                heapq.heappush(queue, (new, neighbor))
    return None


def motion_segments(path: list[Pose], parameters: dict[str, Any], cargo: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Graph junctions are not compulsory stops. Merge collinear forward edges;
    # real corners still stop, turn, and accelerate with the public model.
    compact: list[Pose] = []
    for point in path:
        if compact and _distance(compact[-1], point) < 1e-8:
            compact[-1] = point
            continue
        while len(compact) >= 2:
            a, b = compact[-2], compact[-1]
            u = (b["x_mm"] - a["x_mm"], b["y_mm"] - a["y_mm"])
            v = (point["x_mm"] - b["x_mm"], point["y_mm"] - b["y_mm"])
            if abs(u[0] * v[1] - u[1] * v[0]) > 1e-6 or u[0] * v[0] + u[1] * v[1] <= 0:
                break
            compact.pop()
        compact.append(point)
    # Keep the actual starting heading, not a graph node's nominal heading.
    if compact:
        compact[0] = deepcopy(compact[0]) | {"heading_mrad": path[0]["heading_mrad"]}
    path = compact
    speed_key = "ball_speed_mm_s" if any(i["kind"] == "ball" for i in cargo) else (
        "loaded_speed_mm_s" if cargo else "empty_speed_mm_s")
    speed = parameters[speed_key]
    segments: list[dict[str, Any]] = []
    elapsed = 0
    current = deepcopy(path[0])
    for destination in path[1:]:
        dist = _distance(current, destination)
        if dist < 1e-8:
            current = deepcopy(destination) | {"heading_mrad": current["heading_mrad"]}
            continue
        heading = math.atan2(destination["y_mm"] - current["y_mm"],
                             destination["x_mm"] - current["x_mm"]) * 1000
        delta = _heading_delta(current["heading_mrad"], heading)
        turn_ms = math.ceil(abs(delta) / (math.pi * 500) * parameters["turn_90_ms"] - 1e-9)
        if turn_ms:
            turned = deepcopy(current) | {"heading_mrad": heading}
            segments.append({"kind": "turn", "start_ms": elapsed, "end_ms": elapsed + turn_ms,
                             "from_pose": deepcopy(current), "to_pose": turned, "delta_mrad": delta})
            elapsed += turn_ms
            current = turned
        profile = travel_profile(dist, speed, parameters["acceleration_mm_s2"], parameters["braking_mm_s2"])
        end = deepcopy(destination) | {"heading_mrad": heading}
        segments.append({"kind": "move", "start_ms": elapsed, "end_ms": elapsed + profile["duration_ms"],
                         "from_pose": deepcopy(current), "to_pose": end, **profile})
        elapsed += int(profile["duration_ms"])
        current = end
    return segments


def motion_at(segments: list[dict[str, Any]], elapsed_ms: int | float) -> dict[str, Any]:
    """Position, speed and heading from the very same curve used in quotes."""
    if not segments:
        return {"pose": None, "speed_mm_s": 0., "vx_mm_s": 0., "vy_mm_s": 0., "angular_speed_mrad_s": 0.}
    for segment in segments:
        if elapsed_ms < segment["end_ms"]:
            break
    else:
        return {"pose": deepcopy(segments[-1]["to_pose"]), "speed_mm_s": 0.,
                "vx_mm_s": 0., "vy_mm_s": 0., "angular_speed_mrad_s": 0.}
    source, dest = segment["from_pose"], segment["to_pose"]
    duration = segment["end_ms"] - segment["start_ms"]
    local_ms = max(0., elapsed_ms - segment["start_ms"])
    fraction = min(1., local_ms / duration) if duration else 1.
    pose = deepcopy(source)
    speed = angular = 0.
    kind = segment["kind"]
    if kind == "move":
        t = local_ms / 1000
        ta, tc = segment["acceleration_ms"] / 1000, segment["cruise_ms"] / 1000
        a, v, b = segment["acceleration_mm_s2"], segment["max_speed_mm_s"], segment["braking_mm_s2"]
        if t < ta:
            distance, speed = .5 * a * t * t, a * t
        elif t < ta + tc:
            distance, speed = .5 * v * ta + v * (t - ta), v
        else:
            dt = min(t - ta - tc, segment["deceleration_ms"] / 1000)
            distance, speed = .5 * v * ta + v * tc + v * dt - .5 * b * dt * dt, max(0., v - b * dt)
        fraction = min(1., distance / segment["distance_mm"]) if segment["distance_mm"] else 1.
    elif kind == "brake":
        t = local_ms / 1000
        v, b = segment["initial_speed_mm_s"], segment["braking_mm_s2"]
        distance = v * t - .5 * b * t * t
        speed = max(0., v - b * t)
        fraction = min(1., max(0., distance / segment["distance_mm"])) if segment["distance_mm"] else 1.
    elif kind == "turn":
        pose["heading_mrad"] = source["heading_mrad"] + segment["delta_mrad"] * fraction
        angular = segment["delta_mrad"] / (duration / 1000)
    if kind in ("move", "brake", "traverse"):
        for key in ("x_mm", "y_mm", "z_mm"):
            pose[key] = source[key] + (dest[key] - source[key]) * fraction
    if elapsed_ms > 0 and kind in ("move", "turn", "brake", "traverse"):
        pose["workpoint_id"] = None
    if kind == "traverse":
        pose["layer"] = source["layer"]
    heading = pose["heading_mrad"] / 1000
    return {"pose": pose, "speed_mm_s": speed, "vx_mm_s": speed * math.cos(heading),
            "vy_mm_s": speed * math.sin(heading), "angular_speed_mrad_s": angular,
            "segment_kind": kind, "segment_progress": fraction}


def pose_at(segments: list[dict[str, Any]], elapsed_ms: int | float) -> Pose | None:
    return motion_at(segments, elapsed_ms)["pose"]


def braking_preview(observation: dict[str, Any]) -> tuple[Pose, list[dict[str, Any]]]:
    me = observation["self"]
    pose = deepcopy(me["pose"])
    parameters = {**DEFAULT_PARAMETERS, **observation.get("parameters", {})}
    state = me.get("motion_state") or {}
    action = me.get("action") or {}
    if action.get("segments"):
        state = motion_at(action["segments"], observation["sim_time_ms"] - action["started_at_ms"])
    speed = max(0., float(state.get("speed_mm_s", state.get("linear_speed_mm_s", 0))))
    brake = parameters["braking_mm_s2"]
    distance = speed * speed / (2 * brake)
    vx, vy = state.get("vx_mm_s"), state.get("vy_mm_s")
    heading = math.atan2(vy, vx) if vx is not None and vy is not None and speed else pose["heading_mrad"] / 1000
    stop = deepcopy(pose) | {"x_mm": pose["x_mm"] + distance * math.cos(heading),
                             "y_mm": pose["y_mm"] + distance * math.sin(heading)}
    if distance:
        stop["workpoint_id"] = None
    duration = math.ceil(speed / brake * 1000 - 1e-9)
    if not duration:
        return stop, []
    return stop, [{"kind": "brake", "start_ms": 0, "end_ms": duration,
                  "from_pose": pose, "to_pose": stop, "distance_mm": distance,
                  "initial_speed_mm_s": speed, "braking_mm_s2": brake}]


class _Rejected(Exception):
    def __init__(self, code: str, reason: str):
        self.code, self.reason = code, reason


def _check(condition: bool, code: str, reason: str) -> None:
    if not condition:
        raise _Rejected(code, reason)


def quote_action(observation: dict[str, Any], action: dict[str, Any], after_stop: bool=False) -> dict[str, Any]:
    """Validate current facts and estimate one action; never mutate/reserve anything."""
    result: dict[str, Any] = {"ok": False, "error": None, "reason": "", "action": deepcopy(action),
        "based_on_seq": observation.get("seq", 0), "estimate_ms": 0,
        "expected_end_ms": observation.get("sim_time_ms", 0), "segments": [],
        "preview_only": False, "preconditions": {}, "alignment_ms": 0}
    try:
        _quote(observation, action, after_stop, result)
        result["ok"] = True
    except _Rejected as exc:
        result["error"], result["reason"] = exc.code, exc.reason
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        result["error"], result["reason"] = "INVALID_ACTION", f"Invalid action or state: {exc}"
    return result


def _quote(obs: dict[str, Any], action: dict[str, Any], after_stop: bool, out: dict[str, Any]) -> None:
    _check(isinstance(action, dict) and set(action) == {"kind", "params"} and isinstance(action.get("kind"), str) and isinstance(action.get("params"), dict),
           "INVALID_ACTION", "Expected {kind: string, params: object}")
    kind, params = action["kind"], action["params"]
    fields = _ACTION_FIELDS.get(kind)
    _check(fields is not None, "INVALID_ACTION", f"Unknown action kind: {kind}")
    _check(set(params) == set(fields) and all(type(params[key]) is expected for key, expected in fields.items()),
           "INVALID_ACTION", f"Invalid parameters for {kind}")
    if kind == "pick":
        _check(all(isinstance(item_id, str) for item_id in params["item_ids"]),
               "INVALID_ACTION", "item_ids must contain item ID strings")
    me = obs["self"]
    _check(obs.get("remaining_ms", 1) > 0 and me["control_state"] != "FINISHED", "MATCH_FINISHED", "Match has ended")
    _check(me["control_state"] == "READY", "RETRY_REQUIRED", "Robot is not READY")
    busy = me.get("action")
    pose = deepcopy(me["pose"])
    segments: list[dict[str, Any]] = []
    if busy:
        _check(after_stop, "BUSY", "Main action is still running")
        _check(busy["kind"] == "move_to" and kind == "move_to", "PREVIEW_UNAVAILABLE", "Only movement braking previews are supported")
        pose, segments = braking_preview(obs)
        out.update(preview_only=True, stop_pose=deepcopy(pose))
    elif after_stop:
        raise _Rejected("PREVIEW_UNAVAILABLE", "The robot is already stopped; quote the action directly")
    team, cargo = me["team"], me.get("cargo", [])
    parameters = {**DEFAULT_PARAMETERS, **obs.get("parameters", {})}
    map_data = obs["map"]
    points = map_data.get("points", map_data.get("workpoints", {}))
    supplies = obs.get("supply", {})
    supply = supplies.get(team, {})
    slots = supply.get("slots", {})
    items, stacks = obs.get("items", {}), obs.get("stacks", {})
    cargo_ids = {i["id"] for i in cargo}
    duration = 0
    target_pose = deepcopy(pose)
    pre = out["preconditions"]

    def at_transfer() -> None:
        _check(pose.get("workpoint_id") == team.upper() + "_TRANSFER", "NOT_AT_WORKPOINT", "Robot must be at its transfer workpoint")

    def version(actual: Any) -> None:
        expected = params.get("expected_version")
        _check(type(expected) is int and actual == expected, "TARGET_CHANGED", "Expected version does not match current resource")

    def item_available(iid: str) -> dict[str, Any]:
        _check(iid in items, "TARGET_CHANGED", f"Unknown item {iid}")
        item = items[iid]
        _check(not item.get("disabled_for_use"), "RULE_FORBIDDEN", "Disabled item cannot be used")
        return item

    def stack_valid(sid: str, held: bool) -> dict[str, Any]:
        _check(sid in stacks, "STACK_BROKEN", "Stack relationship no longer exists")
        stack = stacks[sid]
        _check(stack.get("intact", False), "STACK_BROKEN", "Stack relationship is broken")
        ids = [stack["bottom_item_id"], stack["top_item_id"]]
        _check(len(set(ids)) == 2, "STACK_BROKEN", "Stack must contain two distinct boxes")
        for iid in ids:
            _check(item_available(iid)["kind"] == "box", "STACK_BROKEN", "Stack must reference two boxes")
        if held:
            _check(me.get("carried_stack_id") == sid and stack.get("holder_id") == me["id"] and set(ids) == cargo_ids,
                   "INVALID_CARGO", "Robot is not holding this intact two-box stack")
        pre["stack_id"], pre["stack_version"], pre["item_ids"] = sid, stack["version"], ids
        pre["item_versions"] = {iid: items[iid].get("version", 0) for iid in ids}
        return stack

    def tower_here(site_id: str | None=None) -> dict[str, Any]:
        sid = site_id if site_id is not None else params["site_id"]
        _check(sid in obs["towers"], "TARGET_CHANGED", "Unknown tower")
        tower = obs["towers"][sid]
        _check(tower.get("owner") in (None, team), "RULE_FORBIDDEN", "Opponent exclusive site")
        if error := tower_access_error(map_data, tower, pose, team, kind, params.get("level")):
            raise _Rejected(*error)
        _check(not tower.get("operation"), "RESOURCE_BUSY", "Tower has an operation in progress")
        version(tower["version"])
        pre.update(site_id=sid, tower_version=tower["version"])
        return tower

    if kind == "move_to":
        pid = params["point_id"]
        _check(pid in points, "UNREACHABLE", "Unknown workpoint")
        _check(points[pid]["layer"] == pose["layer"], "WRONG_LAYER", "Use an explicit traverse action for layer changes")
        path = route(map_data, pose, pid, team)
        _check(path is not None, "UNREACHABLE", "No same-layer legal graph path")
        new = motion_segments(path, parameters, cargo)
        offset = segments[-1]["end_ms"] if segments else 0
        for segment in new:
            segment["start_ms"] += offset
            segment["end_ms"] += offset
        segments.extend(new)
        if segments:
            duration = segments[-1]["end_ms"]
            target_pose = deepcopy(segments[-1]["to_pose"])
        else:
            duration = 1  # Never schedule a zero-time action loop.
            target_pose = point_pose(points[pid]) | {"heading_mrad": pose["heading_mrad"]}
            segments = [{"kind": "wait", "start_ms": 0, "end_ms": 1, "from_pose": pose, "to_pose": target_pose}]
        target_pose["workpoint_id"] = pid
        segments[-1]["to_pose"] = deepcopy(target_pose)
        pre["point_id"] = pid
    elif kind == "traverse":
        _check(params["destination_layer"] != "GROUND", "RULE_FORBIDDEN", "BR cannot voluntarily return to the ground after entry (4.3.2)")
        connection = map_data["connections"].get(params["connection_id"])
        _check(connection is not None and _available(connection, team), "UNREACHABLE", "Unknown or forbidden connection")
        source, dest = connection["from_point_id"], connection["to_point_id"]
        if pose["layer"] == connection["to_layer"]:
            source, dest = dest, source
        _check(pose.get("workpoint_id") == source, "NOT_AT_WORKPOINT", "Move to the connection entrance first")
        _check(params["destination_layer"] == points[dest]["layer"], "WRONG_LAYER", "Wrong destination layer for this connection")
        up = points[dest]["z_mm"] > pose["z_mm"]
        duration = parameters[connection["up_ms_parameter"] if up else connection["down_ms_parameter"]]
        target_pose = point_pose(points[dest]) | {"heading_mrad": pose["heading_mrad"]}
        segments = [{"kind": "traverse", "start_ms": 0, "end_ms": duration, "from_pose": pose, "to_pose": target_pose}]
        pre.update(connection_id=connection["id"], destination_point_id=dest)
    elif kind == "pick_stack":
        at_transfer()
        _check(not cargo, "INVALID_CARGO", "An intact stack requires two empty cargo slots")
        stack = stack_valid(params["stack_id"], False)
        version(stack["version"])
        _check(supply.get("stack_id") == stack["id"] and not stack.get("holder_id"), "TARGET_CHANGED", "Stack is no longer in this team's bay")
        _check(slots.get("BOX_LOWER") == stack["bottom_item_id"] and slots.get("BOX_UPPER") == stack["top_item_id"], "STACK_BROKEN", "Both ordered box slots must match the stack")
        pre.update(slot_versions=deepcopy(supply.get("slot_versions", {})), box_bay_version=supply.get("box_bay_version", 0))
        duration = parameters["stack_pick_ms"]
    elif kind == "build_stack":
        tower = tower_here()
        stack_valid(params["stack_id"], True)
        _check(all(tower.get(k) is None for k in ("base", "middle", "cap")), "TARGET_CHANGED", "Whole stacks may only be placed on completely empty sites")
        duration = parameters["stack_build_ms"]
    elif kind == "put_down_stack":
        at_transfer()
        stack_valid(params["stack_id"], True)
        _check(params["bay_id"] in ("BOX_STACK_BAY", team.upper() + "_BOX_STACK_BAY", team + ":BOX_STACK_BAY"), "RULE_FORBIDDEN", "Unknown box stack bay")
        version(supply.get("box_bay_version", 0))
        _check(not slots.get("BOX_LOWER") and not slots.get("BOX_UPPER"), "RESOURCE_BUSY", "Both box slots must be empty")
        pre.update(box_bay_version=supply.get("box_bay_version", 0), slot_versions=deepcopy(supply.get("slot_versions", {})))
        duration = parameters["stack_return_ms"]
    elif kind == "pick":
        at_transfer()
        ids = params["item_ids"]
        _check(isinstance(ids, list) and 1 <= len(ids) <= 2 and len(set(ids)) == len(ids), "INVALID_CARGO", "Pick one or two distinct ordinary items")
        _check(not me.get("carried_stack_id") and len(cargo) + len(ids) <= 2 and not any(i["kind"] == "ball" for i in cargo), "INVALID_CARGO", "Cargo capacity exceeded")
        stack = stacks.get(supply.get("stack_id"))
        if stack and stack.get("intact"):
            pair = {stack["bottom_item_id"], stack["top_item_id"]}
            _check(not pair.issubset(set(ids)), "STACK_ACTION_REQUIRED", "Use pick_stack for the complete pre-stacked pair")
            _check(stack["bottom_item_id"] not in ids or not slots.get("BOX_UPPER"), "RULE_FORBIDDEN", "Cannot pull a lower box from beneath the upper box")
        for iid in ids:
            item = item_available(iid)
            _check(iid in slots.values() and item["kind"] != "ball", "TARGET_CHANGED", "Item must be in the team's ordinary slots")
            _check(not item.get("holder_id"), "TARGET_CHANGED", "Item is held elsewhere")
        pre.update(item_ids=ids, item_versions={iid: items[iid].get("version", 0) for iid in ids}, slot_versions=deepcopy(supply.get("slot_versions", {})))
        duration = parameters["pick_one_ms" if len(ids) == 1 else "pick_two_ms"]
    elif kind == "build":
        tower = tower_here()
        iid, level = params["item_id"], params["level"]
        _check(iid in cargo_ids and not me.get("carried_stack_id"), "INVALID_CARGO", "Hold this loose item before building")
        item = item_available(iid)
        _check(level in ("base", "middle", "cap"), "RULE_FORBIDDEN", "Unknown tower level")
        _check(item["kind"] == ("cap" if level == "cap" else "box"), "INVALID_CARGO", "Item kind does not match level")
        _check(tower.get(level) is None, "TARGET_CHANGED", "Requested tower level is occupied")
        if level != "base":
            _check(tower.get("base") is not None, "RULE_FORBIDDEN", "Base support is missing")
        if level == "cap":
            _check(tower.get("middle") is not None, "RULE_FORBIDDEN", "Middle support is missing")
        pre.update(item_ids=[iid], item_versions={iid: item.get("version", 0)})
        duration = parameters[f"build_{level}_ms"]
    elif kind == "flip_cap":
        tower = tower_here()
        _check(not cargo, "INVALID_CARGO", "Flip requires an empty gripper")
        _check(tower.get("base") and tower.get("middle") and tower.get("cap"), "TARGET_CHANGED", "Tower is incomplete")
        _check(tower.get("top_color") != team, "RULE_FORBIDDEN", "Tower already shows this team's color")
        duration = parameters["flip_cap_ms"]
        pre["item_ids"] = [tower["cap"]]
    elif kind == "receive_ball":
        at_transfer()
        _check(not cargo, "INVALID_CARGO", "Ball handoff requires empty cargo")
        task = supply.get("ball_task") or {}
        _check(task.get("status") == "waiting_handoff", "RESOURCE_BUSY", "Ball has not arrived for direct handoff")
        _check(obs.get("qualified", {}).get(team), "RULE_FORBIDDEN", "Team has not qualified")
        _check(not obs.get("central_ball"), "RULE_FORBIDDEN", "Central ball already placed")
        duration = parameters["receive_ball_ms"]
        pre["ball_task_id"] = task.get("id")
    elif kind == "place_ball":
        _check(pose.get("workpoint_id") == "CENTRAL_BALL_APPROACH" and pose["layer"] == "L2", "NOT_AT_WORKPOINT", "Robot must stand at the central L2 ball workpoint")
        _check(len(cargo) == 1 and cargo[0]["kind"] == "ball", "INVALID_CARGO", "Robot must hold only a ball")
        _check(obs.get("qualified", {}).get(team), "RULE_FORBIDDEN", "Team has not qualified")
        _check(not obs.get("central_ball"), "TARGET_CHANGED", "Central ball already placed")
        pre["item_ids"] = [cargo[0]["id"]]
        duration = parameters["place_ball_ms"]
    elif kind == "put_down":
        at_transfer()
        iid, slot = params["item_id"], params["slot_id"]
        _check(iid in cargo_ids and not me.get("carried_stack_id"), "INVALID_CARGO", "Hold this loose item; stacks require put_down_stack")
        item = item_available(iid)
        _check(slot in ("BOX_LOWER", "BOX_UPPER", "CAP_BAY"), "RULE_FORBIDDEN", "Unknown ordinary slot")
        version(supply.get("slot_versions", {}).get(slot, 0))
        _check(not slots.get(slot), "RESOURCE_BUSY", "Slot is occupied")
        _check(item["kind"] == ("cap" if slot == "CAP_BAY" else "box"), "INVALID_CARGO", "Wrong item type for slot")
        _check(slot != "BOX_UPPER" or bool(slots.get("BOX_LOWER")), "RULE_FORBIDDEN", "Upper box slot needs lower support")
        _check(slot != "BOX_LOWER" or not slots.get("BOX_UPPER"), "RULE_FORBIDDEN", "Cannot insert a lower box under an upper box")
        pre.update(item_ids=[iid], slot_id=slot, slot_version=supply.get("slot_versions", {}).get(slot, 0))
        duration = parameters["put_down_ms"]
    elif kind == "take_from":
        iid, source = params["item_id"], params["source_id"]
        item = item_available(iid)
        _check(item["kind"] == "box", "RULE_FORBIDDEN", "Placed tower caps can only be operated with flip_cap")
        _check(not me.get("carried_stack_id") and len(cargo) < 2 and not any(i["kind"] == "ball" for i in cargo), "INVALID_CARGO", "Cargo capacity exceeded")
        if source in obs["towers"]:
            tower = tower_here(source)
            _check(item.get("owner_color") == team or not item.get("valid", True), "RULE_FORBIDDEN", "Protected correctly placed opponent boxes cannot be removed")
            level = next((k for k in ("base", "middle") if tower.get(k) == iid), None)
            _check(level is not None, "TARGET_CHANGED", "Item is not on the source tower")
            _check(not tower.get("cap") and (level != "base" or not tower.get("middle")), "RULE_FORBIDDEN", "Cannot remove support from a tower's upper components")
        else:
            at_transfer()
            _check(source in slots and slots[source] == iid, "TARGET_CHANGED", "Item is not in source slot")
            version(supply.get("slot_versions", {}).get(source, 0))
            _check(source != "BOX_LOWER" or not slots.get("BOX_UPPER"), "RULE_FORBIDDEN", "Cannot pull a supporting lower box")
        pre["item_ids"] = [iid]
        duration = parameters["take_from_ms"]
    else:
        raise _Rejected("INVALID_ACTION", f"Unknown action kind: {kind}")
    if not segments:
        # Contact CAS occurs after precision alignment; engine rechecks all prerequisites.
        alignment = max(1, int(duration) // 3)
        out["alignment_ms"] = alignment
        segments = [{"kind": "align", "start_ms": 0, "end_ms": alignment, "from_pose": pose, "to_pose": pose},
                    {"kind": "contact", "start_ms": alignment, "end_ms": int(duration), "from_pose": pose, "to_pose": target_pose}]
    out.update(estimate_ms=int(duration), expected_end_ms=obs["sim_time_ms"] + int(duration),
               segments=segments, target_pose=target_pose)
