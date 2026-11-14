from pathlib import Path
import sqlite3
import tempfile
import unittest

from robocon_sim.storage import Storage

CODE = b'from brsdk import Strategy\nclass MyStrategy(Strategy):\n def on_step(self, obs, api): return None\n'


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage(self.temp.name)
        self.versions = {}
        for name in ('A', 'B', 'C'):
            team = self.storage.create_team(name)
            self.versions[name] = self.storage.upload(team['id'], 'strategy.py', CODE)['id']
            self.versions[name + '_team'] = team['id']

    def run_series(self, a, b, results, count=2, ranked=False):
        series_id = self.storage.create_series(self.versions[a], self.versions[b], count, ranked=ranked)
        batch = self.storage.claim_batch()
        self.assertTrue(self.storage.finish_series(batch['id'], batch['lease'], series_id, results))
        self.storage.finish_batch(batch['id'], batch['lease'], 'COMPLETED', {})
        return series_id

    def test_complete_pair_settles_once(self):
        series_id = self.run_series('A', 'B', [{'winner': 'red', 'status': 'completed'},
                                               {'winner': 'blue', 'status': 'completed'}], ranked=True)
        with self.storage.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM settlements').fetchone()[0], 1)
        ratings = {t['name']: t['elo'] for t in self.storage.ranking()}
        self.assertEqual(ratings['A'], 1012)
        self.assertEqual(ratings['B'], 988)
        self.assertEqual(self.storage.series(series_id)['ranked'], 1)

    def test_unranked_series_never_touches_elo(self):
        self.run_series('A', 'B', [{'winner': 'red'}, {'winner': 'red'}])
        with self.storage.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM settlements').fetchone()[0], 0)
        self.assertTrue(all(t['elo'] == 1000 for t in self.storage.ranking()))

    def test_team_cannot_have_two_open_batches(self):
        self.storage.create_series(self.versions['A'], self.versions['B'], 2)
        with self.assertRaisesRegex(ValueError, 'already has a queued match'):
            self.storage.create_series(self.versions['A'], self.versions['C'], 2)
        batch = self.storage.claim_batch()
        self.storage.finish_batch(batch['id'], batch['lease'], 'VOID', {})
        self.assertIsNotNone(self.storage.create_series(self.versions['A'], self.versions['C'], 2))

    def test_incomplete_series_is_rejected(self):
        series_id = self.storage.create_series(self.versions['A'], self.versions['B'], 2)
        batch = self.storage.claim_batch()
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.storage.finish_series(batch['id'], batch['lease'], series_id, [{'winner': 'red'}])
        self.assertTrue(all(t['elo'] == 1000 for t in self.storage.ranking()))

    def test_stale_lease_cannot_settle(self):
        series_id = self.storage.create_series(self.versions['A'], self.versions['B'], 2)
        self.storage.claim_batch()
        self.assertFalse(self.storage.finish_series('nope', 'wrong-lease', series_id,
                                                    [{'winner': 'red'}, {'winner': 'red'}]))

    def test_failed_series_retries_once_then_voids(self):
        series_id = self.storage.create_series(self.versions['A'], self.versions['B'], 2)
        batch = self.storage.claim_batch()
        self.storage.fail_series(batch['id'], batch['lease'], series_id, 'boom', max_attempts=1)
        self.assertEqual(self.storage.series(series_id)['status'], 'QUEUED')
        self.storage.fail_series(batch['id'], batch['lease'], series_id, 'boom again', max_attempts=1)
        self.assertEqual(self.storage.series(series_id)['status'], 'VOID')
        self.storage.finish_batch(batch['id'], batch['lease'], 'VOID', {})
        with self.storage.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM occupancy').fetchone()[0], 0)

    def test_frozen_version_survives_a_new_upload(self):
        series_id = self.storage.create_series(self.versions['A'], self.versions['B'], 2)
        new = self.storage.upload(self.versions['A_team'], 'new.py', CODE + b'# new\n')
        self.assertNotEqual(new['id'], self.versions['A'])
        self.assertEqual(self.storage.series(series_id)['a_version'], self.versions['A'])
        self.assertEqual(self.storage.version_source(self.versions['A'])[1], CODE)

    def test_unsafe_filename_and_bad_entry(self):
        team = self.storage.create_team('D')
        for name, code in [('../x.py', CODE), ('x.zip', CODE), ('x.py', b'print(1)'),
                           ('x.py', b'from brsdk import Strategy\nclass X(Strategy):\n async def on_step(self,o,a): pass\n')]:
            with self.assertRaises(ValueError):
                self.storage.upload(team['id'], name, code)

    def test_official_version_is_unique_per_team(self):
        team = self.versions['A_team']
        second = self.storage.upload(team, 'second.py', CODE + b'# two\n')
        self.storage.promote(self.versions['A'])
        self.storage.promote(second['id'])
        official = [v for v in self.storage.versions(team) if v['kind'] == 'official']
        self.assertEqual([v['id'] for v in official], [second['id']])
        ranked = {t['name']: t for t in self.storage.ranking()}
        self.assertEqual(ranked['A']['official_version'], second['id'])
        self.assertIsNone(ranked['B']['official_version'])

    def test_unversioned_database_is_rejected(self):
        root = Path(self.temp.name) / 'old'
        Storage(root)
        db = sqlite3.connect(root / 'platform.sqlite3')
        try:
            db.execute('PRAGMA user_version=1')
            db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(ValueError, 'incompatible'):
            Storage(root)


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = Storage(self.temp.name)
        self.team = self.storage.create_team('Alpha')

    def test_first_admin_is_created_once(self):
        created = self.storage.ensure_admin('adminpass123')
        self.assertEqual(created['name'], 'admin')
        self.assertIsNone(self.storage.ensure_admin('adminpass123'))

    def test_login_session_and_logout(self):
        self.storage.create_user('alice', 'password123', 'member', self.team['id'])
        session = self.storage.login('alice', 'password123')
        self.assertEqual(session['user']['team_name'], 'Alpha')
        self.assertIsNotNone(self.storage.session(session['token']))
        self.storage.logout(session['token'])
        self.assertIsNone(self.storage.session(session['token']))

    def test_wrong_password_and_short_password(self):
        self.storage.create_user('alice', 'password123', 'member', self.team['id'])
        with self.assertRaises(ValueError):
            self.storage.login('alice', 'wrongpassword')
        with self.assertRaises(ValueError):
            self.storage.create_user('bob', 'short', 'member', self.team['id'])

    def test_self_service_password_change(self):
        user = self.storage.create_user('alice', 'password123', 'member', self.team['id'])
        with self.assertRaisesRegex(ValueError, 'current password'):
            self.storage.change_password(user['id'], 'wrongpassword', 'password456')
        self.storage.change_password(user['id'], 'password123', 'password456')
        self.storage.login('alice', 'password456')

    def test_member_needs_a_team_and_reset_revokes_sessions(self):
        with self.assertRaisesRegex(ValueError, 'team'):
            self.storage.create_user('bob', 'password123', 'member', None)
        user = self.storage.create_user('alice', 'password123', 'member', self.team['id'])
        token = self.storage.login('alice', 'password123')['token']
        self.storage.set_password(user['id'], 'password456')
        self.assertIsNone(self.storage.session(token))
        self.storage.login('alice', 'password456')


if __name__ == '__main__':
    unittest.main()
