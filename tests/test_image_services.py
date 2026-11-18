"""Inventory/API persistence and execution contracts for the management redesign."""
import copy
import io
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import image_services as images
import repository as repo
import target_admin as ta
import versions


@pytest.fixture
def image_api(client, mock_bridge, monkeypatch):
    raw={'Id':'sha256:'+'a'*64,'Size':1024,'Os':'linux','Architecture':'amd64','Config':{},'RepoTags':['test-image:v1']}
    monkeypatch.setattr(ta,'_docker',lambda command,timeout=120:(json.dumps([raw]),0))
    calls=[]
    monkeypatch.setattr('core_adapter.activate',lambda bridge,directories,required:(calls.append((directories,required)) or {'missing':[]}))
    mock_bridge.get_sessions.return_value=[]
    catalog=client.get('/image-services/detail',params={'ref':'test-image:v1'}).json()['data']
    return client,catalog,calls,mock_bridge


def service(sid='web',port=80,dependencies=None):
    return {'id':sid,'name':sid,'software':'1','ports':[{'port':port,'protocol':'tcp','address':'0.0.0.0'}],
            'modes':[{'id':sid+'-normal','name':'常规','strategy':'manual','default':True,'env':[],
                      'ports':None,'files':[],'depends_on':dependencies or []}]}


def save(client,catalog,services,action='save',**extra):
    return client.post('/image-services/'+catalog['catalog_id']+'/commit',json={
        **catalog,'services':services,'base_revision':catalog['revision'],'action':action,'acknowledge':True,**extra})


def test_staging_and_confirmation_preserve_optimistic_revision(image_api):
    client,catalog,calls,bridge=image_api
    staged=save(client,catalog,[service()]).json()['data']
    assert staged['state']=='pending' and calls==[]
    stale=save(client,catalog,[service('other')])
    assert stale.status_code==409
    applied=save(client,staged,[service()],'apply',request_id='one-confirmation').json()['data']
    assert applied['state']=='applied' and len(calls)==1
    repeated=save(client,staged,[service()],'apply',request_id='one-confirmation').json()['data']
    assert repeated['revision']==applied['revision'] and len(calls)==1
    assert client.get('/targets/registry').json()['data'][0]['version']==applied['applied_target_version']


def test_existing_sessions_and_incomplete_modes_are_saved_not_activated(image_api):
    client,catalog,calls,bridge=image_api
    bridge.get_sessions.return_value=[SimpleNamespace(id=1)]
    current=save(client,catalog,[service()],'apply').json()['data']
    assert current['state']=='pending' and not calls
    broken=service();broken['modes'][0]['strategy']='script'
    result=save(client,current,[broken],'apply').json()['data']
    assert result['state']=='incomplete' and '缺少启动脚本' in result['issues'][0]
    assert calls==[]


def test_activation_lists_only_sessions_that_block_registration(image_api):
    client, catalog, calls, bridge = image_api
    stopped = SimpleNamespace(id=1, state=1)
    bridge.get_sessions.return_value = [stopped, SimpleNamespace(id=2, state=4)]
    blockers = [{'id': 2, 'state': 4}]
    assert client.get('/image-services/activation-status').json()['data']['blockers'] == blockers
    pending = save(client, catalog, [service()], 'apply').json()['data']
    assert pending['activation_blockers'] == blockers and not calls
    bridge.get_sessions.return_value = [stopped]
    applied = save(client, pending, [service()], 'apply').json()['data']
    assert applied['state'] == 'applied' and len(calls) == 1
    assert 'activation_blockers' not in applied


def test_failed_registration_reuses_saved_revision_and_preserves_last_applied(image_api,monkeypatch):
    client,catalog,calls,bridge=image_api
    first=save(client,catalog,[service()],'apply').json()['data']
    monkeypatch.setattr('core_adapter.activate',lambda *args: {'missing':['unavailable']})
    second_service=service();second_service['modes'][0]['name']='第二种显示名'
    failed=save(client,first,[second_service],'apply').json()['data']
    assert failed['state']=='failed' and failed['applied_target_version']==first['applied_target_version']
    before=len(repo.history('target',catalog['target_name']))
    monkeypatch.setattr('core_adapter.activate',lambda *args: {'missing':[]})
    retried=save(client,failed,[second_service],'apply').json()['data']
    assert retried['state']=='applied'
    assert len(repo.history('target',catalog['target_name']))==before


def test_port_scopes_dependencies_and_cycles_are_checked_only_when_enabled():
    web=service('web');proxy=service('proxy');cache=service('cache',6379)
    catalog={'services':[web,proxy,cache]}
    assert not images.combination(catalog,{'web':'web-normal'})['errors']
    assert images.combination(catalog,{'web':'web-normal','proxy':'proxy-normal'})['errors']
    proxy['ports'][0]['protocol']='udp'
    assert not images.combination(catalog,{'web':'web-normal','proxy':'proxy-normal'})['errors']
    proxy['ports'][0].update(protocol='tcp',address='127.0.0.2')
    web['ports'][0]['address']='127.0.0.1'
    assert not images.combination(catalog,{'web':'web-normal','proxy':'proxy-normal'})['errors']
    web['modes'][0]['depends_on']=[{'service_id':'cache'}]
    assert any('缺少依赖' in error for error in images.combination(catalog,{'web':'web-normal'})['errors'])
    selected={'web':'web-normal','cache':'cache-normal'}
    assert images.combination(catalog,selected)['order']==['cache','web']
    cache['modes'][0]['depends_on']=[{'service_id':'web'}]
    assert any('依赖循环' in error for error in images.combination(catalog,selected)['errors'])


