"""SQLite: accounts, team ownership, batch scheduling and idempotent Elo settlement."""
from copy import deepcopy
from typing import Any
import ast
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from .auth import hash_password, new_token, session_expiry, verify_password
from .config import MatchConfig, RATING_TIMING_PROFILE
from .report import series_summary

MAX_UPLOAD = 256 * 1024
INITIAL_ELO = 1000
SCHEMA_VERSION = 4
BATCH_KINDS = ('manual', 'auto_round', 'all_play', 'official')
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, salt TEXT NOT NULL,
    password_hash TEXT NOT NULL, role TEXT NOT NULL, team_id TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
    token TEXT PRIMARY KEY, user_id TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS teams(
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS versions(
    id TEXT PRIMARY KEY, team_id TEXT NOT NULL, sha256 TEXT NOT NULL, path TEXT NOT NULL,
    filename TEXT NOT NULL, created REAL NOT NULL, kind TEXT NOT NULL DEFAULT 'draft',
    uploaded_by TEXT, retired REAL);
CREATE TABLE IF NOT EXISTS batches(
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, team_id TEXT NOT NULL, requested_by TEXT,
    status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, lease TEXT,
    config TEXT, result TEXT, error TEXT, created REAL NOT NULL, started REAL, finished REAL);
CREATE TABLE IF NOT EXISTS series(
    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, position INTEGER NOT NULL,
    a TEXT NOT NULL, b TEXT NOT NULL, a_version TEXT NOT NULL, b_version TEXT NOT NULL,
    count INTEGER NOT NULL, status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, lease TEXT,
    config TEXT NOT NULL, matches TEXT NOT NULL, result TEXT, error TEXT, created REAL NOT NULL,
    ranked INTEGER NOT NULL DEFAULT 0, rating_timing_profile TEXT,
    red_profile TEXT, blue_profile TEXT);
CREATE TABLE IF NOT EXISTS occupancy(team_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settlements(
    id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, series_id TEXT NOT NULL, pair_index INTEGER NOT NULL,
    a_result REAL NOT NULL, delta REAL NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS attempts(
    id TEXT PRIMARY KEY, series_id TEXT NOT NULL, number INTEGER NOT NULL,
    status TEXT NOT NULL, error TEXT, result TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS team_ratings(
    team_id TEXT NOT NULL REFERENCES teams(id), timing_profile TEXT NOT NULL,
    elo REAL NOT NULL, PRIMARY KEY (team_id, timing_profile));
CREATE INDEX IF NOT EXISTS idx_batches_created ON batches(created DESC);
CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);
CREATE INDEX IF NOT EXISTS idx_series_batch ON series(batch_id, position);
CREATE INDEX IF NOT EXISTS idx_versions_team ON versions(team_id, created DESC);
"""
_TEAM_WITH_CURRENT_ELO = """
    SELECT t.id, t.name, COALESCE(r.elo, ?) AS elo,
           (SELECT id FROM versions v WHERE v.team_id=t.id AND v.kind='official' ORDER BY v.created DESC, v.rowid DESC LIMIT 1) AS official_version
    FROM teams AS t
    LEFT JOIN team_ratings AS r ON t.id=r.team_id AND r.timing_profile=?
"""


class ManagedConnection(sqlite3.Connection):

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class Storage:

    def __init__(self, root: str | Path, recover: bool = False) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'strategies').mkdir(exist_ok=True)
        (self.root / 'runs').mkdir(exist_ok=True)
        self._initialize()
        if recover:
            self._recover()

    # -- plumbing ----------------------------------------------------------

    def connect(self) -> ManagedConnection:
        db = sqlite3.connect(self.root / 'platform.sqlite3', timeout=30, factory=ManagedConnection)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def _initialize(self) -> None:
        with self.connect() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            tables = db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
            if version == 0 and tables:
                raise ValueError(
                    'incompatible database (unversioned schema); delete the data directory to start over')
            if version not in (0, SCHEMA_VERSION):
                raise ValueError(
                    f'incompatible database (schema {version}, expected {SCHEMA_VERSION}); '
                    'delete the data directory to start over')
            if version == 0:
                db.executescript(SCHEMA)
                db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                db.commit()
            elif version == 3:
                # A team may hold several official entries: being official is a
                # one-way certification, not a single lineup slot.
                db.execute('DROP INDEX IF EXISTS idx_one_official')
                db.execute('PRAGMA user_version=4')
                db.commit()

    def _recover(self) -> None:
        """A restart cannot resume a half-run batch; requeue or void it."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE batches SET status='VOID', lease=NULL, error='scheduler restarted' "
                       "WHERE status='RUNNING' AND attempt>=2")
            db.execute("UPDATE batches SET status='QUEUED', lease=NULL, error='scheduler restarted' "
                       "WHERE status='RUNNING'")
            db.execute("UPDATE series SET status='VOID', error='scheduler restarted' "
                       "WHERE status='RUNNING'")
            db.execute("UPDATE attempts SET status='VOID', error='scheduler restarted' WHERE status='RUNNING'")
            db.execute("DELETE FROM occupancy WHERE batch_id IN (SELECT id FROM batches WHERE status='VOID')")

    # -- accounts ----------------------------------------------------------

    def create_team(self, name: str) -> dict[str, Any]:
        name = str(name).strip()
        if not 1 <= len(name) <= 60:
            raise ValueError('team name must contain 1–60 characters')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            found = db.execute('SELECT id, name, created FROM teams WHERE name=?', (name,)).fetchone()
            if found:
                return dict(found)
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO teams(id,name,created) VALUES(?,?,?)', (ident, name, time.time()))
            return {'id': ident, 'name': name, 'created': time.time()}

    def teams(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,name,created FROM teams ORDER BY name')]

    def create_user(self, name: str, password: str, role: str = 'member',
                    team_id: str | None = None) -> dict[str, Any]:
        name = str(name).strip()
        if not 1 <= len(name) <= 40:
            raise ValueError('user name must contain 1–40 characters')
        if role not in ('admin', 'member'):
            raise ValueError('role must be admin or member')
        if role == 'member' and not team_id:
            raise ValueError('a member must belong to a team')
        salt, digest = hash_password(password)
        ident = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM users WHERE name=?', (name,)).fetchone():
                raise ValueError('user name already taken')
            if team_id and not db.execute('SELECT 1 FROM teams WHERE id=?', (team_id,)).fetchone():
                raise ValueError('unknown team')
            db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?)',
                       (ident, name, salt, digest, role, team_id, time.time()))
        return {'id': ident, 'name': name, 'role': role, 'team_id': team_id}

    def ensure_admin(self, password: str | None = None) -> dict[str, Any] | None:
        """Create the first administrator and return its credentials once."""
        with self.connect() as db:
            if db.execute('SELECT COUNT(*) FROM users').fetchone()[0]:
                return None
        from .auth import new_password
        secret = password or new_password(14)
        created = self.create_user('admin', secret, 'admin', None)
        created['password'] = secret
        return created

    def set_password(self, user_id: str, password: str) -> None:
        salt, digest = hash_password(password)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('UPDATE users SET salt=?, password_hash=? WHERE id=?',
                              (salt, digest, user_id)).rowcount:
                raise ValueError('unknown user')
            db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))

    def change_password(self, user_id: str, current: str, password: str) -> None:
        """Self-service change: the current password still has to be proven."""
        with self.connect() as db:
            row = db.execute('SELECT salt, password_hash FROM users WHERE id=?', (user_id,)).fetchone()
        if row is None or not verify_password(current, row['salt'], row['password_hash']):
            raise ValueError('current password is incorrect')
        self.set_password(user_id, password)

    def users(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                'SELECT u.id,u.name,u.role,u.team_id,t.name AS team_name,u.created '
                'FROM users u LEFT JOIN teams t ON t.id=u.team_id ORDER BY u.created'
            )
            return [dict(r) for r in rows]

    def login(self, name: str, password: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute('SELECT * FROM users WHERE name=?', (str(name).strip(),)).fetchone()
        if row is None or not verify_password(password, row['salt'], row['password_hash']):
            raise ValueError('invalid credentials')
        token = new_token()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',
                       (token, row['id'], time.time(), session_expiry()))
        return {'token': token, 'user': self.session(token)}

    def session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self.connect() as db:
            row = db.execute(
                'SELECT u.*, t.name AS team_name FROM sessions s '
                'JOIN users u ON u.id=s.user_id LEFT JOIN teams t ON t.id=u.team_id '
                'WHERE s.token=? AND s.expires>?', (token, time.time())
            ).fetchone()
        return self._public_user(dict(row)) if row else None

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM sessions WHERE token=?', (token,))

    @staticmethod
    def _public_user(row: dict[str, Any]) -> dict[str, Any]:
        return {'id': row['id'], 'name': row['name'], 'role': row['role'],
                'team_id': row.get('team_id'), 'team_name': row.get('team_name')}

    # -- strategies --------------------------------------------------------

    def upload(self, team_id: str, filename: str, source: bytes,
               uploaded_by: str | None = None) -> dict[str, Any]:
        if Path(filename).name != filename or not filename.lower().endswith('.py'):
            raise ValueError('upload one .py file with a plain filename')
        if not source or len(source) > MAX_UPLOAD:
            raise ValueError('strategy must be 1–262144 bytes')
        text = source.decode('utf-8-sig')
        tree = ast.parse(text, filename=filename)
        candidates = [node for node in tree.body if isinstance(node, ast.ClassDef) and any((isinstance(b, ast.Name) and b.id == 'Strategy' or (isinstance(b, ast.Attribute) and b.attr == 'Strategy') for b in node.bases))]
        if len(candidates) != 1:
            raise ValueError('define exactly one direct Strategy subclass')
        callbacks = {node.name: node for node in candidates[0].body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if 'on_step' not in callbacks or isinstance(callbacks['on_step'], ast.AsyncFunctionDef):
            raise ValueError('synchronous on_step(self, obs, api) required')
        if len(callbacks['on_step'].args.args) != 3:
            raise ValueError('on_step must accept self, obs, api')
        ident = uuid.uuid4().hex
        sha = hashlib.sha256(source).hexdigest()
        path = self.root / 'strategies' / (ident + '.py')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT id FROM teams WHERE id=?', (team_id,)).fetchone():
                raise ValueError('unknown team')
            path.write_bytes(source)
            db.execute('INSERT INTO versions(id,team_id,sha256,path,filename,created,kind,uploaded_by) '
                       "VALUES(?,?,?,?,?,?,'draft',?)",
                       (ident, team_id, sha, str(path), filename, time.time(), uploaded_by))
        return {'id': ident, 'team_id': team_id, 'sha256': sha, 'filename': filename, 'kind': 'draft',
                'check': 'syntax and synchronous entry passed; trusted-code review required'}

    def versions(self, team_id: str | None = None) -> list[dict[str, Any]]:
        query = ('SELECT v.id,v.team_id,t.name AS team_name,v.sha256,v.filename,v.created,'
                 'v.kind,v.uploaded_by,v.retired FROM versions v JOIN teams t ON t.id=v.team_id')
        with self.connect() as db:
            if team_id:
                rows = db.execute(query + ' WHERE v.team_id=? ORDER BY v.created DESC', (team_id,))
            else:
                rows = db.execute(query + ' ORDER BY v.created DESC LIMIT 200')
            return [dict(r) for r in rows]

    def version_source(self, version_id: str) -> tuple[str, bytes, dict[str, Any]]:
        with self.connect() as db:
            row = db.execute('SELECT * FROM versions WHERE id=?', (version_id,)).fetchone()
        if row is None:
            raise ValueError('unknown strategy version')
        path = Path(row['path'])
        if not path.is_file():
            raise ValueError('strategy source is missing on disk')
        return row['filename'], path.read_bytes(), dict(row)

    def promote(self, version_id: str) -> dict[str, Any]:
        """Certify a version as official. One-way: official entries never revert.

        A team may hold many official versions over its lifetime; the newest one is
        the entry that future round robins are scheduled against.
        """
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM versions WHERE id=?', (version_id,)).fetchone()
            if row is None:
                raise ValueError('unknown strategy version')
            if row['kind'] != 'official':
                db.execute("UPDATE versions SET kind='official', retired=NULL WHERE id=?", (version_id,))
            return dict(db.execute('SELECT * FROM versions WHERE id=?', (version_id,)).fetchone())

    def official_version(self, team_id: str) -> str | None:
        """The team's current entry: the most recently certified official version."""
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM versions WHERE team_id=? AND kind='official' "
                "ORDER BY created DESC, rowid DESC LIMIT 1", (team_id,)).fetchone()
        return row['id'] if row else None

    # -- batches -----------------------------------------------------------

    def create_batch(self, kind: str, team_id: str, requested_by: str | None,
                     series: list[dict[str, Any]], config: dict[str, Any] | None = None) -> str:
        """Queue one batch of series. `team_id` is the team that asked for it.

        All series share one occupancy: a team cannot be scheduled twice at once,
        so the checker is per batch instead of per series.
        """
        if kind not in BATCH_KINDS:
            raise ValueError('unknown batch kind')
        if not series:
            raise ValueError('a batch needs at least one series')
        batch_id = uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            involved: set[str] = set()
            prepared = []
            for position, spec in enumerate(series):
                versions = [db.execute('SELECT * FROM versions WHERE id=?', (spec[key],)).fetchone()
                            for key in ('a_version', 'b_version')]
                if not all(versions):
                    raise ValueError('unknown strategy version')
                a, b = [v['team_id'] for v in versions]
                if a == b:
                    raise ValueError('choose two different teams')
                count = int(spec['count'])
                if count not in (1, 2, 10):
                    raise ValueError('match count must be 1, 2 or 10')
                matches = [MatchConfig.from_dict(item).to_dict() for item in spec['matches']]
                if len(matches) != count:
                    raise ValueError('matches must describe every game in the series')
                involved.update((a, b))
                prepared.append((uuid.uuid4().hex, position, a, b,
                                 spec['a_version'], spec['b_version'], count, matches,
                                 bool(spec.get('ranked')), spec.get('rating_profile'),
                                 spec.get('red_profile'), spec.get('blue_profile')))
            for team in sorted(involved):
                holder = db.execute('SELECT batch_id FROM occupancy WHERE team_id=?', (team,)).fetchone()
                if holder:
                    found = db.execute('SELECT name FROM teams WHERE id=?', (team,)).fetchone()
                    raise ValueError(f"队伍「{found['name'] if found else team}」还有未完成的对局，"
                                     "等它跑完再创建")
                db.execute('INSERT INTO occupancy VALUES(?,?)', (team, batch_id))
            db.execute('INSERT INTO batches(id,kind,team_id,requested_by,status,config,created) '
                       "VALUES(?,?,?,?,'QUEUED',?,?)",
                       (batch_id, kind, team_id, requested_by, json.dumps(config or {}), now))
            for (ident, position, a, b, a_version, b_version, count, matches,
                 ranked, rating, red_profile, blue_profile) in prepared:
                db.execute(
                    'INSERT INTO series(id,batch_id,position,a,b,a_version,b_version,count,status,'
                    'config,matches,created,ranked,rating_timing_profile,red_profile,blue_profile) '
                    "VALUES(?,?,?,?,?,?,?,?,'QUEUED',?,?,?,?,?,?,?)",
                    (ident, batch_id, position, a, b, a_version, b_version, count,
                     json.dumps(matches[0]), json.dumps(matches), now, int(ranked),
                     (rating or RATING_TIMING_PROFILE) if ranked else None,
                     red_profile, blue_profile))
        return batch_id

    def create_series(self, a_version: str, b_version: str, count: int = 2,
                      config: dict[str, Any] | None = None, *, ranked: bool | None = None,
                      matches: list[dict[str, Any]] | None = None,
                      red_profile: str | None = None, blue_profile: str | None = None,
                      rating_profile: str | None = None) -> str:
        """Single-series convenience wrapper used by direct callers (CLI, tests)."""
        if matches is None:
            frozen = MatchConfig.from_dict(config or {}).to_dict()
            matches = [deepcopy(frozen) for _ in range(count)]
        if ranked is None:
            ranked = count > 1 and config is None
        with self.connect() as db:
            row = db.execute('SELECT team_id FROM versions WHERE id=?', (a_version,)).fetchone()
        batch = self.create_batch('official' if ranked else 'manual', row['team_id'] if row else '',
                                  None,
                                  [{'a_version': a_version, 'b_version': b_version, 'count': count,
                                    'matches': matches, 'ranked': ranked, 'rating_profile': rating_profile,
                                    'red_profile': red_profile, 'blue_profile': blue_profile}])
        with self.connect() as db:
            return db.execute('SELECT id FROM series WHERE batch_id=?', (batch,)).fetchone()['id']

    def claim_batch(self) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM batches WHERE status='QUEUED' ORDER BY created LIMIT 1").fetchone()
            if row is None:
                return None
            lease = uuid.uuid4().hex
            db.execute("UPDATE batches SET status='RUNNING', lease=?, attempt=attempt+1, started=? WHERE id=?",
                       (lease, time.time(), row['id']))
            batch = dict(db.execute('SELECT * FROM batches WHERE id=?', (row['id'],)).fetchone())
            batch['config'] = json.loads(batch['config'] or '{}')
            batch['series'] = [self._series(db, r) for r in
                               db.execute('SELECT * FROM series WHERE batch_id=? ORDER BY position',
                                          (batch['id'],))]
            return batch

    def _series(self, db: ManagedConnection, row: Any) -> dict[str, Any]:
        data = dict(row)
        for field in ('config', 'result', 'matches'):
            if data.get(field):
                data[field] = json.loads(data[field])
        if data.get('result'):
            data['summary'] = series_summary(data['result'])
        versions = [db.execute('SELECT path,sha256 FROM versions WHERE id=?', (data[key],)).fetchone()
                    for key in ('a_version', 'b_version')]
        data['paths'] = [v['path'] for v in versions if v]
        data['strategy_hashes'] = [v['sha256'] for v in versions if v]
        return data

    def series(self, series_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute('SELECT * FROM series WHERE id=?', (series_id,)).fetchone()
            if row is None:
                raise ValueError('unknown series')
            return self._series(db, row)

    def finish_series(self, batch_id: str, lease: str, series_id: str,
                      results: list[dict[str, Any]]) -> bool:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch = db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if batch is None or batch['lease'] != lease:
                return False
            row = db.execute('SELECT * FROM series WHERE id=?', (series_id,)).fetchone()
            if row is None or row['status'] not in ('QUEUED', 'RUNNING'):
                return False
            if len(results) != row['count']:
                raise ValueError('incomplete series cannot settle')
            if any(r.get('status') in ('VOID', 'PLATFORM_FAILURE') for r in results):
                raise ValueError('platform failure cannot settle')
            db.execute("UPDATE series SET status='RUNNING', lease=? WHERE id=?", (lease, series_id))
            if row['ranked']:
                self._settle(db, batch_id, row, results)
            encoded = json.dumps(results, ensure_ascii=False)
            db.execute("UPDATE series SET status='COMPLETED', result=?, lease=NULL WHERE id=?",
                       (encoded, series_id))
            return True

    def _settle(self, db: ManagedConnection, batch_id: str, row: Any,
                results: list[dict[str, Any]]) -> None:
        profile = row['rating_timing_profile'] or RATING_TIMING_PROFILE
        for pair in range(row['count'] // 2):
            values = []
            for offset in (0, 1):
                winner = results[pair * 2 + offset].get('winner')
                a_color = 'red' if offset == 0 else 'blue'
                values.append(0.5 if winner is None else float(winner == a_color))
            actual = sum(values) / 2
            ratings = [self._rating(db, team, profile) for team in (row['a'], row['b'])]
            expected = 1 / (1 + 10 ** ((ratings[1] - ratings[0]) / 400))
            delta = round(24 * (actual - expected), 6)
            db.execute('INSERT INTO settlements VALUES(?,?,?,?,?,?,?)',
                       (f'{row["id"]}:{pair}', batch_id, row['id'], pair, actual, delta, time.time()))
            for team, change in ((row['a'], delta), (row['b'], -delta)):
                self._update_rating(db, team, profile, change)

    def fail_series(self, batch_id: str, lease: str, series_id: str, error: Any,
                    max_attempts: int = 2) -> bool:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch = db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if batch is None or batch['lease'] != lease:
                return False
            row = db.execute('SELECT * FROM series WHERE id=?', (series_id,)).fetchone()
            if row is None or row['status'] not in ('QUEUED', 'RUNNING'):
                return False
            retry = row['attempt'] < max_attempts
            db.execute('UPDATE series SET status=?, error=?, lease=NULL, attempt=attempt+1 WHERE id=?',
                       ('QUEUED' if retry else 'VOID', str(error), series_id))
            db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?)',
                       (uuid.uuid4().hex, series_id, row['attempt'] + 1,
                        'RETRY' if retry else 'VOID', str(error), None, time.time()))
            return True

    def finish_batch(self, batch_id: str, lease: str, status: str,
                     result: dict[str, Any] | None = None) -> bool:
        if status not in ('COMPLETED', 'PARTIAL', 'VOID'):
            raise ValueError('unknown batch status')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if row is None or row['lease'] != lease:
                return False
            db.execute('UPDATE batches SET status=?, lease=NULL, result=?, finished=? WHERE id=?',
                       (status, json.dumps(result or {}, ensure_ascii=False), time.time(), batch_id))
            db.execute("UPDATE series SET status='VOID' WHERE batch_id=? AND status='QUEUED'", (batch_id,))
            db.execute('DELETE FROM occupancy WHERE batch_id=?', (batch_id,))
            return True

    def batch(self, batch_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if row is None:
                raise ValueError('unknown batch')
            data = dict(row)
            for field in ('config', 'result'):
                if data.get(field):
                    data[field] = json.loads(data[field])
            rows = [self._series(db, r) for r in
                    db.execute('SELECT * FROM series WHERE batch_id=? ORDER BY position', (batch_id,))]
            data['series'] = rows
            settled = {row['series_id']: row['delta'] for row in db.execute(
                'SELECT series_id, SUM(delta) AS delta FROM settlements WHERE series_id IN '
                '(SELECT id FROM series WHERE batch_id=?) GROUP BY series_id', (batch_id,))}
            data['elo_delta'] = self._delta_for([dict(r) for r in rows], settled, data['team_id'])
            return data

    def _series_brief(self, row: Any) -> dict[str, Any]:
        """What the queue needs to draw one series, without the version lookups."""
        data = dict(row)
        if data.get('result'):
            data['result'] = json.loads(data['result'])
        return {key: data.get(key) for key in
                ('id', 'batch_id', 'position', 'a', 'b', 'count', 'status', 'ranked',
                 'result', 'red_profile', 'blue_profile')}

    def batches(self, limit: int = 50, team_id: str | None = None) -> list[dict[str, Any]]:
        """Recent batches. `elo_delta` is signed for `team_id` (the viewer)."""
        with self.connect() as db:
            rows = db.execute('SELECT * FROM batches ORDER BY created DESC LIMIT ?', (limit,)).fetchall()
            if not rows:
                return []
            ids = [row['id'] for row in rows]
            marks = ','.join('?' * len(ids))
            series: dict[str, list[dict[str, Any]]] = {}
            for row in db.execute(
                    f'SELECT * FROM series WHERE batch_id IN ({marks}) ORDER BY position', ids):
                series.setdefault(row['batch_id'], []).append(self._series_brief(row))
            settled = {row['series_id']: row['delta'] for row in db.execute(
                f'SELECT series_id, SUM(delta) AS delta FROM settlements WHERE series_id IN '
                f'(SELECT id FROM series WHERE batch_id IN ({marks})) GROUP BY series_id', ids)}
            out = []
            for row in rows:
                data = dict(row)
                for field in ('config', 'result'):
                    if data.get(field):
                        data[field] = json.loads(data[field])
                rows_series = series.get(row['id'], [])
                data['series'] = rows_series
                data['progress'] = {
                    'total': len(rows_series),
                    'done': sum(1 for s in rows_series if s['status'] == 'COMPLETED'),
                    'failed': sum(1 for s in rows_series if s['status'] == 'VOID'),
                }
                data['elo_delta'] = self._delta_for(rows_series, settled,
                                                    team_id or row['team_id'])
                out.append(data)
            return out

    @staticmethod
    def _delta_for(rows_series: list[dict[str, Any]], settled: dict[str, float],
                   team_id: str | None) -> float:
        total = 0.0
        for series in rows_series:
            change = settled.get(series['id'])
            if not change:
                continue
            if series['a'] == team_id:
                total += change
            elif series['b'] == team_id:
                total -= change
        return round(total, 4)

    # -- ratings -----------------------------------------------------------

    def _rating(self, db: ManagedConnection, team: str, profile: str) -> float:
        row = db.execute('SELECT elo FROM team_ratings WHERE team_id=? AND timing_profile=?',
                         (team, profile)).fetchone()
        return row['elo'] if row is not None else INITIAL_ELO

    def _update_rating(self, db: ManagedConnection, team: str, profile: str, change: float) -> None:
        db.execute(
            'INSERT INTO team_ratings(team_id,timing_profile,elo) VALUES(?,?,?) '
            'ON CONFLICT(team_id,timing_profile) DO UPDATE SET elo=team_ratings.elo+?',
            (team, profile, INITIAL_ELO + change, change),
        )

    # -- views -------------------------------------------------------------

    def rating_profile(self) -> str:
        """The pool the table is shown for: the one with the most settled pairs.

        Ratings are keyed by profile digest, and the key changes whenever the
        physical parameters change, so the ranking must not assume a fixed name.
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT rating_timing_profile AS pool, COUNT(*) AS pairs FROM series "
                "WHERE ranked=1 AND rating_timing_profile IS NOT NULL "
                "GROUP BY pool ORDER BY pairs DESC, pool LIMIT 1").fetchone()
        return row['pool'] if row else RATING_TIMING_PROFILE

    def ranking(self) -> list[dict[str, Any]]:
        pool = self.rating_profile()
        with self.connect() as db:
            rows = db.execute(_TEAM_WITH_CURRENT_ELO + ' ORDER BY elo DESC, t.name',
                              (INITIAL_ELO, pool))
            out = []
            for row in rows:
                data = dict(row)
                record = db.execute(
                    'SELECT COUNT(*) AS pairs, '
                    'SUM(CASE WHEN (x.a=:t AND s.a_result>0.5) OR (x.b=:t AND s.a_result<0.5) '
                    'THEN 1 ELSE 0 END) AS wins, '
                    'SUM(CASE WHEN s.a_result=0.5 THEN 1 ELSE 0 END) AS draws '
                    'FROM settlements s JOIN series x ON x.id=s.series_id WHERE x.a=:t OR x.b=:t',
                    {'t': data['id']}).fetchone()
                data['pairs'] = record['pairs'] or 0
                data['wins'] = record['wins'] or 0
                data['draws'] = record['draws'] or 0
                data['losses'] = max(0, data['pairs'] - data['wins'] - data['draws'])
                data['rating_profile'] = pool
                out.append(data)
            return out

    def overview(self) -> dict[str, Any]:
        return {'teams': self.ranking(), 'versions': self.versions(), 'batches': self.batches()}
