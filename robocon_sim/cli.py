import argparse
import json
from pathlib import Path
from .runner import run_match
from .replay import verify_replay

def main(argv: list[str] | None=None) -> None:
    parser = argparse.ArgumentParser(description='BR strategy arena — local reviewed Python code only')
    sub = parser.add_subparsers(dest='command', required=True)
    demo = sub.add_parser('demo')
    demo.add_argument('--output', default='runs/demo')
    demo.add_argument('--duration-ms', type=int, default=180000)
    for name in ('match', 'series'):
        p = sub.add_parser(name)
        p.add_argument('--red', required=True)
        p.add_argument('--blue', required=True)
        p.add_argument('--output', default='runs/' + name)
        p.add_argument('--config')
        p.add_argument('--duration-ms', type=int)
        if name == 'series':
            p.add_argument('--count', type=int, choices=(2, 10), default=2)
    p = sub.add_parser('replay')
    p.add_argument('directory')
    p = sub.add_parser('dump', help='print decoded frames from a match bundle')
    p.add_argument('directory')
    p.add_argument('--frame', type=int, default=0)
    p.add_argument('--count', type=int, default=1)
    p.add_argument('--meta', action='store_true', help='list the frame index instead of states')
    p = sub.add_parser('serve')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--data', default='data/l1-reach')
    p.add_argument('--trusted-code', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'serve':
        from .server import serve
        serve(args.data, args.host, args.port, args.trusted_code)
        return
    if args.command == 'replay':
        result = verify_replay(args.directory)
    elif args.command == 'dump':
        from .frame_codec import FrameReader
        reader = FrameReader(Path(args.directory) / 'snapshots.rcf')
        if args.meta:
            result = {'static_keys': sorted(reader.static), 'frames': reader.meta()}
        else:
            result = reader.states(args.frame, args.count, include_static=True)
            if args.count == 1:
                result = result[0]
    elif args.command == 'demo':
        base = Path(__file__).resolve().parent.parent / 'examples'
        result = run_match(base / 'stack_builder.py', base / 'adaptive_raider.py', {'duration_ms': args.duration_ms}, args.output)
    else:
        config = json.loads(Path(args.config).read_text(encoding='utf-8-sig')) if args.config else {}
        if args.duration_ms:
            config['duration_ms'] = args.duration_ms
        if args.command == 'match':
            result = run_match(args.red, args.blue, config, args.output)
        else:
            from .storage import Storage
            from .server import execute_job
            storage = Storage(args.output)
            ids = []
            for name, path in [('Strategy A', args.red), ('Strategy B', args.blue)]:
                team = storage.create_team(name)
                ids.append(storage.upload(team['id'], Path(path).name, Path(path).read_bytes())['id'])
            ident = storage.create_series(*ids, args.count, config, ranked=False)
            job = storage.claim(ident)
            try:
                execute_job(storage, job)
            except Exception as exc:
                storage.failed(ident, job['lease'], repr(exc), max_attempts=1)
                raise
            result = storage.job(ident)
    print(json.dumps(result, ensure_ascii=False, indent=2))
