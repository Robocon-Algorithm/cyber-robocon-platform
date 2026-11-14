"""Authoritative material records. Relations never replace physical items."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

TEAMS = ("red", "blue")
SLOTS = ("BOX_LOWER", "BOX_UPPER", "CAP_BAY")


def point_pose(map_data: dict[str, Any], point_id: str) -> dict[str, Any]:
    points = map_data.get("workpoints", map_data.get("points", {}))
    p = points[point_id]
    p = p.get("pose", p)
    return {"x_mm": p["x_mm"], "y_mm": p["y_mm"],
            "z_mm": p.get("z_mm", {"GROUND": 0, "L1": 600, "L2": 900}.get(p.get("layer"), 0)),
            "heading_mrad": p.get("heading_mrad", 0), "layer": p.get("layer", "L1"),
            "workpoint_id": point_id}


def make_world(map_data: dict[str, Any], auto_entry: bool = True) -> dict[str, Any]:
    towers = deepcopy(map_data["towers"])
    if isinstance(towers, list):
        towers = {t["id"]: t for t in towers}
    for tid, t in towers.items():
        t.update(id=tid, base=None, middle=None, cap=None, version=0, operation=None,
                 top_color=None)
        t.setdefault("public", t.get("owner") is None)
        t.setdefault("approach_point_id", tid + "_APPROACH")
    robots = {}
    for team in TEAMS:
        rid = team + "_br"
        pose = point_pose(map_data, team.upper() + ("_START" if auto_entry else "_TRANSFER"))
        robots[rid] = {"id": rid, "team": team, "pose": pose,
                       "motion_state": {"speed_mm_s": 0, "linear_speed_mm_s": 0,
                                        "angular_speed_mrad_s": 0},
                       "cargo": [], "carried_stack_id": None,
                       "control_state": "INIT" if auto_entry else "READY", "action": None}
    supply = {team: {"slots": dict.fromkeys(SLOTS), "slot_versions": dict.fromkeys(SLOTS, 0),
                     "box_bay_version": 0, "stack_id": None, "batch": None,
                     "inventory": {"boxes_remaining": 20, "caps_remaining": 6},
                     "control_state": "READY", "recovery_phase": None,
                     "recovery_until_ms": None, "ball_task": None,
                     "slot_operations": dict.fromkeys(SLOTS)} for team in TEAMS}
    return {"robots": robots, "towers": towers, "items": {}, "stacks": {},
            "supply": supply, "qualified": dict.fromkeys(TEAMS, False),
            "central_ball": None, "score": {}, "map": deepcopy(map_data)}


def new_item(item_id: str, kind: str, team: str, location: str,
             support: str | None = None) -> dict[str, Any]:
    return {"id": item_id, "kind": kind, "owner_color": team, "holder_id": None,
            "location": location, "disabled_for_use": False, "version": 0,
            "support_id": support, "orientation": team if kind == "cap" else "upright",
            "valid": False, "transported_by": None}


def relocate(item: dict[str, Any], location: str, holder: str | None = None,
             support: str | None = None, valid: bool = False) -> None:
    item.update(location=location, holder_id=holder, support_id=support, valid=valid,
                version=item["version"] + 1)


def sync_cargo(world: dict[str, Any]) -> None:
    for r in world["robots"].values():
        r["cargo"] = [i for i in world["items"].values() if i["holder_id"] == r["id"]
                      and not i["location"].startswith("held_flip:")]


def break_stack(world: dict[str, Any], item_ids: list[str]) -> None:
    for stack in world["stacks"].values():
        if stack["intact"] and set(item_ids) & {stack["bottom_item_id"], stack["top_item_id"]}:
            stack.update(intact=False, version=stack["version"] + 1)
            for r in world["robots"].values():
                if r["carried_stack_id"] == stack["id"]:
                    r["carried_stack_id"] = None
