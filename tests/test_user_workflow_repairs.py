"""Regressions found by actual page use: CORE defaults, immutable history, and image size compatibility."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch
import pytest
import platform_db as db
import repository as repo


def topology(mtu=0):
    return {'name':'用户命名的混合拓扑','nodes':[
        {'id':1,'name':'主机甲','type':'default','model':'bare','services':[]},
        {'id':2,'name':'主机乙','type':'default','model':'bare','services':[]}],
        'links':[{'node1_id':1,'node2_id':2,'iface1':{'id':0,'mtu':mtu,'ip4':'10.77.0.1','ip4_mask':24},
                  'iface2':{'id':0,'mtu':0,'ip4':'10.77.0.2','ip4_mask':24},'vendor_extension':{'keep':True}}]}


@pytest.mark.parametrize('mtu',[0,None,68,1500,65535])
def test_core_default_and_valid_mtu_survive_save_and_reopen(client,mtu):
    response=client.post('/topologies',json={'definition':topology(mtu)})
    assert response.status_code==201,response.text
    saved=response.json()['data']
    reopened=client.get('/topologies/'+saved['topology_id']).json()['data']['definition']
    assert reopened['links'][0]['iface1'].get('mtu')==mtu
    assert reopened['links'][0]['vendor_extension']=={'keep':True}


@pytest.mark.parametrize('mtu',[-1,10,65536])
def test_nonzero_invalid_mtu_still_rejected(client,mtu):
    assert client.post('/topologies',json={'definition':topology(mtu)}).status_code==400
    assert repo.read('topologies')=={}


def test_old_execution_recovers_name_after_saved_configuration_deleted_without_changing_frozen_data(client,mock_bridge):
    saved=client.post('/topologies',json={'definition':topology()}).json()['data']
    frozen=topology();frozen['name']='session-1'
    original={'deployment_id':'historical','core_sid':1,'drill_id':'closed-drill','topology_id':saved['topology_id'],
              'revision':1,'status':'closed','execution_definition':frozen,'source':frozen,'snapshot':frozen}
    repo.put('deployments','historical',original)
    client.delete('/topologies/'+saved['topology_id'])
    mock_bridge.get_sessions.return_value=[]
    history=client.get('/topology-workspace').json()['data']['deployments'][0]
    assert history['display_name']=='用户命名的混合拓扑'
    assert repo.read('deployments','historical')==original
    copy_definition=copy.deepcopy(history['execution_definition']);copy_definition['name']=history['display_name']
    response=client.post('/topologies',json={'definition':copy_definition})
    assert response.status_code==201,response.text
    assert response.json()['data']['definition']['links'][0]['iface2']['mtu']==0


def test_historical_name_uses_origin_revision_instead_of_later_rename(client,mock_bridge):
    saved=client.post('/topologies',json={'definition':topology()}).json()['data']
    newer=topology();newer['name']='后来改的名字'
    client.put('/topologies/'+saved['topology_id'],json={'definition':newer,'revision':1})
    repo.put('deployments','old',{'deployment_id':'old','core_sid':1,'topology_id':saved['topology_id'],'revision':1,'status':'closed'})
    mock_bridge.get_sessions.return_value=[]
    assert client.get('/topology-workspace').json()['data']['deployments'][0]['display_name']=='用户命名的混合拓扑'


def test_legacy_captured_topology_name_survives_deletion(client,mock_bridge):
    saved=client.post('/topologies',json={'definition':topology()}).json()['data']
    repo.put('deployments','captured',{'deployment_id':'captured','core_sid':1,'captured_topology_id':saved['topology_id'],
                                    'status':'closed','source':{'name':'session-1'}})
    client.delete('/topologies/'+saved['topology_id']);mock_bridge.get_sessions.return_value=[]
    assert client.get('/topology-workspace').json()['data']['deployments'][0]['display_name']=='用户命名的混合拓扑'


def test_new_deployment_and_live_snapshot_keep_user_name(client,mock_bridge):
    saved=client.post('/topologies',json={'definition':topology()}).json()['data']
    mock_bridge.create_session.return_value=SimpleNamespace(id=41)
    mock_bridge.get_session.return_value=SimpleNamespace(nodes={},links=[],options={})
    with patch('app._build_topology'):
        response=client.post('/topologies/'+saved['topology_id']+'/deploy',json={'revision':1,'start':False})
    assert response.status_code==201,response.text
    assert response.json()['data']['topology_name']=='用户命名的混合拓扑'
    assert client.get('/api/sessions/41/topology').json()['name']=='用户命名的混合拓扑'


def test_capture_only_adds_presentation_metadata_and_keeps_original_execution(client,mock_bridge):
    drill=db.issue_drill(41,identity='test-identity:41')
    frozen=topology();frozen['name']='session-41'
    repo.put('deployments','live',{'deployment_id':'live','core_sid':41,'drill_id':drill,'status':'running','execution_definition':frozen})
    mock_bridge.get_session.return_value=SimpleNamespace(state=4)
    response=client.post('/topology-workspace/sessions/41/capture',json={'identity':'test-identity:41','definition':topology()})
    assert response.status_code==200,response.text
    run=repo.read('deployments','live')
    assert run['captured_topology_name']=='用户命名的混合拓扑'
    assert run['captured_topology_revision']==1
    assert run['execution_definition']==frozen


def test_image_list_adds_detail_byte_count_and_preserves_legacy_size_string(client,monkeypatch):
    import target_admin as ta
    def docker(command,timeout):
        if command.startswith('images '):
            return json.dumps({'Repository':'fixture','Tag':'v1','ID':'image-id','Size':'186MB'}),0
        if command.startswith('image inspect '):
            return json.dumps({'RepoTags':['fixture:v1'],'Size':45137148}),0
        raise AssertionError(command)
    monkeypatch.setattr(ta,'_docker',docker)
    monkeypatch.setattr(ta,'_manifest_index',lambda:{'fixture:v1':{'name':'fixture','image':{'name':'fixture','tag':'v1'},'services':[]}})
    item=client.get('/targets').json()['data']['images'][0]
    assert item['size']=='186MB'
    assert item['size_bytes']==45137148


def test_missing_image_inspection_keeps_existing_image_list_usable(client,monkeypatch):
    import target_admin as ta
    monkeypatch.setattr(ta,'_docker',lambda command,timeout:(json.dumps({'Repository':'fixture','Tag':'v1','Size':'186MB'}),0) if command.startswith('images ') else ('image disappeared',1))
    monkeypatch.setattr(ta,'_manifest_index',lambda:{'fixture:v1':{'name':'fixture','image':{'name':'fixture','tag':'v1'},'services':[]}})
    response=client.get('/targets')
    assert response.status_code==200
    assert response.json()['data']['images'][0]['size']=='186MB'
