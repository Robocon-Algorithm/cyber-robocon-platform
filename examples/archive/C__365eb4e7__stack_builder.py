"""Build nearby public and home towers, then plan the ball handoff."""
from __future__ import annotations

import math
from typing import Any

from brsdk import Action, Strategy, Observation, StrategyAPI, Decision


class MyStrategy(Strategy):
    def on_start(self, context: Any) -> None:
        self.goal_id: str | None = None
        self.avoid_until: dict[str, int] = {}

    def _navigate(self, obs: Observation, point_id: str) -> Action | None:
        me = obs.self
        point = obs.map.points[point_id]
        prefix = me.team.upper()
        if me.pose.layer == point.layer:
            return None if me.pose.workpoint_id == point_id else Action.move_to(point_id)
        if me.pose.layer == "GROUND":
            return Action.traverse(prefix + "_RAMP", "L1")
        connection = obs.map.connections[prefix + "_L2_STEP"]
        entrance = connection.from_point_id if me.pose.layer == "L1" else connection.to_point_id
        if me.pose.workpoint_id != entrance:
            return Action.move_to(entrance)
        return Action.traverse(connection.id, point.layer)

    def _choose(self, obs: Observation, candidates: list[Any]) -> Any:
        if not candidates:
            return None
        if self.goal_id:
            existing = next((t for t in candidates if t.id == self.goal_id), None)
            if existing is not None and self.avoid_until.get(existing.id, 0) <= obs.sim_time_ms:
                return existing
        has_public_full = any(t.public and t.base and t.middle and t.cap and t.top_color == obs.self.team
                              for t in obs.towers.values())
        def rank(t: Any) -> tuple[Any, ...]:
            avoid = self.avoid_until.get(t.id, 0) > obs.sim_time_ms
            opening = 0 if t.id == "L1_PUBLIC_1" else 1
            layer_cost = 0 if t.layer == obs.self.pose.layer else 5000
            dist = math.hypot(t.center.x_mm - obs.self.pose.x_mm, t.center.y_mm - obs.self.pose.y_mm) + layer_cost
            public_cost = 0 if t.public and not has_public_full else 1
            return avoid, public_cost, opening, dist, t.id

        goal = min(candidates, key=rank)
        self.goal_id = goal.id
        return goal

    def on_step(self, obs: Observation, api: StrategyAPI) -> Decision | None:
        me = obs.self
        team = me.team
        supply = obs.supply[team]
        task = supply.ball_task
        task_status = task.get("status") if task else None
        central_done = bool(obs.central_ball)
        request = bool(obs.qualified[team] and not central_done and task_status in (None, "failed", "cancelled"))
        for receipt in obs.receipts:
            if receipt.code in ("TARGET_CHANGED", "RESOURCE_CONTESTED", "RESOURCE_BUSY") and self.goal_id:
                self.avoid_until[self.goal_id] = obs.sim_time_ms + 3000
                self.goal_id = None
        if me.control_state != "READY":
            return api.request_ball() if request else None
        if me.action is not None:
            goal = obs.towers.get(self.goal_id)
            if (me.carried_stack_id and goal and any(goal[k] is not None for k in ("base", "middle", "cap"))
                    and me.action.kind == "move_to" and me.action.cancel_state == "NONE"):
                self.goal_id = None
                return api.cancel(me.action.id, request_ball=request)
            return api.request_ball() if request else None

        def submit(action: Action | None) -> Decision | None:
            if action is not None:
                quote = api.quote(action)
                if quote.ok:
                    return api.execute(quote, request_ball=request)
            return api.request_ball() if request else None

        cargo = me.cargo
        legal_towers = [t for t in obs.towers.values() if t.owner in (None, team) and t.operation is None]
        if cargo and cargo[0].kind == "ball":
            return submit(self._navigate(obs, "CENTRAL_BALL_APPROACH") or Action.place_ball())
        if me.carried_stack_id:
            candidates = [t for t in legal_towers if not t.base and not t.middle and not t.cap]
            goal = self._choose(obs, candidates)
            if goal:
                return submit(self._navigate(obs, goal.approach_point_id) or Action.build_stack(goal.id, me.carried_stack_id, goal.version))
            return submit(self._navigate(obs, team.upper() + "_TRANSFER") or Action.put_down_stack(
                me.carried_stack_id, "BOX_STACK_BAY", supply.box_bay_version))
        if cargo:
            item = cargo[0]
            if item.kind == "cap":
                candidates = [t for t in legal_towers if t.base and t.middle and not t.cap]
                level = "cap"
            else:
                candidates = [t for t in legal_towers if not t.base or (not t.middle and not t.cap)]
                level = "base"
            goal = self._choose(obs, candidates)
            if goal:
                if item.kind == "box" and goal.base:
                    level = "middle"
                return submit(self._navigate(obs, goal.approach_point_id) or Action.build(goal.id, item.id, level, goal.version))
            slot = "CAP_BAY" if item.kind == "cap" else "BOX_LOWER"
            if item.kind == "box" and supply.slots.BOX_LOWER:
                slot = "BOX_UPPER"
            return submit(self._navigate(obs, team.upper() + "_TRANSFER") or Action.put_down(item.id, slot, supply.slot_versions[slot]))

        # A ball in transit owns the service token: arrive empty and wait there.
        if not central_done and task_status in ("in_transit", "waiting_handoff"):
            move = self._navigate(obs, team.upper() + "_TRANSFER")
            return submit(move or (Action.receive_ball() if task_status == "waiting_handoff" else None))

        unfinished = [t for t in legal_towers if t.base and t.middle and not t.cap]
        cap_id = supply.slots.CAP_BAY
        if cap_id and unfinished:
            return submit(self._navigate(obs, team.upper() + "_TRANSFER") or Action.pick([cap_id]))
        empties = [t for t in legal_towers if not t.base and not t.middle and not t.cap]
        stack = obs.stacks.get(supply.stack_id)
        if stack and stack.intact and empties:
            # Clear both box slots to allow the committed next batch to arrive.
            return submit(self._navigate(obs, team.upper() + "_TRANSFER") or Action.pick_stack(stack.id, stack.version))
        # Recovery: consume a surviving loose box; never manufacture a stack.
        loose_id = supply.slots.BOX_UPPER or supply.slots.BOX_LOWER
        if loose_id and any(not t.base or (not t.middle and not t.cap) for t in legal_towers):
            return submit(self._navigate(obs, team.upper() + "_TRANSFER") or Action.pick([loose_id]))
        return submit(self._navigate(obs, team.upper() + "_TRANSFER"))
