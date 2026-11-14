import json
import tempfile
import threading
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from robocon_sim.server import make_server

class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.server=make_server(self.temp.name,port=0,trusted_code=True,start_scheduler=False);threading.Thread(target=self.server.serve_forever,daemon=True).start();self.addCleanup(self.close)
        self.url='http://127.0.0.1:'+str(self.server.server_port)
    def close(self):self.server.shutdown();self.server.server_close()
    def post(self,path,data,token=None):
        req=Request(self.url+path,json.dumps(data).encode(),{'Content-Type':'application/json','X-Robocon-Token':token or ''})
        return urlopen(req)
    def test_frontend_upload_and_token(self):
        response=urlopen(self.url+'/');self.assertIn('BR /'.encode(),response.read());self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
        token=json.load(urlopen(self.url+'/api/bootstrap'))['token']
        with self.assertRaises(HTTPError) as caught:self.post('/api/teams',{'name':'A'})
        self.assertEqual(caught.exception.code,403);caught.exception.close()
        payload={'team_name':'<img onerror=alert(1)>','filename':'x.py','source':'from brsdk import Strategy\nclass MyStrategy(Strategy):\n def on_step(self,obs,api): return None\n'}
        result=json.load(self.post('/api/upload',payload,token));self.assertIn('sha256',result)
        with self.assertRaises(HTTPError) as caught:urlopen(self.url+'/../platform.sqlite3')
        caught.exception.close()
        with self.assertRaises(HTTPError) as caught:urlopen(self.url+'/api/replay?job=../../secret')
        caught.exception.close()
    def test_loopback_and_trusted_requirements(self):
        with self.assertRaises(ValueError):make_server(self.temp.name,host='0.0.0.0',trusted_code=True)
        with self.assertRaises(ValueError):make_server(self.temp.name)
    def test_config_endpoint_and_queued_job_freeze(self):
        from pathlib import Path
        from robocon_sim.platform_config import platform_dict
        from robocon_sim.storage import Storage
        token=json.load(urlopen(self.url+'/api/bootstrap'))['token']
        source='from brsdk import Strategy\nclass MyStrategy(Strategy):\n def on_step(self,obs,api): return None\n'
        a=json.load(self.post('/api/upload',{'team_name':'A','filename':'a.py','source':source},token))
        b=json.load(self.post('/api/upload',{'team_name':'B','filename':'b.py','source':source},token))
        config=json.load(urlopen(self.url+'/api/config'))
        self.assertEqual(config['schema'],1)
        self.assertEqual(config['warnings'],[])
        path=Path(self.temp.name)/'platform.json'
        path.write_text(json.dumps({**platform_dict(),'default_match':{'duration_ms':2000}}),encoding='utf-8')
        reply=json.load(self.post('/api/jobs',{'a_version':a['id'],'b_version':b['id'],'count':2},token))
        job=Storage(self.temp.name).job(reply['id'])
        self.assertEqual([m['duration_ms'] for m in job['matches']],[2000,2000])
        path.write_text(json.dumps({**platform_dict(),'default_match':{'duration_ms':9000}}),encoding='utf-8')
        self.assertEqual([m['duration_ms'] for m in Storage(self.temp.name).job(reply['id'])['matches']],[2000,2000])
        path.write_text('{ broken',encoding='utf-8')
        config=json.load(urlopen(self.url+'/api/config'))
        self.assertEqual(len(config['warnings']),1)
        self.assertEqual(config['default_match']['duration_ms'],2000)

    def test_replay_metadata_and_frame_chunks(self):
        import contextlib,io
        from robocon_sim.cli import main as cli
        from robocon_sim.storage import Storage
        with contextlib.redirect_stdout(io.StringIO()):
            cli(['series','--red','examples/stack_builder.py','--blue','examples/stack_builder.py',
                 '--count','2','--duration-ms','3000','--output',self.temp.name])
        job=Storage(self.temp.name).overview()['jobs'][0]
        app=self.server.app
        bundle=app.replay(job['id'],0)
        self.assertLess(len(json.dumps(bundle)),120*1024)
        self.assertEqual(bundle['frame_count'],len(bundle['times_ms']))
        self.assertIn('map',bundle['static'])
        self.assertNotIn('snapshots',bundle)
        self.assertNotIn('commands',bundle)
        block=app.frames(job['id'],0,0,50)
        self.assertEqual(block['start'],0)
        self.assertEqual(len(block['frames']),min(50,bundle['frame_count']))
        self.assertEqual(block['frames'][0]['sim_time_ms'],0)
        tail=app.frames(job['id'],0,bundle['frame_count']-5,50)
        self.assertEqual(tail['frames'][-1]['seq'],bundle['frame_count']-1)
        last=app.frames(job['id'],0,bundle['frame_count']-1,1)['frames'][0]
        self.assertEqual(last['seq'],bundle['frame_count']-1)
        self.assertEqual(json.load(urlopen(self.url+'/api/replay?job='+job['id']+'&index=0'))['frame_count'],bundle['frame_count'])
        chunk=json.load(urlopen(self.url+'/api/replay/frames?job='+job['id']+'&index=0&from=10&count=5'))
        self.assertEqual([f['sim_time_ms'] for f in chunk['frames']],[1000,1100,1200,1300,1400])


if __name__=='__main__':unittest.main()
