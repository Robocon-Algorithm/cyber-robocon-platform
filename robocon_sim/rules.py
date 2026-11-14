"""V3 material/support checks, permanent qualification, and per-item audit."""
from __future__ import annotations
from typing import Any

from .map import tower_access_error
from .world import TEAMS

RULESET_VERSION = "V3-training-2-l1-l2-box-build"


class RuleError(ValueError):
    def __init__(self, code: str, reason: str, rule: str | None = None) -> None:
        super().__init__(reason)
        self.code, self.reason, self.rule = code, reason, rule


def require(condition: Any, code: str, reason: str, rule: str | None = None) -> None:
    if not condition:
        raise RuleError(code, reason, rule)


def valid_tower(world: dict[str, Any], tower: dict[str, Any]) -> tuple[bool, bool, bool]:
    items = world["items"]
    base, middle, cap = (items.get(tower[level]) for level in ("base", "middle", "cap"))
    b = bool(base and base.get("valid") and not base["holder_id"] and
             base["support_id"] == "green:" + tower["id"] and not base["disabled_for_use"])
    m = bool(b and middle and middle.get("valid") and not middle["holder_id"] and
             middle["support_id"] == base["id"] and not middle["disabled_for_use"])
    c = bool(m and cap and cap.get("valid") and not cap["holder_id"] and
             cap["support_id"] == middle["id"] and not cap["disabled_for_use"])
    return b, m, c


def update_qualification(world: dict[str, Any]) -> list[str]:
    newly = []
    for team in TEAMS:
        full = [t for t in world["towers"].values() if all(valid_tower(world, t))
                and world["items"][t["cap"]]["orientation"] == team]
        if not world["qualified"][team] and len(full) >= 2 and any(t["public"] for t in full):
            world["qualified"][team] = True
            newly.append(team)
    return newly


def score_world(world: dict[str, Any], final: bool = False) -> dict[str, Any]:
    """Compute from physical facts; transported boxes never receive a second 5."""
    scores = {team: {"total": 0, "boxes": 0, "caps": 0, "cap_transport": 0,
                     "ball": 0, "ledger": []} for team in TEAMS}

    def add(team, item_id, points, category, reason, **extra):
        if team not in scores:
            return
        entry = {"item_id": item_id, "points": points, "category": category,
                 "reason": reason, **extra}
        s = scores[team]
        s["total"] += points
        s[category] += points
        s["ledger"].append(entry)

    placed = set()
    for t in world["towers"].values():
        flags = valid_tower(world, t)
        for level, valid in zip(("base", "middle", "cap"), flags):
            if not valid:
                continue
            item = world["items"][t[level]]
            placed.add(item["id"])
            if level == "cap":
                add(item["orientation"], item["id"], 40 if t["layer"] == "L1" else 80,
                    "caps", "valid cap, V3 8.1.4", tower_id=t["id"], level=level)
            else:
                pts = {"L1": {"base": 15, "middle": 25}, "L2": {"base": 25, "middle": 45}}
                add(item["owner_color"], item["id"], pts[t["layer"]][level], "boxes",
                    "valid box including transport, V3 appendix 2", tower_id=t["id"], level=level)
        if not final and t["cap"]:
            cap = world["items"][t["cap"]]
            if cap["location"].startswith("held_flip:") and flags[1]:
                add(cap["orientation"], cap["id"], 40 if t["layer"] == "L1" else 80,
                    "caps", "original ownership retained during legal flip, V3 3.5.8", tower_id=t["id"], level="cap")
    for item in world["items"].values():
        loc = item["location"]
        if item["kind"] == "box" and item["id"] not in placed:
            # At final audit all robot contact cancels even the transport portion.
            on_field = loc.startswith(("buffer:", "disabled:L1:", "loose:L1:", "loose:L2:", "tower:"))
            if on_field and not item["holder_id"]:
                add(item["owner_color"], item["id"], 5, "boxes", "box on field, V3 8.2 / 5.1.3")
        if item["kind"] == "cap" and item.get("transported_by"):
            add(item["transported_by"], item["id"], 5, "cap_transport",
                "recorded successful cap delivery; provisional held-cap history interpretation")
    ball = world["central_ball"]
    if ball:
        add(ball["team"], ball["item_id"], 250, "ball", "first valid central ball, V3 8.1.5")
    return scores


