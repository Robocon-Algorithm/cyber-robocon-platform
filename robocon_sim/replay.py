"""Replay only recorded decisions; Python strategy code is never imported."""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from .frame_codec import FrameReader
from .runner import BUNDLE_FORMAT, digest, file_crc32, file_hash, frame_state

COMMAND_LOGS = ('commands.jsonl.gz', 'commands.jsonl')
RECEIPT_LOGS = ('receipts.jsonl.gz',)
EVENT_LOGS = ('events.jsonl',)
DIAGNOSTIC_LOGS = ('diagnostics.jsonl',)
TEAMS = ('red', 'blue')


def read_log(path: str | Path) -> list[dict[str, Any]]:
    """Read one JSONL side channel, plain or gzipped."""
    path = Path(path)
    opener = gzip.open if path.suffix == '.gz' else open
    rows = []
    with opener(path, 'rt', encoding='utf-8') as stream:
        for index, line in enumerate(stream):
            if not line.strip():
                raise ValueError('corrupt log: ' + str(path))
            row = json.loads(line)
            if row.get('log_seq') != index:
                raise ValueError('corrupt log: ' + str(path))
            rows.append(row['payload'])
    return rows


def log_path(root: str | Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        path = Path(root) / name
        if path.is_file():
            return path
    return None


def read_named(root: str | Path, names: tuple[str, ...]) -> list[dict[str, Any]]:
    path = log_path(root, names)
    return read_log(path) if path else []


def event_feed(root: str | Path) -> list[dict[str, Any]]:
    """World events merged with policy receipts and policy errors, time ordered."""
    events = list(read_named(root, EVENT_LOGS))
    known = {event.get('id') for event in events if 'id' in event}

    def add_receipt(receipt: dict[str, Any], team: str) -> None:
        if receipt.get('id') in known:
            return
        known.add(receipt.get('id'))
        events.append({**receipt, 'type': 'RECEIPT ' + str(receipt.get('code', '')), 'team': team})

    for row in read_named(root, RECEIPT_LOGS):
        for team in TEAMS:
            for receipt in row.get(team) or []:
                add_receipt(receipt, team)
    for command in read_named(root, COMMAND_LOGS):
        # Bundles written before receipts moved to their own log still carry them.
        for team in TEAMS:
            for receipt in (command.get('receipts') or {}).get(team) or []:
                add_receipt(receipt, team)
            response = (command.get('responses') or {}).get(team)
            if response and not response.get('ok'):
                events.append({'type': 'POLICY_ERROR', 'occurred_at_ms': command.get('sim_time_ms', 0),
                               'team': team, 'reason': response.get('error')})
    for row in read_named(root, DIAGNOSTIC_LOGS):
        if row.get('phase') == 'step' and row.get('error'):
            events.append({'type': 'POLICY_ERROR', 'occurred_at_ms': row.get('sim_time_ms', 0),
                           'team': row.get('team'), 'reason': row['error']})
    events.sort(key=lambda event: event.get('occurred_at_ms') or 0)
    return events


def verify_replay(directory: str | Path) -> dict[str, Any]:
    from .engine import Engine
    root = Path(directory)
    integrity = json.loads((root / 'integrity.json').read_text(encoding='utf-8'))
    if integrity.get('format') != BUNDLE_FORMAT:
        raise ValueError('unsupported bundle format: ' + str(integrity.get('format')))
    for name, expected in integrity.get('files', {}).items():
        path = root / name
        if path.parent != root or not path.is_file():
            raise ValueError('bundle integrity failed: ' + name)
        size, crc = file_crc32(path)
        if size != expected.get('size') or f'{crc:08x}' != expected.get('crc32'):
            raise ValueError('bundle integrity failed: ' + name)
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('bundle_format') != BUNDLE_FORMAT:
        raise ValueError('unsupported bundle format: ' + str(manifest.get('bundle_format')))
    source_root = Path(__file__).resolve().parent.parent
    for name, expected in manifest.get('implementation_hashes', {}).items():
        path = (source_root / name).resolve()
        if not path.is_relative_to(source_root) or file_hash(path) != expected:
            raise ValueError('implementation version mismatch: ' + name)
    reader = FrameReader(root / 'snapshots.rcf')
    reader.verify()
    commands = read_named(root, COMMAND_LOGS)
    if reader.frame_count != len(commands) + 1:
        raise ValueError('snapshot count does not match the command count')
    engine = Engine(manifest['config'], map_data=reader.static['map'])
    result = json.loads((root / 'result.json').read_text(encoding='utf-8'))
    snapshot = engine.public_state()
    if reader.state(0, copy_state=False) != frame_state(snapshot):
        raise ValueError('initial state mismatch')
    observations = engine.observations(snapshot)
    cursor = 1
    for frame in commands:
        if engine.seq != frame['seq']:
            raise ValueError('command sequence mismatch')
        if frame.get('failures'):
            engine.fail_policies(frame['failures'])
            snapshot = engine.public_state()
        else:
            snapshot = engine.step(frame['decisions'], observations)
        observations = engine.observations(snapshot)
        if cursor < reader.frame_count:
            if reader.state(cursor, copy_state=False) != frame_state(snapshot):
                raise ValueError('state mismatch at seq ' + str(engine.seq))
            cursor += 1
    final_hash = digest(frame_state(snapshot))
    if not engine.done or final_hash != result['final_hash']:
        raise ValueError('final state mismatch')
    if reader.final_hash != result['final_hash']:
        raise ValueError('frame stream final hash mismatch')
    if engine.events != read_named(root, EVENT_LOGS):
        raise ValueError('event mismatch')
    return {'verified': True, 'frames': len(commands), 'final_hash': final_hash,
            'scores': engine.result()['scores']}
