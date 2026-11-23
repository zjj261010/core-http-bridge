"""Persistence and callback batching for the fixed HTTP validation workflow."""
import json
import time

import pytest

import platform_db
import repository as repo
import validation_sequences as validation


def seed_task():
    validation.initialize()
    drill = platform_db.issue_drill(41, identity='session-one')
    task = {'task_id': 'task-one', 'scenario_id': '41', 'sequence_id': 'test-one'}
    context = {'source_id': 1, 'target_id': 2, 'port': 8080, 'run_id': 'probe-run',
               'drill_id': drill, 'observation_start': 1000, 'observation_end': 1002, 'cursor': 0,
               'probes': {'p': {'status': 'running', 'metrics': {'drops': 0}}}}
    with repo.transaction() as connection:
        connection.execute('INSERT INTO validation_tasks VALUES(?,?,?,?,?,?,?,?)',
                           (task['task_id'], task['scenario_id'], task['sequence_id'], 'deploy-test',
                            'executing', 'digest', json.dumps(context), time.time()))
        for seq, state in ((1, 'SUCCESS'), (2, 'running'), (3, 'pending')):
            body = {'request': {'seq': seq, 'action_name': 'HttpServiceCheck', 'target_node': {'ip': '10.0.0.2'}}}
            if seq == 1:
                body.update(started=1000.1, ended=1000.4, duration=0.3,
                            result=dict(exit_code=0, source_ip='10.0.0.1', source_port=40001, http_status=200))
            if seq == 2:
                body.update(started=1001.3)
            connection.execute('INSERT INTO validation_steps VALUES(?,?,?,?)',
                               (task['task_id'], seq, state, json.dumps(body)))
    return task, context


def deliveries():
    with repo.connection() as connection:
        return [dict(row) for row in connection.execute('SELECT * FROM validation_deliveries ORDER BY batch_no')]


def test_recovery_keeps_completed_steps_without_replaying_uncertain_work():
    task, _ = seed_task()
    validation._recover_task(task['task_id'])
    reports = deliveries()
    steps = json.loads(reports[0]['payload'])['results']
    assert [step['actual'] for step in steps] == ['SUCCESS', 'FAIL', 'SKIPPED']
    assert steps[1]['reason'] == 'interrupted_outcome_unknown'
    assert steps[1]['exit_code'] is None and steps[1]['ended_at'] is None
    assert steps[2]['reason'] == 'bridge_interrupted'
    assert json.loads(reports[1]['payload'])['is_last']
    validation._recover_task(task['task_id'])
    assert deliveries() == reports


def test_mapping_catalog_readonly_and_reference_action_accept(client, monkeypatch):
    """动作映射字典只读（编辑端点 403）；参考字典 9 条 + HttpServiceCheck 可被 IF-01 受理；版本钉定不变。"""
    validation.initialize()
    monkeypatch.setenv('BRIDGE_VALIDATION_TOKEN', 'test-token')
    monkeypatch.setattr(validation, '_bind', lambda *_: {
        'deployment_id': 'deploy-test', 'source_id': 1, 'target_id': 2,
    })
    mapping_route = '/validation/action-mappings'
    incoming = '/api/v1/attack-sequences'
    headers = {'Authorization': 'Bearer test-token'}
    default = client.get(mapping_route).json()['data']
    assert default['dictionary_version'] == 'validation/2'
    assert [item['action_name'] for item in default['items'] if 'simulation_steps' not in item] == ['HttpServiceCheck']

    # 编辑端点全部 403（字典只读）
    readonly = {'action_name': 'LabHttpCheck', 'label': '测试站点', 'description': '检查已配置的网页'}
    assert client.post(mapping_route, json=readonly).status_code == 403
    assert client.put(mapping_route + '/HttpServiceCheck', json=readonly).status_code == 403
    assert client.delete(mapping_route + '/HttpServiceCheck').status_code == 403
    # 版本不受"编辑尝试"影响
    assert client.get(mapping_route).json()['data']['dictionary_version'] == 'validation/2'

    # 预置动作 HttpServiceCheck 仍可被 IF-01 受理，且受理不再 bump 字典版本
    request = {'scenario_id': '41', 'sequence_id': 'readonly-accept', 'actions': [
        {'seq': 1, 'action_name': 'HttpServiceCheck', 'target_node': {'subnet': 'lab', 'ip': '10.0.0.2'}},
    ]}
    receipt = client.post(incoming, json=request, headers=headers).json()
    assert receipt['code'] == 0 and receipt['dictionary_version'] == 'validation/2'
    assert client.get(mapping_route).json()['data']['dictionary_version'] == 'validation/2'

    with repo.connection() as connection:
        task = connection.execute('SELECT * FROM validation_tasks').fetchone()
    validation._freeze(task, json.loads(task['context']), 'test', interrupted=False)
    report = json.loads(deliveries()[0]['payload'])
    assert report['dictionary_version'] == receipt['dictionary_version']
    assert report['results'][0]['action_name'] == 'HttpServiceCheck'


