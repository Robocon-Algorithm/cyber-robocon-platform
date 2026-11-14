"""从 L1 给 L2 公共塔安放箱子，再上 L2 加盖。"""
from __future__ import annotations

from brsdk import Action, Decision, Observation, Strategy, StrategyAPI


class MyStrategy(Strategy):
    @staticmethod
    def _move_toward(obs: Observation, point_id: str) -> Action | None:
        """返回下一段移动；已经到作业位时返回 None。"""
        pose = obs.self.pose
        target = obs.map.points[point_id]
        if pose.layer == target.layer:
            return None if pose.workpoint_id == point_id else Action.move_to(point_id)

        prefix = obs.self.team.upper()
        if pose.layer == "GROUND":
            return Action.traverse(prefix + "_RAMP", "L1")
        step = obs.map.connections[prefix + "_L2_STEP"]
        entrance = step.from_point_id if pose.layer == "L1" else step.to_point_id
        if pose.workpoint_id != entrance:
            return Action.move_to(entrance)
        return Action.traverse(step.id, target.layer)

    def _next_action(self, obs: Observation) -> Action | None:
        me = obs.self
        supply = obs.supply[me.team]
        transfer = me.team.upper() + "_TRANSFER"

        # 按塔位 ID 稳定选择。塔是公共的，L1 作业位仍可能是本队专用的。
        towers = []
        for tower_id in sorted(obs.towers):
            tower = obs.towers[tower_id]
            point = obs.map.points.get(tower.get("l1_approach_point_id"))
            if (tower.public and tower.layer == "L2" and tower.operation is None
                    and point and point.owner in (None, me.team)):
                towers.append(tower)

        empty = next((t for t in towers if not (t.base or t.middle or t.cap)), None)
        our_base = [t for t in towers if t.base and obs.items[t.base].owner_color == me.team]
        partial = next((t for t in our_base if not t.middle and not t.cap), None)
        uncapped = next((t for t in our_base if t.middle and not t.cap), None)
        # 如果供应槽里仍是整摞，补中箱时只拿上箱，不能抽走下箱。
        available_box = supply.slots.BOX_UPPER or supply.slots.BOX_LOWER

        # 持整摞时只能去空塔；箱子从 L1 放，盖子必须从 L2 放。
        if me.carried_stack_id and empty:
            return self._move_toward(obs, empty.l1_approach_point_id) or Action.build_stack(
                empty.id, me.carried_stack_id, empty.version)
        if me.carried_stack_id:
            return None
        if me.cargo:
            box = next((item for item in me.cargo if item.kind == "box"), None)
            cap = next((item for item in me.cargo if item.kind == "cap"), None)
            box_target = partial or empty
            if box and box_target:
                level = "middle" if partial else "base"
                return self._move_toward(obs, box_target.l1_approach_point_id) or Action.build(
                    box_target.id, box.id, level, box_target.version)
            if cap and uncapped:
                return self._move_toward(obs, uncapped.approach_point_id) or Action.build(
                    uncapped.id, cap.id, "cap", uncapped.version)
            if cap and not box and partial:
                # 先腾空盖槽让下一批到货，带着盖子等一个补中箱用的箱子。
                return self._move_toward(obs, transfer) or (
                    Action.pick([available_box]) if available_box else None)
            return None  # 目标暂时不可用，等下一次观察。

        if (uncapped or partial) and supply.slots.CAP_BAY:
            return self._move_toward(obs, transfer) or Action.pick([supply.slots.CAP_BAY])
        if partial and available_box:
            return self._move_toward(obs, transfer) or Action.pick([available_box])
        stack = obs.stacks.get(supply.stack_id)
        if empty and stack and stack.intact:
            return self._move_toward(obs, transfer) or Action.pick_stack(stack.id, stack.version)
        if empty and available_box:
            return self._move_toward(obs, transfer) or Action.pick([available_box])
        return self._move_toward(obs, transfer) if (uncapped or partial or empty) else None

    def on_step(self, obs: Observation, api: StrategyAPI) -> Decision | None:
        if obs.self.control_state != "READY" or obs.self.action is not None:
            return None
        action = self._next_action(obs)
        if action is None:
            return None
        # 每帧最多提交一步。若裁判因争用拒绝，下一帧用新观察重选。
        quote = api.quote(action)
        if quote.ok and quote.estimate_ms <= obs.remaining_ms:
            return api.execute(quote)
        return None
