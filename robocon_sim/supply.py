"""Finite virtual-TR conveyor, backpressure and the independent ball service."""
from __future__ import annotations
from typing import TYPE_CHECKING, Any

from .rules import require
from .world import new_item, relocate

ACTIVE_BALL = {"queued", "in_transit", "waiting_handoff", "held_by_br", "clearing"}

if TYPE_CHECKING:
    from .engine import Engine


class Supply:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.batch_counts = {"red": 0, "blue": 0}
        self.ball_counts = {"red": 0, "blue": 0}

    def start(self, team: str) -> None:
        e, s = self.e, self.e.world["supply"][team]
        if s["control_state"] != "READY" or s["batch"] is not None:
            return
        task = s["ball_task"]
        if task and task["status"] == "queued":
            task.update(status="in_transit", expected_arrival_ms=e.sim_time_ms + e.param(team, "ball_transport_ms"))
            e.schedule(task["expected_arrival_ms"], "ball_arrival", team=team, task_id=task["id"])
            e.faults.arm(team, "TR", "ball_transport", [task["item_id"]], task["id"],
                         e.param(team, "ball_transport_ms"), risk_start_ms=e.sim_time_ms)
            e.emit("BALL_DISPATCHED", team=team, task=task.copy())
            return
        if task and task["status"] in ("in_transit", "waiting_handoff", "clearing"):
            return
        inv = s["inventory"]
        if not inv["boxes_remaining"] and not inv["caps_remaining"]:
            return
        self.batch_counts[team] += 1
        bid = f"{team}:batch:{self.batch_counts[team]}"
        ids = []
        for kind, count in (("box", min(2, inv["boxes_remaining"])), ("cap", min(1, inv["caps_remaining"]))):
            inv["boxes_remaining" if kind == "box" else "caps_remaining"] -= count
            for n in range(count):
                iid = f"{bid}:{kind}:{n + 1}"
                e.world["items"][iid] = new_item(iid, kind, team, "transit:" + team)
                ids.append(iid)
        boxes = [i for i in ids if e.world["items"][i]["kind"] == "box"]
        sid = None
        if len(boxes) == 2:
            sid = bid + ":stack"
            e.world["stacks"][sid] = {"id": sid, "version": 0, "bottom_item_id": boxes[0],
                                       "top_item_id": boxes[1], "intact": True,
                                       "holder_id": None, "source_bay_id": None}
            e.world["items"][boxes[1]]["support_id"] = boxes[0]
        arrival = e.sim_time_ms + int(e.config.supply_delays_ms[team])
        batch = {"id": bid, "item_ids": ids, "stack_id": sid, "status": "in_transit",
                 "departed_at_ms": e.sim_time_ms, "expected_arrival_ms": arrival}
        s["batch"] = batch
        e.schedule(arrival, "supply_arrival", team=team, batch_id=bid)
        e.faults.arm(team, "TR", "transport", ids, bid, arrival - e.sim_time_ms,
                     risk_start_ms=e.sim_time_ms)
        e.emit("MATERIAL_DISPATCHED", team=team, batch=batch.copy())

    def request_ball(self, team: str) -> None:
        e, s = self.e, self.e.world["supply"][team]
        require(e.world["qualified"][team], "RULE_FORBIDDEN", "permanent two-tower qualification required", "3.6.1")
        require(e.world["central_ball"] is None, "TARGET_CHANGED", "central ball already placed")
        task = s["ball_task"]
        require(not task or task["status"] not in ACTIVE_BALL, "RESOURCE_BUSY", "one active ball token per team")
        self.ball_counts[team] += 1
        tid = f"{team}:ball_task:{self.ball_counts[team]}"
        iid = tid + ":ball"
        e.world["items"][iid] = new_item(iid, "ball", team, "stock")
        s["ball_task"] = {"id": tid, "item_id": iid, "status": "queued", "requested_at_ms": e.sim_time_ms,
                           "expected_arrival_ms": None, "token_occupied": True}
        e.emit("BALL_REQUESTED", team=team, task_id=tid)
        self.start(team)

    def required_slots(self, team: str) -> dict[str, str]:
        s, w = self.e.world["supply"][team], self.e.world
        b = s["batch"]
        if not b:
            return {}
        boxes = [i for i in b["item_ids"] if w["items"][i]["kind"] == "box" and not w["items"][i]["disabled_for_use"]]
        caps = [i for i in b["item_ids"] if w["items"][i]["kind"] == "cap" and not w["items"][i]["disabled_for_use"]]
        return {**dict(zip(("BOX_LOWER", "BOX_UPPER"), boxes)), **({"CAP_BAY": caps[0]} if caps else {})}

    def try_deliver(self, team: str, contested_slots: tuple[str, ...] | set[str] = ()) -> bool:
        e, s = self.e, self.e.world["supply"][team]
        b = s["batch"]
        if s["control_state"] != "READY" or not b or b["status"] != "waiting_slots":
            return False
        if s.get("_delivery_after_ms", -1) >= e.sim_time_ms:
            return False
        slots = self.required_slots(team)
        if any(s["slots"][k] or s["slot_operations"][k] or k in contested_slots for k in slots):
            return False
        for slot, iid in slots.items():
            support = slots.get("BOX_LOWER") if slot == "BOX_UPPER" else "bay:" + team + ":" + slot
            item = e.world["items"][iid]
            relocate(item, "buffer:" + team + ":" + slot, support=support)
            if item["kind"] == "cap" and not item["transported_by"]:
                item["transported_by"] = team
            s["slots"][slot] = iid
            s["slot_versions"][slot] += 1
        if any(k.startswith("BOX_") for k in slots):
            s["box_bay_version"] += 1
        sid = b.get("stack_id")
        stack = e.world["stacks"].get(sid)
        if stack and stack["intact"] and len(slots) >= 2 and "BOX_UPPER" in slots:
            stack.update(source_bay_id=team + ":BOX_STACK_BAY", version=stack["version"] + 1)
            s["stack_id"] = sid
        b.update(status="delivered", delivered_at_ms=e.sim_time_ms)
        e.emit("MATERIAL_DELIVERED", team=team, batch=b.copy(), item_ids=list(slots.values()))
        s["batch"] = None
        self.start(team)
        return True

    def ball_arrival(self, team: str, task_id: str) -> None:
        s = self.e.world["supply"][team]
        task = s["ball_task"]
        if s["control_state"] == "READY" and task and task["id"] == task_id and task["status"] == "in_transit":
            task["status"] = "waiting_handoff"
            relocate(self.e.world["items"][task["item_id"]], "handoff:" + team)
            self.e.emit("BALL_HANDOFF_READY", team=team, item_id=task["item_id"])

    def clear_ball(self, team: str, status: str = "cancelled", pause_br: bool = False) -> None:
        e, s = self.e, self.e.world["supply"][team]
        task = s["ball_task"]
        if not task or task["status"] not in ACTIVE_BALL:
            return
        task.update(status="clearing", clearance_result=status, clearance_until_ms=e.sim_time_ms + e.param(team, "central_clearance_ms"))
        if pause_br:
            e.abort_action(team, "BALL_CLEARANCE", "invalid ball cleared safely")
            e.world["robots"][team + "_br"]["control_state"] = "PAUSED"
        e.schedule(task["clearance_until_ms"], "ball_clearance", team=team, task_id=task["id"], pause_br=pause_br)
        e.emit("BALL_CLEARANCE_STARTED", team=team, until_ms=task["clearance_until_ms"])

    def finish_clearance(self, team: str, task_id: str, pause_br: bool) -> None:
        e, s = self.e, self.e.world["supply"][team]
        task = s["ball_task"]
        if not task or task["id"] != task_id or task["status"] != "clearing":
            return
        relocate(e.world["items"][task["item_id"]], "out")
        task.update(status=task.pop("clearance_result", "failed"), token_occupied=False)
        if pause_br:
            r = e.world["robots"][team + "_br"]
            if r["control_state"] == "PAUSED":
                r["control_state"] = "READY"
        e.emit("BALL_CLEARED", team=team, task_id=task_id)
        self.start(team)
