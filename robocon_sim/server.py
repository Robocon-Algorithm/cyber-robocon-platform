"""Local-only HTTP product shell. Uploaded strategies must already be reviewed."""
from typing import Any
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlparse, parse_qs
from .storage import Storage, MAX_UPLOAD
from .runner import run_match, file_hash
from .frame_codec import FrameReader
from .platform_config import ConfigStore, SCHEMA_VERSION
from .replay import event_feed
from .report import series_summary
WEB_ROOT = Path(__file__).resolve().parent / 'web'
if not WEB_ROOT.is_dir():
    WEB_ROOT = WEB_ROOT.parent.parent / 'web'
FRAME_CHUNK = 50

def execute_job(storage: Any, job: Any, frame: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Run a claimed series. Every match config was frozen when the job was created."""
    results = []
    for index in range(job['count']):
        if [file_hash(path) for path in job['paths']] != job['strategy_hashes']:
            raise RuntimeError('frozen strategy source changed')
        a, b = job['paths']
        red, blue = (a, b) if index % 2 == 0 else (b, a)
        output = storage.root / 'runs' / job['id'] / job['lease'] / str(index)
        result = run_match(red, blue, job['matches'][index], output, frame)
        result['replay_index'] = index
        results.append(result)
    if storage.finish(job['id'], job['lease'], results):
        directory = storage.root / 'runs' / job['id'] / job['lease']
        try:
            (directory / 'series_report.json').write_text(
                json.dumps(series_summary(results), ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
        except OSError:
            # SQLite already holds the source results; the report is also available
            # from /api/state. A failed optional export cannot reverse settlement.
            pass
    return results

class Application:

    def __init__(self, root: str | Path, config_path: str | Path | None = None) -> None:
        self.storage = Storage(root, recover=True)
        self.config = ConfigStore(config_path or (Path(root) / 'platform.json'))
        self.token = secrets.token_urlsafe(32)
        self.stop = threading.Event()
        self.thread = None
        self._readers: dict[tuple[str, int, float], FrameReader] = {}

    def start(self) -> None:
        self.thread = threading.Thread(target=self.schedule, daemon=True)
        self.thread.start()

    def schedule(self) -> None:
        while not self.stop.is_set():
            jobs = self.storage.overview()['jobs']
            queued = next((j for j in reversed(jobs) if j['status'] == 'QUEUED'), None)
            if not queued:
                self.stop.wait(0.25)
                continue
            job = self.storage.claim(queued['id'])
            if not job:
                continue
            try:
                execute_job(self.storage, job, self.config.current().frame.to_dict())
            except Exception as exc:
                self.storage.failed(job['id'], job['lease'], repr(exc))

    def platform(self) -> dict[str, Any]:
        config = self.config.current()
        payload = config.public_dict()
        payload['warnings'] = self.config.warnings
        return payload

    def create_job(self, a_version: str, b_version: str, count: int) -> dict[str, Any]:
        config = self.config.current()
        matches = config.matches(count)
        ident = self.storage.create_series(
            a_version, b_version, count, matches=matches, ranked=count > 1)
        return {'id': ident, 'count': count, 'frame': config.frame.to_dict()}

    def bundle_dir(self, job_id: str, index: int) -> tuple[dict[str, Any], Path]:
        job = self.storage.job(job_id)
        if not job.get('result') or not 0 <= index < len(job['result']):
            raise ValueError('replay not available')
        result = job['result'][index]
        path = Path(result['output_dir']).resolve()
        run_root = (self.storage.root / 'runs').resolve()
        if not path.is_relative_to(run_root):
            raise ValueError('invalid replay path')
        return result, path

    def reader(self, path: Path) -> FrameReader:
        """A frame file is ~1 MB; keep a few decoded indexes instead of re-reading."""
        target = path / 'snapshots.rcf'
        key = (str(target), target.stat().st_mtime_ns, target.stat().st_size)
        reader = self._readers.get(key)
        if reader is None:
            if len(self._readers) >= 4:
                self._readers.clear()
            reader = FrameReader(target)
            self._readers[key] = reader
        return reader

    def replay(self, job_id: str, index: int) -> dict[str, Any]:
        result, path = self.bundle_dir(job_id, index)
        reader = self.reader(path)
        meta = reader.meta()
        manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
        manifest.pop('map', None)   # identical to static["map"], do not ship it twice
        return {
            'result': result,
            'manifest': manifest,
            'ledger': json.loads((path / 'score_ledger.json').read_text(encoding='utf-8')),
            'events': event_feed(path),
            'static': reader.static,
            'frame_count': reader.frame_count,
            'chunk': FRAME_CHUNK,
            'times_ms': [row['sim_time_ms'] for row in meta],
            'keyframes': [row['frame'] for row in meta if row['keyframe']],
        }

    def frames(self, job_id: str, index: int, start: int, count: int) -> dict[str, Any]:
        _result, path = self.bundle_dir(job_id, index)
        reader = self.reader(path)
        count = max(1, min(int(count), 200))
        start = max(0, min(int(start), max(0, reader.frame_count - 1)))
        states = reader.states(start, count)
        return {
            'start': start,
            'count': len(states),
            'frames': [
                {'seq': state.get('seq'), 'sim_time_ms': state.get('sim_time_ms'), 'state': state}
                for state in states
            ],
        }

def handler_for(app: Any):

    class Handler(BaseHTTPRequestHandler):
        server_version = 'RoboConLocal/1'

        def log_message(self, *args) -> None:
            pass

        def respond(self, status: Any, data: Any, ctype: Any='application/json; charset=utf-8') -> None:
            raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def valid_host(self) -> bool:
            value = self.headers.get('Host', '').split(':')[0].strip('[]')
            return value in ('127.0.0.1', 'localhost', '::1')

        def do_GET(self) -> None:
            if not self.valid_host():
                self.respond(403, {'error': 'loopback Host required'})
                return
            parsed = urlparse(self.path)
            route = parsed.path
            try:
                if route == '/api/bootstrap':
                    self.respond(200, dict(app.storage.overview(), token=app.token, mode='trusted local code only'))
                    return
                if route == '/api/state':
                    self.respond(200, app.storage.overview())
                    return
                if route == '/api/config':
                    self.respond(200, app.platform())
                    return
                if route == '/api/replay':
                    query = parse_qs(parsed.query)
                    self.respond(200, app.replay(query['job'][0], int(query.get('index', ['0'])[0])))
                    return
                if route == '/api/replay/frames':
                    query = parse_qs(parsed.query)
                    self.respond(200, app.frames(
                        query['job'][0], int(query.get('index', ['0'])[0]),
                        int(query.get('from', ['0'])[0]), int(query.get('count', [FRAME_CHUNK])[0])))
                    return
                files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'application/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
                if route not in files:
                    self.respond(404, {'error': 'not found'})
                    return
                name, ctype = files[route]
                self.respond(200, (WEB_ROOT / name).read_bytes(), ctype)
            except (ValueError, KeyError, IndexError) as exc:
                self.respond(400, {'error': str(exc)})
            except Exception:
                self.respond(500, {'error': 'platform read failure'})

        def do_POST(self) -> None:
            if not self.valid_host() or self.headers.get('X-Robocon-Token') != app.token:
                self.respond(403, {'error': 'local session token required'})
                return
            origin = self.headers.get('Origin')
            if origin and origin not in ('http://' + self.headers.get('Host', ''),):
                self.respond(403, {'error': 'cross-origin mutation blocked'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size < MAX_UPLOAD * 2:
                    raise ValueError('request size exceeded')
                data = json.loads(self.rfile.read(size))
                route = urlparse(self.path).path
                if route == '/api/teams':
                    result = app.storage.create_team(data['name'])
                elif route == '/api/upload':
                    team = app.storage.create_team(data['team_name'])
                    result = app.storage.upload(team['id'], data['filename'], data['source'].encode('utf-8'))
                elif route == '/api/jobs':
                    result = app.create_job(data['a_version'], data['b_version'], int(data.get('count', 2)))
                else:
                    self.respond(404, {'error': 'not found'})
                    return
                self.respond(201, result)
            except (ValueError, KeyError, TypeError, SyntaxError, UnicodeError) as exc:
                self.respond(400, {'error': str(exc)})
            except Exception:
                self.respond(500, {'error': 'platform write failure'})
    return Handler

def make_server(root: str | Path, host: str='127.0.0.1', port: int=8765, trusted_code: bool=False,
                start_scheduler: bool=True, config_path: str | Path | None=None) -> ThreadingHTTPServer:
    if not trusted_code:
        raise ValueError('review strategies first, then explicitly pass --trusted-code')
    if host != 'localhost' and (not ipaddress.ip_address(host).is_loopback):
        raise ValueError('only loopback binding is supported without a verified sandbox')
    app = Application(root, config_path)
    server = ThreadingHTTPServer((host, port), handler_for(app))
    server.app = app
    if start_scheduler:
        app.start()
    return server

def serve(root: str | Path='data/l1-reach', host: str='127.0.0.1', port: int=8765,
          trusted_code: bool=False, config_path: str | Path | None=None) -> None:
    server = make_server(root, host, port, trusted_code, config_path=config_path)
    config = server.app.config.current()
    print('RoboCon local: http://%s:%s — reviewed team code only; process isolation is not a security sandbox'
          % (host, server.server_port), flush=True)
    print('platform config: %s' % (config.path or '(built-in defaults)'), flush=True)
    for warning in server.app.config.warnings:
        print('config warning: %s' % warning['error'], flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app.stop.set()
        server.server_close()
