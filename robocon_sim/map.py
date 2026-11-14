"""Versioned static graph. Millimetres are dimensions, never image pixel measurements."""
from __future__ import annotations

from typing import Any


def default_map() -> dict[str, Any]:
    points: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    towers: dict[str, dict[str, Any]] = {}
    connections: dict[str, dict[str, Any]] = {}

    def point(pid: str, x: int, y: int, layer: str = "L1", owner: str | None = None,
              **extra: Any) -> None:
        points[pid] = {"id": pid, "x_mm": x, "y_mm": y,
                       "z_mm": {"GROUND": 0, "L1": 600, "L2": 900}[layer],
                       "layer": layer, "owner": owner, "heading_mrad": 0, **extra}

    def edge(a: str, b: str, owner: str | None = None) -> None:
        edges.append({"a": a, "b": b, "owner": owner, "bidirectional": True})

    for team, x, inner in (("red", 9000, 7500), ("blue", 2000, 3500)):
        prefix = team.upper()
        point(f"{prefix}_START", x, 1000, "GROUND", team)
        point(f"{prefix}_TRANSFER", x, 4200, owner=team, kind="transfer")
        for label, y in (("NORTH", 3500), ("MID", 5500), ("SOUTH", 7500)):
            point(f"{prefix}_{label}", inner, y, owner=team)
        point(f"{prefix}_ENTRY", inner, 4200, owner=team)
        edge(f"{prefix}_TRANSFER", f"{prefix}_ENTRY", team)
        retry_x = 7380 if team == "red" else 3620
        point(f"{prefix}_L1_RETRY", retry_x, 8150, owner=team, kind="retry")
        edge(f"{prefix}_SOUTH", f"{prefix}_L1_RETRY", team)
        for a, b in (("NORTH", "ENTRY"), ("ENTRY", "MID"), ("MID", "SOUTH")):
            edge(f"{prefix}_{a}", f"{prefix}_{b}", team)
        point(f"{prefix}_L2_GATE", inner, 5500, owner=team)
        edge(f"{prefix}_MID", f"{prefix}_L2_GATE", team)
        l2x = 6500 if team == "red" else 4500
        point(f"{prefix}_L2_LANDING", l2x, 5500, "L2")
        connections[f"{prefix}_L2_STEP"] = {
            "id": f"{prefix}_L2_STEP", "kind": "step",
            "from_point_id": f"{prefix}_L2_GATE", "to_point_id": f"{prefix}_L2_LANDING",
            "from_layer": "L1", "to_layer": "L2", "owner": team,
            "height_mm": 300, "up_ms_parameter": "l2_up_ms", "down_ms_parameter": "l2_down_ms"}
        for kind in ("RAMP", "STAIRS"):
            connections[f"{prefix}_{kind}"] = {
                "id": f"{prefix}_{kind}", "kind": kind.lower(),
                "from_point_id": f"{prefix}_START", "to_point_id": f"{prefix}_TRANSFER",
                "from_layer": "GROUND", "to_layer": "L1", "owner": team,
                "up_ms_parameter": "ramp_up_ms" if kind == "RAMP" else "stairs_up_ms",
                "down_ms_parameter": "ramp_down_ms" if kind == "RAMP" else "stairs_down_ms"}

    for i, y in ((1, 2750), (2, 8250)):
        for team, x, ax in (("red", 8250, 7750), ("blue", 2750, 3250)):
            tid = f"L1_{team.upper()}_{i}"
            ap = tid + "_APPROACH"
            point(ap, ax, 3250 if i == 1 else 7750, owner=team, tower_ids=[tid])
            edge(ap, f"{team.upper()}_{'NORTH' if i == 1 else 'SOUTH'}", team)
            towers[tid] = _tower(tid, "L1", x, y, ap, team)
        tid = f"L1_PUBLIC_{i}"
        ap = tid + "_APPROACH"
        point(ap, 5500, 3250 if i == 1 else 7750, tower_ids=[tid])
        # Both teams enter the small public region through their own side.
        for team in ("red", "blue"):
            mid = f"{team.upper()}_PUBLIC_{i}_ENTRY"
            point(mid, 5500, 3500 if i == 1 else 7500)
            edge(f"{team.upper()}_{'NORTH' if i == 1 else 'SOUTH'}", mid, team)
            edge(mid, ap, team)
        towers[tid] = _tower(tid, "L1", 5500, y, ap, None)

    for i, (x, y, ax, ay) in enumerate(((4250, 4250, 4500, 4500), (6750, 4250, 6500, 4500),
                                       (4250, 6750, 4500, 6500), (6750, 6750, 6500, 6500)), 1):
        tid = f"L2_PUBLIC_{i}"
        ap = tid + "_APPROACH"
        point(ap, ax, ay, "L2", tower_ids=[tid])
        towers[tid] = _tower(tid, "L2", x, y, ap, None)
        # One L1 branch per site, attached to the same team's legal rim.
        owner, rim_x = ("blue", 3750) if x < 5500 else ("red", 7250)
        l1_ap = "L1_BUILD_" + tid
        point(l1_ap, rim_x, y, owner=owner, kind="cross_layer_build", build_tower_id=tid)
        edge(l1_ap, f"{owner.upper()}_{'ENTRY' if y < 5500 else 'SOUTH'}", owner)
        towers[tid]["l1_approach_point_id"] = l1_ap
    for a, b in ((1, 2), (2, 4), (4, 3), (3, 1)):
        edge(f"L2_PUBLIC_{a}_APPROACH", f"L2_PUBLIC_{b}_APPROACH")
    for team, ids in (("BLUE", (1, 3)), ("RED", (2, 4))):
        for i in ids:
            edge(f"{team}_L2_LANDING", f"L2_PUBLIC_{i}_APPROACH")
    point("CENTRAL_BALL_APPROACH", 5500, 5000, "L2", kind="central_ball")
    edge("CENTRAL_BALL_APPROACH", "L2_PUBLIC_1_APPROACH")
    edge("CENTRAL_BALL_APPROACH", "L2_PUBLIC_2_APPROACH")
    # Access from either landing stays on L2 and stops before the pedestal.
    edge("CENTRAL_BALL_APPROACH", "RED_L2_LANDING")
    edge("CENTRAL_BALL_APPROACH", "BLUE_L2_LANDING")
    return {"version": "teaching-v2-l1-l2-box-build", "units": "mm",
            "width_mm": 11000, "height_mm": 11000,
            "dimensions": {"field_mm": [11000, 11000], "l1_mm": [6000, 6000],
                           "l2_mm": [3000, 3000], "l1_height_mm": 600, "l2_height_mm": 900,
                           "ramp_mm": [3500, 1000, 600], "stairs_step_mm": [300, 1000, 150],
                           "transfer_mm": [1000, 1000], "green_pad_mm": [500, 500],
                           "box_mm": [350, 350, 350], "cap_mm": [200, 200, 200],
                           "central_pedestal_height_above_l2_mm": 800},
            "layers": {"GROUND": {"z_mm": 0}, "L1": {"z_mm": 600, "bounds": [2500, 2500, 8500, 8500]},
                       "L2": {"z_mm": 900, "bounds": [4000, 4000, 7000, 7000]}},
            "points": points, "workpoints": points, "edges": edges,
            "connections": connections, "towers": towers,
            "sources": ["V3 第1、2节及附录3（本地rule-33.png已查阅）", "03_场地与动作时间模型.md"],
            "assumptions": ["坐标原点为展示图左上角，x向红方增大；不是图像像素测量。",
                "L1/L2中心重合；绿垫中心按标注尺寸和对称关系建模，细部须现场复核。",
                "机器人作业位、路网和越阶入口为平台教学假设，不是官方标注。",
                "L1 重试作业位采用南侧 700 mm 重试区中心，距相邻绿垫 270 mm，以对称布局推导。",
                "平台假设 BR 可在本方 L1 边缘专用作业位向相邻 L2 绿垫放底箱、中箱或整摞，实际机械可达性待验证。",
                "静态图抽象车体为点，不模拟车辆碰撞；路网禁止穿平台和对方专属区域。"]}


