"""Contact locks and material commits, separate from the event clock."""
from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any
from .rules import RuleError, require, validate_operation
from .world import relocate, break_stack

if TYPE_CHECKING:
    from .engine import Engine


class Operations:
    def __init__(self, engine: Engine) -> None:
        self.e = engine

    def resources(self, team: str, action: dict[str, Any]) -> list[str]:
        w, p, kind = self.e.world, action["params"], action["kind"]
        if kind in ("build", "build_stack", "flip_cap"):
            return ["tower:" + p["site_id"]]
        if kind == "take_from" and p["source_id"] in w["towers"]:
            return ["tower:" + p["source_id"]]
        s = w["supply"][team]
        if kind in ("pick_stack", "put_down_stack"):
            slots = ["BOX_LOWER", "BOX_UPPER"]
        elif kind == "put_down":
            slots = [p["slot_id"]]
        elif kind in ("pick", "take_from"):
            ids = p.get("item_ids", [p.get("item_id")])
            slots = [k for k, v in s["slots"].items() if v in ids]
        else:
            slots = []
        return [f"slot:{team}:{slot}" for slot in slots]

    def check_preconditions(self, team: str, action: dict[str, Any]) -> None:
        """CAS includes both real boxes and the slots implicated by this action."""
        w, pre = self.e.world, action["_preconditions"]
        for iid, expected in pre.get("item_versions", {}).items():
            require(iid in w["items"] and w["items"][iid]["version"] == expected,
                    "TARGET_CHANGED", "quoted material version changed during alignment")
        for resource in self.resources(team, action):
            if not resource.startswith("slot:"):
                continue
            _, owner, slot = resource.split(":")
            versions = pre.get("slot_versions", {})
            if slot in versions:
                require(w["supply"][owner]["slot_versions"][slot] == versions[slot],
                        "TARGET_CHANGED", "quoted slot version changed during alignment")
        if "box_bay_version" in pre:
            require(w["supply"][team]["box_bay_version"] == pre["box_bay_version"],
                    "TARGET_CHANGED", "quoted box bay changed during alignment")

    def contact(self, team: str, action: dict[str, Any]) -> None:
        e, w = self.e, self.e.world
        validate_operation(w, team, {"kind": action["kind"], "params": action["params"]})
        resources = self.resources(team, action)
        for resource in resources:
            if resource.startswith("slot:"):
                _, owner, slot = resource.split(":")
                require(w["supply"][owner]["slot_operations"][slot] is None, "RESOURCE_BUSY", "transfer slot operation in progress")
        for resource in resources:
            if resource.startswith("tower:"):
                t = w["towers"][resource[6:]]
                t["operation"] = action["id"]
                t["version"] += 1
            else:
                _, owner, slot = resource.split(":")
                w["supply"][owner]["slot_operations"][slot] = action["id"]
        action.update(phase="contact", _resources=resources, _contacted=True)
        if action["kind"] == "flip_cap":
            t = w["towers"][action["params"]["site_id"]]
            cap = w["items"][t["cap"]]
            action["_flip_original"] = deepcopy(cap)
            relocate(cap, "held_flip:" + t["id"], holder=team + "_br", support=team + "_br")
        e.emit("ACTION_CONTACT", team=team, action_id=action["id"], resources=resources)

    def release(self, action: dict[str, Any], changed: bool = False) -> None:
        w = self.e.world
        for resource in action.get("_resources", []):
            if resource.startswith("tower:"):
                t = w["towers"][resource[6:]]
                if t["operation"] == action["id"]:
                    t["operation"] = None
                    t["version"] += 1
                    if changed:
                        self.e.emit("TOWER_CHANGED", tower_id=t["id"], version=t["version"])
            else:
                _, owner, slot = resource.split(":")
                s = w["supply"][owner]
                if s["slot_operations"][slot] == action["id"]:
                    s["slot_operations"][slot] = None

    def restore_flip(self, action: dict[str, Any]) -> None:
        original = action.get("_flip_original")
        if original:
            current = self.e.world["items"][original["id"]]
            version = current["version"] + 1
            current.clear()
            current.update(deepcopy(original), version=version)
            self.e.emit("TOWER_RESTORED", tower_id=action["params"]["site_id"], item_id=original["id"], rule="3.5.8")

    def clear_slot(self, team: str, slot: str) -> None:
        s = self.e.world["supply"][team]
        s["slots"][slot] = None
        s["slot_versions"][slot] += 1
        if slot.startswith("BOX_"):
            s["box_bay_version"] += 1

    def fill_slot(self, team: str, slot: str, item: dict[str, Any]) -> None:
        s = self.e.world["supply"][team]
        support = s["slots"]["BOX_LOWER"] if slot == "BOX_UPPER" else "bay:" + team + ":" + slot
        relocate(item, "buffer:" + team + ":" + slot, support=support)
        s["slots"][slot] = item["id"]
        s["slot_versions"][slot] += 1
        if slot.startswith("BOX_"):
            s["box_bay_version"] += 1

    def preserve_base(self, action: dict[str, Any]) -> None:
        """Audit independent lower-box release on partial fault/terminal stop."""
        w, p = self.e.world, action["params"]
        stack = w["stacks"].get(p.get("stack_id"))
        if not stack:
            return
        t, iid = w["towers"][p["site_id"]], stack["bottom_item_id"]
        if t["base"] is None and not w["items"][iid]["disabled_for_use"]:
            t["base"] = iid
            relocate(w["items"][iid], "tower:" + t["id"] + ":base", support="green:" + t["id"], valid=True)
            break_stack(w, [iid])
            self.e.emit("PARTIAL_BASE_VALID", tower_id=t["id"], item_id=iid, rule="3.5.2 / 8.1.2")

    def complete(self, team: str, action: dict[str, Any]) -> list[str]:
        e, w, p, kind = self.e, self.e.world, action["params"], action["kind"]
        r, s = w["robots"][team + "_br"], w["supply"][team]
        validate_operation(w, team, {"kind": kind, "params": p}, own_token=action["id"] if action.get("_contacted") else None)
        ids = []
        if kind in ("pick_stack", "build_stack", "put_down_stack"):
            stack = w["stacks"][p["stack_id"]]
            ids = [stack["bottom_item_id"], stack["top_item_id"]]
            if kind == "pick_stack":
                for slot, iid in zip(("BOX_LOWER", "BOX_UPPER"), ids):
                    self.clear_slot(team, slot)
                    relocate(w["items"][iid], "held:" + r["id"], holder=r["id"], support=r["id"] if slot == "BOX_LOWER" else ids[0])
                stack.update(holder_id=r["id"], source_bay_id=None, version=stack["version"] + 1)
                s["stack_id"] = None
                r["carried_stack_id"] = stack["id"]
            elif kind == "build_stack":
                t = w["towers"][p["site_id"]]
                for level, iid, support in (("base", ids[0], "green:" + t["id"]), ("middle", ids[1], ids[0])):
                    t[level] = iid
                    relocate(w["items"][iid], "tower:" + t["id"] + ":" + level, support=support, valid=True)
                stack.update(holder_id=None, source_bay_id=None, version=stack["version"] + 1)
                r["carried_stack_id"] = None
            else:
                for slot, iid in zip(("BOX_LOWER", "BOX_UPPER"), ids):
                    self.fill_slot(team, slot, w["items"][iid])
                stack.update(holder_id=None, source_bay_id=team + ":BOX_STACK_BAY", version=stack["version"] + 1)
                s["stack_id"], r["carried_stack_id"] = stack["id"], None
        elif kind in ("pick", "take_from"):
            ids = p.get("item_ids", [p.get("item_id")])
            for iid in ids:
                if kind == "take_from" and p.get("source_id") in w["towers"]:
                    t = w["towers"][p["source_id"]]
                    t["base" if t["base"] == iid else "middle"] = None
                else:
                    slot = next(k for k, v in s["slots"].items() if v == iid)
                    self.clear_slot(team, slot)
                    break_stack(w, [iid])
                    if s["stack_id"] and not w["stacks"][s["stack_id"]]["intact"]:
                        s["stack_id"] = None
                relocate(w["items"][iid], "held:" + r["id"], holder=r["id"], support=r["id"])
        elif kind == "put_down":
            ids = [p["item_id"]]
            self.fill_slot(team, p["slot_id"], w["items"][ids[0]])
        elif kind == "build":
            iid, level, t = p["item_id"], p["level"], w["towers"][p["site_id"]]
            ids, t[level] = [iid], iid
            support = "green:" + t["id"] if level == "base" else t["base" if level == "middle" else "middle"]
            relocate(w["items"][iid], "tower:" + t["id"] + ":" + level, support=support, valid=True)
            if level == "cap":
                t["top_color"] = w["items"][iid]["orientation"]
        elif kind == "flip_cap":
            t = w["towers"][p["site_id"]]
            cap = w["items"][t["cap"]]
            cap["orientation"] = team
            relocate(cap, "tower:" + t["id"] + ":cap", support=t["middle"], valid=True)
            t["top_color"], ids = team, [cap["id"]]
        elif kind == "receive_ball":
            task = s["ball_task"]
            ids = [task["item_id"]]
            relocate(w["items"][ids[0]], "held:" + r["id"], holder=r["id"], support=r["id"])
            task["status"] = "held_by_br"
            e.supplies.start(team)
        elif kind == "place_ball":
            task = s["ball_task"]
            iid = next(i["id"] for i in r["cargo"] if i["kind"] == "ball")
            relocate(w["items"][iid], "central", support="central_cradle", valid=True)
            w["central_ball"] = {"team": team, "placed_by": team, "item_id": iid, "placed_at_ms": e.sim_time_ms}
            if task:
                task.update(status="placed", token_occupied=False)
            ids = [iid]
            e.emit("CENTRAL_BALL_PLACED", team=team, item_id=iid, points=250)
        self.release(action, changed=kind in ("build", "build_stack", "flip_cap", "take_from"))
        return ids
