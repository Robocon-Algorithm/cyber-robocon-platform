"""Persistent SDK policy process. This is isolation for reliability, not a sandbox."""
from typing import Any
import importlib.util
import inspect
import json
import sys
import time
import traceback
MAX_MESSAGE = 2 * 1024 * 1024

class Diagnostics:

    def __init__(self) -> None:
        self.parts = []
        self.size = 0

    def write(self, text: Any):
        text = str(text)
        remaining = max(0, 8192 - self.size)
        if remaining:
            self.parts.append(text[:remaining])
            self.size += min(len(text), remaining)
        return len(text)

    def flush(self):
        pass

    def take(self):
        result = ''.join(self.parts)
        self.parts = []
        self.size = 0
        return result

def main() -> None:
    protocol = sys.stdout
    diagnostic = Diagnostics()
    sys.stdout = diagnostic
    sys.stderr = diagnostic
    instance = None
    for raw in sys.stdin.buffer:
        started = time.process_time_ns()
        try:
            if len(raw) > MAX_MESSAGE:
                raise ValueError('request too large')
            request = json.loads(raw)
            if request['type'] == 'start':
                from brsdk import Strategy, Observation, StrategyAPI
                spec = importlib.util.spec_from_file_location('submitted_strategy', request['path'])
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                classes = [v for v in vars(module).values() if inspect.isclass(v) and v is not Strategy and issubclass(v, Strategy) and (v.__module__ == module.__name__)]
                if len(classes) != 1:
                    raise ValueError('exactly one Strategy subclass required')
                instance = classes[0]()
                instance.on_start(Observation(request['context']))
                decision = None
            else:
                obs = Observation(request['observation'])
                api = StrategyAPI(obs)
                decision = instance.on_step(obs, api)
                if decision is not None and hasattr(decision, 'to_dict'):
                    decision = decision.to_dict()
                if decision is not None and (not isinstance(decision, dict)):
                    raise TypeError('on_step must return Decision or None')
                if decision is not None:
                    if type(decision.get('based_on_seq')) is not int:
                        raise ValueError('decision.based_on_seq must be an integer')
                    if not isinstance(decision.get('request_ball', False), bool):
                        raise ValueError('decision.request_ball must be boolean')
                    primary = decision.get('primary')
                    if primary is not None and not isinstance(primary, dict):
                        raise ValueError('decision.primary must be object or null')
            response = {'seq': request.get('seq', -1), 'ok': True, 'decision': decision}
        except BaseException as exc:
            response = {'seq': request.get('seq', -1) if isinstance(locals().get('request'), dict) else -1, 'ok': False, 'error': type(exc).__name__ + ': ' + str(exc), 'traceback': traceback.format_exc(limit=6)[-4096:]}
        response['cpu_ms'] = (time.process_time_ns() - started) / 1000000.0
        response['diagnostics'] = diagnostic.take()
        try:
            encoded = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
            if len(encoded.encode('utf-8')) > MAX_MESSAGE:
                raise ValueError('response too large')
        except Exception as exc:
            encoded = json.dumps({'seq': response['seq'], 'ok': False, 'error': 'invalid response: ' + str(exc)})
        protocol.write(encoded + '\n')
        protocol.flush()
if __name__ == '__main__':
    main()
