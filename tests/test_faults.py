import unittest

from robocon_sim.engine import Engine
from robocon_sim.rules import score_world, valid_tower
from robocon_sim.world import point_pose
from test_engine import advance, decision, finish, seed_stack, seed_tower
import test_supply


class FaultTests(unittest.TestCase):
    def test_upper_slip_preserves_lower_independent_base_and_one_fault(self):
        e = Engine({"auto_entry": False, "faults": [{"team": "red", "role": "BR", "material_kind": "box",
                     "action_kind": "build_stack", "mode": "upper_slip", "phase": "stable_release"}]})
        sid, ids = seed_stack(e, "red", "L1_PUBLIC_1")
        e.step({"red": decision(e, "red", "build_stack", site_id="L1_PUBLIC_1", stack_id=sid, expected_version=0)})
        advance(e, 1400)
        t = e.world["towers"]["L1_PUBLIC_1"]
        self.assertEqual(t["base"], ids[0])
        self.assertIsNone(t["middle"])
        self.assertTrue(valid_tower(e.world, t)[0])
        self.assertFalse(e.world["items"][ids[0]]["disabled_for_use"])
        self.assertTrue(e.world["items"][ids[1]]["disabled_for_use"])
        self.assertEqual(score_world(e.world)["red"]["boxes"], 20)
        self.assertEqual(len([f for f in e.faults.statistics() if f["status"] == "triggered"]), 1)
        r = e.world["robots"]["red_br"]
        self.assertEqual(r["control_state"], "PAUSED")
        self.assertEqual(r["recovery_until_ms"], 3350)
        advance(e, 3400)
        self.assertEqual(r["control_state"], "READY")

    def test_stack_drop_disables_two_original_boxes_without_refill(self):
        e = Engine({"auto_entry": False, "faults": [{"team": "red", "role": "BR", "material_kind": "box",
                     "action_kind": "move_to", "mode": "stack_drop", "offset_ms": 500}]})
        _, ids = seed_stack(e, "red")
        e.step({"red": decision(e, "red", "move_to", point_id="RED_ENTRY")})
        advance(e, 500)
        self.assertTrue(all(e.world["items"][iid]["disabled_for_use"] for iid in ids))
        self.assertEqual(len([ev for ev in e.events if ev["type"] == "ITEM_DROPPED"]), 1)
        self.assertEqual(score_world(e.world)["red"]["boxes"], 10)
        self.assertEqual(e.world["robots"]["red_br"]["cargo"], [])

    def test_flip_drop_restores_original_tower_and_retry_hold_after_return(self):
        e = Engine({"auto_entry": False, "faults": [{"team": "red", "role": "BR", "material_kind": "cap",
                     "action_kind": "flip_cap", "offset_ms": 500}]})
        ids = seed_tower(e, "L1_PUBLIC_1", "blue")
        e.world["robots"]["red_br"]["pose"] = point_pose(e.world["map"], "L1_PUBLIC_1_APPROACH")
        e.step({"red": decision(e, "red", "flip_cap", site_id="L1_PUBLIC_1", expected_version=0)})
        advance(e, 2500)
        cap = e.world["items"][ids[2]]
        self.assertEqual(cap["orientation"], "blue")
        self.assertIsNone(cap["holder_id"])
        self.assertTrue(all(valid_tower(e.world, e.world["towers"]["L1_PUBLIC_1"])))
        r = e.world["robots"]["red_br"]
        self.assertEqual(r["return_at_ms"], 5500)
        self.assertEqual(r["recovery_until_ms"], 20500)
        advance(e, 5500)
        self.assertEqual(r["pose"]["workpoint_id"], "RED_L1_RETRY")
        self.assertEqual(r["recovery_phase"], "mandatory_hold")
        self.assertEqual(r["control_state"], "RETRYING")
        advance(e, 20500)
        self.assertEqual(r["control_state"], "READY")

    def test_scripted_impact_invalidates_placement_and_restores_other_tower(self):
        e = Engine({"auto_entry": False, "faults": [{"team": "red", "role": "BR", "material_kind": "box",
                     "action_kind": "build_stack", "mode": "tower_impact", "offset_ms": 500,
                     "collision_tower_id": "L1_PUBLIC_2"}]})
        sid, _ = seed_stack(e, "red", "L1_PUBLIC_1")
        original = seed_tower(e, "L1_PUBLIC_2", "blue")
        e.step({"red": decision(e, "red", "build_stack", site_id="L1_PUBLIC_1", stack_id=sid, expected_version=0)})
        advance(e, 1500)
        self.assertIsNone(e.world["towers"]["L1_PUBLIC_1"]["base"])
        t = e.world["towers"]["L1_PUBLIC_2"]
        self.assertEqual([t[k] for k in ("base", "middle", "cap")], original)
        self.assertTrue(all(valid_tower(e.world, t)))
        self.assertEqual(t["top_color"], "blue")
        self.assertTrue(any(ev["type"] == "TOWER_RESTORED" and ev["rule"] == "6.8" for ev in e.events))
        r = e.world["robots"]["red_br"]
        self.assertEqual(r["control_state"], "RETRYING")
        self.assertEqual(r["recovery_until_ms"] - r["return_at_ms"], 15000)

    def test_TR_cap_fault_does_not_copy_surviving_batch_or_stop_BR(self):
        e = Engine({"auto_entry": False, "faults": [{"team": "red", "role": "TR", "material_kind": "cap", "offset_ms": 1000}]})
        advance(e, 1000)
        s = e.world["supply"]["red"]
        self.assertEqual(s["control_state"], "RETRYING")
        self.assertEqual(e.world["robots"]["red_br"]["control_state"], "READY")
        self.assertEqual(s["return_at_ms"], 4000)
        self.assertEqual(s["recovery_until_ms"], 19000)
        ids = s["batch"]["item_ids"][:]
        inventory = s["inventory"].copy()
        advance(e, 19000)
        self.assertEqual(s["inventory"], inventory)
        self.assertEqual(s["batch"]["item_ids"], [iid for iid in ids if e.world["items"][iid]["kind"] == "box"])
        self.assertEqual(s["batch"]["expected_arrival_ms"], 28000)
        advance(e, 28000)
        self.assertEqual(s["slots"]["BOX_LOWER"], ids[0])
        self.assertEqual(s["slots"]["BOX_UPPER"], ids[1])
        self.assertIsNone(s["slots"]["CAP_BAY"])

    def test_cancel_does_not_redraw_bound_fault(self):
        e = Engine({"auto_entry": False, "faults": [{"team": "red", "role": "BR", "material_kind": "box",
                     "action_kind": "move_to", "offset_ms": 5000, "mode": "upper_slip"}]})
        _, ids = seed_stack(e, "red")
        e.step({"red": decision(e, "red", "move_to", point_id="RED_ENTRY")})
        a = e.world["robots"]["red_br"]["action"]
        e.step({"red": {"based_on_seq": e.seq, "primary": {"kind": "cancel", "action_id": a["id"]}}})
        finish(e)
        f = e.faults.scripts[0]
        self.assertEqual(f["status"], "armed")
        self.assertEqual(f["affected_item_ids"], [ids[1]])
        e.step({"red": decision(e, "red", "move_to", point_id="RED_ENTRY")})
        finish(e)
        self.assertEqual(f["status"], "triggered")
        self.assertEqual(f["affected_item_ids"], [ids[1]])
        self.assertEqual(f["seen"], 1)

    def test_BR_ball_fault_keeps_token_until_recovery_then_new_request_allowed(self):
        e = Engine({"auto_entry": False, "faults": [{"team": "red", "role": "BR", "material_kind": "ball",
                     "action_kind": "place_ball", "offset_ms": 100}]})
        test_supply.SupplyTests().give_ball(e, "red")
        e.step({"red": decision(e, "red", "place_ball")})
        advance(e, 1500)
        task = e.world["supply"]["red"]["ball_task"]
        self.assertEqual(task["status"], "clearing")
        self.assertTrue(task["token_occupied"])
        until = e.world["robots"]["red_br"]["recovery_until_ms"]
        advance(e, until + 100)
        self.assertEqual(task["status"], "failed")
        self.assertFalse(task["token_occupied"])
        e.supplies.request_ball("red")
        self.assertNotEqual(e.world["supply"]["red"]["ball_task"]["id"], task["id"])


if __name__ == "__main__":
    unittest.main()
