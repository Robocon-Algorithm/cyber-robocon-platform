"""Two persistent policies, shared simulation boundaries and auditable match bundles."""
from __future__ import annotations
from typing import Any
import concurrent.futures
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import subprocess
import sys
import threading
import time
import uuid
import zlib

from .frame_codec import STATIC_KEYS, FrameWriter
from .report import MatchMetrics
MAX_MESSAGE = 2 * 1024 * 1024
BUNDLE_FORMAT = 'rcf/1'
BUDGETS = {
    'cpu_ms': 20,
    'wall_ms': 200,
    'startup_ms': 5000,
    'max_message_bytes': MAX_MESSAGE,
    'diagnostic_bytes_per_callback': 8192,
    'memory_limit_enforced': True,
    'memory_bytes': 256 * 1024 * 1024,
}

def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)

def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def write_json(path: str | Path, value: Any) -> None:
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def file_hash(path: str | Path) -> str:
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def file_crc32(path: str | Path) -> tuple[int, int]:
    """Return (size, crc32) without loading the file into memory."""
    checksum = 0
    size = 0
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            size += len(chunk)
            checksum = zlib.crc32(chunk, checksum)
    return size, checksum & 0xFFFFFFFF

def frame_state(state: dict[str, Any]) -> dict[str, Any]:
    """The per-frame payload: everything except the published statics."""
    return {key: value for key, value in state.items() if key not in STATIC_KEYS}

class PolicyFailure(Exception):
    """An author process failed; infrastructure exceptions use other types."""

