"""Loopback HTTP API: accounts, strategies, match batches and replay."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
import ipaddress
import json
import mimetypes
import threading
import traceback

from .frame_codec import FrameReader
from .platform_config import ConfigStore, SCHEMA_VERSION, seed_platform_files
from .replay import event_feed
from .report import series_summary
from .runner import run_match, file_hash
from .storage import Storage

WEB_ROOT = Path(__file__).resolve().parent / 'web'
if not WEB_ROOT.is_dir():
    WEB_ROOT = WEB_ROOT.parent.parent / 'web'
FRAME_CHUNK = 50
OFFICIAL_GAMES = 2


class ApiError(Exception):

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def execute_series(storage: Storage, batch: dict[str, Any], series: dict[str, Any],
                   frame: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run every game of one series. Configs were frozen when the batch was queued."""
    lease = batch['lease']
    results = []
    for index in range(series['count']):
        if [file_hash(path) for path in series['paths']] != series['strategy_hashes']:
            raise RuntimeError('frozen strategy source changed')
        a, b = series['paths']
        red, blue = (a, b) if index % 2 == 0 else (b, a)
        output = storage.root / 'runs' / batch['id'] / lease / str(series['position']) / str(index)
        result = run_match(red, blue, series['matches'][index], output, frame)
        result['replay_index'] = index
        results.append(result)
    try:
        (storage.root / 'runs' / batch['id'] / lease / str(series['position']) / 'series_report.json'
         ).write_text(json.dumps(series_summary(results), ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError:
        # SQLite already holds the results; the report is a convenience export.
        pass
    return results


class Application:

    def __init__(self, root: str | Path, config_path: str | Path | None = None,
                 admin_password: str | None = None) -> None:
        self.storage = Storage(root, recover=True)
        self.created_admin = self.storage.ensure_admin(admin_password)
        self.config = ConfigStore(config_path or (Path(root) / 'platform.json'))
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self._readers: dict[tuple[str, int, float], FrameReader] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop.is_set():
            batch = self.storage.claim_batch()
            if batch is None:
                self.stop.wait(0.25)
                continue
            try:
                self._run_batch(batch)
            except Exception:
                traceback.print_exc()
                self.storage.finish_batch(batch['id'], batch['lease'], 'VOID',
                                          {'error': 'scheduler crashed'})

    def _run_batch(self, batch: dict[str, Any]) -> None:
        frame = self.config.current().frame.to_dict()
        completed = 0
        outcomes = []
        for series in batch['series']:
            ok = self._run_series(batch, series, frame)
            completed += 1 if ok else 0
            outcomes.append({'series': series['id'], 'position': series['position'],
                             'a': series['a'], 'b': series['b'], 'ok': ok})
        total = len(batch['series'])
        status = 'COMPLETED' if completed == total else ('VOID' if completed == 0 else 'PARTIAL')
        self.storage.finish_batch(batch['id'], batch['lease'], status,
                                  {'series': total, 'completed': completed, 'outcomes': outcomes})

    def _run_series(self, batch: dict[str, Any], series: dict[str, Any],
                    frame: dict[str, Any]) -> bool:
        for attempt in (0, 1):
            try:
                results = execute_series(self.storage, batch, series, frame)
            except Exception as exc:
                traceback.print_exc()
                self.storage.fail_series(batch['id'], batch['lease'], series['id'], exc,
                                         max_attempts=1 if attempt == 0 else 0)
                continue
            try:
                return self.storage.finish_series(batch['id'], batch['lease'], series['id'], results)
            except Exception as exc:
                self.storage.fail_series(batch['id'], batch['lease'], series['id'], exc, max_attempts=0)
                return False
        return False

    # -- auth --------------------------------------------------------------

    def session(self, token: str | None) -> dict[str, Any] | None:
        return self.storage.session(token)

    def require(self, token: str | None) -> dict[str, Any]:
        user = self.session(token)
        if user is None:
            raise ApiError(401, 'login required')
        return user

    def require_admin(self, token: str | None) -> dict[str, Any]:
        user = self.require(token)
        if user.get('role') != 'admin':
            raise ApiError(403, 'administrator role required')
        return user

    def login(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.storage.login(payload.get('name', ''), payload.get('password', ''))
        except ValueError:
            raise ApiError(401, 'user name or password is incorrect')

    def logout(self, token: str | None) -> dict[str, Any]:
        self.storage.logout(token)
        return {'ok': True}

    # -- views -------------------------------------------------------------

    def state(self, user: dict[str, Any]) -> dict[str, Any]:
        admin = user.get('role') == 'admin'
        platform = self.config.current()
        payload: dict[str, Any] = {
            'user': user,
            'is_admin': admin,
            'teams': self.storage.ranking(),
            'batches': self.storage.batches(60 if admin else 30),
            'strategies': self.storage.versions(None if admin else user.get('team_id')),
            'platform': {
                'schema': SCHEMA_VERSION,
                'profiles': [platform.profiles[name].public_dict() for name in sorted(platform.profiles)],
                'default_profiles': dict(platform.default_profiles),
                'perturbation': platform.perturbation.to_dict(),
                'duration_ms': platform.default_match.duration_ms,
            },
        }
        if admin:
            payload['users'] = self.storage.users()
        return payload

    def create_batch(self, user: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        if not user.get('team_id'):
            raise ApiError(403, 'this account has no team; ask an administrator')
        kind = str(payload.get('kind') or 'manual')
        if kind not in ('manual', 'auto_round', 'all_play'):
            raise ApiError(400, 'unknown batch kind')
        team_id = user['team_id']
        own = self._own_version(team_id, payload.get('version_id'))
        opponents = self._opponents(team_id)
        if kind == 'manual':
            target = payload.get('opponent_version')
            if not target:
                raise ApiError(400, 'choose an opponent')
            opponents = [o for o in opponents if o['version'] == target]
            if not opponents:
                raise ApiError(400, 'opponent has no official strategy')
        if not opponents:
            raise ApiError(400, 'no opponent with an official strategy yet')
        default_count = 1 if kind == 'auto_round' else 2
        count = int(payload.get('count') or default_count)
        if count not in (1, 2, 10):
            raise ApiError(400, 'count must be 1, 2 or 10')
        plan = self._plan(kind, {'team': team_id, 'version': own['id']}, opponents, count, payload)
        batch_id = self.storage.create_batch(
            kind, team_id, user['id'], [spec for spec, _ in plan],
            {'kind': kind, 'count': count, 'opponents': [o['team'] for o in opponents],
             'notes': [note for _, note in plan]})
        return {'id': batch_id, 'kind': kind, 'series': len(plan)}

    def submit_official(self, user: dict[str, Any], version_id: str,
                        payload: dict[str, Any]) -> dict[str, Any]:
        if not user.get('team_id'):
            raise ApiError(403, 'this account has no team; ask an administrator')
        team_id = user['team_id']
        version = self._version_row(team_id, version_id)
        self.storage.promote(version['id'])
        opponents = self._opponents(team_id)
        if not opponents:
            # First official entry on the platform: nothing to play yet. Later
            # submissions from other teams will schedule series against it.
            return {'id': None, 'kind': 'official', 'series': 0, 'version': version['id'],
                    'waiting': True}
        own = {'team': team_id, 'version': version['id']}
        plan = self._plan('official', own, opponents, OFFICIAL_GAMES, payload)
        batch_id = self.storage.create_batch(
            'official', team_id, user['id'], [spec for spec, _ in plan],
            {'kind': 'official', 'count': OFFICIAL_GAMES, 'version': version['id'],
             'opponents': [o['team'] for o in opponents],
             'notes': [note for _, note in plan]})
        return {'id': batch_id, 'kind': 'official', 'series': len(plan), 'version': version['id']}

    # -- helpers -----------------------------------------------------------

    def _own_version(self, team_id: str, version_id: str | None) -> dict[str, Any]:
        versions = self.storage.versions(team_id)
        if version_id:
            found = [v for v in versions if v['id'] == version_id]
            if not found:
                raise ApiError(400, 'unknown strategy version')
            return found[0]
        official = [v for v in versions if v['kind'] == 'official']
        if official:
            return official[0]
        if versions:
            return versions[0]
        raise ApiError(400, 'upload a strategy first')

    def _version_row(self, team_id: str, version_id: str) -> dict[str, Any]:
        versions = [v for v in self.storage.versions(team_id) if v['id'] == version_id]
        if not versions:
            raise ApiError(400, 'unknown strategy version')
        return versions[0]

    def _opponents(self, team_id: str) -> list[dict[str, Any]]:
        return [{'team': team['id'], 'name': team['name'], 'version': team['official_version']}
                for team in self.storage.ranking()
                if team['id'] != team_id and team.get('official_version')]

    def _plan(self, kind: str, own: dict[str, Any], opponents: list[dict[str, Any]],
              count: int, payload: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
        platform = self.config.current()
        # Official play must be symmetric, so both sides share the red default
        # profile regardless of what the requester picked for training runs.
        if kind == 'official':
            red_profile = blue_profile = platform.default_profiles.get('red', 'baseline')
        else:
            red_profile = payload.get('red_profile') or platform.default_profiles.get('red', 'baseline')
            blue_profile = payload.get('blue_profile') or red_profile
        red = platform.profile(red_profile, 'red')
        blue = platform.profile(blue_profile, 'blue')
        ranked = kind == 'official' and red.parameters == blue.parameters
        plan = []
        for opponent in opponents:
            matches = platform.matches(count, red_profile=red.name, blue_profile=blue.name)
            note = ''
            if platform.perturbation.applies(kind):
                key = f'{own["team"]}|{opponent["team"]}|{kind}|{payload.get("nonce", "")}'
                jittered, factors = platform.perturb(matches[0]['parameters'], key)
                for item in matches:
                    item['parameters'] = dict(jittered)
                    item['sides'] = {}
                note = 'jitter ' + ' '.join(f'{k}x{v}' for k, v in factors.items())
            plan.append(({
                'a_version': own['version'], 'b_version': opponent['version'], 'count': count,
                'matches': matches, 'ranked': ranked, 'rating_profile': red.rating_key(),
                'red_profile': red.name, 'blue_profile': blue.name,
            }, note))
        return plan

    # -- replays -----------------------------------------------------------

    def series_dir(self, series_id: str, index: int) -> tuple[dict[str, Any], Path]:
        series = self.storage.series(series_id)
        results = series.get('result') or []
        if not 0 <= index < len(results):
            raise ApiError(400, 'replay not available')
        path = Path(results[index]['output_dir']).resolve()
        run_root = (self.storage.root / 'runs').resolve()
        if not path.is_relative_to(run_root):
            raise ApiError(400, 'invalid replay path')
        return series, path

    def reader(self, path: Path) -> FrameReader:
        target = path / 'snapshots.rcf'
        key = (str(target), target.stat().st_mtime_ns, target.stat().st_size)
        reader = self._readers.get(key)
        if reader is None:
            if len(self._readers) >= 4:
                self._readers.clear()
            reader = FrameReader(target)
            self._readers[key] = reader
        return reader

    def replay(self, series_id: str, index: int) -> dict[str, Any]:
        series, path = self.series_dir(series_id, index)
        reader = self.reader(path)
        meta = reader.meta()
        manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
        manifest.pop('map', None)
        return {
            'result': series['result'][index],
            'series': {key: series.get(key) for key in
                       ('id', 'batch_id', 'position', 'a', 'b', 'a_version', 'b_version',
                        'count', 'ranked', 'red_profile', 'blue_profile', 'rating_timing_profile')},
            'manifest': manifest,
            'ledger': json.loads((path / 'score_ledger.json').read_text(encoding='utf-8')),
            'events': event_feed(path),
            'static': reader.static,
            'frame_count': reader.frame_count,
            'chunk': FRAME_CHUNK,
            'times_ms': [row['sim_time_ms'] for row in meta],
            'keyframes': [row['frame'] for row in meta if row['keyframe']],
        }

    def frames(self, series_id: str, index: int, start: int, count: int) -> dict[str, Any]:
        _series, path = self.series_dir(series_id, index)
        reader = self.reader(path)
        count = max(1, min(int(count), 200))
        start = max(0, min(int(start), max(0, reader.frame_count - 1)))
        states = reader.states(start, count)
        return {'start': start, 'count': len(states),
                'frames': [{'seq': s.get('seq'), 'sim_time_ms': s.get('sim_time_ms'), 'state': s}
                           for s in states]}

    # -- admin -------------------------------------------------------------

    def admin_create_user(self, token: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_admin(token)
        team_id = payload.get('team_id')
        if payload.get('team_name') and not team_id:
            team_id = self.storage.create_team(payload['team_name'])['id']
        return self.storage.create_user(payload.get('name', ''), payload.get('password', ''),
                                        str(payload.get('role') or 'member'), team_id)

    def admin_create_team(self, token: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_admin(token)
        return self.storage.create_team(payload.get('name', ''))

    def admin_set_password(self, token: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_admin(token)
        self.storage.set_password(payload.get('user_id', ''), payload.get('password', ''))
        return {'ok': True}


def handler_for(app: Application):

    class Handler(BaseHTTPRequestHandler):
        server_version = 'RoboConLocal/1.0'
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def respond(self, status: int, value: Any) -> None:
            body = json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def respond_bytes(self, body: bytes, content_type: str, filename: str) -> None:
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def token(self) -> str | None:
            return self.headers.get('X-Robocon-Session')

        def body(self) -> dict[str, Any]:
            length = int(self.headers.get('Content-Length') or 0)
            if length > 2 * 1024 * 1024:
                raise ApiError(413, 'request too large')
            raw = self.rfile.read(length) if length else b'{}'
            try:
                value = json.loads(raw or b'{}')
            except json.JSONDecodeError:
                raise ApiError(400, 'invalid JSON')
            if not isinstance(value, dict):
                raise ApiError(400, 'JSON object required')
            return value

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            route, query = parsed.path, parse_qs(parsed.query)
            try:
                if route in ('/', '/index.html', '/app.js', '/style.css'):
                    return self.static(route)
                user = app.session(self.token())
                if route == '/api/session':
                    self.respond(200, {'user': user})
                    return
                if user is None:
                    raise ApiError(401, 'login required')
                if route == '/api/state':
                    self.respond(200, app.state(user))
                    return
                if route == '/api/config':
                    payload = app.config.current().public_dict()
                    payload['warnings'] = app.config.warnings
                    self.respond(200, payload)
                    return
                if route == '/api/replay':
                    self.respond(200, app.replay(query['series'][0], int(query.get('index', ['0'])[0])))
                    return
                if route == '/api/replay/frames':
                    self.respond(200, app.frames(query['series'][0], int(query.get('index', ['0'])[0]),
                                                 int(query.get('from', ['0'])[0]),
                                                 int(query.get('count', [FRAME_CHUNK])[0])))
                    return
                if route.startswith('/api/strategies/') and route.endswith('/source'):
                    name, data, row = app.storage.version_source(route.split('/')[3])
                    if user['role'] != 'admin' and row['team_id'] != user.get('team_id'):
                        raise ApiError(403, 'not your team strategy')
                    self.respond_bytes(data, 'text/x-python; charset=utf-8', name)
                    return
                if route.startswith('/api/batches/'):
                    batch = app.storage.batch(route.split('/')[3])
                    if user['role'] != 'admin' and batch['team_id'] != user.get('team_id'):
                        raise ApiError(403, 'not your batch')
                    self.respond(200, batch)
                    return
                raise ApiError(404, 'not found')
            except ApiError as exc:
                self.respond(exc.status, {'error': exc.message})
            except KeyError:
                self.respond(400, {'error': 'missing parameter'})
            except ValueError as exc:
                self.respond(400, {'error': str(exc)})
            except Exception as exc:
                traceback.print_exc()
                self.respond(500, {'error': 'internal error: ' + str(exc)})

        def do_POST(self) -> None:
            route = urlparse(self.path).path
            try:
                payload = self.body()
                if route == '/api/login':
                    self.respond(200, app.login(payload))
                    return
                if route == '/api/logout':
                    self.respond(200, app.logout(self.token()))
                    return
                user = app.require(self.token())
                if route == '/api/strategies':
                    if not user.get('team_id'):
                        raise ApiError(403, 'this account has no team; ask an administrator')
                    source = payload.get('source')
                    if not isinstance(source, str):
                        raise ApiError(400, 'source text required')
                    self.respond(200, app.storage.upload(user['team_id'], payload.get('filename', ''),
                                                         source.encode('utf-8'), user['id']))
                    return
                if route == '/api/batches':
                    self.respond(200, app.create_batch(user, payload))
                    return
                if route.startswith('/api/strategies/') and route.endswith('/official'):
                    self.respond(200, app.submit_official(user, route.split('/')[3], payload))
                    return
                if route == '/api/admin/users':
                    self.respond(200, app.admin_create_user(self.token(), payload))
                    return
                if route == '/api/admin/teams':
                    self.respond(200, app.admin_create_team(self.token(), payload))
                    return
                if route == '/api/admin/password':
                    self.respond(200, app.admin_set_password(self.token(), payload))
                    return
                raise ApiError(404, 'not found')
            except ApiError as exc:
                self.respond(exc.status, {'error': exc.message})
            except (ValueError, PermissionError) as exc:
                self.respond(400, {'error': str(exc)})
            except Exception as exc:
                traceback.print_exc()
                self.respond(500, {'error': 'internal error: ' + str(exc)})

        def static(self, route: str) -> None:
            name = 'index.html' if route in ('/', '/index.html') else route.lstrip('/')
            path = (WEB_ROOT / name).resolve()
            if not path.is_file() or WEB_ROOT.resolve() not in path.parents:
                raise ApiError(404, 'not found')
            body = path.read_bytes()
            kind = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', f'{kind}; charset=utf-8' if kind.startswith('text/') else kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

    return Handler


def make_server(root: str | Path, host: str = '127.0.0.1', port: int = 8765,
                trusted_code: bool = False, start_scheduler: bool = True,
                config_path: str | Path | None = None,
                admin_password: str | None = None) -> ThreadingHTTPServer:
    if not trusted_code:
        raise ValueError('review strategies first, then explicitly pass --trusted-code')
    if host != 'localhost' and (not ipaddress.ip_address(host).is_loopback):
        raise ValueError('only loopback binding is supported without a verified sandbox')
    app = Application(root, config_path, admin_password)
    server = ThreadingHTTPServer((host, port), handler_for(app))
    server.app = app
    if start_scheduler:
        app.start()
    return server


def serve(root: str | Path = 'data/arena', host: str = '127.0.0.1', port: int = 8765,
          trusted_code: bool = False, config_path: str | Path | None = None,
          admin_password: str | None = None) -> None:
    seeded = seed_platform_files(Path(config_path).parent if config_path else Path(root))
    server = make_server(root, host, port, trusted_code, config_path=config_path,
                         admin_password=admin_password)
    config = server.app.config.current()
    print('RoboCon local: http://%s:%s — reviewed team code only; process isolation is not a security sandbox'
          % (host, server.server_port), flush=True)
    print('platform config: %s' % (config.path or '(built-in defaults)'), flush=True)
    print('side profiles: %s' % ', '.join(sorted(config.profiles)), flush=True)
    created = server.app.created_admin
    if created:
        print('', flush=True)
        print('created administrator account', flush=True)
        print('  user:     %s' % created['name'], flush=True)
        print('  password: %s' % created['password'], flush=True)
        print('  shown once; change it after the first login', flush=True)
        print('', flush=True)
    for path in seeded:
        print('created default config: %s' % path, flush=True)
    for warning in server.app.config.warnings:
        print('config warning: %s' % warning['error'], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app.stop.set()
        server.server_close()
