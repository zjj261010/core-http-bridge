import json,urllib.request,subprocess,time
from pathlib import Path
import argparse
parser=argparse.ArgumentParser();parser.add_argument('--run-dir',required=True);args=parser.parse_args()
root=Path(args.run_dir);c=json.loads((root/'context.json').read_text());base='http://127.0.0.1:18080'
def request(method,path):
    with urllib.request.urlopen(urllib.request.Request(base+path,method=method),timeout=60) as response:return json.load(response)
result={'sessions':[],'containers':[],'images':[]}
deployments=request('GET','/deployments')['data'];owned_names={b['runtime_name'] for d in deployments for b in d.get('bindings',{}).values() if b.get('runtime_name')}
for session in request('GET','/api/sessions'):
    response=request('DELETE','/api/sessions/'+str(session['id']));result['sessions'].append({'id':session['id'],'result':response['result']})
result['remaining_sessions']=request('GET','/api/sessions')
output=subprocess.run(['docker','ps','-a','--format','{{.Names}}'],capture_output=True,text=True,check=True)
for name in output.stdout.splitlines():
    if name.startswith('standalone-'+c['prefix']) or name in owned_names:
        deleted=subprocess.run(['docker','rm','-f',name],capture_output=True,text=True);result['containers'].append({'name':name,'code':deleted.returncode})
for tag in set((c['image'],c['old_image'])):
    deleted=subprocess.run(['docker','rmi',tag],capture_output=True,text=True);result['images'].append({'tag':tag,'code':deleted.returncode,'output':deleted.stdout+deleted.stderr})
token=json.loads((root/'control.json').read_text())['token']
with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:18081/shutdown',data=b'',method='POST',headers={'X-Test-Key':token}),timeout=40) as response: result['shutdown']=json.load(response)
(root/'cleanup-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2))
