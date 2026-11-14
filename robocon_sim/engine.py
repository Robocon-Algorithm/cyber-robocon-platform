"""One deterministic integer-millisecond clock and simultaneous command batches."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import heapq
import json
from typing import Any
import zlib

from .config import MatchConfig
from .map import default_map
from .planning import quote_action, motion_at, braking_preview
from .world import make_world, point_pose, sync_cargo, TEAMS, relocate
from .rules import RULESET_VERSION, RuleError, require, validate_operation, score_world, update_qualification, tiebreak
from .supply import Supply, ACTIVE_BALL
from .faults import Faults
from .operations import Operations


_SHARED_KEYS = ("map", "parameters")


def _public(value: Any) -> Any:
    # Scalars are immutable, so they can be published as-is. Calling deepcopy on
    # every leaf was the single largest cost of building a frame snapshot: the
    # world has ~1000 leaves and deepcopy dispatches on each of them.
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {k: _public(v) for k, v in value.items() if not k.startswith("_")}
    if isinstance(value, list):
        return [_public(v) for v in value]
    return deepcopy(value)


def _static_stamp(value: Any) -> int:
    """Cheap non-cryptographic guard against in-place mutation of shared statics."""
    return zlib.crc32(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


class Engine:
    def __init__(self, config: MatchConfig | dict[str, Any] | None = None,
                 map_data: dict[str, Any] | None = None) -> None:
        self.config = config if isinstance(config, MatchConfig) else MatchConfig.from_dict(config or {})
        self.sim_time_ms = 0
        self.seq = 0
        self.done = False
        self.events = []
        self.world_revision = 0
        self.world = make_world(map_data or default_map(), self.config.auto_entry)
        # Physical parameters are per side. With one profile both entries hold the
        # same dict; a perturbation profile only needs to differ for one side.
        self._parameters = {team: self.config.merged_parameters(team) for team in TEAMS}
        self.world["parameters"] = self._parameters
        # Published read-only statics: shared by reference with every snapshot
        # instead of being copied 2-3 times per frame. Guarded by _static_stamp.
        self._statics = {key: self.world[key] for key in _SHARED_KEYS}
        self._static_stamps = {key: _static_stamp(value) for key, value in self._statics.items()}
        self._queue = []
        self._event_serial = 0
        self._action_serial = 0
        self._receipt_serial = 0
        self._event_window_start = 0
        self._receipts = {team: [] for team in TEAMS}
        self._receipt_window_start = {team: 0 for team in TEAMS}
        self._technical_failures = {}
        self.faults = Faults(self, self.config.faults)
        self.supplies = Supply(self)
        self.ops = Operations(self)
        for team in TEAMS:
            self.supplies.start(team)
            if self.config.auto_entry:
                self._start_entry(team)
        self._audit()

    def param(self, team: str, name: str) -> int:
        return int(self._parameters[team][name])

    def schedule(self, at: int, event_type: str, **data: Any) -> None:
        at = int(at)
        if at <= self.sim_time_ms:
            raise ValueError("events must be scheduled at a future positive-duration stage")
        self._event_serial += 1
        heapq.heappush(self._queue, (at, self._event_serial, event_type, data))

    def emit(self, event_type: str, **data: Any) -> dict[str, Any]:
        self.world_revision += 1
        event = {"id": f"event:{len(self.events) + 1}", "type": event_type,
                 "occurred_at_ms": self.sim_time_ms, **deepcopy(data)}
        self.events.append(event)
        return event

    def receipt(self, team: str, code: str, reason: str, action: dict[str, Any] | None = None,
                command_id: str | None = None, **extra: Any) -> None:
        self._receipt_serial += 1
        a = action or {}
        receipt = {"id": f"receipt:{self._receipt_serial}", "command_id": command_id or a.get("command_id"),
                   "action_id": a.get("id"), "code": code, "reason": reason,
                   "occurred_at_ms": self.sim_time_ms, **extra}
        self._receipts[team].append(receipt)

    def _start_entry(self, team):
        r = self.world["robots"][team + "_br"]
        start, target, duration = deepcopy(r["pose"]), point_pose(self.world["map"], team.upper() + "_TRANSFER"), self.param(team, "entry_ms")
        self._action_serial += 1
        action = {"id": f"action:{self._action_serial}", "kind": "entry", "target": team.upper() + "_TRANSFER",
                  "params": {}, "phase": "traverse", "started_at_ms": 0, "expected_end_ms": duration,
                  "cancel_state": "NONE", "segments": [{"kind": "traverse", "start_ms": 0, "end_ms": duration,
                                                            "from_pose": start, "to_pose": target}], "_target_pose": target}
        r["action"] = action
        self.schedule(duration, "action_done", team=team, action_id=action["id"])
        self.emit("ENTRY_STARTED", team=team, expected_end_ms=duration)

    def _audit(self, score: bool = True):
        sync_cargo(self.world)
        for t in self.world["towers"].values():
            if t["cap"]:
                t["top_color"] = self.world["items"][t["cap"]]["orientation"]
            else:
                t["top_color"] = None
        for team in update_qualification(self.world):
            self.emit("QUALIFIED", team=team, rule="3.6.1 / 3.6.2")
        if score:
            # Full re-scoring is only needed at published boundaries and at the
            # final result. `world_revision` must NOT be used as a dirty flag:
            # relocate(), tower writes and ops.contact() mutate the world without
            # emitting an event, so a revision-based skip would silently miss them.
            self.world["score"] = score_world(self.world, self.done)

    def _update_motion(self):
        for r in self.world["robots"].values():
            a = r["action"]
            if not a:
                r["motion_state"] = {"speed_mm_s": 0, "linear_speed_mm_s": 0,
                                     "vx_mm_s": 0, "vy_mm_s": 0, "angular_speed_mrad_s": 0,
                                     "braking_model": "constant-deceleration-v1"}
                continue
            state = motion_at(a["segments"], self.sim_time_ms - a["started_at_ms"])
            if state["pose"] is not None:
                r["pose"] = state.pop("pose")
            # The segment list is already published verbatim as r["action"]["segments"];
            # duplicating it here cost 3.4 KB per frame and had no consumer.
            r["motion_state"] = {**state, "linear_speed_mm_s": state["speed_mm_s"],
                                 "braking_model": "constant-deceleration-v1"}
            if a["kind"] in ("move_to", "entry", "traverse") and a["cancel_state"] != "BRAKING":
                a["phase"] = state.get("segment_kind", "moving")

    def public_state(self) -> dict[str, Any]:
        self._update_motion()
        self._audit()
        body = {k: v for k, v in self.world.items() if k not in _SHARED_KEYS}
        body.update({"seq": self.seq, "sim_time_ms": self.sim_time_ms,
                     "world_revision": self.world_revision,
                     "remaining_ms": max(0, self.config.duration_ms - self.sim_time_ms),
                     "done": self.done, "ruleset_version": RULESET_VERSION,
                     "events": self.events[self._event_window_start:]})
        public = _public(body)
        public.update(self._statics)
        return public

    def observations(self, shared: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        shared = self.public_state() if shared is None else shared
        result = {}
        for team in TEAMS:
            other = "blue" if team == "red" else "red"
            obs = dict(shared)
            # Only the two robot subtrees are copied per team; every other value is
            # written once by the audit and never mutated afterwards. This keeps the
            # per-team envelope independent (pose edits do not leak across teams)
            # without deep-copying the whole 30-45 KB world twice.
            obs["robots"] = {robot_id: deepcopy(robot) for robot_id, robot in shared["robots"].items()}
            obs["self"] = obs["robots"][team + "_br"]
            obs["opponent"] = obs["robots"][other + "_br"]
            # The envelope carries the flat parameter set of this side, so quotes
            # and braking previews use the same physics the kernel applies.
            obs["parameters"] = self._parameters[team]
            obs["receipts"] = [dict(r) for r in self._receipts[team][self._receipt_window_start[team]:]]
            result[team] = obs
        return result

    def _submit(self, team, decision, obs):
        if decision is None:
            return
        command = f"command:{self.seq}:{team}"
        try:
            require(isinstance(decision, dict), "INVALID_DECISION", "decision must be a dictionary or null")
            require(type(decision.get("based_on_seq")) is int and decision["based_on_seq"] == self.seq,
                    "STALE_OBSERVATION", "decision is not based on current common boundary")
            require(isinstance(decision.get("request_ball", False), bool), "INVALID_DECISION", "request_ball must be boolean")
            primary = decision.get("primary")
            require(primary is None or isinstance(primary, dict), "INVALID_DECISION", "primary must be a dictionary or null")
            if primary is not None:
                require(primary.get("kind") in ("execute", "cancel"), "INVALID_DECISION", "unknown primary command")
                if primary["kind"] == "cancel":
                    require(isinstance(primary.get("action_id"), str), "INVALID_DECISION", "cancel needs an action identifier")
                else:
                    intent = primary.get("action")
                    require(isinstance(intent, dict) and isinstance(intent.get("kind"), str)
                            and isinstance(intent.get("params"), dict), "INVALID_DECISION", "execute needs a structured action")
                    require(type(primary.get("quote_seq")) is int, "INVALID_DECISION", "quote_seq must be integer")
                    require(isinstance(primary.get("preview_only", False), bool), "INVALID_DECISION", "preview_only must be boolean")
        except RuleError as exc:
            self.receipt(team, exc.code, exc.reason, command_id=command)
            return
        if decision.get("request_ball"):
            try:
                self.supplies.request_ball(team)
                self.receipt(team, "BALL_REQUEST_ACCEPTED", "ball service queued independently", command_id=command + ":ball")
            except RuleError as exc:
                self.receipt(team, exc.code, exc.reason, command_id=command + ":ball", channel="ball")
        if primary is None:
            return
        try:
            require(primary.get("kind") in ("execute", "cancel"), "INVALID_DECISION", "unknown primary command")
            if primary["kind"] == "cancel":
                self._cancel(team, primary.get("action_id"), command, obs)
                return
            require(primary.get("quote_seq") == self.seq, "STALE_OBSERVATION", "quote is from an old frame")
            require(not primary.get("preview_only"), "PREVIEW_ONLY", "braking previews cannot execute")
            action = primary.get("action")
            quote = quote_action(obs, action)
            require(quote["ok"], quote["error"], quote.get("reason", "action rejected"))
            validate_operation(self.world, team, action)
            self._begin(team, action, quote, command)
        except RuleError as exc:
            self.receipt(team, exc.code, exc.reason, command_id=command, rule=exc.rule)

    def _begin(self, team, action, quote, command):
        self._action_serial += 1
        kind, params = action["kind"], deepcopy(action["params"])
        target = params.get("point_id", params.get("site_id", params.get("stack_id", params.get("source_id", params.get("connection_id")))))
        a = {"id": f"action:{self._action_serial}", "command_id": command, "kind": kind,
             "params": params, "target": target, "phase": "align" if quote["alignment_ms"] else "moving",
             "started_at_ms": self.sim_time_ms, "expected_end_ms": quote["expected_end_ms"],
             "cancel_state": "NONE", "segments": deepcopy(quote["segments"]),
             "_target_pose": deepcopy(quote["target_pose"]), "_preconditions": deepcopy(quote["preconditions"])}
        self.world["robots"][team + "_br"]["action"] = a
        if quote["alignment_ms"]:
            self.schedule(self.sim_time_ms + quote["alignment_ms"], "contact", team=team, action_id=a["id"])
        else:
            self._arm_action_fault(team, a)
        self.schedule(a["expected_end_ms"], "action_done", team=team, action_id=a["id"])
        self.receipt(team, "ACCEPTED", "action accepted; result awaits physical completion", a)

    def _arm_action_fault(self, team, action):
        ids = action["_preconditions"].get("item_ids", [])
        if not ids:
            ids = [i["id"] for i in self.world["robots"][team + "_br"]["cargo"]]
        self.faults.arm(team, "BR", action["kind"], ids, action["id"],
                        action["expected_end_ms"] - self.sim_time_ms, risk_start_ms=self.sim_time_ms)

    def _cancel(self, team, action_id, command, obs=None):
        r = self.world["robots"][team + "_br"]
        a = r["action"]
        require(a and a["id"] == action_id, "TARGET_CHANGED", "action no longer active")
        require(a["kind"] != "entry", "NOT_CANCELLABLE", "automatic entry must finish at its safe L1 node")
        require(a["cancel_state"] == "NONE", "CANCEL_PENDING", "safe stop already requested")
        if a["kind"] == "move_to":
            # Reuse the envelope already frozen for this decision instead of
            # rebuilding the whole public state just to read the self subtree.
            stop, segments = braking_preview(obs if obs is not None else self.observations()[team])
            if not segments:
                segments = [{"kind": "wait", "start_ms": 0, "end_ms": 1,
                             "from_pose": deepcopy(r["pose"]), "to_pose": stop}]
            a.update(cancel_state="BRAKING", phase="braking", segments=segments,
                     started_at_ms=self.sim_time_ms, expected_end_ms=self.sim_time_ms + segments[-1]["end_ms"],
                     _target_pose=stop)
            # Original completion is ignored by its expected timestamp guard.
            self.schedule(a["expected_end_ms"], "action_done", team=team, action_id=a["id"])
            self.receipt(team, "CANCEL_PENDING", "braking to physical stop", a, command_id=command)
        elif a["kind"] == "traverse" or a.get("_contacted"):
            a["cancel_state"] = "SAFE_NODE"
            self.receipt(team, "CANCEL_PENDING", "finish contact/traverse at next safe node", a, command_id=command)
        else:
            a["cancel_state"] = "CANCELLED"
            self.abort_action(team, "CANCELLED", "alignment stopped before contact")

    def abort_action(self, team: str, code: str, reason: str, restore_flip: bool = True) -> None:
        r = self.world["robots"][team + "_br"]
        a = r["action"]
        if a:
            if restore_flip and a["kind"] == "flip_cap":
                self.ops.restore_flip(a)
            self.ops.release(a)
            r["action"] = None
            self.receipt(team, code, reason, a)

    def _active(self, data):
        r = self.world["robots"][data["team"] + "_br"]
        a = r["action"]
        return a if a and a["id"] == data["action_id"] else None

    def _contacts(self, entries):
        candidates, resources = [], defaultdict(list)
        for _, _, _, data in entries:
            a = self._active(data)
            if not a or a["cancel_state"] == "BRAKING":
                continue
            team = data["team"]
            try:
                validate_operation(self.world, team, {"kind": a["kind"], "params": a["params"]})
                self.ops.check_preconditions(team, a)
                keys = self.ops.resources(team, a)
                candidates.append((team, a, keys))
                for key in keys:
                    resources[key].append(team)
            except RuleError as exc:
                self.abort_action(team, exc.code, exc.reason)
        # A TR attempting to atomically fill an empty bay at the same contact time
        # is also a contender; neither side gets a hidden callback-order advantage.
        contested_tr = defaultdict(set)
        for team in TEAMS:
            s = self.world["supply"][team]
            if s["batch"] and s["batch"]["status"] == "waiting_slots" and s["control_state"] == "READY":
                needed = self.supplies.required_slots(team)
                if all(s["slots"][k] is None and s["slot_operations"][k] is None for k in needed):
                    for slot in needed:
                        key = f"slot:{team}:{slot}"
                        if key in resources:
                            resources[key].append("TR:" + team)
                            contested_tr[team].add(slot)
                            s["_delivery_after_ms"] = self.sim_time_ms
        for team, a, keys in candidates:
            if any(len(resources[key]) > 1 for key in keys):
                self.abort_action(team, "RESOURCE_CONTESTED", "same-millisecond valid contacts mutually rejected")
                continue
            try:
                self.ops.contact(team, a)
                self._arm_action_fault(team, a)
                if a["kind"] == "build_stack":
                    remaining = a["expected_end_ms"] - self.sim_time_ms
                    if remaining > 1:
                        self.schedule(self.sim_time_ms + max(1, remaining * 3 // 4), "base_seated",
                                      team=team, action_id=a["id"])
            except RuleError as exc:
                self.abort_action(team, exc.code, exc.reason)
        return contested_tr

    def _completions(self, entries):
        candidates = []
        for at, _, _, data in entries:
            a = self._active(data)
            if a and a["expected_end_ms"] == at:
                candidates.append((data["team"], a))
        balls = []
        for team, a in candidates:
            if a["kind"] == "place_ball":
                try:
                    validate_operation(self.world, team, {"kind": a["kind"], "params": a["params"]}, own_token=a["id"])
                    balls.append(team)
                except RuleError:
                    pass
        if len(balls) == 2:
            for team in balls:
                self.supplies.clear_ball(team, status="failed", pause_br=True)
            self.emit("CENTRAL_BALL_CONTESTED", teams=sorted(balls), rule="3.6.5")
        placed = None
        for team, a in candidates:
            if team in balls and len(balls) == 2:
                continue
            if not self.world["robots"][team + "_br"]["action"]:
                continue
            r = self.world["robots"][team + "_br"]
            try:
                if a["kind"] in ("entry", "move_to", "traverse"):
                    r["pose"] = deepcopy(a["_target_pose"])
                    ids = []
                    if a["kind"] == "entry":
                        r["control_state"] = "READY"
                else:
                    ids = self.ops.complete(team, a)
                r["action"] = None
                self.receipt(team, "CANCELLED" if a["cancel_state"] != "NONE" else "COMPLETED", "safe node reached", a)
                self.emit("ACTION_COMPLETED", team=team, action_id=a["id"], kind=a["kind"], item_ids=ids)
                if a["kind"] == "place_ball":
                    placed = team
            except RuleError as exc:
                self.abort_action(team, exc.code, exc.reason)
        if placed:
            other = "blue" if placed == "red" else "red"
            task = self.world["supply"][other]["ball_task"]
            if task and task["status"] in ACTIVE_BALL:
                item = self.world["items"][task["item_id"]]
                self.supplies.clear_ball(other, pause_br=item["holder_id"] == other + "_br")

    def _handle(self, kind, data):
        team = data.get("team")
        if kind == "fault":
            self.faults.trigger(data["fault_id"], data["context_id"])
        elif kind == "supply_arrival":
            # Delivery deferred until simultaneous contacts are resolved.
            s = self.world["supply"][team]
            if s["control_state"] == "READY" and s["batch"] and s["batch"]["id"] == data["batch_id"]:
                s["batch"]["status"] = "waiting_slots"
        elif kind == "ball_arrival":
            self.supplies.ball_arrival(team, data["task_id"])
        elif kind == "ball_clearance":
            self.supplies.finish_clearance(team, data["task_id"], data["pause_br"])
        elif kind == "base_seated":
            a = self._active(data)
            if a:
                a["phase"] = "base_seated"
                a["support_state"] = {"base_seated": True, "base_independent": False}
        elif kind == "br_returned":
            r = self.world["robots"][team + "_br"]
            r["pose"] = point_pose(self.world["map"], team.upper() + "_L1_RETRY")
            r["recovery_phase"] = "mandatory_hold"
            self.emit("RETRY_RETURNED", team=team, hold_started_at_ms=self.sim_time_ms)
        elif kind == "br_recovered":
            self.world["robots"][team + "_br"].update(control_state="READY", recovery_phase=None, recovery_until_ms=None)
            self.emit("RETRY_READY", team=team)
        elif kind == "tr_returned":
            self.world["supply"][team]["recovery_phase"] = "mandatory_hold"
            self.emit("TR_RETRY_RETURNED", team=team, hold_started_at_ms=self.sim_time_ms)
        elif kind == "tr_recovered":
            self.faults.recovered_tr(team)

    def _batch(self, entries):
        # Faults interrupt due commits; reserved physical completions precede fresh contacts.
        for entry in entries:
            if entry[2] == "fault":
                self._handle(entry[2], entry[3])
        for entry in entries:
            if entry[2] not in ("fault", "contact", "action_done"):
                self._handle(entry[2], entry[3])
        self._completions([entry for entry in entries if entry[2] == "action_done"])
        blocked = self._contacts([entry for entry in entries if entry[2] == "contact"])
        for team in TEAMS:
            self.supplies.try_deliver(team, blocked.get(team, ()))
        self._audit(score=False)

    def step(self, decisions: dict[str, dict[str, Any] | None], observations: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """Advance one common boundary and return the published state of that boundary.

        Pass `observations` (the envelopes handed to the policies for this boundary)
        to avoid rebuilding them; the returned snapshot is the single public state
        of this frame and should be reused by the caller instead of calling
        `public_state()` again.
        """
        if self.done:
            return self.public_state()
        observations = self.observations() if observations is None else observations
        self._event_window_start = len(self.events)
        self._receipt_window_start = {team: len(self._receipts[team]) for team in TEAMS}
        # All quotes see the two frozen envelopes, never the prior command's world.
        for team in TEAMS:
            self._submit(team, (decisions or {}).get(team), observations[team])
        target = min(self.config.duration_ms, self.sim_time_ms + self.config.tick_ms)
        while self._queue and self._queue[0][0] <= target and self._queue[0][0] < self.config.duration_ms:
            at = self._queue[0][0]
            entries = []
            while self._queue and self._queue[0][0] == at:
                entries.append(heapq.heappop(self._queue))
            self.sim_time_ms = at
            self._update_motion()
            self._batch(entries)
        self.sim_time_ms = target
        self._update_motion()
        self.seq += 1
        if target >= self.config.duration_ms:
            self.done = True
            for r in self.world["robots"].values():
                r["control_state"] = "FINISHED"
            self.emit("MATCH_FINISHED", reason="clock", ruleset_version=RULESET_VERSION)
        else:
            # Backpressure may clear precisely at a decision boundary.
            for team in TEAMS:
                self.supplies.try_deliver(team)
        return self.public_state()

    def fail_policies(self, failures: dict[str, str]) -> None:
        if self.done or not failures:
            return
        valid = {t: str(reason) for t, reason in failures.items() if t in TEAMS}
        if not valid:
            return
        self._technical_failures = valid
        self.done = True
        for r in self.world["robots"].values():
            r["control_state"] = "FINISHED"
        self.emit("MATCH_FINISHED", reason="technical_failure", failures=valid)
        self._audit()

    def result(self) -> dict[str, Any]:
        self._audit()
        for key, stamp in self._static_stamps.items():
            if self.world.get(key) is not self._statics[key] or _static_stamp(self._statics[key]) != stamp:
                raise RuntimeError(f"shared static '{key}' was mutated in place; published snapshots are no longer valid")
        scores = self.world["score"]
        winner, ties = tiebreak(self.world, scores)
        if self._technical_failures:
            winner = None if len(self._technical_failures) == 2 else next(t for t in TEAMS if t not in self._technical_failures)
        return {"status": "technical_failure" if self._technical_failures else ("completed" if self.done else "running"),
                "winner": winner, "scores": {t: scores[t]["total"] for t in TEAMS},
                "score_ledger": {t: deepcopy(scores[t]["ledger"]) for t in TEAMS},
                "sim_time_ms": self.sim_time_ms, "qualified": deepcopy(self.world["qualified"]),
                "tiebreak": ties, "faults": self.faults.statistics(self.done),
                "technical_failures": deepcopy(self._technical_failures), "ruleset_version": RULESET_VERSION}
