from types import SimpleNamespace

import pytest
from core.api.grpc.wrappers import NodeType

import network_activity as network
import repository as repo


def test_preflight_records_each_failed_item_without_starting_workers(client, mock_bridge, monkeypatch):
    plan = network.Plan.model_validate({
        'traffic': [{'id':'http', 'name':'网页流量', 'protocol':'http', 'source_id':1,
                     'target_id':2, 'target_link':[1, 2], 'port':80, 'rate':2, 'duration':1},
                    {'id':'tcp', 'name':'TCP 流量', 'protocol':'tcp', 'source_id':2,
                     'target_id':1, 'target_link':[1, 2], 'port':5201, 'rate':1000, 'duration':1}],
        'probes': [{'id':'snort', 'name':'告警', 'node_id':1, 'link':[1, 3]}],
    })
    definition = {'links': [{'node1_id':1, 'node2_id':2, 'iface1':{'id':0, 'ip4':'10.90.0.1'},
                             'iface2':{'id':0}}]}

    def incompatible(kind, tools, ruleset):
        assert kind == 'iperf3'
        raise ValueError('缺少 --json-stream')

    monkeypatch.setattr(network, '_preflight_tool', incompatible)
    errors = network.preflight(plan, definition)
    assert '缺少 IPv4' in errors['http']
    assert '--json-stream' in errors['tcp']
    assert '链路接口' in errors['snort']
    record = {'core_sid':12, 'drill_id':'drill-test', 'deployment_id':'deploy-test',
              'execution_definition':definition}
    repo.put('deployments', 'deploy-test', record)
    result = network.start(record, mock_bridge, plan, errors)
    assert set(result['failed_items']) == set(errors)
    mock_bridge.get_session.assert_not_called()
    history = client.get('/deployments/deploy-test/network-activity').json()['data']
    items = history['runs'][0]['items']
    assert {key: item['status'] for key, item in items.items()} == dict.fromkeys(errors, 'failed')
    assert {key: item['error'] for key, item in items.items()} == errors


def test_network_initialization_error_is_returned_and_persisted(client, mock_bridge, monkeypatch):
    import topologies

    record = {'core_sid':12, 'drill_id':'drill-test', 'deployment_id':'deploy-test',
              'execution_definition':{'network_activity':{'probes': [
                  {'id':'snort', 'name':'告警', 'node_id':1, 'link':[1, 2]},
              ]}}}
    monkeypatch.setattr(topologies, 'prepare_start', lambda sid: record)
    monkeypatch.setattr(topologies, 'for_session', lambda sid: record)
    monkeypatch.setattr(topologies, 'state', lambda *args: None)
    monkeypatch.setattr(network, 'preflight', lambda *args: {})

    def fail(*args):
        raise OSError('无法创建运行目录')

    monkeypatch.setattr(network, 'start', fail)
    mock_bridge.get_session.return_value.nodes = {}
    mock_bridge.start_session.return_value = (True, [])
    outcome = client.post('/api/sessions/12/start', json={}).json()
    assert outcome['result'] is True
    assert '无法创建运行目录' in outcome['network_activity']['error']
    data = client.get('/deployments/deploy-test/network-activity').json()['data']
    assert data['startup_error'] == outcome['network_activity']['error']