def test_script_bytes_survive_core_generation_and_execute(image_api,tmp_path):
    client,catalog,calls,bridge=image_api
    marker=tmp_path/'result.txt'
    text="#!/bin/sh\nprintf '%s' '${literal}' > "+str(marker)+"\n"
    uploaded=client.post('/image-services/attachments?name=start.sh&role=startup',content=text.encode()).json()['data']
    entry=service();mode=entry['modes'][0]
    mode.update(strategy='script',interpreter='sh',workdir=str(tmp_path),files=[uploaded])
    applied=save(client,catalog,[entry],'apply').json()['data']
    assert applied['state']=='applied'
    target=repo.revision('target',catalog['target_name'],applied['applied_target_version'])
    selected,directory=images.execution(target,{'web':'web-normal'})
    from core.services.manager import ServiceManager
    manager=ServiceManager();assert manager.load(Path(directory))==[]
    cls=manager.get_service(selected[0]['execution_name'])
    class Node:
        name='script-test'
        def create_dir(self,path): Path(path).mkdir(parents=True,exist_ok=True)
        def create_file(self,path,content): Path(path).write_text(content)
        def cmd(self,command,**kwargs): subprocess.run(command,shell=True,check=True,cwd=tmp_path)
    try:
        cls(Node()).start()
        assert marker.read_text()=='${literal}'
    finally:
        for path in cls.directories: shutil.rmtree(path,ignore_errors=True)
    exported=client.post('/image-services/export/config',json={'catalog':{**catalog,'services':[entry]}})
    assert exported.status_code==200
    preview=client.post('/image-services/import/preview',params={'ref':'test-image:v1'},content=exported.content).json()['data']
    assert preview['rows'][0]['incoming']['modes'][0]['files'][0]['sha256']==uploaded['sha256']
    assert client.get('/image-services/attachments/'+uploaded['sha256']).content==text.encode()


def test_import_skip_and_add_remap_dependencies_without_changing_saved_catalog(image_api):
    client,catalog,calls,bridge=image_api
    cache=service('cache',6379);web=service('web',80,[{'service_id':'cache','mode_id':'cache-normal'}])
    preview={'rows':[{'incoming':s,'action':'add'} for s in [cache,web]],'legacy_sources':{},'legacy_templates':{}}
    response=client.post('/image-services/import/merge',json={'catalog':catalog,'preview':preview})
    assert response.status_code==200
    merged=response.json()['data']['catalog']
    chosen={s['id']:s['modes'][0]['id'] for s in merged['services']}
    assert not images.combination(merged,chosen)['errors']
    assert repo.read('image_catalogs',catalog['catalog_id']) is None and not calls
    skipped=client.post('/image-services/import/merge',json={'catalog':merged,'preview':{'rows':[{'incoming':web,'action':'skip'}],'legacy_sources':{'broken.py':'not parsed on skip'}}}).json()['data']['catalog']
    assert skipped==merged


def test_template_defaults_node_overrides_and_unknown_extensions_roundtrip(image_api):
    client,catalog,calls,bridge=image_api
    entry=service();entry['modes'].append({**copy.deepcopy(entry['modes'][0]),'id':'web-alt','name':'另一模式','default':False})
    applied=save(client,catalog,[entry],'apply').json()['data']
    response=client.post('/instances',json={'name':'web-template','runtime_type':'docker','image_tag':'test-image:v1','target_name':catalog['target_name'],'target_version':applied['applied_target_version'],'service_selection':{'web':'web-normal'},'services':[]})
    assert response.status_code==200,response.text
    template=repo.revision('template','web-template')
    definition={'name':'two-nodes','extension':{'preserve':True},'nodes':[
        {'id':1,'name':'A','instance':'web-template','instance_version':template['version'],'service_overrides':{'web':'web-alt'},'extension':['retained']},
        {'id':2,'name':'B','instance':'web-template','instance_version':template['version']}],'links':[]}
    saved=client.post('/topologies',json={'definition':definition})
    assert saved.status_code==201,saved.text
    normalized=saved.json()['data']['definition']
    assert normalized['extension']=={'preserve':True} and normalized['nodes'][0]['extension']==['retained']
    a,_=versions.resolve('web-template',template['version'],normalized['nodes'][0]['service_overrides'])
    b,_=versions.resolve('web-template',template['version'])
    assert a['effective_services'][0]['mode_id']=='web-alt'
    assert b['effective_services'][0]['mode_id']=='web-normal'
    assert a['services']!=b['services']
    assert repo.revision('template','web-template')['service_selection']=={'web':'web-normal'}


def test_legacy_shared_implementation_can_be_grouped_without_overwriting_native_services(image_api):
    client,catalog,calls,bridge=image_api
    source='from core.services.base import CoreService\nclass Existing(CoreService):\n name="SSH"\n group="legacy"\n startup=[]\n'
    legacy=service();legacy['modes'][0].update(strategy='legacy',implementation='SSH')
    legacy['modes'].append({**copy.deepcopy(legacy['modes'][0]),'id':'maintenance','name':'维护模式','default':False})
    catalog['legacy_sources']={'existing.py':source}
    result=save(client,catalog,[legacy],'apply').json()['data']
    assert result['state']=='applied'
    target=repo.revision('target',catalog['target_name'],result['applied_target_version'])
    from core.services.manager import ServiceManager
    from core.services.base import CoreService
    class NativeSSH(CoreService):
        name='SSH'
        group='Native test fixture'
        executables=[]
    for mode in legacy['modes']:
        selected,directory=images.execution(target,{'web':mode['id']})
        manager=ServiceManager();manager.load_locals()
        if 'SSH' not in manager.services: manager.add(NativeSSH)
        native=manager.get_service('SSH')
        assert manager.load(Path(directory))==[]
        assert manager.get_service('SSH') is native
        assert manager.get_service(selected[0]['execution_name']).group=='Image services'
