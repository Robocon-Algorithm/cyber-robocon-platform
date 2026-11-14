import json
import tempfile
import unittest
from pathlib import Path

from robocon_sim.platform_config import (
    ConfigStore,
    FrameOptions,
    SCHEMA_VERSION,
    platform_dict,
)


class MatchesTests(unittest.TestCase):
    def store(self, document=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'platform.json'
        if document is not None:
            path.write_text(json.dumps(document), encoding='utf-8')
        return ConfigStore(path), path

    def test_defaults_expand_two_games_with_one_seed(self):
        store, _ = self.store()
        matches = store.current().matches(2)
        self.assertEqual(len(matches), 2)
        self.assertEqual([m['duration_ms'] for m in matches], [180000, 180000])
        self.assertEqual([m['seed'] for m in matches], [0, 0])

    def test_ten_game_set_applies_five_pair_templates(self):
        store, _ = self.store()
        matches = store.current().matches(10)
        self.assertEqual([m['seed'] for m in matches], [0, 0, 1, 1, 2, 2, 3, 3, 4, 4])
        self.assertEqual([m['supply_delays_ms']['red'] for m in matches],
                         [9000, 9000, 8000, 8000, 11000, 11000, 9500, 9500, 9000, 9000])
        self.assertEqual(matches[8]['faults'][0]['mode'], 'upper_slip')
        self.assertEqual(matches[0]['faults'], [])

    def test_overrides_are_validated_and_applied(self):
        store, _ = self.store()
        matches = store.current().matches(2, {'duration_ms': 5000})
        self.assertEqual([m['duration_ms'] for m in matches], [5000, 5000])
        with self.assertRaises(ValueError):
            store.current().matches(2, {'duration_ms': 0})
        with self.assertRaises(ValueError):
            store.current().matches(2, {'unknown_field': 1})

    def test_frame_options_round_trip(self):
        store, _ = self.store({**platform_dict(), 'frame': {'keyframe_interval': 7}})
        self.assertEqual(store.current().frame.keyframe_interval, 7)
        self.assertEqual(store.current().frame.to_dict()['max_delta_ops'], FrameOptions().max_delta_ops)


class ReloadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'platform.json'
        self.warnings = []
        self.store = ConfigStore(self.path, warn=self.warnings.append)

    def write(self, document):
        self.path.write_text(json.dumps(document), encoding='utf-8')

    def test_missing_file_falls_back_to_builtin_defaults(self):
        self.assertEqual(self.store.current().default_match.duration_ms, 180000)
        self.assertEqual(self.warnings, [])

    def test_edit_is_picked_up_on_the_next_read(self):
        self.write({**platform_dict(), 'default_match': {'duration_ms': 9000}})
        self.assertEqual(self.store.current().default_match.duration_ms, 9000)
        self.write({**platform_dict(), 'default_match': {'duration_ms': 12000}})
        self.assertEqual(self.store.current().default_match.duration_ms, 12000)

    def test_broken_file_keeps_the_last_good_configuration(self):
        self.write({**platform_dict(), 'default_match': {'duration_ms': 9000}})
        self.assertEqual(self.store.current().default_match.duration_ms, 9000)
        self.path.write_text('{ not json', encoding='utf-8')
        self.assertEqual(self.store.current().default_match.duration_ms, 9000)
        self.assertEqual(len(self.warnings), 1)
        self.assertEqual(len(self.store.warnings), 1)
        self.path.write_text(json.dumps({**platform_dict(), 'default_match': {'duration_ms': 11000}}), encoding='utf-8')
        self.assertEqual(self.store.current().default_match.duration_ms, 11000)

    def test_unknown_schema_and_bad_parameters_are_rejected(self):
        self.write({**platform_dict(), 'schema': 99})
        self.store.current()
        self.assertIn('schema', self.warnings[-1])
        self.write({**platform_dict(), 'default_match': {'parameters': {'nope': 1}}})
        self.store.current()
        self.assertIn('parameters', self.warnings[-1])

    def test_bad_scenario_template_is_rejected_at_load_time(self):
        self.write({**platform_dict(), 'scenarios': {'10': [{'duration_ms': -5}]}})
        self.store.current()
        self.assertTrue(any('duration_ms' in warning for warning in self.warnings))

    def test_public_dict_is_json_safe_and_hides_nothing_private(self):
        self.write(platform_dict())
        payload = self.store.current().public_dict()
        self.assertEqual(payload['schema'], SCHEMA_VERSION)
        json.dumps(payload)
        self.assertNotIn('seed', payload['default_match'])
        self.assertNotIn('faults', payload['default_match'])


if __name__ == '__main__':
    unittest.main()
