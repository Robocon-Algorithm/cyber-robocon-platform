import unittest

from robocon_sim.engine import Engine
from robocon_sim.rules import score_world, valid_tower, validate_operation, RuleError
from robocon_sim.world import point_pose, new_item, relocate
from test_engine import seed_tower, seed_stack, decision, advance, finish


class RuleTests(unittest.TestCase):
    def test_two_full_towers_one_public_permanent_qualification(self):
        e = Engine({"auto_entry": False})
        seed_tower(e, "L1_RED_1")
        seed_tower(e, "L1_RED_2")
        self.assertFalse(e.world["qualified"]["red"])
        ids = seed_tower(e, "L2_PUBLIC_1")
        self.assertTrue(e.world["qualified"]["red"])
        e.world["items"][ids[2]]["holder_id"] = "blue_br"
        e._audit()
        self.assertTrue(e.world["qualified"]["red"])

    def test_held_flip_retains_color_and_dynamic_score_not_full_tower(self):
        e = Engine({"auto_entry": False})
        ids = seed_tower(e, "L1_PUBLIC_1", "blue")
        e.world["robots"]["red_br"]["pose"] = point_pose(e.world["map"], "L1_PUBLIC_1_APPROACH")
        e.step({"red": decision(e, "red", "flip_cap", site_id="L1_PUBLIC_1", expected_version=0)})
        advance(e, 2100)
        t = e.world["towers"]["L1_PUBLIC_1"]
        self.assertFalse(all(valid_tower(e.world, t)))
        self.assertEqual(e.world["items"][ids[2]]["orientation"], "blue")
        self.assertEqual(score_world(e.world)["blue"]["caps"], 40)
        self.assertEqual(score_world(e.world, final=True)["blue"]["caps"], 0)
        finish(e)
        self.assertTrue(all(valid_tower(e.world, t)))
        self.assertEqual(score_world(e.world)["red"]["caps"], 40)

    def test_same_batch_does_not_grant_transient_qualification(self):
        e = Engine({"auto_entry": False})
        seed_tower(e, "L1_PUBLIC_1", "red")
        seed_tower(e, "L1_PUBLIC_2", "blue")
        r = e.world["robots"]["red_br"]
        r["pose"] = point_pose(e.world["map"], "L1_PUBLIC_2_APPROACH")
        b = e.world["robots"]["blue_br"]
        b["pose"] = point_pose(e.world["map"], "L1_PUBLIC_1_APPROACH")
        cmds = {team: decision(e, team, "flip_cap", site_id="L1_PUBLIC_" + ("2" if team == "red" else "1"), expected_version=0)
                for team in ("red", "blue")}
        e.step(cmds)
        advance(e, 6100)
        self.assertFalse(e.world["qualified"]["red"])
        self.assertFalse(e.world["qualified"]["blue"])

    def test_mixed_color_per_item_L1_L2_no_double_transport(self):
        e = Engine({"auto_entry": False})
        ids = seed_tower(e, "L1_PUBLIC_1", "red")
        e.world["items"][ids[1]]["owner_color"] = "blue"
        self.assertEqual(score_world(e.world)["red"]["total"], 55)
        self.assertEqual(score_world(e.world)["blue"]["total"], 25)
        t = e.world["towers"]["L1_PUBLIC_1"]
        t["layer"] = "L2"
        self.assertEqual(score_world(e.world)["red"]["total"], 105)
        self.assertEqual(score_world(e.world)["blue"]["total"], 45)

    def test_disabled_L1_box_still_five_but_held_box_zero(self):
        e = Engine({"auto_entry": False})
        i = new_item("disabled", "box", "red", "disabled:L1:red")
        i["disabled_for_use"] = True
        e.world["items"]["disabled"] = i
        self.assertEqual(score_world(e.world, True)["red"]["total"], 5)
        i["holder_id"] = "red_br"
        self.assertEqual(score_world(e.world, True)["red"]["total"], 0)

    def test_protected_box_and_buffer_version(self):
        e = Engine({"auto_entry": False})
        ids = seed_tower(e, "L1_PUBLIC_1", "blue")
        t = e.world["towers"]["L1_PUBLIC_1"]
        t["middle"], t["cap"] = None, None
        e.world["robots"]["red_br"]["pose"] = point_pose(e.world["map"], "L1_PUBLIC_1_APPROACH")
        with self.assertRaises(RuleError) as ctx:
            validate_operation(e.world, "red", {"kind": "take_from", "params": {"source_id": t["id"], "item_id": ids[0], "expected_version": 0}})
        self.assertEqual(ctx.exception.code, "RULE_FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
