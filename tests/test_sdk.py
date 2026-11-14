import unittest

from brsdk import Action, Decision, Observation, StrategyAPI
from tests.test_planning import observation


class SDKTests(unittest.TestCase):
    def test_snapshot_is_deeply_readonly_and_detached(self):
        source = observation()
        source["self"]["cargo"] = [{"id": "box1", "kind": "box"}]
        obs = Observation.from_dict(source)
        source["self"]["pose"]["x_mm"] = 999
        self.assertEqual(obs.self.pose.x_mm, 0)
        self.assertEqual(obs["self"]["pose"]["x_mm"], 0)
        self.assertIsInstance(obs.self.cargo, tuple)
        with self.assertRaises(TypeError):
            obs.self.pose.x_mm = 1
        with self.assertRaises(TypeError):
            obs.self.cargo[0]["id"] = "hacked"
        with self.assertRaises(TypeError):
            obs._data["seq"] = 10
        copied = obs.to_dict()
        copied["self"]["pose"]["x_mm"] = 100
        self.assertEqual(obs.self.pose.x_mm, 0)
        # "items" is an observation field, not the Mapping.items method.
        self.assertEqual(obs.items.to_dict(), {})
        self.assertIsNone(obs.stacks.get("missing"))

    def test_all_actions_roundtrip(self):
        actions = [Action.move_to("P"), Action.traverse("C", "L2"), Action.pick_stack("S", 1),
                   Action.build_stack("T", "S", 2), Action.put_down_stack("S", "BOX_STACK_BAY", 3),
                   Action.pick(["I"]), Action.build("T", "I", "base", 4), Action.flip_cap("T", 5),
                   Action.receive_ball(), Action.place_ball(), Action.take_from("I", "T", 6),
                   Action.put_down("I", "BOX_LOWER", 7)]
        for action in actions:
            with self.subTest(action=action.kind):
                self.assertEqual(Action.from_dict(action.to_dict()).to_dict(), action.to_dict())

    def test_execute_is_only_a_serializable_decision(self):
        source = observation()
        api = StrategyAPI(source)
        quote = api.quote(Action.move_to("B"))
        decision = api.execute(quote, request_ball=True)
        self.assertIsNone(source["self"]["action"])
        self.assertEqual(decision.to_dict(), {"based_on_seq": 7, "primary": {
            "kind": "execute", "action": {"kind": "move_to", "params": {"point_id": "B"}},
            "quote_seq": 7, "preview_only": False}, "request_ball": True})
        self.assertEqual(Decision.from_dict(decision.to_dict()).to_dict(), decision.to_dict())
        self.assertEqual(api.cancel("a1").primary.action_id, "a1")
        self.assertTrue(api.request_ball().request_ball)
        self.assertIsNone(api.request_ball().primary)

    def test_old_and_preview_quotes_cannot_execute(self):
        source = observation()
        old = StrategyAPI(source).quote(Action.move_to("B"))
        source["seq"] += 1
        api = StrategyAPI(source)
        with self.assertRaisesRegex(ValueError, "STALE_OBSERVATION"):
            api.execute(old)
        source["self"]["action"] = {"kind": "move_to"}
        preview = StrategyAPI(source).quote(Action.move_to("B"), after_stop=True)
        with self.assertRaisesRegex(ValueError, "PREVIEW_ONLY"):
            api.execute(preview)

    def test_quote_budget_and_failure_are_explicit(self):
        api = StrategyAPI(observation(), max_quotes=1)
        rejected = api.quote(Action.move_to("unknown"))
        self.assertEqual(rejected.error, "UNREACHABLE")
        with self.assertRaises(ValueError):
            api.execute(rejected)
        with self.assertRaisesRegex(RuntimeError, "quote budget"):
            api.quote(Action.move_to("B"))


if __name__ == "__main__":
    unittest.main()
