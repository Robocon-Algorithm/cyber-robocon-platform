import json
import random
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from robocon_sim.frame_codec import (
    FrameFormatError,
    FrameReader,
    FrameWriter,
    STATIC_KEYS,
    _prepare_static,
    diff_state,
)


STATIC = {
    "map": {
        "points": {"A": {"x_mm": 1, "y_mm": 2}},
        "workpoints": {"A": {"x_mm": 1, "y_mm": 2}},
        "towers": {"T1": {"layer": 1}},
    },
    "parameters": {"empty_speed_mm_s": 3000, "parameter_status": "教学初值"},
    "ruleset_version": "br-1",
}


def state(seq, **overrides):
    frame = {
        "seq": seq,
        "sim_time_ms": seq * 100,
        "remaining_ms": 180000 - seq * 100,
        "done": False,
        "world_revision": seq,
        "robots": {
            "red_br": {"id": "red_br", "team": "red", "pose": {"x_mm": seq, "y_mm": 0},
                       "cargo": [], "action": None},
            "blue_br": {"id": "blue_br", "team": "blue", "pose": {"x_mm": 0, "y_mm": seq},
                        "cargo": [], "action": None},
        },
        "items": {"i1": {"id": "i1", "location": "stock", "support_id": None}},
        "towers": {"T1": {"base": None, "cap": None, "top_color": None}},
        "stacks": {},
        "supply": {"red": {"slots": {"A": None}, "slot_versions": {"A": 0}}},
        "qualified": {"red": False, "blue": False},
        "central_ball": None,
        "score": {"red": {"total": 0, "ledger": []}, "blue": {"total": 0, "ledger": []}},
        "events": [],
    }
    frame.update(overrides)
    return frame


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "snapshots.rcf"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, frames, **kwargs):
        writer = FrameWriter(self.path, STATIC, tick_ms=100, duration_ms=180000, **kwargs)
        for frame in frames:
            writer.append(frame)
        writer.close()
        return FrameReader(self.path)

    def test_every_value_kind_survives(self):
        frames = [
            state(0, extra={"n": None, "t": True, "f": False, "zero": 0, "neg": -7,
                            "big": 2 ** 70, "small": -2 ** 70, "ratio": 0.1,
                            "neg_zero": -0.0, "text": "tower:L1_PUBLIC_1:base",
                            "unicode": "塔", "empty_map": {}, "empty_list": [],
                            "nested": {"a": [1, {"b": [True, None]}]}}),
        ]
        reader = self.write(frames)
        got = reader.state(0)
        self.assertEqual(got["extra"], frames[0]["extra"])
        self.assertIs(type(got["extra"]["t"]), bool)
        self.assertIs(type(got["extra"]["zero"]), int)
        self.assertIs(type(got["extra"]["ratio"]), float)
        self.assertEqual(str(got["extra"]["neg_zero"]), "-0.0")
        self.assertEqual(got["extra"]["big"], 2 ** 70)

    def test_bool_and_int_stay_distinct(self):
        reader = self.write([state(0, flag=True), state(1, flag=1), state(2, flag=0),
                             state(3, flag=False)])
        kinds = [(reader.state(i)["flag"], type(reader.state(i)["flag"])) for i in range(4)]
        self.assertEqual(kinds, [(True, bool), (1, int), (0, int), (False, bool)])

    def test_static_is_stored_once_and_workpoints_is_restored(self):
        static = _prepare_static(STATIC)
        self.assertIn("points", static["map"])
        self.assertNotIn("workpoints", static["map"])
        reader = self.write([state(0), state(1)])
        merged = reader.state(1, include_static=True)
        self.assertIs(merged["map"]["workpoints"], merged["map"]["points"])
        self.assertEqual(merged["parameters"]["empty_speed_mm_s"], 3000)
        for key in STATIC_KEYS:
            self.assertNotIn(key, reader.state(1))

    def test_delta_chain_matches_full_states(self):
        frames = [state(i) for i in range(40)]
        for i in range(1, 40):
            frames[i]["robots"]["red_br"]["pose"]["x_mm"] = i * 3
            if i % 5 == 0:
                frames[i]["items"]["i%d" % i] = {"id": "i%d" % i, "location": "held:red_br"}
            if i % 7 == 0 and i > 7:
                frames[i]["items"].pop("i%d" % (i - 7), None)
            if i % 3 == 0:
                frames[i]["events"] = [{"id": "event:%d" % i, "type": "X"}]
            if i % 4 == 0:
                frames[i]["score"]["red"]["ledger"] = [{"reason": "box", "points": 5}] * (i // 4)
        reader = self.write(frames, keyframe_interval=7)
        for i, expected in enumerate(frames):
            self.assertEqual(reader.state(i), expected, "frame %d" % i)
        self.assertGreater(reader.keyframe_count, 1)

    def test_keyframe_promotion_keeps_the_chain_in_sync(self):
        # A tiny interval and a tight byte budget force promotion on almost every frame.
        frames = [state(i, payload="x" * (i % 11)) for i in range(12)]
        reader = self.write(frames, keyframe_interval=3, max_keyframe_bytes=8, max_delta_ops=0)
        for i, expected in enumerate(frames):
            self.assertEqual(reader.state(i), expected)
        self.assertEqual(reader.keyframe_count, reader.frame_count)

    def test_random_access_equals_sequential(self):
        frames = [state(i) for i in range(60)]
        for i in range(1, 60):
            frames[i]["towers"]["T1"]["base"] = "i%d" % i if i % 2 else None
            frames[i]["supply"]["red"]["slots"]["A"] = i % 3 or None
        reader = self.write(frames, keyframe_interval=10)
        order = list(range(60))
        random.Random(7).shuffle(order)
        for i in order:
            self.assertEqual(reader.state(i), frames[i], "frame %d" % i)
        self.assertEqual(reader.states(13, 4), frames[13:17])

    def test_seek_time(self):
        reader = self.write([state(i) for i in range(10)])
        self.assertEqual(reader.seek_time(0), 0)
        self.assertEqual(reader.seek_time(350), 4)
        self.assertEqual(reader.seek_time(3500), 9)

    def test_encoding_is_deterministic(self):
        frames = [state(i, note="n%d" % (i % 3)) for i in range(20)]
        first = self.write(frames)
        payload = self.path.read_bytes()
        second = self.write(frames)
        self.assertEqual(self.path.read_bytes(), payload)
        self.assertEqual(first.state(19), second.state(19))

    def test_mapping_order_is_preserved(self):
        # The scoring ledger is generated by iterating world maps, so a decoded
        # frame must keep the producer's key order instead of sorting it.
        frames = [state(0, towers={"Z_LAST": {"base": None}, "A_FIRST": {"base": None}},
                        note="x")]
        reader = self.write(frames, keyframe_interval=2)
        self.assertEqual(list(reader.state(0)["towers"]), ["Z_LAST", "A_FIRST"])

    def test_meta_and_verify(self):
        frames = [state(i) for i in range(15)]
        reader = self.write(frames, keyframe_interval=5)
        meta = reader.meta()
        self.assertEqual(len(meta), 15)
        self.assertEqual(meta[0]["sim_time_ms"], 0)
        self.assertEqual(meta[-1]["sim_time_ms"], 1400)
        self.assertEqual([m["frame"] for m in meta if m["keyframe"]], [0, 5, 10])
        report = reader.verify()
        self.assertEqual(report["frames"], 15)


class CorruptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "snapshots.rcf"
        writer = FrameWriter(self.path, STATIC, tick_ms=100, duration_ms=180000, keyframe_interval=4)
        for i in range(12):
            writer.append(state(i, note="n%d" % i))
        writer.close()

    def tearDown(self):
        self.tmp.cleanup()

    def corrupt(self, offset):
        raw = bytearray(self.path.read_bytes())
        raw[offset] ^= 0xFF
        self.path.write_bytes(bytes(raw))

    def test_flipped_payload_byte_is_rejected(self):
        reader = FrameReader(self.path)
        target = reader._entries[3][0] + reader._entries[3][1] - 1
        self.corrupt(target)
        with self.assertRaises(FrameFormatError):
            FrameReader(self.path).verify()

    def test_trailing_bytes_are_rejected(self):
        raw = self.path.read_bytes()
        self.path.write_bytes(raw + b"\x00")
        with self.assertRaisesRegex(FrameFormatError, "trailing"):
            FrameReader(self.path)

    def test_truncation_is_rejected(self):
        raw = self.path.read_bytes()
        self.path.write_bytes(raw[: len(raw) // 2])
        with self.assertRaises(FrameFormatError):
            FrameReader(self.path)

    def test_unfinalised_stream_is_rejected(self):
        writer = FrameWriter(self.path.with_name("open.rcf"), STATIC, tick_ms=100, duration_ms=1000)
        writer.append(state(0))
        writer.abort()
        with self.assertRaisesRegex(FrameFormatError, "finalised"):
            FrameReader(self.path.with_name("open.rcf"))

    def test_foreign_bytes_are_rejected(self):
        self.path.write_bytes(b"not a frame stream at all" * 8)
        with self.assertRaises(FrameFormatError):
            FrameReader(self.path)


class DiffTests(unittest.TestCase):
    def test_only_changed_leaves_are_reported(self):
        sets, dels = diff_state(state(0), state(1))
        paths = {path for path, _ in sets}
        self.assertIn(("seq",), paths)
        self.assertIn(("robots", "red_br", "pose", "x_mm"), paths)
        self.assertNotIn(("items",), paths)
        self.assertEqual(dels, [])

    def test_removed_map_keys_are_deleted(self):
        before = state(0, extra={"keep": 1, "drop": 2})
        after = state(1, extra={"keep": 1})
        sets, dels = diff_state(before, after)
        self.assertIn(("extra", "drop"), dels)
        self.assertNotIn(("extra", "keep"), [path for path, _ in sets])

    def test_lists_are_replaced_atomically(self):
        before = state(0, events=[{"id": "a"}])
        after = state(1, events=[{"id": "a"}, {"id": "b"}])
        sets, _ = diff_state(before, after)
        self.assertIn(("events",), [path for path, _ in sets])


if __name__ == "__main__":
    unittest.main()
