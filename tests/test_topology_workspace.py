"""Workspace behavior across saved configurations, live identities and retained history."""
from types import SimpleNamespace

import pytest
import platform_db as db
import repository as repo


def summary(sid=41, state=4):
    return SimpleNamespace(id=sid, state=state, nodes=2, file='', dir='/test/session')


def definition():
    return {'name':'原生拓扑', 'nodes':[
        {'id':1,'name':'a','type':'default','model':'bare','services':[]},
        {'id':2,'name':'b','type':'default','model':'bare','services':[]}],
        'links':[{'node1_id':1,'node2_id':2,'iface1':{'id':0,'ip4':'10.90.0.1','ip4_mask':24},
                  'iface2':{'id':0,'ip4':'10.90.0.2','ip4_mask':24}}],
        'extension':{'keep':'unchanged'}}


def test_workspace_shows_unbound_live_session_without_creating_saved_topology(client, mock_bridge):
    mock_bridge.get_sessions.return_value=[summary()]
    result=client.get('/topology-workspace').json()['data']
    assert result['topologies']==[]
    assert result['sessions'][0]['state']==4
    assert result['sessions'][0]['drill_id'] is None
    assert result['sessions'][0]['identity']=='test-identity:41'
    assert repo.read('topologies')=={}
    assert repo.read('deployments')=={}


def test_workspace_uses_actual_state_and_never_matches_reused_session_number(client, mock_bridge):
    db.issue_drill(41,identity='old-entity')
    old=db.get_active_drill(41)
    db.close_drill(41)
    current=db.issue_drill(41,identity='test-identity:41')
    repo.put('deployments','old',{'deployment_id':'old','core_sid':41,'drill_id':old,'status':'running'})
    repo.put('deployments','new',{'deployment_id':'new','core_sid':41,'drill_id':current,'status':'running'})
    mock_bridge.get_sessions.return_value=[summary(state=1)]
    result=client.get('/topology-workspace').json()['data']
    runs={r['deployment_id']:r for r in result['deployments']}
    assert runs['old']['live'] is False
    assert runs['old']['current_status']=='历史记录'
    assert runs['new']['live'] is True
    assert runs['new']['current_state']==1
    assert runs['new']['current_status']=='未启动'
    assert repo.read('deployments','old')['status']=='running'  # Historical facts are not rewritten by a GET.


def test_workspace_does_not_trust_stale_database_identity(client, mock_bridge):
    drill=db.issue_drill(41,identity='old-entity')
    repo.put('deployments','stale',{'deployment_id':'stale','core_sid':41,'drill_id':drill,'status':'running'})
    mock_bridge.get_sessions.return_value=[summary()]
    result=client.get('/topology-workspace').json()['data']
    assert not result['deployments'][0]['live']
    assert result['sessions'][0]['drill_id'] is None


def test_saved_topologies_and_history_remain_available_when_core_is_offline(client, mock_bridge):
    saved=client.post('/topologies',json={'definition':definition()}).json()['data']
    repo.put('deployments','past',{'deployment_id':'past','status':'running','core_sid':41})
    mock_bridge.get_sessions.side_effect=ConnectionError('offline')
    result=client.get('/topology-workspace').json()['data']
    assert result['topologies'][0]['topology_id']==saved['topology_id']
    assert result['runtime_error']
    assert result['deployments'][0]['current_status']=='状态待确认'
    assert result['sessions']==[]


@pytest.mark.parametrize('action',['stop','delete','connectivity','capture'])
def test_obsolete_view_cannot_operate_on_reused_session(client, mock_bridge, action):
    result=client.post('/topology-workspace/sessions/41/'+action,json={'identity':'old-entity','definition':definition()})
    assert result.status_code==409
    mock_bridge.get_session.assert_not_called()
    mock_bridge.stop_session.assert_not_called()
    mock_bridge.delete_session.assert_not_called()
    assert repo.read('topologies')=={}


def test_capture_links_running_session_to_saved_configuration_without_changing_execution(client, mock_bridge):
    drill=db.issue_drill(41,identity='test-identity:41')
    frozen=definition()
    repo.put('deployments','live',{'deployment_id':'live','core_sid':41,'drill_id':drill,'status':'running','execution_definition':frozen})
    mock_bridge.get_session.return_value=summary()
    mock_bridge.get_sessions.return_value=[summary()]
    edited=definition(); edited['name']='另存拓扑'
    response=client.post('/topology-workspace/sessions/41/capture',json={'identity':'test-identity:41','definition':edited})
    assert response.status_code==200,response.text
    saved=response.json()['data']
    run=repo.read('deployments','live')
    assert run['execution_definition']==frozen
    assert not run.get('topology_id')
    assert run['captured_topology_id']==saved['topology_id']
    workspace=client.get('/topology-workspace').json()['data']
    assert workspace['deployments'][0]['saved_topology_id']==saved['topology_id']
    assert workspace['deployments'][0]['live']


@pytest.mark.parametrize('iface,options',[
    ({'ip4':'bad-address'},{}), ({'ip4':'::1'},{}), ({'ip4_mask':33},{}),
    ({'ip6_mask':129},{}), ({'id':-1},{}), ({'mtu':10},{}),
    ({},{'delay':-5}), ({},{'loss':101}), ({},{'dup':-1}),
])
def test_invalid_link_settings_rejected_before_saving(client, iface, options):
    topo=definition();topo['links'][0]['iface1']=iface;topo['links'][0]['options']=options
    response=client.post('/topologies',json={'definition':topo})
    assert response.status_code==400,response.text
    assert repo.read('topologies')=={}


def test_link_edit_round_trip_retains_extensions_and_zero_mask(client):
    topo=definition();topo['nodes'][0]['type']='PC'
    topo['links'][0]['iface1']['ip4_mask']=0
    topo['links'][0]['extension']={'vendor':'keep'}
    topo['links'][0]['options']={'delay':100,'loss':0.5,'unidirectional':True}
    created=client.post('/topologies',json={'definition':topo})
    assert created.status_code==201,created.text
    record=created.json()['data']
    loaded=client.get('/topologies/'+record['topology_id']).json()['data']['definition']
    assert loaded['extension']==topo['extension']
    assert loaded['links'][0]['extension']=={'vendor':'keep'}
    assert loaded['links'][0]['iface1']['ip4_mask']==0
    assert loaded['links'][0]['options']['unidirectional'] is True
