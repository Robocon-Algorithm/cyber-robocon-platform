from pathlib import Path
import json
import tempfile
import unittest
from robocon_sim.config import RATING_TIMING_PROFILE
from robocon_sim.storage import Storage

CODE=b'from brsdk import Strategy\nclass MyStrategy(Strategy):\n def on_step(self, obs, api): return None\n'
class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.storage=Storage(self.temp.name);self.versions=[]
        for name in ('A','B','C'):
            team=self.storage.create_team(name);self.versions.append(self.storage.upload(team['id'],'strategy.py',CODE)['id'])
    def test_complete_pair_settles_once(self):
        ident=self.storage.create_series(*self.versions[:2]);job=self.storage.claim(ident)
        results=[{'winner':'red','status':'completed'},{'winner':'blue','status':'completed'}]
        self.assertTrue(self.storage.finish(ident,job['lease'],results));self.assertFalse(self.storage.finish(ident,job['lease'],results))
        with self.storage.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM settlements').fetchone()[0],1)
        ratings={t['name']:t['elo'] for t in self.storage.overview()['teams']};self.assertEqual(ratings['A'],1012);self.assertEqual(ratings['B'],988)
    def test_legacy_database_keeps_strategies_and_settles_old_jobs_separately(self):
        old_id = self.storage.create_series(*self.versions[:2])
        old_config = self.storage.job(old_id)['config']
        old_config['parameters'].update(
            empty_speed_mm_s=1000, loaded_speed_mm_s=800, ball_speed_mm_s=600, place_ball_ms=4000,
        )
        with self.storage.connect() as db:
            db.execute('ALTER TABLE series DROP COLUMN rating_timing_profile')
            db.execute('DROP TABLE team_ratings')
            db.execute('UPDATE teams SET elo=1234 WHERE name=?', ('A',))
            db.execute('UPDATE series SET config=? WHERE id=?', (json.dumps(old_config), old_id))

        storage = Storage(self.temp.name)
        overview = storage.overview()
        self.assertEqual([t['elo'] for t in overview['teams']], [1000, 1000, 1000])
        self.assertEqual([t['active_version'] for t in overview['teams']], self.versions)
        self.assertEqual(storage.create_team('A')['elo'], 1000)
        old = storage.claim(old_id)
        self.assertEqual(old['rating_timing_profile'], 'legacy')
        self.assertEqual(old['config'], old_config)
        results = [{'winner': 'red'}, {'winner': 'blue'}]
        self.assertTrue(storage.finish(old_id, old['lease'], results))
        with storage.connect() as db:
            legacy_ratings = [r['elo'] for r in db.execute('SELECT elo FROM teams ORDER BY name')]
        self.assertGreater(legacy_ratings[0], 1234)
        self.assertEqual([t['elo'] for t in storage.overview()['teams']], [1000, 1000, 1000])

        new_id = storage.create_series(*self.versions[:2])
        new = storage.claim(new_id)
        self.assertEqual(new['rating_timing_profile'], RATING_TIMING_PROFILE)
        self.assertTrue(storage.finish(new_id, new['lease'], results))
        self.assertEqual(
            [(t['name'], t['elo']) for t in storage.overview()['teams']],
            [('A', 1012), ('C', 1000), ('B', 988)],
        )
        with storage.connect() as db:
            self.assertEqual([r['elo'] for r in db.execute('SELECT elo FROM teams ORDER BY name')], legacy_ratings)

    def test_custom_series_does_not_enter_either_rating_profile(self):
        ident = self.storage.create_series(
            *self.versions[:2], config={'parameters': {'place_ball_ms': 4000}},
        )
        job = self.storage.claim(ident)
        self.assertFalse(job['ranked'])
        self.assertIsNone(job['rating_timing_profile'])
        self.assertTrue(self.storage.finish(ident, job['lease'], [{'winner': 'red'}, {'winner': 'blue'}]))
        with self.storage.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM settlements').fetchone()[0], 0)
        self.assertEqual([t['elo'] for t in self.storage.overview()['teams']], [1000, 1000, 1000])

    def test_occupancy_and_incomplete_result(self):
        ident=self.storage.create_series(*self.versions[:2]);job=self.storage.claim(ident)
        with self.assertRaisesRegex(ValueError,'active series'):self.storage.create_series(self.versions[0],self.versions[2])
        with self.assertRaisesRegex(ValueError,'incomplete'):self.storage.finish(ident,job['lease'],[{'winner':'red'}])
        self.assertTrue(all(t['elo']==1000 for t in self.storage.overview()['teams']))
    def test_retry_rejects_old_worker(self):
        ident=self.storage.create_series(*self.versions[:2]);old=self.storage.claim(ident);self.storage.failed(ident,old['lease'],'platform fault');new=self.storage.claim(ident)
        self.assertNotEqual(old['lease'],new['lease']);self.assertFalse(self.storage.finish(ident,old['lease'],[{'winner':'red'}]*2));self.storage.failed(ident,new['lease'],'failed again')
        self.assertEqual(self.storage.job(ident)['status'],'VOID');self.assertTrue(all(t['elo']==1000 for t in self.storage.overview()['teams']))
    def test_frozen_version(self):
        ident=self.storage.create_series(*self.versions[:2]);job=self.storage.claim(ident);team=job['a'];new=self.storage.upload(team,'new.py',CODE+b'# new version\n')
        self.assertNotEqual(new['id'],self.storage.job(ident)['a_version']);self.assertEqual(self.storage.job(ident)['a_version'],self.versions[0])
    def test_unsafe_filename_and_bad_entry(self):
        team=self.storage.create_team('D')
        for name,code in [('../x.py',CODE),('x.zip',CODE),('x.py',b'print(1)'),('x.py',b'from brsdk import Strategy\nclass X(Strategy):\n async def on_step(self,o,a): pass\n')]:
            with self.assertRaises(ValueError):self.storage.upload(team['id'],name,code)

if __name__=='__main__':unittest.main()
