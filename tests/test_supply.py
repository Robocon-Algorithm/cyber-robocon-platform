import unittest

from robocon_sim.engine import Engine
from robocon_sim.world import point_pose, relocate
from test_engine import advance, decision, finish, seed_stack


def clear_slots(e, team):
    s = e.world["supply"][team]
    for slot, iid in list(s["slots"].items()):
        if iid:
            relocate(e.world["items"][iid], "loose:L1:" + team)
            e.ops.clear_slot(team, slot)
    s["stack_id"] = None


class SupplyTests(unittest.TestCase):
    def test_TR_arrival_and_BR_return_same_empty_slots_mutually_reject(self):
        e = Engine({"auto_entry": False, "supply_delays_ms": {"red": 100, "blue": 10000},
                    "parameters": {"stack_return_ms": 300}})
        sid, _ = seed_stack(e, "red")
        e.step({"red": decision(e, "red", "put_down_stack", stack_id=sid,
                                bay_id="BOX_STACK_BAY", expected_version=0)})
        s = e.world["supply"]["red"]
        self.assertEqual(e.sim_time_ms, 100)
        self.assertEqual(e._receipts["red"][-1]["code"], "RESOURCE_CONTESTED")
        self.assertEqual(s["slots"], {"BOX_LOWER": None, "BOX_UPPER": None, "CAP_BAY": None})
        self.assertEqual(s["batch"]["status"], "waiting_slots")
        e.step({})
        self.assertIsNotNone(s["slots"]["BOX_LOWER"])

    def test_backpressure_next_trip_from_actual_delivery(self):
        e = Engine({"auto_entry": False})
        advance(e, 21000)
        s = e.world["supply"]["red"]
        self.assertEqual(s["batch"]["status"], "waiting_slots")
        self.assertEqual(s["batch"]["expected_arrival_ms"], 18000)
        clear_slots(e, "red")
        e.supplies.try_deliver("red")
        self.assertEqual(s["batch"]["departed_at_ms"], 21000)
        self.assertEqual(s["batch"]["expected_arrival_ms"], 30000)

    def test_finite_inventory_caps_exhaust_boxes_continue(self):
        e = Engine({"auto_entry": False, "supply_delays_ms": {"red": 10, "blue": 10}})
        for _ in range(12):
            e.step({})
            clear_slots(e, "red")
            e.supplies.try_deliver("red")
        red = [i for i in e.world["items"].values() if i["owner_color"] == "red"]
        self.assertEqual(sum(i["kind"] == "box" for i in red), 20)
        self.assertEqual(sum(i["kind"] == "cap" for i in red), 6)
        self.assertEqual(e.world["supply"]["red"]["inventory"], {"boxes_remaining": 0, "caps_remaining": 0})
        self.assertIsNone(e.world["supply"]["red"]["batch"])

    def test_cap_full_blocks_entire_batch_even_box_bay_empty(self):
        e = Engine({"auto_entry": False, "supply_delays_ms": {"red": 10, "blue": 10}})
        advance(e, 100)
        s = e.world["supply"]["red"]
        sid = s["stack_id"]
        e.step({"red": decision(e, "red", "pick_stack", stack_id=sid, expected_version=e.world["stacks"][sid]["version"])})
        finish(e)
        self.assertIsNone(s["slots"]["BOX_LOWER"])
        self.assertEqual(s["batch"]["status"], "waiting_slots")
        self.assertEqual(s["batch"]["id"], "red:batch:2")

    def test_ball_queues_behind_committed_batch_and_handoff_needs_empty_BR(self):
        e = Engine({"auto_entry": False, "supply_delays_ms": {"red": 10, "blue": 10}, "parameters": {"ball_transport_ms": 300}})
        e.world["qualified"]["red"] = True
        advance(e, 100)
        e.step({"red": {"based_on_seq": e.seq, "primary": None, "request_ball": True}})
        s = e.world["supply"]["red"]
        self.assertEqual(s["ball_task"]["status"], "queued")
        first_task = s["ball_task"]["id"]
        e.step({"red": {"based_on_seq": e.seq, "primary": None, "request_ball": True}})
        self.assertEqual(s["ball_task"]["id"], first_task)
        clear_slots(e, "red")
        e.supplies.try_deliver("red")
        self.assertEqual(s["ball_task"]["status"], "in_transit")
        advance(e, 600)
        self.assertEqual(s["ball_task"]["status"], "waiting_handoff")
        self.assertNotIn(s["ball_task"]["item_id"], s["slots"].values())
        e.step({"red": decision(e, "red", "receive_ball")})
        finish(e)
        self.assertEqual(s["ball_task"]["status"], "held_by_br")
        self.assertTrue(s["ball_task"]["token_occupied"])
        self.assertEqual(len(e.world["robots"]["red_br"]["cargo"]), 1)

    def give_ball(self, e, team):
        from robocon_sim.world import new_item
        iid = team + ":ball"
        item = new_item(iid, "ball", team, "held:" + team + "_br")
        relocate(item, item["location"], holder=team + "_br", support=team + "_br")
        e.world["items"][iid] = item
        e.world["qualified"][team] = True
        e.world["robots"][team + "_br"]["pose"] = point_pose(e.world["map"], "CENTRAL_BALL_APPROACH")
        e.world["supply"][team]["ball_task"] = {"id": team + ":task", "item_id": iid, "status": "held_by_br", "token_occupied": True}
        e._audit()

    def test_same_time_balls_both_drop_and_release_only_after_clearance(self):
        e = Engine({"auto_entry": False})
        for team in ("blue", "red"):
            self.give_ball(e, team)
        e.step({team: decision(e, team, "place_ball") for team in ("blue", "red")})
        advance(e, 2000)
        self.assertIsNone(e.world["central_ball"])
        self.assertEqual(e.result()["scores"], {"red": 0, "blue": 0})
        for team in ("red", "blue"):
            self.assertEqual(e.world["robots"][team + "_br"]["control_state"], "PAUSED")
            self.assertTrue(e.world["supply"][team]["ball_task"]["token_occupied"])
        advance(e, 4000)
        for team in ("red", "blue"):
            self.assertEqual(e.world["robots"][team + "_br"]["control_state"], "READY")
            self.assertFalse(e.world["supply"][team]["ball_task"]["token_occupied"])

    def test_first_ball_unique_250_clears_other_safely_and_match_continues(self):
        e = Engine({"auto_entry": False})
        for team in ("red", "blue"):
            self.give_ball(e, team)
        e.step({"red": decision(e, "red", "place_ball")})
        advance(e, 2000)
        self.assertFalse(e.done)
        self.assertEqual(e.world["central_ball"]["team"], "red")
        self.assertEqual(e.result()["scores"]["red"], 250)
        self.assertEqual(e.world["robots"]["blue_br"]["control_state"], "PAUSED")
        advance(e, 4000)
        self.assertEqual(e.world["robots"]["blue_br"]["cargo"], [])
        self.assertEqual(e.world["supply"]["blue"]["ball_task"]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