def _tower(tid: str, layer: str, x: int, y: int, ap: str, owner: str | None) -> dict[str, Any]:
    return {"id": tid, "layer": layer, "public": owner is None, "owner": owner,
            "center": {"x_mm": x, "y_mm": y}, "approach_point_id": ap,
            "base": None, "middle": None, "cap": None, "version": 0, "operation": None, "top_color": None}


def tower_access_error(map_data: dict[str, Any], tower: dict[str, Any], pose: dict[str, Any],
                       team: str, kind: str, level: str | None) -> tuple[str, str] | None:
    """Check approach geometry and action scope without inspecting mutable tower state."""
    point_id, layer = pose.get("workpoint_id"), pose.get("layer")
    if layer == tower["layer"]:
        if point_id == tower["approach_point_id"]:
            return None
        return "NOT_AT_WORKPOINT", "Move to the tower approach first"

    box_build = kind == "build_stack" or (kind == "build" and level in ("base", "middle"))
    l1_approach = tower.get("l1_approach_point_id")
    if layer == "L1" and tower["layer"] == "L2" and box_build and l1_approach is not None:
        point = map_data.get("points", map_data.get("workpoints", {})).get(l1_approach, {})
        if point_id == l1_approach and point.get("layer") == "L1" and point.get("build_tower_id") == tower["id"]:
            if point.get("owner") in (None, team):
                return None
            return "RULE_FORBIDDEN", "Opponent exclusive workpoint"
        return "NOT_AT_WORKPOINT", "Move to the L1 box-build workpoint for this tower"
    return "WRONG_LAYER", "Robot must stand on the tower layer"
