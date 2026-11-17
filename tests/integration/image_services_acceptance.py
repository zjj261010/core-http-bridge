"""One-pass acceptance against an isolated real CORE daemon and Docker.

Preparation/cleanup use prepare_environment.py and cleanup_environment.py.
No product functions are patched. Never point this runner at production.
"""
import argparse
import copy
import io
import json
import shlex
import subprocess
import tarfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--run-dir',required=True)
args=parser.parse_args()
root=Path(args.run_dir)
context=json.loads((root/'context.json').read_text())
base='http://127.0.0.1:18080'
results=[]


def request(method,path,body=None,raw=None):
    data=raw if raw is not None else json.dumps(body).encode() if body is not None else None
    headers={'Content-Type':'application/octet-stream' if raw is not None else 'application/json'}
    with urllib.request.urlopen(urllib.request.Request(base+path,data=data,method=method,headers=headers),timeout=180) as response:
        content=response.read()
        if response.headers.get_content_type()=='application/json':
            value=json.loads(content)
            if isinstance(value,dict) and value.get('ok') is False: raise RuntimeError(str(value))
            return value.get('data',value) if isinstance(value,dict) else value
        return content


def record(name,detail):
    results.append({'case':name,'status':'passed','detail':detail})
    (root/'image-services-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    print(name+': passed',flush=True)


def job_wait(job):
    end=time.monotonic()+180
    while time.monotonic()<end:
        value=request('GET','/jobs/'+job['job_id'])
        if value['status'] not in ('queued','running'): return value
        time.sleep(.5)
    raise TimeoutError('后台导入超时')


def catalog(ref):
    return request('GET','/image-services/detail?ref='+urllib.parse.quote(ref))


def commit(current,services,action='apply',**extra):
    return request('POST','/image-services/'+current['catalog_id']+'/commit',{
        **current,'base_revision':current['revision'],'services':services,'action':action,'acknowledge':True,**extra})


def script(name,text,role='startup'):
    return request('POST','/image-services/attachments?name='+urllib.parse.quote(name)+'&role='+role,raw=text.encode())


def main():
    legacy=job_wait(request('POST','/jobs/import',{'dir':context['bundle'],'defer_services':True}))
    assert legacy['status']=='succeeded',legacy
    original=catalog(context['old_image'])
    assert original['imports'] and original['state']=='unconfigured'
    assert original['imports'][0]['catalog']['legacy_sources']
    record('legacy-bundle-import-is-staged',{'job':legacy['job_id'],'imports':len(original['imports'])})

    ref=context['prefix']+'-python:v1'
    subprocess.run(['docker','tag','python:3.10-slim',ref],check=True)
    context['image']=ref
    (root/'context.json').write_text(json.dumps(context,indent=2))
    image_path=root/'fixtures'/'python-image.tar'
    subprocess.run(['docker','save','-o',str(image_path),ref],check=True)
    imported=job_wait(request('POST','/jobs/import',{'image_path':str(image_path),'defer_services':True}))
    assert imported['status']=='succeeded',imported
    current=catalog(ref)
    assert current['services']==[] and current['revision']==0
    record('generic-image-with-no-services-imports',{'ref':ref,'job':imported['job_id']})

    prepare=script('prepare.sh','#!/bin/sh\nprintf ready > /tmp/chb-ready\n')
    start=script('serve.sh','#!/bin/sh\n[ -f /tmp/chb-ready ] || exit 7\npython3 -m http.server "$1" --bind 0.0.0.0 >/tmp/chb-http.log 2>&1 &\necho "$!" > /tmp/chb-http.pid\n')
    checks={port:script('ready-'+str(port)+'.sh',"#!/bin/sh\npython3 -c \"import socket; socket.create_connection(('127.0.0.1',"+str(port)+"), 1).close()\"\n",'validate') for port in (8081,8082)}
    stop=script('stop.sh','#!/bin/sh\nkill "$(cat /tmp/chb-http.pid)"\n','shutdown')
    services=[{'id':'prepare','name':'初始化','software':'shell','ports':[],'modes':[{'id':'prepare-default','name':'准备目录','strategy':'script','default':True,'files':[prepare],'depends_on':[]}]},
              {'id':'http','name':'HTTP 服务','software':'Python','ports':[{'port':8081,'protocol':'tcp','address':'0.0.0.0'}],'modes':[]},
              {'id':'proxy','name':'代理服务','ports':[{'port':8081,'protocol':'tcp','address':'0.0.0.0'}],'modes':[{'id':'proxy-default','name':'手动代理','strategy':'manual','default':True,'files':[]}]},
              {'id':'ssh','name':'SSH','ports':[{'port':22,'protocol':'tcp'}],'modes':[{'id':'ssh-default','name':'手动维护','strategy':'manual','default':True,'files':[]}]}]
    for port in (8081,8082):
        services[1]['modes'].append({'id':'http-'+str(port),'name':'常规启动' if port==8081 else '备用端口','strategy':'script','default':port==8081,
                                    'interpreter':'sh','workdir':'/tmp','args':str(port),'env':[],
                                    'ports':None if port==8081 else [{'port':8082,'protocol':'tcp'}],
                                    'files':[start,checks[port],stop],'depends_on':[{'service_id':'prepare'}]})
    validated=request('POST','/image-services/'+current['catalog_id']+'/validate',{'services':services})
    assert not validated['issues'],validated
    current=commit(current,services,'save')
    assert current['state']=='pending' and not current.get('applied_target_version')
    assert request('GET','/targets/registry')==[]
    record('files-and-full-inventory-save-without-registration',{'revision':current['revision'],'services':len(services)})

    current=commit(current,services)
    assert current['state']=='applied',current
    version=current['applied_target_version']
    activation=request('GET','/versions/target/'+current['target_name']+'/'+str(version)+'/activation')
    assert activation['state']=='registered',activation
    available=request('GET','/instances/available-services')
    assert not any(name.startswith(('imgsvc-','imgmode-')) for name in available['system'])
    record('final-confirmation-registers-isolated-mode-implementations',{'version':version,'registered':activation['services']})

    missing=request('POST','/image-services/check/validate',{'services':services,'selection':{'http':'http-8081'}})
    conflict=request('POST','/image-services/check/validate',{'services':services,'selection':{'prepare':'prepare-default','http':'http-8081','proxy':'proxy-default'}})
    assert any('缺少依赖' in error for error in missing['errors'])
    assert any('端口冲突' in error for error in conflict['errors'])
    default={'prepare':'prepare-default','http':'http-8081'}
    request('POST','/instances',{'name':context['prefix']+'-template','runtime_type':'docker','image_tag':ref,
                               'target_name':current['target_name'],'target_version':version,'services':[],'service_selection':default})
    template=request('GET','/instances')[0]
    record('template-defaults-check-active-combination',{'template':template['name'],'revision':template['version']})

    definition={'name':'真实服务模式验收','schema_version':2,'extension':{'preserved':True},'nodes':[
        {'id':1,'name':'常规节点','instance':template['name'],'instance_version':template['version'],'x':120,'y':150},
        {'id':2,'name':'备用节点','instance':template['name'],'instance_version':template['version'],'x':400,'y':150,
         'service_overrides':{'prepare':'prepare-default','http':'http-8082'}}],
        'links':[{'node1_id':1,'node2_id':2,'iface1':{'id':0,'ip4':'10.77.0.1','ip4_mask':24},'iface2':{'id':0,'ip4':'10.77.0.2','ip4_mask':24}}]}
    topology=request('POST','/topologies',{'definition':definition})
    deployment=request('POST','/topologies/'+topology['topology_id']+'/deploy',{'revision':topology['revision'],'start':True})
    sid=deployment['core_sid']
    for source,address,port in [(1,'10.77.0.2',8082),(2,'10.77.0.1',8081)]:
        code="import urllib.request; print(urllib.request.urlopen('http://"+address+':'+str(port)+"/',timeout=5).status)"
        response=request('POST','/api/sessions/'+str(sid)+'/nodes/'+str(source)+'/command',{'command':'python3 -c '+shlex.quote(code),'wait':True,'shell':True})
        assert response['return_code']==0 and '200' in response['output'],response
    snapshot=request('GET','/api/sessions/'+str(sid)+'/topology')
    actual={n['id']:n for n in snapshot['nodes']}
    assert next(s for s in actual[1]['effective_services'] if s['service_id']=='http')['mode_id']=='http-8081'
    assert next(s for s in actual[2]['effective_services'] if s['service_id']=='http')['mode_id']=='http-8082'
    assert request('GET','/instances')[0]['service_selection']==default
    record('two-real-containers-run-distinct-modes-with-dependency-order',{'sid':sid,'deployment':deployment['deployment_id'],'http_status':[200,200]})

    pending_services=copy.deepcopy(services);pending_services[1]['description']='待下一次生效的说明'
    pending=commit(current,pending_services)
    assert pending['state']=='pending' and pending['applied_target_version']==version
    assert request('GET','/targets/registry')[0]['version']==version
    history=request('GET','/deployments')
    frozen=next(d for d in history if d['deployment_id']==deployment['deployment_id'])['execution_definition']
    assert next(s for n in frozen['nodes'] if n['id']==2 for s in n['effective_services'] if s['service_id']=='http')['mode_id']=='http-8082'
    record('existing-sessions-save-pending-and-keep-frozen-execution',{'revision':pending['revision'],'applied_version':version})
    request('DELETE','/api/sessions/'+str(sid))

    archive=request('POST','/image-services/export/config',{'catalog':{**current,'services':services}})
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        assert 'catalog.json' in package.namelist() and any(name.startswith('scripts/') for name in package.namelist())
    preview=request('POST','/image-services/import/preview?ref='+urllib.parse.quote(ref),raw=archive)
    assert all(row['action']=='skip' for row in preview['rows'])
    before=catalog(ref)['revision']
    merged=request('POST','/image-services/import/merge',{'catalog':current,'preview':preview})
    assert merged['catalog']['services']==current['services'] and catalog(ref)['revision']==before
    record('configuration-and-script-export-import-roundtrip',{'zip_bytes':len(archive),'conflicts':len(preview['rows'])})

    export=request('POST','/image-services/export/full',{'ref':ref})
    deadline=time.monotonic()+180
    while export['state'] in ('queued','running') and time.monotonic()<deadline:
        time.sleep(.5);export=request('GET','/image-services/exports/'+export['job_id'])
    assert export['state']=='succeeded',export
    bundle=request('GET',export['download'])
    with tarfile.open(fileobj=io.BytesIO(bundle),mode='r:gz') as package:
        assert {'image.tar','catalog.json'}<=set(package.getnames())
    record('complete-image-package-exports-real-docker-image',{'bytes':len(bundle),'job_id':export['job_id']})

    broken=copy.deepcopy(pending_services)
    broken.append({'id':'missing-executable','name':'注册故障用例','ports':[],'modes':[{'id':'missing-mode','name':'缺少宿主执行器','strategy':'legacy','implementation':'missing-executable','default':True,'files':[]}]})
    pending['legacy_sources']={'fault.py':'from core.services.base import CoreService\nclass MissingExecutable(CoreService):\n name="missing-executable"\n group="Acceptance"\n executables=["chb_nonexistent_executable_987654"]\n'}
    failed=commit(pending,broken)
    assert failed['state']=='failed' and failed['failed_stage']=='registering' and failed['applied_target_version']==version,failed
    failed_version=failed['pending_target_version']
    repeated=commit(failed,broken)
    assert repeated['state']=='failed' and repeated['pending_target_version']==failed_version
    assert request('GET','/targets/registry')[0]['version']==version
    repaired=copy.deepcopy(repeated)
    repaired['legacy_sources']['fault.py']=repaired['legacy_sources']['fault.py'].replace('executables=["chb_nonexistent_executable_987654"]','executables=[]')
    final=commit(repaired,broken)
    assert final['state']=='applied',final
    record('real-registration-failure-preserves-old-version-and-retry-state',{'failed_version':failed_version,'repaired_version':final['applied_target_version']})
    (root/'browser-context.json').write_text(json.dumps({'ref':ref,'catalog_id':current['catalog_id'],'template':template['name'],'topology_id':topology['topology_id'],'legacy_ref':context['old_image']},ensure_ascii=False,indent=2))


try:
    main()
except Exception as error:
    results.append({'status':'failed','error':str(error),'traceback':traceback.format_exc()})
    (root/'image-services-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
    raise
