from pathlib import Path
import tempfile
import unittest
from robocon_sim.runner import run_match
from robocon_sim.replay import verify_replay,read_log

class MatchRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
    def policy(self,name,body):
        path=self.root/(name+'.py');path.write_text('from brsdk import Strategy\nclass MyStrategy(Strategy):\n def on_step(self,obs,api):\n  '+body+'\n',encoding='utf-8');return path
    def test_accepted_commands_replay_and_tamper_detection(self):
        result=run_match('examples/stack_builder.py','examples/adaptive_raider.py',{'duration_ms':22000},self.root/'match')
        self.assertEqual(result['status'],'completed');self.assertTrue(verify_replay(self.root/'match')['verified'])
        frames=read_log(self.root/'match'/'commands.jsonl');self.assertTrue(any(f['decisions']['red'] is not None for f in frames))
        snapshots=self.root/'match'/'snapshots.rcf'
        self.assertTrue(snapshots.is_file())
        self.assertLess(snapshots.stat().st_size,2*1024*1024)
        with snapshots.open('ab') as stream:stream.write(b'\x00')
        with self.assertRaisesRegex(ValueError,'integrity'):verify_replay(self.root/'match')
    def test_frame_checksums_detect_corruption(self):
        from robocon_sim.frame_codec import FrameFormatError,FrameReader
        run_match('examples/stack_builder.py','examples/stack_builder.py',{'duration_ms':2200},self.root/'match')
        path=self.root/'match'/'snapshots.rcf'
        reader=FrameReader(path);meta=reader.meta();self.assertGreater(len(meta),1)
        target=meta[1]['frame']
        offset=reader._entries[target][0]+reader._entries[target][1]-1
        raw=bytearray(path.read_bytes());raw[offset]^=0xFF;path.write_bytes(bytes(raw))
        with self.assertRaises(FrameFormatError):FrameReader(path).verify()
        with self.assertRaises(ValueError):verify_replay(self.root/'match')
    def test_three_returned_errors_both_technical_draw(self):
        policy=self.policy('error','raise ValueError("bad decision")')
        result=run_match(policy,policy,{'duration_ms':1000},self.root/'match')
        self.assertEqual(result['status'],'technical_failure');self.assertIsNone(result['winner']);self.assertEqual(set(result['technical_failures']),{'red','blue'});self.assertEqual(result['sim_time_ms'],200);self.assertTrue(verify_replay(self.root/'match')['verified'])
    def test_startup_failures_are_replayable(self):
        path=self.root/'bad.py';path.write_text('raise ValueError("bad import")',encoding='utf-8')
        result=run_match(path,path,{'duration_ms':1000},self.root/'match')
        self.assertEqual(result['sim_time_ms'],0);self.assertIsNone(result['winner']);self.assertTrue(verify_replay(self.root/'match')['verified'])

if __name__=='__main__':unittest.main()
