from copy import deepcopy
import math
import unittest

from robocon_sim.config import MatchConfig
from robocon_sim.map import default_map
from robocon_sim.planning import quote_action, travel_profile, motion_at, route


def observation():
    def point(pid, x, y, layer="L1", owner=None):
        return {"id": pid, "x_mm": x, "y_mm": y, "z_mm": 600 if layer == "L1" else 900,
                "heading_mrad": 0, "layer": layer, "owner": owner}
    points = {"A": point("A", 0, 0), "B": point("B", 3000, 0),
              "N": point("N", 0, 1000), "L2": point("L2", 3000, 0, "L2"),
              "FORBIDDEN": point("FORBIDDEN", 0, -1000, owner="blue")}
    pose = {**{k: v for k, v in points["A"].items() if k not in ("id", "owner")}, "workpoint_id": "A"}
    return {"seq": 7, "sim_time_ms": 100, "remaining_ms": 179900, "parameters": MatchConfig().parameters,
            "self": {"id": "red_br", "team": "red", "pose": pose, "cargo": [], "carried_stack_id": None,
                    "control_state": "READY", "action": None, "motion_state": {}},
            "map": {"points": points, "edges": [{"a": "A", "b": "B"}, {"a": "A", "b": "N"},
                                               {"a": "A", "b": "FORBIDDEN"}], "connections": {}},
            "towers": {}, "items": {}, "stacks": {}, "supply": {"red": {}}, "qualified": {"red": False},
            "central_ball": {}, "receipts": [], "events": []}


def stacked_observation(held=False):
    obs = observation()
    obs["self"]["pose"]["workpoint_id"] = "RED_TRANSFER"
    items = {iid: {"id": iid, "kind": "box", "owner_color": "red", "holder_id": "red_br" if held else None,
                  "disabled_for_use": False, "version": 0} for iid in ("bottom", "upper")}
    obs["items"] = items
    obs["stacks"] = {"stack1": {"id": "stack1", "version": 2, "bottom_item_id": "bottom", "top_item_id": "upper",
                              "intact": True, "holder_id": "red_br" if held else None, "source_bay_id": "BOX_STACK_BAY"}}
    obs["supply"]["red"] = {"slots": {"BOX_LOWER": None if held else "bottom", "BOX_UPPER": None if held else "upper", "CAP_BAY": None},
                            "slot_versions": {"BOX_LOWER": 1, "BOX_UPPER": 1, "CAP_BAY": 0},
                            "box_bay_version": 3, "stack_id": None if held else "stack1"}
    if held:
        obs["self"]["cargo"] = list(items.values())
        obs["self"]["carried_stack_id"] = "stack1"
    return obs


class MotionTests(unittest.TestCase):
    def test_loaded_3000mm_is_4550ms_by_hand(self):
        # Accel/decel each .8s, each covers 320mm; cruise 2360/800=2.95s.
        p = travel_profile(3000, 800, 1000)
        self.assertEqual(p["duration_ms"], 4550)
        self.assertEqual(p["acceleration_ms"], 800)
        self.assertEqual(p["cruise_ms"], 2950)
        obs = observation()
        obs["parameters"]["loaded_speed_mm_s"] = 800
        obs["self"]["cargo"] = [{"id": "b", "kind": "box"}]
        q = quote_action(obs, {"kind": "move_to", "params": {"point_id": "B"}})
        self.assertTrue(q["ok"], q)
        self.assertEqual(q["estimate_ms"], 4550)
        for elapsed, x, speed in ((400, 80, 400), (800, 320, 800), (3750, 2680, 800), (4150, 2920, 400), (4550, 3000, 0)):
            state = motion_at(q["segments"], elapsed)
            self.assertAlmostEqual(state["pose"]["x_mm"], x)
            self.assertAlmostEqual(state["speed_mm_s"], speed)

    def test_default_3m_leg_is_acceleration_limited_for_every_load(self):
        for kind in (None, "box", "ball"):
            with self.subTest(kind=kind):
                obs = observation()
                if kind:
                    obs["self"]["cargo"] = [{"id": "load", "kind": kind}]
                quote = quote_action(obs, {"kind": "move_to", "params": {"point_id": "B"}})
                self.assertTrue(quote["ok"], quote)
                self.assertEqual(quote["estimate_ms"], 3465)
                self.assertAlmostEqual(quote["segments"][-1]["max_speed_mm_s"], math.sqrt(3_000_000))

    def test_short_move_has_triangular_profile(self):
        # 250mm: 125mm accelerating +125mm braking, peak500, .5+.5 seconds.
        p = travel_profile(250, 1000, 1000)
        self.assertEqual(p["duration_ms"], 1000)
        self.assertEqual(p["max_speed_mm_s"], 500)
        self.assertEqual(p["cruise_ms"], 0)

    def test_turn_90_then_move_1000mm_is_3500ms(self):
        q = quote_action(observation(), {"kind": "move_to", "params": {"point_id": "N"}})
        self.assertTrue(q["ok"], q)
        self.assertEqual(q["estimate_ms"], 3500)
        self.assertEqual(q["segments"][0]["kind"], "turn")
        middle = motion_at(q["segments"], 750)
        self.assertAlmostEqual(middle["pose"]["heading_mrad"], math.pi * 250)
        self.assertEqual(middle["pose"]["x_mm"], 0)
        self.assertEqual(middle["pose"]["y_mm"], 0)

    def test_cancel_preview_brakes_800mm_s_in_800ms_over_320mm(self):
        obs = observation()
        obs["self"]["pose"].update(x_mm=1000, workpoint_id=None)
        obs["self"]["motion_state"] = {"speed_mm_s": 800, "vx_mm_s": 800, "vy_mm_s": 0}
        obs["self"]["action"] = {"id": "a1", "kind": "move_to"}
        action = {"kind": "move_to", "params": {"point_id": "B"}}
        before = deepcopy(obs)
        self.assertEqual(quote_action(obs, action)["error"], "BUSY")
        q = quote_action(obs, action, after_stop=True)
        self.assertTrue(q["ok"], q)
        self.assertTrue(q["preview_only"])
        self.assertAlmostEqual(q["stop_pose"]["x_mm"], 1320)
        self.assertEqual(q["segments"][0]["end_ms"], 800)
        self.assertAlmostEqual(motion_at(q["segments"], 400)["pose"]["x_mm"], 1240)
        self.assertEqual(obs, before)
        forbidden = quote_action(obs, {"kind": "receive_ball", "params": {}}, after_stop=True)
        self.assertEqual(forbidden["error"], "PREVIEW_UNAVAILABLE")

    def test_same_layer_route_cannot_teleport_or_cross_layers(self):
        obs = observation()
        self.assertEqual(quote_action(obs, {"kind": "move_to", "params": {"point_id": "L2"}})["error"], "WRONG_LAYER")
        self.assertEqual(quote_action(obs, {"kind": "move_to", "params": {"point_id": "FORBIDDEN"}})["error"], "UNREACHABLE")
        pose = deepcopy(obs["self"]["pose"])
        pose.update(x_mm=500, y_mm=500, workpoint_id=None)
        self.assertIsNone(route(obs["map"], pose, "B", "red"))


