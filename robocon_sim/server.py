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
from .replay import read_log
from .report import series_summary
WEB_ROOT = Path(__file__).resolve().parent / 'web'
if not WEB_ROOT.is_dir():
    WEB_ROOT = WEB_ROOT.parent.parent / 'web'

def scenario(config: Any, index: int, count: int) -> dict[str, Any]:
    data = dict(config)
    data['seed'] = int(data.get('seed', 0)) + index // 2
    if count == 10:
        pair = index // 2
        data['supply_delays_ms'] = [{'red': 9000, 'blue': 10000}, {'red': 8000, 'blue': 11000}, {'red': 11000, 'blue': 9000}, {'red': 9500, 'blue': 10500}, {'red': 9000, 'blue': 10000}][pair]
        if pair == 4 and (not data.get('faults')):
            data['faults'] = [{'team': 'red', 'role': 'TR', 'material_kind': 'box', 'occurrence': 1, 'mode': 'upper_slip', 'phase': 'transport'}]
    return data

def execute_job(storage: Any, job: Any) -> list[dict[str, Any]]:
    results = []
    for index in range(job['count']):
        if [file_hash(path) for path in job['paths']] != job['strategy_hashes']:
            raise RuntimeError('frozen strategy source changed')
        a, b = job['paths']
        red, blue = (a, b) if index % 2 == 0 else (b, a)
        output = storage.root / 'runs' / job['id'] / job['lease'] / str(index)
        result = run_match(red, blue, scenario(job['config'], index, job['count']), output)
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

    def __init__(self, root: str | Path) -> None:
        self.storage = Storage(root, recover=True)
        self.token = secrets.token_urlsafe(32)
        self.stop = threading.Event()
        self.thread = None

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
                execute_job(self.storage, job)
            except Exception as exc:
                self.storage.failed(job['id'], job['lease'], repr(exc))

    def replay(self, job_id: str, index: int) -> dict[str, Any]:
        job = self.storage.job(job_id)
        if not job.get('result') or not 0 <= index < len(job['result']):
            raise ValueError('replay not available')
        result = job['result'][index]
        path = Path(result['output_dir']).resolve()
        run_root = (self.storage.root / 'runs').resolve()
        if not path.is_relative_to(run_root):
            raise ValueError('invalid replay path')
        return {'result': result, 'manifest': json.loads((path / 'manifest.json').read_text(encoding='utf-8')), 'snapshots': read_log(path / 'snapshots.jsonl'), 'events': read_log(path / 'events.jsonl'), 'commands': read_log(path / 'commands.jsonl'), 'ledger': json.loads((path / 'score_ledger.json').read_text(encoding='utf-8'))}

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
                if route == '/api/replay':
                    query = parse_qs(parsed.query)
                    self.respond(200, app.replay(query['job'][0], int(query.get('index', ['0'])[0])))
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
                    result = {'id': app.storage.create_series(data['a_version'], data['b_version'], int(data.get('count', 2)), data.get('config'))}
                else:
                    self.respond(404, {'error': 'not found'})
                    return
                self.respond(201, result)
            except (ValueError, KeyError, TypeError, SyntaxError, UnicodeError) as exc:
                self.respond(400, {'error': str(exc)})
            except Exception:
                self.respond(500, {'error': 'platform write failure'})
    return Handler

def make_server(root: str | Path, host: str='127.0.0.1', port: int=8765, trusted_code: bool=False, start_scheduler: bool=True) -> ThreadingHTTPServer:
    if not trusted_code:
        raise ValueError('review strategies first, then explicitly pass --trusted-code')
    if host != 'localhost' and (not ipaddress.ip_address(host).is_loopback):
        raise ValueError('only loopback binding is supported without a verified sandbox')
    app = Application(root)
    server = ThreadingHTTPServer((host, port), handler_for(app))
    server.app = app
    if start_scheduler:
        app.start()
    return server

def serve(root: str | Path='data/l1-reach', host: str='127.0.0.1', port: int=8765, trusted_code: bool=False) -> None:
    server = make_server(root, host, port, trusted_code)
    print('RoboCon local: http://%s:%s — reviewed team code only; process isolation is not a security sandbox' % (host, server.server_port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app.stop.set()
        server.server_close()
