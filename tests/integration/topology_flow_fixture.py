"""Prepare one HTTP image catalog, leaving templates/topologies empty for the browser flow."""
import argparse
import json
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('--run-dir',required=True);args=p.parse_args()
root=Path(args.run_dir);context=json.loads((root/'context.json').read_text());base='http://127.0.0.1:18080'


def api(method,path,body=None,raw=None):
    data=raw if raw is not None else json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(base+path,data=data,method=method,headers={'Content-Type':'application/octet-stream' if raw is not None else 'application/json'}),timeout=90) as response:
        value=json.load(response)
        return value.get('data',value) if isinstance(value,dict) else value


ref=context['prefix']+'-python:v1'
subprocess.run(['docker','tag','python:3.10-slim',ref],check=True)
context['image']=ref
(root/'context.json').write_text(json.dumps(context,indent=2))
image_path=root/'fixtures/python-image.tar'
subprocess.run(['docker','save','-o',str(image_path),ref],check=True)
job=api('POST','/jobs/import',{'image_path':str(image_path),'defer_services':True})
deadline=time.monotonic()+120
while time.monotonic()<deadline:
    job=api('GET','/jobs/'+job['job_id'])
    if job['status'] not in ('queued','running'):break
    time.sleep(.2)
assert job['status']=='succeeded',job
catalog=api('GET','/image-services/detail?ref='+urllib.parse.quote(ref))
files=[]
for name,role,text in [
    ('serve.sh','startup','#!/bin/sh\npython3 -m http.server 8080 --bind 0.0.0.0 >/tmp/topology-http.log 2>&1 &\necho "$!" >/tmp/topology-http.pid\n'),
    ('ready.sh','validate','#!/bin/sh\npython3 -c "import socket; socket.create_connection((\'127.0.0.1\',8080),1).close()"\n'),
    ('stop.sh','shutdown','#!/bin/sh\nkill "$(cat /tmp/topology-http.pid)"\n')]:
    files.append(api('POST','/image-services/attachments?name='+name+'&role='+role,raw=text.encode()))
services=[{'id':'http','name':'HTTP 服务','software':'Python','ports':[{'port':8080,'protocol':'tcp'}],
           'modes':[{'id':'default','name':'默认启动','strategy':'script','default':True,'interpreter':'sh','workdir':'/tmp','files':files,'depends_on':[]}]}]
catalog=api('POST','/image-services/'+catalog['catalog_id']+'/commit',{**catalog,'base_revision':catalog['revision'],'services':services,'action':'apply','acknowledge':True})
assert catalog['state']=='applied',catalog
(root/'topology-fixture.json').write_text(json.dumps({'ref':ref,'catalog':catalog,'prefix':context['prefix']},ensure_ascii=False,indent=2))
print('专用 HTTP 镜像配置就绪；未创建模板或拓扑。')
