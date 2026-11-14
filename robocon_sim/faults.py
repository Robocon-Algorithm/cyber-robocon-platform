"""Small deterministic fault scripts bound to stable material identities."""
from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any
from .world import break_stack, relocate

if TYPE_CHECKING:
    from .engine import Engine


class Faults:
    def __init__(self, engine: Engine, scripts: list[dict[str, Any]]) -> None:
        self.e = engine
        self.scripts = []
        for n, src in enumerate(scripts):
            f = deepcopy(src)
            f.update(id=f.get("id", f"fault:{n + 1}"), status="pending", seen=0,
                     affected_item_ids=[], context_id=None)
            f.setdefault("role", f.get("robot", "BR").upper())
            f.setdefault("material_kind", f.get("item_kind", "box"))
            f.setdefault("occurrence", f.get("nth", 1))
            f.setdefault("mode", "drop")
            self.scripts.append(f)

    def arm(self, team, role, action_kind, item_ids, context_id, duration_ms, risk_start_ms=None):
        e = self.e
        start = e.sim_time_ms if risk_start_ms is None else risk_start_ms
        for f in self.scripts:
            if f["status"] == "triggered" or f.get("team", team) != team or f["role"].upper() != role:
                continue
            if f.get("action_kind") and f["action_kind"] != action_kind:
                continue
            eligible = [iid for iid in item_ids if e.world["items"][iid]["kind"] == f["material_kind"]
                        and not e.world["items"][iid]["disabled_for_use"]]
            if not eligible:
                continue
            if f["status"] == "pending":
                f["seen"] += 1
                if f["seen"] < int(f["occurrence"]):
                    continue
                chosen = eligible[-1:] if f["mode"] == "upper_slip" else (eligible if f["mode"] == "stack_drop" else eligible[:1])
                f.update(status="armed", affected_item_ids=chosen, bound_team=team,
                         bound_role=role, bound_action_kind=action_kind)
            elif not set(f["affected_item_ids"]).issubset(eligible):
                continue
            if f.get("bound_action_kind") != action_kind:
                continue
            offset = int(f.get("offset_ms", max(1, int(duration_ms * (0.85 if f.get("phase") == "stable_release" else 0.5)))))
            offset = max(1, min(max(1, duration_ms - 1), offset))
            f["context_id"] = context_id
            f["trigger_at_ms"] = start + offset
            e.schedule(start + offset, "fault", fault_id=f["id"], context_id=context_id)

    def trigger(self, fault_id: str, context_id: str) -> None:
        e = self.e
        f = next(f for f in self.scripts if f["id"] == fault_id)
        if f["status"] != "armed" or f["context_id"] != context_id:
            return
        if any(other["status"] == "triggered" for other in self.scripts):
            return
        team, role = f["bound_team"], f["bound_role"]
        s, r = e.world["supply"][team], e.world["robots"][team + "_br"]
        if role == "BR":
            if not r["action"] or r["action"]["id"] != context_id:
                return
        else:
            task, b = s["ball_task"], s["batch"]
            if not ((b and b["id"] == context_id and b["status"] == "in_transit") or
                    (task and task["id"] == context_id and task["status"] == "in_transit")):
                return
        affected = f["affected_item_ids"]
        if any(e.world["items"][iid]["disabled_for_use"] for iid in affected):
            return
        f.update(status="triggered", triggered_at_ms=e.sim_time_ms)
        kind = f["material_kind"]
        impact = f.get("collision_tower_id", f.get("tower_impact_id"))
        if f["mode"] == "tower_impact" and role == "BR" and not impact:
            impact = r["action"]["params"].get("site_id")
        impacted = impact in e.world["towers"]
        if (role == "BR" and not impacted and r["action"]["kind"] == "build_stack"
                and r["action"].get("support_state", {}).get("base_seated")
                and f.get("phase") == "stable_release" and f["mode"] == "upper_slip"):
            # This script explicitly models lower detachment before upper slip.
            e.ops.preserve_base(r["action"])
        # A failed flip restores the original cap instead of disabling a new copy.
        flip = role == "BR" and r["action"]["kind"] == "flip_cap"
        if flip:
            e.ops.restore_flip(r["action"])
        else:
            for iid in affected:
                item = e.world["items"][iid]
                item["disabled_for_use"] = True
                relocate(item, "out" if kind == "ball" else ("disabled:L1:" + team if role == "BR" else "disabled:GROUND:" + team))
                for slot in s["slots"]:
                    if s["slots"][slot] == iid:
                        s["slots"][slot] = None
                        s["slot_versions"][slot] += 1
                        s["box_bay_version"] += int(slot.startswith("BOX_"))
            break_stack(e.world, affected)
            sid = s.get("stack_id")
            if sid and not e.world["stacks"][sid]["intact"]:
                s["stack_id"] = None
        e.emit("ITEM_DROPPED", team=team, role=role, affected_item_ids=affected,
               mode=f["mode"], fault_id=f["id"], rule="6.8" if impacted else ("5.1.3" if kind == "box" else "6.2"))
        if impacted:
            # No collision physics: the disturbance and referee restoration form one
            # event. The pre-impact order, supports and orientation remain authoritative.
            t = e.world["towers"][impact]
            t["version"] += 1
            e.emit("TOWER_RESTORED", team=team, tower_id=impact, rule="6.8",
                   item_ids=[t[k] for k in ("base", "middle", "cap") if t[k]], top_color=t["top_color"])
        if role == "BR":
            e.abort_action(team, "FAULT", "material fault; support audited individually", restore_flip=False)
            self.recover_br(team, forced=impacted or kind != "box", ball=kind == "ball")
        else:
            self.recover_tr(team, forced=impacted or kind != "box", ball=kind == "ball")

    def recover_br(self, team, forced, ball=False):
        e, r = self.e, self.e.world["robots"][team + "_br"]
        r["control_state"] = "RETRYING" if forced else "PAUSED"
        if forced:
            return_at = e.sim_time_ms + e.param("retry_return_ms")
            until = return_at + max(e.param("retry_hold_ms"), e.param("retry_clearance_ms"))
            r.update(recovery_phase="returning", return_at_ms=return_at, recovery_until_ms=until)
            e.schedule(return_at, "br_returned", team=team)
            e.emit("RETRY_STARTED", team=team, return_at_ms=return_at, recovery_until_ms=until)
        else:
            until = e.sim_time_ms + e.param("box_clearance_ms")
            r.update(recovery_phase="box_clearance", recovery_until_ms=until)
            e.emit("BOX_CLEARANCE_STARTED", team=team, recovery_until_ms=until)
        if ball:
            task = e.world["supply"][team]["ball_task"]
            if task:
                task.update(status="clearing", clearance_result="failed", clearance_until_ms=until)
                e.schedule(until, "ball_clearance", team=team, task_id=task["id"], pause_br=False)
        e.schedule(until, "br_recovered", team=team)

    def recover_tr(self, team, forced, ball=False):
        e, s = self.e, self.e.world["supply"][team]
        s["control_state"] = "RETRYING" if forced else "PAUSED"
        if forced:
            return_at = e.sim_time_ms + e.param("tr_retry_return_ms")
            until = return_at + max(e.param("retry_hold_ms"), e.param("tr_retry_clearance_ms"))
            s.update(recovery_phase="returning", return_at_ms=return_at, recovery_until_ms=until)
            e.schedule(return_at, "tr_returned", team=team)
        else:
            until = e.sim_time_ms + e.param("box_clearance_ms")
            s.update(recovery_phase="box_clearance", recovery_until_ms=until)
        if s["batch"]:
            s["batch"].update(status="recovering", expected_arrival_ms=until + int(e.config.supply_delays_ms[team]))
        if ball and s["ball_task"]:
            task = s["ball_task"]
            task.update(status="clearing", clearance_result="failed", clearance_until_ms=until)
            e.schedule(until, "ball_clearance", team=team, task_id=task["id"], pause_br=False)
        e.schedule(until, "tr_recovered", team=team)
        e.emit("TR_RETRY_STARTED" if forced else "TR_BOX_CLEARANCE_STARTED", team=team, recovery_until_ms=until)

    def recovered_tr(self, team):
        e, s = self.e, self.e.world["supply"][team]
        s.update(control_state="READY", recovery_phase=None, recovery_until_ms=None)
        b = s["batch"]
        if b:
            b["item_ids"] = [i for i in b["item_ids"] if not e.world["items"][i]["disabled_for_use"]]
            b.update(status="in_transit", expected_arrival_ms=e.sim_time_ms + int(e.config.supply_delays_ms[team]))
            e.schedule(b["expected_arrival_ms"], "supply_arrival", team=team, batch_id=b["id"])
        else:
            e.supplies.start(team)
        e.emit("TR_RETRY_READY", team=team)

    def statistics(self, final: bool = False) -> list[dict[str, Any]]:
        return [{"id": f["id"], "status": "not_triggered" if final and f["status"] != "triggered" else f["status"],
                 "team": f.get("bound_team", f.get("team")), "role": f["role"],
                 "affected_item_ids": f["affected_item_ids"], "triggered_at_ms": f.get("triggered_at_ms")}
                for f in self.scripts]
