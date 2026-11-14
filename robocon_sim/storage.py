"""SQLite version freezing, team occupancy and idempotent Elo settlement."""
from copy import deepcopy
from typing import Any
import ast
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
from .config import MatchConfig, RATING_TIMING_PROFILE
from .report import series_summary
MAX_UPLOAD = 256 * 1024
INITIAL_ELO = 1000
SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS teams(
    id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, active_version TEXT);
CREATE TABLE IF NOT EXISTS versions(
    id TEXT PRIMARY KEY, team_id TEXT NOT NULL, sha256 TEXT NOT NULL,
    path TEXT NOT NULL, filename TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS series(
    id TEXT PRIMARY KEY, a TEXT NOT NULL, b TEXT NOT NULL,
    a_version TEXT NOT NULL, b_version TEXT NOT NULL, count INTEGER NOT NULL,
    status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, lease TEXT,
    config TEXT NOT NULL, matches TEXT NOT NULL, result TEXT, error TEXT,
    created REAL NOT NULL, ranked INTEGER NOT NULL DEFAULT 0,
    rating_timing_profile TEXT);
CREATE TABLE IF NOT EXISTS occupancy(team_id TEXT PRIMARY KEY, series_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settlements(
    id TEXT PRIMARY KEY, series_id TEXT NOT NULL, pair_index INTEGER NOT NULL,
    a_result REAL NOT NULL, delta REAL NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS attempts(
    id TEXT PRIMARY KEY, series_id TEXT NOT NULL, number INTEGER NOT NULL,
    status TEXT NOT NULL, error TEXT, result TEXT, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS team_ratings(
    team_id TEXT NOT NULL REFERENCES teams(id), timing_profile TEXT NOT NULL,
    elo REAL NOT NULL, PRIMARY KEY (team_id, timing_profile));
CREATE INDEX IF NOT EXISTS idx_series_created ON series(created DESC);
CREATE INDEX IF NOT EXISTS idx_series_status ON series(status);
CREATE INDEX IF NOT EXISTS idx_versions_team ON versions(team_id);
"""
_TEAM_WITH_CURRENT_ELO = """
    SELECT t.id,t.name,COALESCE(r.elo,?) AS elo,t.active_version
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

    def __init__(self, root: str | Path, recover: bool=False) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'strategies').mkdir(exist_ok=True)
        (self.root / 'runs').mkdir(exist_ok=True)
        self._initialize()
        if recover:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute(
                    "UPDATE series SET status=CASE WHEN attempt<2 THEN 'QUEUED' ELSE 'VOID' END,"
                    "lease=NULL,error='scheduler restarted' WHERE status='RUNNING'"
                )
                db.execute("DELETE FROM occupancy WHERE series_id IN (SELECT id FROM series WHERE status='VOID')")
                db.execute("UPDATE attempts SET status='VOID',error='scheduler restarted' WHERE status='RUNNING'")

    def _initialize(self) -> None:
        """Create the current schema, and refuse to touch anything older."""
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

    def connect(self) -> ManagedConnection:
        db = sqlite3.connect(self.root / 'platform.sqlite3', timeout=30, factory=ManagedConnection)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def create_team(self, name: str) -> dict[str, Any]:
        name = str(name).strip()
        if not 1 <= len(name) <= 60:
            raise ValueError('team name must contain 1–60 characters')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            found = db.execute(
                _TEAM_WITH_CURRENT_ELO + ' WHERE t.name=?', (INITIAL_ELO, RATING_TIMING_PROFILE, name)
            ).fetchone()
            if found:
                return dict(found)
            ident = uuid.uuid4().hex
            db.execute('INSERT INTO teams(id,name) VALUES(?,?)', (ident, name))
            return {'id': ident, 'name': name, 'elo': INITIAL_ELO, 'active_version': None}

    def upload(self, team_id: str, filename: str, source: bytes) -> dict[str, Any]:
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
            if not db.execute('SELECT id FROM teams WHERE id=?', (team_id,)).fetchone():
                raise ValueError('unknown team')
            path.write_bytes(source)
            db.execute('INSERT INTO versions VALUES(?,?,?,?,?,?)', (ident, team_id, sha, str(path), filename, time.time()))
            db.execute('UPDATE teams SET active_version=? WHERE id=?', (ident, team_id))
        return {'id': ident, 'team_id': team_id, 'sha256': sha, 'filename': filename, 'check': 'syntax and synchronous entry passed; trusted-code review required'}

    def overview(self) -> dict[str, Any]:
        with self.connect() as db:
            teams = db.execute(
                _TEAM_WITH_CURRENT_ELO + ' ORDER BY elo DESC,t.name', (INITIAL_ELO, RATING_TIMING_PROFILE)
            )
            return {'teams': [dict(r) for r in teams], 'versions': [dict(r) for r in db.execute('SELECT id,team_id,sha256,filename,created FROM versions ORDER BY created DESC')], 'jobs': [self._job(r) for r in db.execute('SELECT * FROM series ORDER BY created DESC LIMIT 100')]}

    def _job(self, row: Any):
        data = dict(row)
        for field in ('config', 'result', 'matches'):
            if data.get(field):
                data[field] = json.loads(data[field])
        if data.get('result'):
            data['summary'] = series_summary(data['result'])
        return data

    def create_series(
        self, a_version: str, b_version: str, count: int = 2,
        config: dict[str, Any] | None = None, *, ranked: bool | None = None,
        matches: list[dict[str, Any]] | None = None,
    ) -> str:
        """Freeze one series; every match config is stored so hot reload cannot leak in.

        `matches` is the expanded per-match list produced from the platform
        configuration. `config` is a convenience override applied to every match
        and is kept so direct callers (CLI training runs, tests) stay short.
        """
        if count not in (1, 2, 10):
            raise ValueError('match count must be 1, 2 or 10')
        defaulted = matches is None
        if defaulted:
            frozen = MatchConfig.from_dict(config or {}).to_dict()
            matches = [deepcopy(frozen) for _ in range(count)]
        else:
            matches = [MatchConfig.from_dict(item).to_dict() for item in matches]
            if len(matches) != count:
                raise ValueError('matches must describe every game in the series')
        if ranked is None:
            ranked = count > 1 and config is None and defaulted
        if ranked and (count == 1 or config is not None):
            raise ValueError('ranked matches require the default 2- or 10-match format')
        ident = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            versions = [db.execute('SELECT * FROM versions WHERE id=?', (v,)).fetchone() for v in (a_version, b_version)]
            if not all(versions):
                raise ValueError('unknown strategy version')
            a, b = [v['team_id'] for v in versions]
            if a == b:
                raise ValueError('choose two different teams')
            if ranked:
                for team, version in ((a, a_version), (b, b_version)):
                    active = db.execute('SELECT active_version FROM teams WHERE id=?', (team,)).fetchone()
                    if active['active_version'] != version:
                        raise ValueError('ranked matches require both active strategy versions')
            for team in sorted([a, b]):
                if db.execute('SELECT * FROM occupancy WHERE team_id=?', (team,)).fetchone():
                    raise ValueError('team already has an active series')
                db.execute('INSERT INTO occupancy VALUES(?,?)', (team, ident))
            db.execute(
                "INSERT INTO series(id,a,b,a_version,b_version,count,status,config,matches,created,ranked,rating_timing_profile) "
                "VALUES(?,?,?,?,?,?,'QUEUED',?,?,?,?,?)",
                (ident, a, b, a_version, b_version, count, json.dumps(matches[0]),
                 json.dumps(matches), time.time(), ranked, RATING_TIMING_PROFILE if ranked else None),
            )
        return ident

    def claim(self, ident: str) -> dict[str, Any] | None:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM series WHERE id=?', (ident,)).fetchone()
            if not row or row['status'] != 'QUEUED':
                return None
            lease = uuid.uuid4().hex
            number = row['attempt'] + 1
            db.execute("UPDATE series SET status='RUNNING',lease=?,attempt=? WHERE id=?", (lease, number, ident))
            db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,?,?)', (lease, ident, number, 'RUNNING', None, None, time.time()))
            job = self._job(db.execute('SELECT * FROM series WHERE id=?', (ident,)).fetchone())
            versions = [
                db.execute('SELECT path,sha256 FROM versions WHERE id=?', (job[key],)).fetchone()
                for key in ('a_version', 'b_version')
            ]
            job['paths'] = [version['path'] for version in versions]
            job['strategy_hashes'] = [version['sha256'] for version in versions]
            return job

    def finish(self, ident: str, lease: str, results: list[dict[str, Any]]) -> bool:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM series WHERE id=?', (ident,)).fetchone()
            if not row or row['status'] != 'RUNNING' or row['lease'] != lease:
                return False
            if len(results) != row['count']:
                raise ValueError('incomplete series cannot settle')
            if any((r.get('status') in ('VOID', 'PLATFORM_FAILURE') for r in results)):
                raise ValueError('platform failure cannot settle')
            if row['ranked']:
                profile = row['rating_timing_profile']
                for pair in range(row['count'] // 2):
                    values = []
                    for offset in (0, 1):
                        winner = results[pair * 2 + offset].get('winner')
                        a_color = 'red' if offset == 0 else 'blue'
                        values.append(0.5 if winner is None else float(winner == a_color))
                    actual = sum(values) / 2
                    ratings = [self._rating(db, team, profile) for team in (row['a'], row['b'])]
                    expected = 1 / (1 + 10 ** ((ratings[1] - ratings[0]) / 400))
                    delta = 24 * (actual - expected)
                    db.execute('INSERT INTO settlements VALUES(?,?,?,?,?,?)', (ident + ':' + str(pair), ident, pair, actual, delta, time.time()))
                    for team, change in ((row['a'], delta), (row['b'], -delta)):
                        self._update_rating(db, team, profile, change)
            encoded = json.dumps(results)
            db.execute("UPDATE series SET status='COMPLETED',result=?,lease=NULL WHERE id=?", (encoded, ident))
            db.execute("UPDATE attempts SET status='COMPLETED',result=? WHERE id=?", (encoded, lease))
            db.execute('DELETE FROM occupancy WHERE series_id=?', (ident,))
            return True

    def _rating(self, db: ManagedConnection, team: str, profile: str) -> float:
        row = db.execute(
            'SELECT elo FROM team_ratings WHERE team_id=? AND timing_profile=?', (team, profile)
        ).fetchone()
        return row['elo'] if row is not None else INITIAL_ELO

    def _update_rating(self, db: ManagedConnection, team: str, profile: str, change: float) -> None:
        db.execute(
            'INSERT INTO team_ratings(team_id,timing_profile,elo) VALUES(?,?,?) '
            'ON CONFLICT(team_id,timing_profile) DO UPDATE SET elo=team_ratings.elo+?',
            (team, profile, INITIAL_ELO + change, change),
        )

    def failed(self, ident: str, lease: str, error: Any, max_attempts: int=2) -> bool:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM series WHERE id=?', (ident,)).fetchone()
            if not row or row['status'] != 'RUNNING' or row['lease'] != lease:
                return False
            status = 'QUEUED' if row['attempt'] < max_attempts else 'VOID'
            db.execute('UPDATE series SET status=?,lease=NULL,error=? WHERE id=?', (status, str(error), ident))
            db.execute("UPDATE attempts SET status='VOID',error=? WHERE id=?", (str(error), lease))
            if status == 'VOID':
                db.execute('DELETE FROM occupancy WHERE series_id=?', (ident,))
            return True

    def job(self, ident: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute('SELECT * FROM series WHERE id=?', (ident,)).fetchone()
            if not row:
                raise ValueError('unknown job')
            return self._job(row)