class LegalityTests(unittest.TestCase):
    def test_ball_quote_rejects_an_already_filled_pedestal(self):
        obs = observation()
        obs["self"]["pose"].update(layer="L2", workpoint_id="CENTRAL_BALL_APPROACH")
        obs["self"]["cargo"] = [{"id": "ball", "kind": "ball"}]
        obs["qualified"]["red"] = True
        action = {"kind": "place_ball", "params": {}}
        quote = quote_action(obs, action)
        self.assertTrue(quote["ok"])
        self.assertEqual(quote["estimate_ms"], 2000)
        obs["central_ball"] = {"team": "blue", "item_id": "other_ball"}
        self.assertEqual(quote_action(obs, action)["error"], "TARGET_CHANGED")

    def test_malformed_actions_have_a_clear_rejection(self):
        for action in ({"kind": "move_to", "params": {"point_id": ["B"]}},
                       {"kind": "pick", "params": {"item_ids": [{}]}},
                       {"kind": "place_ball", "params": {"estimate_ms": 1}},
                       {"kind": "teleport", "params": {}}):
            with self.subTest(action=action):
                self.assertEqual(quote_action(observation(), action)["error"], "INVALID_ACTION")

    def test_stack_pick_costs_one_2000ms_action_for_two_real_ids(self):
        obs = stacked_observation()
        before = deepcopy(obs)
        q = quote_action(obs, {"kind": "pick_stack", "params": {"stack_id": "stack1", "expected_version": 2}})
        self.assertTrue(q["ok"], q)
        self.assertEqual(q["estimate_ms"], 2000)
        self.assertEqual(q["preconditions"]["item_ids"], ["bottom", "upper"])
        self.assertEqual(obs, before)
        self.assertEqual(quote_action(obs, {"kind": "pick", "params": {"item_ids": ["bottom", "upper"]}})["error"], "STACK_ACTION_REQUIRED")
        self.assertEqual(quote_action(obs, {"kind": "pick", "params": {"item_ids": ["bottom"]}})["error"], "RULE_FORBIDDEN")
        self.assertTrue(quote_action(obs, {"kind": "pick", "params": {"item_ids": ["upper"]}})["ok"])

    def test_stack_return_needs_two_empty_slots_and_bay_version(self):
        obs = stacked_observation(held=True)
        action = {"kind": "put_down_stack", "params": {"stack_id": "stack1", "bay_id": "BOX_STACK_BAY", "expected_version": 3}}
        q = quote_action(obs, action)
        self.assertTrue(q["ok"], q)
        self.assertEqual(q["estimate_ms"], 1000)
        obs["supply"]["red"]["slots"]["BOX_UPPER"] = "other"
        self.assertEqual(quote_action(obs, action)["error"], "RESOURCE_BUSY")
        action["params"]["expected_version"] = 2
        self.assertEqual(quote_action(obs, action)["error"], "TARGET_CHANGED")

    def test_stack_build_only_empty_site_and_1500ms(self):
        obs = stacked_observation(held=True)
        obs["self"]["pose"]["workpoint_id"] = "T_APPROACH"
        obs["towers"]["T"] = {"id": "T", "layer": "L1", "owner": None, "approach_point_id": "T_APPROACH",
                                  "base": None, "middle": None, "cap": None, "version": 4, "operation": None}
        action = {"kind": "build_stack", "params": {"site_id": "T", "stack_id": "stack1", "expected_version": 4}}
        q = quote_action(obs, action)
        self.assertTrue(q["ok"], q)
        self.assertEqual(q["estimate_ms"], 1500)
        self.assertEqual(q["alignment_ms"], 500)
        obs["towers"]["T"]["base"] = "earlier"
        self.assertEqual(quote_action(obs, action)["error"], "TARGET_CHANGED")