def tiebreak(world: dict[str, Any], scores: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    counts = {team: {"ball": int(bool(world["central_ball"] and world["central_ball"]["team"] == team)),
                     "caps_up": 0, "L2": 0, "L1": 0} for team in TEAMS}
    for item in world["items"].values():
        if (item["kind"] == "cap" and item["orientation"] in TEAMS and not item["holder_id"]
                and not item["disabled_for_use"] and item["location"].startswith(("buffer:", "tower:", "loose:"))):
            counts[item["orientation"]]["caps_up"] += 1
    for t in world["towers"].values():
        for level, valid in zip(("base", "middle", "cap"), valid_tower(world, t)):
            if valid:
                item = world["items"][t[level]]
                team = item["orientation"] if level == "cap" else item["owner_color"]
                if team in counts:
                    counts[team][t["layer"]] += 1
    keys = {team: (scores[team]["total"], *(counts[team][k] for k in ("ball", "caps_up", "L2", "L1")))
            for team in TEAMS}
    winner = None if keys["red"] == keys["blue"] else max(TEAMS, key=lambda team: keys[team])
    return winner, {"criteria": ["total", "ball", "caps_up", "L2", "L1"], "counts": counts,
                    "keys": {team: list(key) for team, key in keys.items()}}


def validate_operation(world: dict[str, Any], team: str, action: dict[str, Any], *,
                       own_token: str | None = None) -> None:
    """Recheck mutable preconditions at contact and at commit."""
    r = world["robots"][team + "_br"]
    p, kind = action["params"], action["kind"]
    items, s = world["items"], world["supply"][team]
    cargo = [i for i in items.values() if i["holder_id"] == r["id"] and not i["location"].startswith("held_flip:")]
    if kind == "traverse":
        require(p.get("destination_layer") != "GROUND", "RULE_FORBIDDEN", "BR cannot return to ground after L1 entry", "4.3.1")
        return
    if kind in ("move_to", "entry"):
        return
    require(r["control_state"] == "READY", "BUSY", "robot is paused or retrying")
    transfer = r["pose"]["workpoint_id"] == team.upper() + "_TRANSFER"
    if kind in ("pick_stack", "pick", "put_down_stack", "put_down", "receive_ball"):
        require(transfer, "NOT_AT_WORKPOINT", "must stand at own transfer workpoint", "4.4")
    if kind in ("build", "build_stack", "flip_cap") or (kind == "take_from" and p.get("source_id") in world["towers"]):
        tid = p.get("site_id", p.get("source_id"))
        require(tid in world["towers"], "TARGET_CHANGED", "unknown tower")
        t = world["towers"][tid]
        require(t.get("owner") in (None, team), "RULE_FORBIDDEN", "opponent exclusive region", "4.3")
        if error := tower_access_error(world["map"], t, r["pose"], team, kind, p.get("level")):
            raise RuleError(*error)
        require(t["operation"] in (None, own_token), "RESOURCE_BUSY", "tower operation in progress")
        if own_token is None:
            require(p.get("expected_version") == t["version"], "TARGET_CHANGED", "tower version changed")
        if kind == "build_stack":
            require(not any(t[k] for k in ("base", "middle", "cap")), "TARGET_CHANGED", "stack requires an entirely empty tower")
        if kind == "build":
            level = p.get("level")
            require(level in ("base", "middle", "cap"), "RULE_FORBIDDEN", "explicit tower level required")
            require(t[level] is None, "TARGET_CHANGED", "target level occupied")
            b, m, _ = valid_tower(world, t)
            require(level == "base" or (level == "middle" and b) or (level == "cap" and m),
                    "RULE_FORBIDDEN", "tower support/order invalid", "3.5.2")
            require(level != "base" or not t["middle"] and not t["cap"], "RULE_FORBIDDEN", "base must be first")
            item = items.get(p.get("item_id"))
            require(item and item["holder_id"] == r["id"] and not item["disabled_for_use"], "INVALID_CARGO", "item not held")
            require(item["kind"] == ("cap" if level == "cap" else "box"), "INVALID_CARGO", "wrong item type")
            require(not r["carried_stack_id"], "STACK_ACTION_REQUIRED", "complete stack must be built as a stack")
        if kind == "flip_cap":
            require(not cargo, "INVALID_CARGO", "flip requires empty manipulator")
            require(t["cap"] and (all(valid_tower(world, t)) if own_token is None else
                    world["items"][t["cap"]]["location"] == "held_flip:" + t["id"]),
                    "TARGET_CHANGED", "no standing full tower or original held cap to flip")
        if kind == "take_from":
            item = items.get(p.get("item_id"))
            require(item and item["id"] in (t["base"], t["middle"]), "RULE_FORBIDDEN", "caps can only be flipped on original tower")
            require(item["owner_color"] == team or not item.get("valid"), "RULE_FORBIDDEN", "opponent correctly placed box protected", "3.5.5")
            level = "base" if item["id"] == t["base"] else "middle"
            above = [t["middle"], t["cap"]] if level == "base" else [t["cap"]]
            require(not any(above), "RULE_FORBIDDEN", "remove upper supported objects first", "3.5.5")
            require(len(cargo) < 2 and not any(i["kind"] == "ball" for i in cargo), "INVALID_CARGO", "carry limit")
    if kind in ("pick_stack", "build_stack", "put_down_stack"):
        stack = world["stacks"].get(p.get("stack_id"))
        require(stack and stack["intact"], "STACK_BROKEN", "stack relationship no longer intact")
        ids = [stack["bottom_item_id"], stack["top_item_id"]]
        require(all(i in items and not items[i]["disabled_for_use"] for i in ids), "STACK_BROKEN", "stack items unusable")
        if kind == "pick_stack":
            require(not cargo, "INVALID_CARGO", "two empty carrying places required", "6.5")
            require(p.get("expected_version") == stack["version"], "TARGET_CHANGED", "stack version changed")
            require(s["stack_id"] == stack["id"] and [s["slots"][k] for k in ("BOX_LOWER", "BOX_UPPER")] == ids,
                    "STACK_BROKEN", "both stack items must remain entirely in transfer bay")
        else:
            require(r["carried_stack_id"] == stack["id"] and stack["holder_id"] == r["id"]
                    and {i["id"] for i in cargo} == set(ids), "INVALID_CARGO", "complete ordered stack must be held")
        if kind == "put_down_stack":
            require(p.get("bay_id") in ("BOX_STACK_BAY", team + ":BOX_STACK_BAY", team.upper() + "_BOX_STACK_BAY"), "RULE_FORBIDDEN", "only own stack bay allowed")
            require(p.get("expected_version") == s["box_bay_version"], "TARGET_CHANGED", "box bay changed")
            require(not s["slots"]["BOX_LOWER"] and not s["slots"]["BOX_UPPER"], "RESOURCE_BUSY", "both box slots must be empty")
    if kind == "pick" or kind == "take_from" and p.get("source_id") not in world["towers"]:
        require(transfer, "NOT_AT_WORKPOINT", "ordinary material pickup requires own transfer")
        ids = p.get("item_ids", [p.get("item_id")])
        require(1 <= len(ids) <= 2 and len(set(ids)) == len(ids), "INVALID_CARGO", "pick one or two distinct items")
        require(len(cargo) + len(ids) <= 2 and not any(i["kind"] == "ball" for i in cargo), "INVALID_CARGO", "carry limit", "6.5")
        stack = world["stacks"].get(s["stack_id"])
        if stack and stack["intact"]:
            require(set(ids) != {stack["bottom_item_id"], stack["top_item_id"]}, "STACK_ACTION_REQUIRED", "use pick_stack for intact pair")
            require(stack["bottom_item_id"] not in ids or stack["top_item_id"] not in s["slots"].values(),
                    "RULE_FORBIDDEN", "cannot extract supporting lower box")
        for iid in ids:
            require(iid in items and iid in s["slots"].values() and not items[iid]["disabled_for_use"], "TARGET_CHANGED", "item no longer available")
        if kind == "take_from":
            slot = next(k for k, iid in s["slots"].items() if iid == ids[0])
            require(p.get("expected_version") == s["slot_versions"][slot], "TARGET_CHANGED", "source slot version changed")
    if kind == "put_down":
        item = items.get(p.get("item_id"))
        slot = p.get("slot_id")
        require(item and item["holder_id"] == r["id"] and item["kind"] != "ball", "INVALID_CARGO", "ordinary held item required")
        require(not r["carried_stack_id"], "STACK_ACTION_REQUIRED", "use put_down_stack")
        require(slot in s["slots"], "RULE_FORBIDDEN", "unknown slot")
        require(p.get("expected_version") == s["slot_versions"][slot], "TARGET_CHANGED", "slot changed")
        require(s["slots"][slot] is None, "RESOURCE_BUSY", "slot full")
        require((item["kind"] == "cap") == (slot == "CAP_BAY"), "RULE_FORBIDDEN", "slot type mismatch")
        require(slot != "BOX_UPPER" or bool(s["slots"]["BOX_LOWER"]), "RULE_FORBIDDEN", "upper box needs lower support")
    if kind == "receive_ball":
        task = s["ball_task"]
        require(not cargo, "INVALID_CARGO", "ball requires empty BR", "6.5")
        require(task and task["status"] == "waiting_handoff" and world["central_ball"] is None,
                "TARGET_CHANGED", "no handoff-ready ball")
    if kind == "place_ball":
        require(r["pose"]["workpoint_id"] == "CENTRAL_BALL_APPROACH" and r["pose"]["layer"] == "L2",
                "NOT_AT_WORKPOINT", "must be fully at central L2 workpoint")
        require(len(cargo) == 1 and cargo[0]["kind"] == "ball", "INVALID_CARGO", "one ball required")
        require(world["central_ball"] is None, "TARGET_CHANGED", "central ball already placed")