def test_batches_and_alert_pages_scope_queries_and_export_only_matching_records(client):
    import csv
    import io

    import platform_db as db

    deployment = {'core_sid':12,'drill_id':'drill-test','deployment_id':'deploy-test','status':'closed'}
    repo.put('deployments', 'deploy-test', deployment)
    for index in range(3):
        run = {**deployment,'run_id':f'run-{index}','created_at':f'2026-11-18 10:00:0{index}','items':{}}
        repo.put(network.RUN_COLLECTION, run['run_id'], run)
    base = '/deployments/deploy-test/network-activity'
    page = client.get(base + '/batches', params={'limit':2,'offset':1}).json()['data']
    assert [item['run_id'] for item in page['items']] == ['run-1','run-0']
    assert page['run']['run_id'] == 'run-1' and not page['has_more']

    for run_id, probe, message in (
        ('run-0','one','old batch'), ('run-1','two','other probe'), ('run-1','one','tracked A'),
        ('run-1','one','tracked B'), ('run-1','one','=tracked formula'),
    ):
        db.record_event(12, {'type':'snort','run_id':run_id,'probe_id':probe,'msg':message}, 'drill-test')
    path = base + '/run-1/alerts'
    filters = {'probe':'one','search':'tracked','start':'2020-01-01T00:00:00'}
    first = client.get(path, params={**filters,'limit':2}).json()['data']
    second = client.get(path, params={**filters,'limit':2,'before_id':first['next_before_id']}).json()['data']
    assert [row['detail']['msg'] for row in first['items']] == ['=tracked formula','tracked B']
    assert [row['detail']['msg'] for row in second['items']] == ['tracked A']
    assert second['next_before_id'] is None
    exported = client.get(path + '.csv', params=filters)
    assert exported.status_code == 200 and exported.headers['content-type'].startswith('text/csv')
    assert [row['提示'] for row in csv.DictReader(io.StringIO(exported.text.lstrip('\ufeff')))] == [
        "'=tracked formula", 'tracked B', 'tracked A',
    ]


@pytest.mark.parametrize('protocol', ['http', 'tcp', 'udp'])
def test_validation_accepts_docker_for_traffic_and_snort(protocol):
    plan = network.Plan.model_validate({
        'traffic': [{'id':'flow','name':'流量','protocol':protocol,'source_id':1,'target_id':2,
                     'target_link':[1,2],'port':8080,'rate':5,'duration':1}],
        'probes': [{'id':'probe','name':'探针','node_id':2,'link':[1,2]}],
    })
    definition = {'links':[{'node1_id':1,'node2_id':2}]}
    network.validate(plan, definition, {1:'docker', 2:'docker'})


def test_docker_namespace_checks_topology_binding_and_reports_per_node(monkeypatch, mock_bridge):
    nodes = {index:SimpleNamespace(type=NodeType.DOCKER, name=f'node{index}') for index in (1, 2)}
    session = SimpleNamespace(id=41, nodes=nodes)
    plan = network.Plan.model_validate({'probes':[
        {'id':'probe','name':'探针','node_id':1,'link':[1,2]},
        {'id':'other','name':'其他探针','node_id':2,'link':[1,2]},
    ]})
    record = {'bindings':{'1':{'runtime_name':'node1'},'2':{'runtime_name':'claimed-name'}}}
    known = {'1':{'name':'node1','pid':20}}
    monkeypatch.setattr('core_adapter.docker_node_processes', lambda bridge, sid, ids: known)
    reference = object()
    monkeypatch.setattr(network, '_bind_docker_namespace', lambda name, process: reference)
    namespaces, errors = network._docker_namespaces(record, session, mock_bridge, plan.probes)
    assert namespaces == {1:reference}
    assert set(errors) == {2}


def test_docker_namespace_never_opens_when_process_identity_has_changed(monkeypatch):
    opened = []
    monkeypatch.setattr(network, '_process_start', lambda pid: 'new-process')
    monkeypatch.setattr(network.os, 'open', lambda *args: opened.append(args))
    namespace = network.DockerNamespace(123, 'old-process', (3, 4))
    with pytest.raises(ValueError, match='已退出或重建'):
        namespace.open()
    assert not opened


def test_docker_namespace_rejects_a_host_network_container(monkeypatch):
    monkeypatch.setattr(network, '_process_start', lambda pid: 'start')
    monkeypatch.setattr(network, '_tool_output', lambda args:
                        '{"pid":42,"running":true,"sandbox":"/proc/42/ns/net","mode":"host"}')
    with pytest.raises(ValueError, match='独立的网络命名空间'):
        network._bind_docker_namespace('node1', {'name':'node1','pid':42})
