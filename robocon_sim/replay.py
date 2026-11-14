"""Replay only recorded decisions; Python strategy code is never imported."""
from typing import Any
import hashlib
import json
from pathlib import Path
from .runner import digest, file_hash

def read_log(path: str | Path) -> list[dict[str, Any]]:
    rows = []
    for index, line in enumerate(Path(path).read_text(encoding='utf-8').splitlines()):
        row = json.loads(line)
        if row['log_seq'] != index or digest(row['payload']) != row['sha256']:
            raise ValueError('corrupt log: ' + str(path))
        rows.append(row['payload'])
    return rows

def verify_replay(directory: str | Path) -> dict[str, Any]:
    from .engine import Engine
    root = Path(directory)
    integrity = json.loads((root / 'integrity.json').read_text(encoding='utf-8'))
    for name, expected in integrity.items():
        path = root / name
        if path.parent != root or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('bundle integrity failed: ' + name)
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    source_root = Path(__file__).resolve().parent.parent
    for name, expected in manifest.get('implementation_hashes', {}).items():
        path = (source_root / name).resolve()
        if not path.is_relative_to(source_root) or file_hash(path) != expected:
            raise ValueError('implementation version mismatch: ' + name)
    engine = Engine(manifest['config'], map_data=manifest.get('map'))
    checkpoints = read_log(root / 'snapshots.jsonl')
    frames = read_log(root / 'commands.jsonl')
    if digest(engine.public_state()) != checkpoints[0]['hash']:
        raise ValueError('initial state mismatch')
    cursor = 1
    for frame in frames:
        if engine.seq != frame['seq']:
            raise ValueError('command sequence mismatch')
        if frame.get('failures'):
            engine.fail_policies(frame['failures'])
        else:
            engine.step(frame['decisions'])
        if cursor < len(checkpoints):
            if digest(engine.public_state()) != checkpoints[cursor]['hash']:
                raise ValueError('state mismatch at seq ' + str(engine.seq))
            cursor += 1
    result = json.loads((root / 'result.json').read_text(encoding='utf-8'))
    if not engine.done or digest(engine.public_state()) != result['final_hash']:
        raise ValueError('final state mismatch')
    if engine.events != read_log(root / 'events.jsonl'):
        raise ValueError('event mismatch')
    return {'verified': True, 'frames': len(frames), 'final_hash': result['final_hash'], 'scores': engine.result()['scores']}
