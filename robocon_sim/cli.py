import argparse
import json
from pathlib import Path
from .runner import run_match
from .replay import verify_replay

def _platform(path):
    """Accept either a full platform document or a bare single-match configuration."""
    from copy import deepcopy
    from .config import MatchConfig
    from .platform_config import ConfigStore, DEFAULT_SCENARIOS, FrameOptions, PlatformConfig
    target = Path(path) if path else Path('data/arena/platform.json')
    if target.is_file():
        try:
            document = json.loads(target.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            document = None
        if isinstance(document, dict) and 'default_match' not in document:
            return PlatformConfig(default_match=MatchConfig.from_dict(document),
                                  scenarios=deepcopy(DEFAULT_SCENARIOS),
                                  frame=FrameOptions(), path=str(target))
    return ConfigStore(target).current()

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
        p.add_argument('--config', help='platform config JSON (default: <output>/platform.json or built-in)')
        p.add_argument('--duration-ms', type=int)
        p.add_argument('--data', help='data root that holds platform.json and profiles/')
        p.add_argument('--red-profile', help='side profile name for red (default from platform.json)')
        p.add_argument('--blue-profile', help='side profile name for blue (default from platform.json)')
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
    p.add_argument('--data', default='data/arena')
    p.add_argument('--config', help='platform config JSON (default: <data>/platform.json)')
    p.add_argument('--trusted-code', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'serve':
        from .server import serve
        serve(args.data, args.host, args.port, args.trusted_code, args.config)
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
        platform = _platform(None)
        config = platform.matches(1, {'duration_ms': args.duration_ms})[0]
        result = run_match(base / 'stack_builder.py', base / 'adaptive_raider.py', config, args.output,
                           platform.frame.to_dict())
    else:
        config_path = args.config or (str(Path(args.data) / 'platform.json') if args.data else None)
        platform = _platform(config_path)
        overrides = {'duration_ms': args.duration_ms} if args.duration_ms else None
        red_profile, blue_profile = args.red_profile, args.blue_profile
        if args.command == 'match':
            result = run_match(args.red, args.blue,
                               platform.matches(1, overrides, red_profile=red_profile,
                                                blue_profile=blue_profile)[0],
                               args.output, platform.frame.to_dict())
        else:
            from .storage import Storage
            from .server import execute_job
            storage = Storage(args.output)
            ids = []
            for name, path in [('Strategy A', args.red), ('Strategy B', args.blue)]:
                team = storage.create_team(name)
                ids.append(storage.upload(team['id'], Path(path).name, Path(path).read_bytes())['id'])
            red = platform.profile(red_profile, 'red')
            blue = platform.profile(blue_profile, 'blue')
            matches = platform.matches(args.count, overrides, red_profile=red.name, blue_profile=blue.name)
            ident = storage.create_series(*ids, args.count, matches=matches,
                                          ranked=overrides is None and args.count > 1
                                          and red.parameters == blue.parameters,
                                          red_profile=red.name, blue_profile=blue.name,
                                          rating_profile=red.rating_key())
            job = storage.claim(ident)
            try:
                execute_job(storage, job, platform.frame.to_dict())
            except Exception as exc:
                storage.failed(ident, job['lease'], repr(exc), max_attempts=1)
                raise
            result = storage.job(ident)
    print(json.dumps(result, ensure_ascii=False, indent=2))
