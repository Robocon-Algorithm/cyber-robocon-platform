import json
from pathlib import Path
import tempfile
import time
import unittest
from robocon_sim.runner import PolicyFailure,PolicyProcess

class RuntimeTests(unittest.TestCase):
    def make_policy(self,code):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        path=Path(temp.name)/'policy.py';path.write_text('from brsdk import Strategy\n'+code,encoding='utf-8')
        worker=PolicyProcess(path);self.addCleanup(worker.close);worker.start({'team':'red'})
        return worker
    def test_persistent_instance_and_print_diagnostics(self):
        worker=self.make_policy('class MyStrategy(Strategy):\n def on_start(self,c): self.n=0\n def on_step(self,o,a):\n  self.n+=1\n  print("diagnostic")\n  return {"based_on_seq": self.n, "primary":None,"request_ball":False}\n')
        responses=[worker.call({'type':'step','seq':i,'observation':{'seq':i}}) for i in range(2)]
        self.assertEqual([r['decision']['based_on_seq'] for r in responses],[1,2]);self.assertIn('diagnostic',responses[0]['diagnostics'])
    def test_wall_timeout_kills_process(self):
        worker=self.make_policy('import time\nclass MyStrategy(Strategy):\n def on_step(self,o,a): time.sleep(10)\n')
        start=time.monotonic()
        with self.assertRaisesRegex(PolicyFailure,'wall timeout'):worker.call({'type':'step','seq':0,'observation':{'seq':0}})
        self.assertLess(time.monotonic()-start,2);self.assertIsNotNone(worker.process.poll())
    def test_cpu_timeout(self):
        worker=self.make_policy('class MyStrategy(Strategy):\n def on_step(self,o,a):\n  while True: pass\n')
        with self.assertRaisesRegex(PolicyFailure,'CPU timeout'):worker.call({'type':'step','seq':0,'observation':{'seq':0}})
    def test_memory_limit(self):
        worker=self.make_policy('import time\nclass MyStrategy(Strategy):\n def on_step(self,o,a):\n  self.data=bytearray(300*1024*1024)\n  time.sleep(10)\n')
        worker.cpu_ms=1000
        with self.assertRaisesRegex(PolicyFailure,'memory limit'):worker.call({'type':'step','seq':0,'observation':{'seq':0}})
    def test_returned_error_does_not_overlap(self):
        worker=self.make_policy('class MyStrategy(Strategy):\n def on_step(self,o,a): raise ValueError("broken")\n')
        for i in range(3):self.assertFalse(worker.call({'type':'step','seq':i,'observation':{'seq':i}})['ok'])

if __name__=='__main__': unittest.main()
