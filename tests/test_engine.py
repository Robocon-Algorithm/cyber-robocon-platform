import unittest
import math
from copy import deepcopy

from robocon_sim.engine import Engine
from robocon_sim.planning import quote_action
from robocon_sim.rules import RuleError, valid_tower, score_world, validate_operation
from robocon_sim.world import point_pose, new_item, relocate


def decision(engine, team, kind, **params):
    obs = engine.observations()[team]
    action = {"kind": kind, "params": params}
    quote = quote_action(obs, action)
    if not quote["ok"]:
        raise AssertionError(quote)
    return {"based_on_seq": engine.seq, "primary": {"kind": "execute", "action": action,
            "quote_seq": engine.seq, "preview_only": False}, "request_ball": False}


def advance(engine, target):
    while engine.sim_time_ms < target and not engine.done:
        engine.step({})


def finish(engine, team="red"):
    while engine.world["robots"][team + "_br"]["action"] and not engine.done:
        engine.step({})


def seed_stack(engine, team, at_tower=None):
    rid, sid = team + "_br", team + ":fixture_stack"
    ids = [team + ":lower", team + ":upper"]
    for iid in ids:
        item = new_item(iid, "box", team, "held:" + rid)
        relocate(item, "held:" + rid, holder=rid, support=rid if iid == ids[0] else ids[0])
        engine.world["items"][iid] = item
    engine.world["stacks"][sid] = {"id": sid, "version": 0, "bottom_item_id": ids[0],
        "top_item_id": ids[1], "intact": True, "holder_id": rid, "source_bay_id": None}
    r = engine.world["robots"][rid]
    r["carried_stack_id"] = sid
    if at_tower:
        r["pose"] = point_pose(engine.world["map"], at_tower + "_APPROACH")
    engine._audit()
    return sid, ids


def seed_tower(engine, tid, team="red"):
    t = engine.world["towers"][tid]
    support = "green:" + tid
    ids = []
    for level, kind in (("base", "box"), ("middle", "box"), ("cap", "cap")):
        iid = tid + ":" + level
        item = new_item(iid, kind, team, "tower:" + tid + ":" + level)
        relocate(item, "tower:" + tid + ":" + level, support=support, valid=True)
        engine.world["items"][iid] = item
        t[level] = iid
        support = iid
        ids.append(iid)
    engine._audit()
    return ids


