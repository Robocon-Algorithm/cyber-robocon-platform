import json
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from robocon_sim.platform_config import platform_dict
from robocon_sim.server import make_server
from robocon_sim.storage import Storage

SOURCE = 'from brsdk import Strategy\nclass MyStrategy(Strategy):\n def on_step(self, obs, api): return None\n'


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server = make_server(self.temp.name, port=0, trusted_code=True,
                                  start_scheduler=True, admin_password='adminpass123')
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.close)
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.admin = self.login('admin', 'adminpass123')

    def close(self):
        self.server.app.stop.set()
        self.server.shutdown()
        self.server.server_close()

    def request(self, method, path, payload=None, token=None):
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Robocon-Session'] = token
        return urlopen(Request(self.url + path, data, headers), timeout=120)

    def get(self, path, token=None):
        return json.load(self.request('GET', path, None, token))

    def post(self, path, payload, token=None):
        return json.load(self.request('POST', path, payload, token))

    def login(self, name, password):
        return self.post('/api/login', {'name': name, 'password': password})

    def make_team(self, name):
        team = self.post('/api/admin/teams', {'name': name}, self.admin['token'])
        self.post('/api/admin/users', {'name': name.lower(), 'password': 'password123',
                                       'role': 'member', 'team_id': team['id']}, self.admin['token'])
        session = self.login(name.lower(), 'password123')
        version = self.post('/api/strategies', {'filename': name + '.py', 'source': SOURCE},
                            session['token'])
        return team, session, version

    def wait(self, batch_id, token, timeout=180):
        for _ in range(timeout * 4):
            batch = self.get('/api/batches/' + batch_id, token)
            if batch['status'] in ('COMPLETED', 'PARTIAL', 'VOID'):
                return batch
            threading.Event().wait(0.25)
        raise AssertionError('batch did not finish')

    def test_login_is_required_and_wrong_password_is_rejected(self):
        with self.assertRaises(HTTPError) as caught:
            self.request('GET', '/api/state')
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()
        with self.assertRaises(HTTPError) as caught:
            self.post('/api/login', {'name': 'admin', 'password': 'wrongpassword'})
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()
        self.assertIsNone(self.get('/api/session')['user'])
        self.assertEqual(self.get('/api/session', self.admin['token'])['user']['role'], 'admin')
        self.post('/api/logout', {}, self.admin['token'])
        self.assertIsNone(self.get('/api/session', self.admin['token'])['user'])
        self.admin = self.login('admin', 'adminpass123')

    def test_frontend_is_served_without_a_session(self):
        response = urlopen(self.url + '/')
        self.assertIn(b'BR /', response.read())
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    def test_members_only_see_their_own_strategies(self):
        _, session_a, version_a = self.make_team('Alpha')
        _, _, version_b = self.make_team('Beta')
        listed = self.get('/api/state', session_a['token'])['strategies']
        self.assertEqual([v['id'] for v in listed], [version_a['id']])
        self.assertEqual(len(self.get('/api/state', self.admin['token'])['strategies']), 2)
        body = self.request('GET', '/api/strategies/' + version_a['id'] + '/source', None,
                            session_a['token']).read()
        self.assertIn(b'class MyStrategy', body)
        with self.assertRaises(HTTPError) as caught:
            self.request('GET', '/api/strategies/' + version_b['id'] + '/source', None,
                         session_a['token'])
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_only_admin_manages_accounts(self):
        team, session, _ = self.make_team('Alpha')
        with self.assertRaises(HTTPError) as caught:
            self.post('/api/admin/users', {'name': 'x', 'password': 'password123',
                                           'role': 'member', 'team_id': team['id']}, session['token'])
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_manual_batch_runs_and_replays(self):
        _, session_a, version_a = self.make_team('Alpha')
        _, session_b, version_b = self.make_team('Beta')
        self.post('/api/strategies/' + version_b['id'] + '/official', {}, session_b['token'])
        reply = self.post('/api/batches', {'kind': 'manual', 'version_id': version_a['id'],
                                           'opponent_version': version_b['id'], 'count': 2},
                          session_a['token'])
        self.assertEqual(reply['series'], 1)
        batch = self.wait(reply['id'], session_a['token'])
        self.assertEqual(batch['status'], 'COMPLETED')
        series = batch['series'][0]
        self.assertEqual(series['status'], 'COMPLETED')
        self.assertEqual(series['ranked'], 0)
        bundle = self.get('/api/replay?series=' + series['id'] + '&index=0', session_a['token'])
        self.assertGreater(bundle['frame_count'], 1)
        self.assertIn('map', bundle['static'])
        chunk = self.get('/api/replay/frames?series=' + series['id'] + '&index=0&from=0&count=5',
                         session_a['token'])
        self.assertEqual([f['sim_time_ms'] for f in chunk['frames']], [0, 100, 200, 300, 400])

    def test_official_submission_runs_the_round_robin(self):
        _, session_a, version_a = self.make_team('Alpha')
        _, session_b, version_b = self.make_team('Beta')
        first = self.post('/api/strategies/' + version_a['id'] + '/official', {}, session_a['token'])
        self.assertTrue(first['waiting'])
        second = self.post('/api/strategies/' + version_b['id'] + '/official', {}, session_b['token'])
        self.assertEqual(second['series'], 1)
        batch = self.wait(second['id'], session_b['token'])
        self.assertEqual(batch['kind'], 'official')
        self.assertEqual(batch['status'], 'COMPLETED')
        ranking = {t['name']: t for t in self.get('/api/state', self.admin['token'])['teams']}
        self.assertEqual(ranking['Alpha']['pairs'], 1)
        self.assertEqual(ranking['Beta']['pairs'], 1)
        self.assertEqual(ranking['Alpha']['elo'], 1000)

    def test_mixed_profiles_are_never_ranked(self):
        _, session_a, version_a = self.make_team('Alpha')
        _, session_b, version_b = self.make_team('Beta')
        self.post('/api/strategies/' + version_b['id'] + '/official', {}, session_b['token'])
        reply = self.post('/api/batches', {'kind': 'manual', 'version_id': version_a['id'],
                                           'opponent_version': version_b['id'], 'count': 2,
                                           'red_profile': 'baseline', 'blue_profile': 'training'},
                          session_a['token'])
        batch = self.wait(reply['id'], session_a['token'])
        self.assertEqual(batch['series'][0]['ranked'], 0)
        self.assertIsNone(batch['series'][0]['rating_timing_profile'])

    def test_loopback_and_trusted_requirements(self):
        with self.assertRaises(ValueError):
            make_server(self.temp.name, host='0.0.0.0', trusted_code=True)
        with self.assertRaises(ValueError):
            make_server(self.temp.name)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Storage(self.temp.name).root
        (root / 'platform.json').write_text(
            json.dumps({**platform_dict(), 'default_match': {'duration_ms': 3000}}), encoding='utf-8')
        self.app = make_server(self.temp.name, port=0, trusted_code=True,
                               start_scheduler=False, admin_password='adminpass123').app
        self.team_a = self.app.storage.create_team('Alpha')
        self.team_b = self.app.storage.create_team('Beta')
        self.user_a = self.app.storage.create_user('alpha', 'password123', 'member', self.team_a['id'])
        self.va = self.app.storage.upload(self.team_a['id'], 'a.py', SOURCE.encode(), self.user_a['id'])
        self.vb = self.app.storage.upload(self.team_b['id'], 'b.py', SOURCE.encode(), None)
        self.app.storage.promote(self.vb['id'])
        self.user = self.app.storage.session(self.app.storage.login('alpha', 'password123')['token'])

    def test_config_public_view(self):
        config = self.app.config.current().public_dict()
        self.assertEqual([p['name'] for p in config['profiles']], ['baseline', 'training'])
        self.assertEqual(config['default_match']['duration_ms'], 3000)
        self.assertTrue(config['perturbation']['enabled'])
        self.assertEqual(config['perturbation']['applies_to'], ['official'])

    def test_perturbation_is_frozen_into_the_queued_series(self):
        reply = self.app.submit_official(self.user, self.va['id'], {'nonce': 'one'})
        batch = self.app.storage.batch(reply['id'])
        matches = batch['series'][0]['matches']
        base = self.app.config.current().profiles['baseline'].parameters
        self.assertNotEqual(matches[0]['parameters']['empty_speed_mm_s'], base['empty_speed_mm_s'])
        self.assertEqual(matches[0]['parameters'], matches[1]['parameters'])
        self.assertTrue(batch['config']['notes'][0].startswith('jitter'))

    def test_perturbation_off_leaves_the_profile_untouched(self):
        platform = self.app.config.current()
        self.app.config._current = type(platform)(
            default_match=platform.default_match, scenarios=platform.scenarios, frame=platform.frame,
            profiles=platform.profiles, default_profiles=platform.default_profiles,
            perturbation=type(platform.perturbation)(enabled=False), schema=platform.schema,
            path=platform.path)
        reply = self.app.submit_official(self.user, self.va['id'], {'nonce': 'two'})
        matches = self.app.storage.batch(reply['id'])['series'][0]['matches']
        base = platform.profiles['baseline'].parameters
        self.assertEqual(matches[0]['parameters']['empty_speed_mm_s'], base['empty_speed_mm_s'])


if __name__ == '__main__':
    unittest.main()