def test_reference_catalog_migrates_once_and_unmapped_actions_cannot_execute(client, monkeypatch):
    import validation_actions as actions

    validation.initialize()
    monkeypatch.setenv('BRIDGE_VALIDATION_TOKEN', 'test-token')
    repo.put(actions.COLLECTION, actions.CATALOG, {'revision': 7, 'items': {
        'LabCheck': {'action_name': 'LabCheck', 'label': '本地检查', 'description': ''},
        'ServiceDiscovery': {'action_name': 'ServiceDiscovery', 'label': '本地显示名', 'description': ''},
    }})
    route = '/validation/action-mappings'
    result = client.get(route).json()['data']
    assert result['dictionary_version'] == 'validation/8'
    items = {item['action_name']: item for item in result['items']}
    assert set(items) == {'DiscoverRemoteSystems', 'ServiceDiscovery', 'DiscoverDeception',
                          'ExploitRemoteService', 'PrivilegeEscalate', 'DegradeServices', 'Impact',
                          'Withdraw', 'Sleep', 'LabCheck'}
    assert items['ServiceDiscovery']['simulation_steps'] == '激进 1 / 隐蔽 3'
    assert items['ServiceDiscovery']['label'] == '本地显示名'
    assert 'simulation_steps' not in items['LabCheck']
    # 字典只读：编辑端点 403（即便直写了历史数据也不允许 API 增删改）
    changed = {'action_name': 'DocumentedDiscovery', 'label': '文档动作', 'description': ''}
    assert client.put(route + '/ServiceDiscovery', json=changed).status_code == 403
    assert client.post(route, json=changed).status_code == 403
    assert client.delete(route + '/Sleep').status_code == 403
    # 未在字典中的动作（DocumentedDiscovery 仅在请求中出现）不能受理
    request = {'scenario_id': '41', 'sequence_id': 'documented-action', 'actions': [
        {'seq': 1, 'action_name': changed['action_name'], 'target_node': {'subnet': 'lab', 'ip': '10.0.0.2'}},
    ]}
    with monkeypatch.context() as patch:
        def cannot_bind(*_):
            pytest.fail('文档条目不应进入会话绑定')
        patch.setattr(validation, '_bind', cannot_bind)
        denial = client.post('/api/v1/attack-sequences', json=request,
                             headers={'Authorization': 'Bearer test-token'})
    assert denial.status_code == 400 and denial.json()['code'] == 1002
    # 列表稳定（无编辑通道, 集合与迁移结果一致）
    assert {item['action_name'] for item in client.get(route).json()['data']['items']} == set(items)


@pytest.mark.parametrize(('total', 'sizes'), [(0, [0]), (100, [100]), (101, [100, 1])])
def test_alert_batch_boundaries_and_precise_step_attribution(total, sizes):
    task, context = seed_task()
    repo.put('network_activity', 'probe-run',
             {'items': {'p': {'kind': 'probe', 'status': 'running', 'metrics': {'drops': 0}}}})
    for i in range(total):
        platform_db.record_event(41, {'type': 'snort', 'run_id': 'probe-run', 'probe_id': 'p',
                                     'seconds': 1000, 'timestamp': '01/01-00:00:00.200000',
                                     'proto': 'TCP', 'rule': '1:1000001:1', 'priority': 3,
                                     'src_addr': '10.0.0.1', 'src_port': 40001 + i,
                                     'dst_addr': '10.0.0.2', 'dst_port': 8080}, context['drill_id'])
    validation._freeze(task, context, 'test', interrupted=False)
    batches = [json.loads(report['payload']) for report in deliveries()[1:]]
    assert [len(batch['alerts']) for batch in batches] == sizes
    assert [batch['is_last'] for batch in batches] == [False] * (len(sizes) - 1) + [True]
    assert all(batch['observation_status'] == 'complete' for batch in batches)
    if total:
        assert [alert['seq'] for alert in batches[0]['alerts']] == [1] + [None] * (sizes[0] - 1)
