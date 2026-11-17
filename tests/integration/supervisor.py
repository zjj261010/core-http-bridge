import os,sys,json,subprocess,signal,threading,time,socket
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
ROOT=Path(__file__).parent
TOKEN=json.loads((ROOT/'control.json').read_text())['token']
env=dict(os.environ,TMPDIR=str(ROOT/'coretmp'),PYTHONDONTWRITEBYTECODE='1',PYTHONPATH='')
children={}; logs={}; guard=threading.Lock()
def start(name):
    log=open(ROOT/(name+'.log'),'ab',buffering=0);logs[name]=log
    command=([sys.executable,'/opt/core/venv/bin/core-daemon','-c',str(ROOT/'core.conf')]
             if name=='daemon' else [sys.executable,str(ROOT/'server.py')])
    children[name]=subprocess.Popen(command,env=env,cwd='/opt/core-http-bridge',stdout=log,stderr=log,start_new_session=True)
def stop(name):
    proc=children.get(name)
    if proc and proc.poll() is None:
        proc.terminate()
        try:proc.wait(timeout=15)
        except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
    if name in logs:logs[name].close()
def ready(port,timeout=25):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=.3):return True
        except OSError:time.sleep(.2)
    return False
start('daemon')
if not ready(15051):raise RuntimeError('test daemon failed; see daemon.log')
start('bridge')
if not ready(18080):raise RuntimeError('test bridge failed; see bridge.log')
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.headers.get('X-Test-Key')!=TOKEN:self.send_error(403);return
        data={}
        with guard:
            if self.path=='/restart-daemon':
                stop('daemon');start('daemon');data={'ready':ready(15051)}
            elif self.path=='/restart-bridge':
                stop('bridge');start('bridge');data={'ready':ready(18080)}
            elif self.path=='/shutdown':
                stop('bridge');stop('daemon');data={'stopped':True}
                threading.Thread(target=self.server.shutdown,daemon=True).start()
            else:self.send_error(404);return
        body=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def log_message(self,*args):pass
(ROOT/'processes.json').write_text(json.dumps({'supervisor':os.getpid(),**{k:v.pid for k,v in children.items()}}))
server=ThreadingHTTPServer(('127.0.0.1',18081),Handler)
try:server.serve_forever()
finally:stop('bridge');stop('daemon')
