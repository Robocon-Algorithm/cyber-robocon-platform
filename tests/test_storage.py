from pathlib import Path
import json
import sqlite3
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
    def test_series_freezes_every_match_config(self):
        matches=[{'duration_ms':5000},{'duration_ms':6000}]
        ident=self.storage.create_series(*self.versions[:2],2,matches=matches,ranked=False)
        job=self.storage.claim(ident)
        self.assertEqual([m['duration_ms'] for m in job['matches']],[5000,6000])
        self.assertEqual(job['config']['duration_ms'],5000)
        self.assertFalse(job['ranked'])
        self.assertIsNone(job['rating_timing_profile'])
        with self.assertRaisesRegex(ValueError,'every game'):
            self.storage.create_series(*self.versions[:2],2,matches=matches[:1],ranked=False)
    def test_ranked_series_uses_the_timing_profile(self):
        ident=self.storage.create_series(*self.versions[:2],2,matches=[{},{}],ranked=True)
        self.assertEqual(self.storage.claim(ident)['rating_timing_profile'],RATING_TIMING_PROFILE)
        with self.assertRaisesRegex(ValueError,'default'):
            self.storage.create_series(*self.versions[:2],2,matches=[{},{}],ranked=True,config={'duration_ms':5000})
    def test_unversioned_database_is_rejected(self):
        root=Path(self.temp.name)/'old'
        Storage(root)
        db=sqlite3.connect(root/'platform.sqlite3')
        try:
            db.execute('PRAGMA user_version=0');db.commit()
        finally:
            db.close()
        with self.assertRaisesRegex(ValueError,'incompatible'):Storage(root)

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
