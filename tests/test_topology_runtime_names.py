"""User labels survive CORE's whitespace/path restrictions on native runtime names."""
from types import SimpleNamespace
from unittest.mock import patch
import repository as repo
import topologies


def test_direct_native_node_keeps_chinese_label_separate_from_runtime(client,mock_bridge):
    mock_bridge.add_node.return_value=7
    result=client.post('/api/sessions/41/nodes',json={'id':7,'name':'原生 主机 / 中文','type':0,'model':'bare','services':[]})
    assert result.status_code==201,result.text
    sent=mock_bridge.add_node.call_args.args[1]
    assert sent.name=='n7'
    binding=topologies.bindings(41)['7']
    assert binding['display_name']=='原生 主机 / 中文'
    assert binding['runtime_name']=='n7'


def test_runtime_names_cannot_collide_with_other_user_labels(client,mock_bridge):
    mock_bridge.create_session.return_value=SimpleNamespace(id=41)
    topo={'name':'中文拓扑','nodes':[
        {'id':1,'name':'中文节点','model':'bare','services':[]},
        {'id':2,'name':'n1','model':'bare','services':[]}], 'links':[]}
    saved=client.post('/topologies',json={'definition':topo}).json()['data']
    with patch('app._build_topology') as build:
        result=client.post('/topologies/'+saved['topology_id']+'/deploy',json={'revision':1,'start':False})
    assert result.status_code==201,result.text
    sent=build.call_args.args[1]
    assert [n.name for n in sent.nodes]==['n1','n2']
    run=result.json()['data']
    assert run['bindings']['1']['display_name']=='中文节点'
    assert run['bindings']['2']['display_name']=='n1'
    assert repo.read('topologies',saved['topology_id'])['definition']['nodes'][0]['name']=='中文节点'
