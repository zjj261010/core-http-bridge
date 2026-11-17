"""Prepare a reproducible isolated test fixture; optional explicit CORE bootstrap."""
import argparse,json,os,secrets,socket,subprocess,tarfile,time,uuid
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('--run-dir',required=True);p.add_argument('--image',default='target-dmz-base:v3');p.add_argument('--start-via-core',action='store_true');args=p.parse_args()
root=Path(args.run_dir).resolve();repo=Path(__file__).resolve().parents[2]
for port in (15051,18080,18081):
    with socket.socket() as probe:
        if probe.connect_ex(('127.0.0.1',port))==0: raise SystemExit(f'专用端口 {port} 已被占用；未改动现有进程')
root.mkdir(parents=True,exist_ok=False)
for path in ('state/custom_services','state/imports','fixtures','evidence','screenshots'):(root/path).mkdir(parents=True,exist_ok=True)
# Unix domain socket paths have a 108-byte limit. Keep runtime paths short and persistent.
runtime=Path.home()/'.local/share/core-http-bridge/run'/uuid.uuid4().hex[:8]
runtime.mkdir(parents=True,exist_ok=False)
(root/'coretmp').symlink_to(runtime,target_is_directory=True)
prefix='chbtest'+time.strftime('%H%M%S')+uuid.uuid4().hex[:6];image=prefix+'-httpd:v1';fixture=root/'fixtures'/prefix;fixture.mkdir();(fixture/'services/templates').mkdir(parents=True)
subprocess.run(['docker','tag',args.image,image],check=True);subprocess.run(['docker','save','-o',str(fixture/'image.tar'),image],check=True)
manifest={'name':prefix,'software':'httpd','version':'fixture-1.0','image':{'name':image.rsplit(':',1)[0],'tag':'v1','ports':[80]},'services':[]}
source='from core.services.base import CoreService\n'
for mode in ('normal','vuln'):
    name=f'svc-{prefix}-httpd-{mode}';filename=f'{prefix}-{mode}.sh'
    manifest['services'].append({'name':name,'mode':mode,'ports':[80]})
    source+=f'\nclass Httpd{mode.title()}(CoreService):\n name={name!r}\n group="Acceptance"\n files=[{filename!r}]\n startup=["bash {filename}"]\n validate=["pidof httpd"]\n shutdown=["pkill httpd"]\n'
    (fixture/'services/templates'/filename).write_text('#!/bin/bash\nhttpd -DFOREGROUND >/dev/null 2>&1 &\n')
(fixture/'services/service.py').write_text(source);(fixture/'manifest.json').write_text(json.dumps(manifest))
with tarfile.open(root/'fixtures/bundle.tar.gz','w:gz') as archive:archive.add(fixture,arcname=prefix)
(root/'context.json').write_text(json.dumps({'prefix':prefix,'image':image,'old_image':image,'bundle':str(fixture),'manifest':manifest,'py':source},indent=2))
(root/'control.json').write_text(json.dumps({'token':secrets.token_hex(24)}));(root/'control.json').chmod(0o600)
(root/'core.conf').write_text('[core-daemon]\ngrpcaddress=127.0.0.1\ngrpcport=15051\ncustom_services_dir='+str(root/'state/custom_services')+'\n')
(root/'supervisor.py').write_text(Path(__file__).with_name('supervisor.py').read_text().replace("cwd='/opt/core-http-bridge'",'cwd='+repr(str(repo))))
(root/'server.py').write_text('''import os,sys
from pathlib import Path
ROOT=Path(__file__).parent
os.environ.update(CORE_GRPC_ADDRESS='127.0.0.1:15051',BRIDGE_DATA_DIR=str(ROOT/'state'),CORE_CUSTOM_SERVICES_DIR=str(ROOT/'state/custom_services'))
sys.path.insert(0,__REPO__)
import app,uvicorn
uvicorn.run(app.app,host='127.0.0.1',port=18080)
'''.replace('__REPO__',repr(str(repo))))
(root/'bootstrap.py').write_text('import subprocess,os\nfrom pathlib import Path\nroot=Path('+repr(str(root))+')\nlog=open(root/"supervisor.log","ab",buffering=0)\nprocess=subprocess.Popen(["/opt/core/venv/bin/python",str(root/"supervisor.py")],cwd=str(root),stdout=log,stderr=log,start_new_session=True,env=dict(os.environ,PYTHONPATH="",PYTHONDONTWRITEBYTECODE="1"))\n(root/"bootstrap-pid").write_text(str(process.pid))\n')
if args.start_via_core:
    from core.api.grpc.client import CoreGrpcClient
    client=CoreGrpcClient(address='127.0.0.1:50051');client.connect();client.execute_script(str(root/'bootstrap.py'),'');client.close()
print(root)
