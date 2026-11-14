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

if __name__=='__main__':unittest.main()