class MapAndConfigTests(unittest.TestCase):
    def test_explicit_entry_cannot_be_reversed_to_ground(self):
        obs = observation()
        obs["map"] = default_map()
        obs["self"]["pose"] = {**obs["map"]["points"]["RED_TRANSFER"], "workpoint_id": "RED_TRANSFER"}
        action = {"kind": "traverse", "params": {"connection_id": "RED_RAMP", "destination_layer": "GROUND"}}
        self.assertEqual(quote_action(obs, action)["error"], "RULE_FORBIDDEN")

    def test_graph_rejects_a_shortcut_through_raised_l2(self):
        m = default_map()
        a = {"id": "A", "x_mm": 3500, "y_mm": 5500, "layer": "L1", "workpoint_id": "A"}
        b = {"id": "B", "x_mm": 7500, "y_mm": 5500, "layer": "L1"}
        m["points"] = {"A": a, "B": b}
        m["edges"] = [{"a": "A", "b": "B"}]
        self.assertIsNone(route(m, a, "B", "red"))

    def test_private_config_not_in_public_context(self):
        c = MatchConfig(seed=314, faults=[{"role": "br", "nth": 1}])
        self.assertNotIn("seed", c.public_dict())
        self.assertNotIn("faults", c.public_dict())
        self.assertEqual(MatchConfig.from_dict(c.to_dict()).to_dict(), c.to_dict())
        public = c.public_dict()
        public["parameters"]["entry_ms"] = 1
        self.assertEqual(c.parameters["entry_ms"], 8000)

    def test_bad_configuration_cannot_create_nonprogressing_events(self):
        for kwargs in ({"tick_ms": True}, {"duration_ms": float("nan")}, {"seed": True},
                       {"parameters": {"empty_speed_mm_s": float("inf")}},
                       {"parameters": {"invented_ms": 1}}, {"auto_entry": 1},
                       {"supply_delays_ms": {"red": 0, "blue": 9000}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                MatchConfig(**kwargs)

    def test_map_has_dimensions_ten_sites_and_explicit_layer_connections(self):
        m = default_map()
        self.assertEqual(len(m["towers"]), 10)
        self.assertEqual(m["dimensions"]["field_mm"], [11000, 11000])
        self.assertEqual(m["dimensions"]["l1_height_mm"], 600)
        self.assertEqual(m["dimensions"]["l2_height_mm"], 900)
        for edge in m["edges"]:
            self.assertEqual(m["points"][edge["a"]]["layer"], m["points"][edge["b"]]["layer"])
        pose = {**m["points"]["RED_TRANSFER"], "workpoint_id": "RED_TRANSFER"}
        self.assertIsNotNone(route(m, pose, "L1_PUBLIC_1_APPROACH", "red"))
        self.assertIsNotNone(route(m, pose, "L1_RED_2_APPROACH", "red"))
        self.assertIsNotNone(route(m, pose, "RED_L1_RETRY", "red"))
        self.assertIsNone(route(m, pose, "BLUE_L1_RETRY", "red"))
        self.assertIsNone(route(m, pose, "L1_BLUE_1_APPROACH", "red"))
        self.assertIsNone(route(m, pose, "L2_PUBLIC_1_APPROACH", "red"))
        self.assertTrue(m["assumptions"])

    def test_l1_build_points_reachable_only_from_own_side_without_a_layer_change(self):
        m = default_map()
        for n, owner in enumerate(("blue", "red", "blue", "red"), 1):
            tid = f"L2_PUBLIC_{n}"
            pid = m["towers"][tid]["l1_approach_point_id"]
            p = m["points"][pid]
            opponent = "blue" if owner == "red" else "red"
            with self.subTest(tower=tid):
                self.assertEqual(p["build_tower_id"], tid)
                start_id = owner.upper() + "_TRANSFER"
                path = route(m, {**m["points"][start_id], "workpoint_id": start_id}, pid, owner)
                self.assertEqual(path[-1]["workpoint_id"], pid)
                self.assertTrue(all(node["layer"] == "L1" for node in path))
                start_id = opponent.upper() + "_TRANSFER"
                self.assertIsNone(route(m, {**m["points"][start_id], "workpoint_id": start_id}, pid, opponent))


if __name__ == "__main__":
    unittest.main()