class PolicyProcess:
    """Keep one strategy instance alive and enforce externally measured budgets."""

    def __init__(self, path: str | Path, cpu_ms: int=20, wall_ms: int=200, startup_ms: int=5000) -> None:
        self.path = str(Path(path).resolve())
        self.cpu_ms = cpu_ms
        self.wall_ms = wall_ms
        self.startup_ms = startup_ms
        root = str(Path(__file__).resolve().parent.parent)
        env = os.environ.copy()
        env['PYTHONPATH'] = root
        env['PYTHONIOENCODING'] = 'utf-8'
        env['PYTHONUNBUFFERED'] = '1'
        self.process = subprocess.Popen(
            [sys.executable, '-m', 'robocon_sim.worker'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=root,
            env=env,
        )
        self.responses = queue.Queue(maxsize=2)
        self.errors = 0
        self.closed = False
        threading.Thread(target=self._reader, daemon=True).start()

    def _deliver(self, response: Any) -> None:
        try:
            self.responses.put_nowait(response)
        except queue.Full:
            self.process.kill()
            try:
                self.responses.get_nowait()
                self.responses.put_nowait(PolicyFailure('protocol response flood'))
            except queue.Empty:
                pass

    def _memory(self) -> int | None:
        if os.name == 'nt':

            class Counters(ctypes.Structure):
                _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong)] + [(name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if not ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(int(self.process._handle)), ctypes.byref(counters), counters.cb):
                return None
            return counters.PrivateUsage
        try:
            lines = Path('/proc/%s/status' % self.process.pid).read_text().splitlines()
            return sum((int(line.split()[1]) * 1024 for line in lines if line.startswith(('VmRSS:', 'VmSwap:'))))
        except OSError:
            return None

    def _reader(self) -> None:
        try:
            while True:
                raw = self.process.stdout.readline(MAX_MESSAGE + 1)
                if not raw:
                    self._deliver(PolicyFailure('policy exited'))
                    return
                if len(raw) > MAX_MESSAGE:
                    self._deliver(PolicyFailure('protocol output exceeds limit'))
                    return
                try:
                    response = json.loads(raw)
                    if not isinstance(response, dict):
                        raise ValueError('response must be a JSON object')
                    self._deliver(response)
                except Exception:
                    self._deliver(PolicyFailure('invalid JSON response'))
                    return
        except Exception as exc:
            self._deliver(PolicyFailure(str(exc)))

    def _cpu(self) -> float | None:
        try:
            if os.name == 'nt':
                values = [ctypes.c_ulonglong() for _ in range(4)]
                ok = ctypes.windll.kernel32.GetProcessTimes(ctypes.c_void_p(int(self.process._handle)), *[ctypes.byref(v) for v in values])
                return (values[2].value + values[3].value) / 10000 if ok else None
            stat = Path('/proc/%s/stat' % self.process.pid).read_text().split()
            return (int(stat[13]) + int(stat[14])) * 1000 / os.sysconf('SC_CLK_TCK')
        except Exception:
            return None

    def call(self, message: dict[str, Any], startup: bool=False) -> dict[str, Any]:
        if self.closed:
            raise PolicyFailure('policy terminated')
        encoded = (canonical(message) + '\n').encode()
        if len(encoded) > MAX_MESSAGE:
            raise RuntimeError('platform snapshot exceeds protocol limit')
        before = self._cpu()
        start = time.monotonic()
        deadline = start + (self.startup_ms if startup else self.wall_ms) / 1000

        def send():
            try:
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._deliver(PolicyFailure('policy exited'))
        threading.Thread(target=send, daemon=True).start()
        while True:
            elapsed = time.monotonic() - start
            if time.monotonic() > deadline:
                self.close()
                raise PolicyFailure('wall timeout')
            memory = self._memory()
            if memory is not None and memory > BUDGETS['memory_bytes']:
                self.close()
                raise PolicyFailure('memory limit exceeded')
            cpu = self._cpu()
            if not startup and cpu is not None and (before is not None) and (cpu - before > self.cpu_ms + 1):
                self.close()
                raise PolicyFailure('CPU timeout')
            try:
                response = self.responses.get(timeout=min(0.005, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if isinstance(response, Exception):
                self.close()
                raise response
            response['wall_ms'] = round((time.monotonic() - start) * 1000, 3)
            if response.get('seq') != message.get('seq', -1):
                self.close()
                raise PolicyFailure('out-of-order response')
            if not startup and response.get('cpu_ms', 0) > self.cpu_ms:
                self.close()
                raise PolicyFailure('CPU timeout')
            return response

    def start(self, context: dict[str, Any]) -> dict[str, Any]:
        response = self.call({'type': 'start', 'seq': -1, 'path': self.path, 'context': context}, True)
        if not response.get('ok'):
            self.close()
            raise PolicyFailure('startup: ' + response.get('error', 'error'))
        return response

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=3)
            for stream in (self.process.stdin, self.process.stdout):
                try:
                    stream.close()
                except Exception:
                    pass

def run_match(red_path: str | Path, blue_path: str | Path, config: Any=None, output_dir: str | Path | None=None,
              frame: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run both policies at every common boundary and stream a replay bundle.

    Unexpected I/O and kernel failures propagate, allowing the scheduler to retry
    the frozen series. Only PolicyFailure contributes a technical loss.
    """
    from brsdk import PROTOCOL_VERSION, SDK_VERSION
    from .config import MatchConfig
    from .engine import Engine
    config = config if isinstance(config, MatchConfig) else MatchConfig.from_dict(config or {})
    engine = Engine(config)
    match_id = uuid.uuid4().hex
    output = Path(output_dir or Path('runs') / match_id).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError(f'output directory must be empty: {output}')
    initial_state = engine.public_state()
    manifest = {
        'id': match_id,
        'bundle_format': BUNDLE_FORMAT,
        'protocol_version': PROTOCOL_VERSION,
        'sdk_version': SDK_VERSION,
        'ruleset_version': initial_state.get('ruleset_version'),
        'runtime': platform.python_version(),
        'platform': platform.platform(),
        'runtime_profile': 'host-python-reviewed-code',
        'handling_profile': 'two-box-stack-v1',
        'budgets': BUDGETS,
        'config': config.to_dict(),
        'strategies': {
            team: {'sha256': file_hash(path), 'name': Path(path).name}
            for team, path in [('red', red_path), ('blue', blue_path)]
        },
        'map': initial_state.get('map'),
        'created_at': time.time(),
    }
    source_root = Path(__file__).resolve().parent.parent
    source_names = [
        'engine', 'world', 'rules', 'supply', 'faults', 'operations',
        'config', 'map', 'planning',
    ]
    source_paths = [source_root / 'robocon_sim' / (name + '.py') for name in source_names]
    source_paths.extend((source_root / 'brsdk').glob('*.py'))
    manifest['implementation_hashes'] = {
        path.relative_to(source_root).as_posix(): file_hash(path)
        for path in source_paths if path.exists()
    }
    write_json(output / 'manifest.json', manifest)
    policies = {}
    diagnostics = []
    streams = {name: (output / (name + '.jsonl')).open('w', encoding='utf-8') for name in ('commands',)}
    counts = {name: 0 for name in streams}
    metrics = MatchMetrics()
    frames = FrameWriter(
        output / 'snapshots.rcf',
        {'map': initial_state['map'], 'parameters': initial_state['parameters'],
         'ruleset_version': initial_state.get('ruleset_version'), 'match_id': match_id},
        tick_ms=config.tick_ms, duration_ms=config.duration_ms, **(frame or {}),
    )

    def append_log(name: str, row: Any) -> None:
        # No per-line digest any more: commands are small, and tampering is caught
        # by replaying the match and comparing every frame, which is stronger than
        # a checksum the writer itself could recompute.
        streams[name].write(canonical({'log_seq': counts[name], 'payload': row}) + '\n')
        counts[name] += 1
        if counts[name] % 100 == 0:
            streams[name].flush()

    def checkpoint(state: dict[str, Any]) -> None:
        frames.append(state)
    try:
        failures = {}
        for team, path in [('red', red_path), ('blue', blue_path)]:
            policies[team] = PolicyProcess(path)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            public_context = {
                **config.public_dict(),
                **{key: manifest[key] for key in ('map', 'ruleset_version', 'sdk_version', 'protocol_version')},
            }
            futures = {
                team: pool.submit(policy.start, {**public_context, 'team': team})
                for team, policy in policies.items()
            }
            for team, future in futures.items():
                try:
                    diagnostics.append({'team': team, 'phase': 'startup', **future.result()})
                except PolicyFailure as exc:
                    failures[team] = str(exc)
            snapshot = initial_state
            checkpoint(snapshot)
            if failures:
                append_log('commands', {'seq': engine.seq, 'failures': failures, 'decisions': {'red': None, 'blue': None}})
                engine.fail_policies(failures)
                snapshot = engine.public_state()
                checkpoint(snapshot)
            observations = engine.observations(snapshot)
            while not engine.done:
                futures = {t: pool.submit(p.call, {'type': 'step', 'seq': engine.seq, 'observation': observations[t]}) for t, p in policies.items()}
                decisions = {'red': None, 'blue': None}
                failures = {}
                responses = {}
                for team, future in futures.items():
                    try:
                        response = future.result()
                        responses[team] = response
                        if response.get('ok'):
                            decisions[team] = response.get('decision')
                            policies[team].errors = 0
                        else:
                            policies[team].errors += 1
                            if policies[team].errors >= 3:
                                failures[team] = 'three consecutive policy errors'
                    except PolicyFailure as exc:
                        failures[team] = str(exc)
                        responses[team] = {'ok': False, 'error': str(exc), 'hard_failure': True}
                frame_record = {
                    'seq': engine.seq,
                    'sim_time_ms': engine.sim_time_ms,
                    'decisions': decisions,
                    'failures': failures,
                    'responses': responses,
                }
                if failures:
                    engine.fail_policies(failures)
                    snapshot = engine.public_state()
                else:
                    snapshot = engine.step(decisions, observations)
                next_observations = engine.observations(snapshot)
                frame_record['receipts'] = {
                    team: next_observations[team]['receipts']
                    for team in ('red', 'blue')
                }
                metrics.interval(observations, engine.sim_time_ms - frame_record['sim_time_ms'])
                metrics.receipts(frame_record['receipts'])
                append_log('commands', frame_record)
                checkpoint(snapshot)
                observations = next_observations
        final_hash = digest(frame_state(snapshot))
        frames.close(final_hash)
        result = engine.result()
        result.update({'match_id': match_id, 'output_dir': str(output), 'final_hash': final_hash})
        result['metrics'] = metrics.finish(engine.events, result['faults'])
        write_json(output / 'result.json', result)
        write_json(output / 'score_ledger.json', result.get('score_ledger', []))
        for name, rows in [('events', engine.events), ('diagnostics', diagnostics)]:
            with (output / (name + '.jsonl')).open('w', encoding='utf-8') as stream:
                for index, row in enumerate(rows):
                    stream.write(canonical({'log_seq': index, 'payload': row}) + '\n')
        for stream in streams.values():
            stream.close()
        files = {}
        for path in sorted(output.iterdir()):
            if not path.is_file() or path.name == 'integrity.json':
                continue
            size, crc = file_crc32(path)
            files[path.name] = {'size': size, 'crc32': f'{crc:08x}'}
        write_json(output / 'integrity.json', {'format': BUNDLE_FORMAT, 'final_hash': final_hash, 'files': files})
        return result
    except BaseException as exc:
        frames.abort()
        write_json(output / 'platform_failure.json', {'error': repr(exc), 'phase': 'platform', 'match_id': match_id})
        raise
    finally:
        for stream in streams.values():
            stream.close()
        frames.abort()
        for policy in policies.values():
            policy.close()