class EngineTests(unittest.TestCase):
    def make(self, **kwargs):
        return Engine({"auto_entry": False, **kwargs})

    def test_snapshots_same_boundary_independent_envelopes(self):
        e = self.make(seed=9823, faults=[{"team": "red", "material_kind": "ball"}])
        obs = e.observations()
        for key in ("seq", "sim_time_ms", "world_revision", "robots", "events", "supply"):
            self.assertEqual(obs["red"][key], obs["blue"][key])
        obs["red"]["robots"]["red_br"]["pose"]["x_mm"] = -123
        self.assertNotEqual(obs["blue"]["robots"]["red_br"]["pose"]["x_mm"], -123)
        self.assertNotIn("faults", obs["red"])
        self.assertNotIn("seed", obs["red"])

    def test_old_quote_and_private_receipts(self):
        e = self.make()
        cmd = decision(e, "red", "move_to", point_id="RED_ENTRY")
        e.step({})
        e.step({"red": cmd})
        obs = e.observations()
        self.assertEqual(obs["red"]["receipts"][-1]["code"], "STALE_OBSERVATION")
        self.assertEqual(obs["blue"]["receipts"], [])
        self.assertFalse(any(ev["type"] == "COMMAND_RECEIPT" for ev in obs["blue"]["events"]))

    def test_motion_updates_and_busy_new_target_cannot_overwrite(self):
        e = self.make()
        e.step({"red": decision(e, "red", "move_to", point_id="RED_ENTRY")})
        first = deepcopy(e.world["robots"]["red_br"]["action"])
        positions = []
        headings = []
        for _ in range(40):
            obs = e.observations()["red"]
            positions.append(obs["self"]["pose"]["x_mm"])
            headings.append(obs["self"]["pose"]["heading_mrad"])
            invalid = {"based_on_seq": e.seq, "primary": {"kind": "execute", "quote_seq": e.seq,
                       "action": {"kind": "move_to", "params": {"point_id": "RED_NORTH"}}}}
            e.step({"red": invalid})
        self.assertGreater(len(set(positions)), 4)
        self.assertGreater(len(set(headings)), 4)
        self.assertLessEqual(max(abs(b - a) for a, b in zip(positions, positions[1:])), 100)
        heading_deltas = [abs((b - a + math.pi * 1000) % (math.tau * 1000) - math.pi * 1000)
                          for a, b in zip(headings, headings[1:])]
        self.assertLessEqual(max(heading_deltas), 106)
        self.assertEqual(e.world["robots"]["red_br"]["action"]["id"], first["id"])
        self.assertEqual(e.observations()["red"]["receipts"][-1]["code"], "BUSY")

    def test_cancel_brakes_continuously_and_requires_stop(self):
        e = self.make()
        e.step({"red": decision(e, "red", "move_to", point_id="RED_ENTRY")})
        advance(e, 3300)
        before = e.observations()["red"]["self"]
        speed = before["motion_state"]["speed_mm_s"]
        self.assertGreater(speed, 0)
        a = before["action"]
        e.step({"red": {"based_on_seq": e.seq, "primary": {"kind": "cancel", "action_id": a["id"]}}})
        braking = e.observations()["red"]["self"]
        self.assertIsNotNone(braking["action"])
        self.assertEqual(braking["action"]["cancel_state"], "BRAKING")
        finish(e)
        stop = e.world["robots"]["red_br"]["pose"]
        self.assertAlmostEqual(stop["x_mm"], before["pose"]["x_mm"] - speed ** 2 / 2000, places=5)
        self.assertIsNone(stop["workpoint_id"])
        self.assertTrue(quote_action(e.observations()["red"], {"kind": "move_to", "params": {"point_id": "RED_ENTRY"}})["ok"])

    def test_pick_build_return_whole_stack_real_durations(self):
        e = self.make(supply_delays_ms={"red": 10, "blue": 10})
        advance(e, 100)
        s = e.world["supply"]["red"]
        sid = s["stack_id"]
        ids = [s["slots"]["BOX_LOWER"], s["slots"]["BOX_UPPER"]]
        started = e.sim_time_ms
        e.step({"red": decision(e, "red", "pick_stack", stack_id=sid, expected_version=e.world["stacks"][sid]["version"])})
        finish(e)
        self.assertEqual(e.sim_time_ms - started, 2000)
        self.assertEqual(len(e.world["robots"]["red_br"]["cargo"]), 2)
        self.assertEqual(score_world(e.world)["red"]["boxes"], 0)
        e.world["robots"]["red_br"]["pose"] = point_pose(e.world["map"], "L1_PUBLIC_1_APPROACH")
        started = e.sim_time_ms
        e.step({"red": decision(e, "red", "build_stack", site_id="L1_PUBLIC_1", stack_id=sid, expected_version=0)})
        finish(e)
        self.assertEqual(e.sim_time_ms - started, 1500)
        self.assertEqual([e.world["towers"]["L1_PUBLIC_1"][k] for k in ("base", "middle")], ids)
        self.assertEqual(score_world(e.world)["red"]["boxes"], 40)
        self.assertFalse(e.world["qualified"]["red"])

    def test_stack_return_is_two_slots_and_cannot_take_extra_cap(self):
        e = self.make(supply_delays_ms={"red": 10, "blue": 10})
        advance(e, 100)
        s = e.world["supply"]["red"]
        sid = s["stack_id"]
        e.step({"red": decision(e, "red", "pick_stack", stack_id=sid, expected_version=e.world["stacks"][sid]["version"])})
        finish(e)
        q = quote_action(e.observations()["red"], {"kind": "pick", "params": {"item_ids": [s["slots"]["CAP_BAY"]]}})
        self.assertEqual(q["error"], "INVALID_CARGO")
        start = e.sim_time_ms
        e.step({"red": decision(e, "red", "put_down_stack", stack_id=sid, bay_id="BOX_STACK_BAY", expected_version=s["box_bay_version"])})
        finish(e)
        self.assertEqual(e.sim_time_ms - start, 1000)
        self.assertTrue(s["slots"]["BOX_LOWER"] and s["slots"]["BOX_UPPER"])
        self.assertEqual(e.world["robots"]["red_br"]["cargo"], [])

    def test_same_millisecond_tower_contest_rejects_both(self):
        e = self.make()
        cmds = {}
        for team in ("blue", "red"):
            sid, _ = seed_stack(e, team, "L1_PUBLIC_1")
            cmds[team] = decision(e, team, "build_stack", site_id="L1_PUBLIC_1", stack_id=sid, expected_version=0)
        e.step(cmds)
        advance(e, 1100)
        self.assertIsNone(e.world["towers"]["L1_PUBLIC_1"]["base"])
        for team in ("red", "blue"):
            self.assertIsNone(e.world["robots"][team + "_br"]["action"])
            self.assertEqual(e._receipts[team][-1]["code"], "RESOURCE_CONTESTED")

    def test_loose_boxes_from_l1_then_cap_after_traversing_to_l2(self):
        e = self.make()
        tid = "L2_PUBLIC_2"
        t = e.world["towers"][tid]
        r = e.world["robots"]["red_br"]

        def act(kind, **params):
            e.step({"red": decision(e, "red", kind, **params)})
            finish(e)
            self.assertEqual(e._receipts["red"][-1]["code"], "COMPLETED")

        def hold(iid, kind):
            i = new_item(iid, kind, "red", "held:red_br")
            relocate(i, "held:red_br", holder="red_br", support="red_br")
            e.world["items"][iid] = i
            e._audit()

        act("move_to", point_id=t["l1_approach_point_id"])
        for level, iid in (("base", "lower"), ("middle", "upper")):
            hold(iid, "box")
            act("build", site_id=tid, item_id=iid, level=level, expected_version=t["version"])
            self.assertEqual(r["pose"]["layer"], "L1")
            self.assertEqual(r["pose"]["workpoint_id"], t["l1_approach_point_id"])
        self.assertEqual(valid_tower(e.world, t), (True, True, False))
        ledger = score_world(e.world)["red"]["ledger"]
        self.assertEqual([entry["points"] for entry in ledger if entry.get("tower_id") == tid], [25, 45])
        hold("cap", "cap")
        act("move_to", point_id="RED_L2_GATE")
        act("traverse", connection_id="RED_L2_STEP", destination_layer="L2")
        act("move_to", point_id=t["approach_point_id"])
        act("build", site_id=tid, item_id="cap", level="cap", expected_version=t["version"])
        self.assertEqual(r["pose"]["layer"], "L2")
        self.assertEqual(valid_tower(e.world, t), (True, True, True))
        self.assertEqual(score_world(e.world)["red"]["caps"], 80)

    def test_l1_and_l2_stack_builds_contest_the_same_tower(self):
        e = self.make()
        tid = "L2_PUBLIC_2"
        t = e.world["towers"][tid]
        rs, ids = seed_stack(e, "red")
        bs, _ = seed_stack(e, "blue", tid)
        r = e.world["robots"]["red_br"]
        r["pose"] = point_pose(e.world["map"], t["l1_approach_point_id"])
        cmds = {team: decision(e, team, "build_stack", site_id=tid, stack_id=sid, expected_version=0)
                for team, sid in (("red", rs), ("blue", bs))}
        e.step(cmds)
        advance(e, 1100)
        self.assertIsNone(t["base"])
        self.assertEqual([e._receipts[team][-1]["code"] for team in ("red", "blue")],
                         ["RESOURCE_CONTESTED", "RESOURCE_CONTESTED"])
        e.step({"red": decision(e, "red", "build_stack", site_id=tid, stack_id=rs, expected_version=0)})
        finish(e)
        self.assertEqual([t["base"], t["middle"]], ids)
        self.assertEqual(r["pose"]["layer"], "L1")
        self.assertEqual(valid_tower(e.world, t), (True, True, False))

    def test_l1_workpoints_reject_other_sites_cap_flip_and_removal(self):
        e = self.make()
        tid = "L2_PUBLIC_2"
        t = e.world["towers"][tid]
        r = e.world["robots"]["red_br"]
        r["pose"] = point_pose(e.world["map"], t["l1_approach_point_id"])
        # Quote checks the item before access in take_from.
        e.world["items"]["box"] = new_item("box", "box", "red", "tower:" + tid + ":base")
        for kind, params, error in (
            ("build", {"site_id": "L2_PUBLIC_4", "item_id": "box", "level": "base"}, "NOT_AT_WORKPOINT"),
            ("build", {"site_id": tid, "item_id": "cap", "level": "cap"}, "WRONG_LAYER"),
            ("flip_cap", {"site_id": tid}, "WRONG_LAYER"),
            ("take_from", {"source_id": tid, "item_id": "box"}, "WRONG_LAYER"),
        ):
            with self.subTest(kind=kind, params=params):
                action = {"kind": kind, "params": {**params, "expected_version": 0}}
                self.assertEqual(quote_action(e.observations()["red"], action)["error"], error)
                with self.assertRaises(RuleError) as ctx:
                    validate_operation(e.world, "red", action)
                self.assertEqual(ctx.exception.code, error)

    def test_reserved_completion_precedes_same_millisecond_new_contact(self):
        # The blue 1500 ms stack action makes contact after 500 ms.
        e = self.make(parameters={"build_base_ms": 500})
        tid = "L1_PUBLIC_1"
        sid, _ = seed_stack(e, "blue", tid)
        e.world["robots"]["red_br"]["pose"] = point_pose(e.world["map"], tid + "_APPROACH")
        item = new_item("reserved_box", "box", "red", "held:red_br")
        relocate(item, "held:red_br", holder="red_br", support="red_br")
        e.world["items"][item["id"]] = item
        e.step({"red": decision(e, "red", "build", site_id=tid, item_id=item["id"], level="base", expected_version=0),
                "blue": decision(e, "blue", "build_stack", site_id=tid, stack_id=sid, expected_version=0)})
        advance(e, 500)
        self.assertEqual(e.world["towers"][tid]["base"], item["id"])
        self.assertEqual(e._receipts["blue"][-1]["occurred_at_ms"], 500)
        self.assertEqual(e._receipts["blue"][-1]["code"], "TARGET_CHANGED")

    def test_boolean_quote_seq_is_invalid_even_when_numerically_equal(self):
        e = self.make()
        cmd = decision(e, "red", "move_to", point_id="RED_ENTRY")
        cmd["primary"]["quote_seq"] = False
        e.step({"red": cmd})
        self.assertEqual(e.observations()["red"]["receipts"][-1]["code"], "INVALID_DECISION")
        self.assertIsNone(e.world["robots"]["red_br"]["action"])

    def test_changed_tower_during_alignment_cannot_overwrite(self):
        e = self.make()
        sid, _ = seed_stack(e, "red", "L1_PUBLIC_1")
        e.step({"red": decision(e, "red", "build_stack", site_id="L1_PUBLIC_1", stack_id=sid, expected_version=0)})
        ids = seed_tower(e, "L1_PUBLIC_1", "blue")
        e.world["towers"]["L1_PUBLIC_1"]["version"] += 1
        advance(e, 1100)
        self.assertEqual(e.world["towers"]["L1_PUBLIC_1"]["base"], ids[0])
        self.assertEqual(e._receipts["red"][-1]["code"], "TARGET_CHANGED")

    def test_179999_effective_180000_not_effective(self):
        for duration, valid in ((99, True), (100, False)):
            with self.subTest(duration=duration):
                e = self.make(parameters={"build_base_ms": duration})
                e._queue.clear()
                e.sim_time_ms = 179900
                r = e.world["robots"]["red_br"]
                r["pose"] = point_pose(e.world["map"], "L1_RED_1_APPROACH")
                item = new_item("last_box", "box", "red", "held:red_br")
                relocate(item, "held:red_br", holder="red_br", support="red_br")
                e.world["items"][item["id"]] = item
                e._audit()
                e.step({"red": decision(e, "red", "build", site_id="L1_RED_1", item_id="last_box", level="base", expected_version=0)})
                self.assertTrue(e.done)
                self.assertEqual(bool(e.world["towers"]["L1_RED_1"]["base"]), valid)
                self.assertEqual(e.result()["scores"]["red"], 15 if valid else 0)

    def test_held_stack_at_final_does_not_keep_delivery_points(self):
        e = self.make(duration_ms=100)
        seed_stack(e, "red")
        e.step({})
        self.assertEqual(e.result()["scores"]["red"], 0)

    def test_terminal_seated_but_still_held_stack_has_no_points(self):
        e = self.make(duration_ms=1400)
        sid, ids = seed_stack(e, "red", "L1_PUBLIC_1")
        e.step({"red": decision(e, "red", "build_stack", site_id="L1_PUBLIC_1", stack_id=sid, expected_version=0)})
        advance(e, 1400)
        self.assertTrue(e.done)
        self.assertTrue(e.world["robots"]["red_br"]["action"]["support_state"]["base_seated"])
        self.assertIsNone(e.world["towers"]["L1_PUBLIC_1"]["base"])
        self.assertIsNone(e.world["towers"]["L1_PUBLIC_1"]["middle"])
        self.assertEqual(e.result()["scores"]["red"], 0)
        self.assertEqual(e.world["items"][ids[0]]["holder_id"], "red_br")
        self.assertEqual(e.world["items"][ids[1]]["holder_id"], "red_br")

    def test_material_version_changes_during_alignment_are_rejected(self):
        e = self.make(supply_delays_ms={"red": 10, "blue": 10})
        advance(e, 100)
        s = e.world["supply"]["red"]
        sid = s["stack_id"]
        e.step({"red": decision(e, "red", "pick_stack", stack_id=sid, expected_version=e.world["stacks"][sid]["version"])})
        e.world["items"][s["slots"]["BOX_UPPER"]]["version"] += 1
        advance(e, 800)
        self.assertEqual(e._receipts["red"][-1]["code"], "TARGET_CHANGED")
        self.assertIsNotNone(s["slots"]["BOX_UPPER"])

    def test_technical_failures_are_one_batch(self):
        e = self.make()
        e.fail_policies({"red": "timeout", "blue": "timeout"})
        self.assertTrue(e.done)
        self.assertIsNone(e.result()["winner"])
        e = self.make()
        e.fail_policies({"red": "timeout"})
        self.assertEqual(e.result()["winner"], "blue")


if __name__ == "__main__":
    unittest.main()
