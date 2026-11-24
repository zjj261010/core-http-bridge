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


def test_auth_optional_off_by_default_and_enforced_when_configured(client, monkeypatch):
    """鉴权默认关闭（未配置 Token = 接口开放，无/错凭据均放行）；配置 Token 才启用 Bearer 校验。"""
    validation.initialize()
    request = {'scenario_id': '41', 'sequence_id': 'auth-off', 'actions': [
        {'seq': 1, 'action_name': 'HttpServiceCheck', 'target_node': {'subnet': 'lab', 'ip': '10.0.0.2'}},
    ]}
    incoming = '/api/v1/attack-sequences'
    monkeypatch.setattr(validation, '_bind', lambda *_: {
        'deployment_id': 'deploy-test', 'source_id': 1, 'target_id': 2,
    })

    # Token 未配置：不带凭据 / 带任意凭据都放行（内网隔离＋端口准入为访问控制手段）
    monkeypatch.delenv('BRIDGE_VALIDATION_TOKEN', raising=False)
    assert client.post(incoming, json=request).json()['code'] == 0
    # 不同会话（同会话第二个未完成任务会被 1006 串行拒绝）
    assert client.post(incoming, json={**request, 'scenario_id': '42', 'sequence_id': 'auth-off-2'},
                       headers={'Authorization': 'Bearer whatever'}).json()['code'] == 0

    # 查询接口恒开放
    assert client.get('/api/v1/attack-sequences/auth-off?scenario_id=41').status_code == 200
    assert client.get('/api/v1/attack-sequences/auth-off/results?scenario_id=41').status_code == 200
    assert client.get('/api/v1/snort-alerts', params={'scenario_id': '41', 'sequence_id': 'auth-off'}).status_code == 200

    # Token 配置后：正确凭据放行，错误/缺失凭据 401/1000
    monkeypatch.setenv('BRIDGE_VALIDATION_TOKEN', 'secret')
    ok = client.post(incoming, json={**request, 'scenario_id': '43', 'sequence_id': 'auth-on'},
                     headers={'Authorization': 'Bearer secret'})
    assert ok.json()['code'] == 0
    bad = client.post(incoming, json={**request, 'scenario_id': '44', 'sequence_id': 'auth-on-bad'},
                      headers={'Authorization': 'Bearer wrong'})
    assert bad.status_code == 401 and bad.json()['code'] == 1000
    none = client.post(incoming, json={**request, 'scenario_id': '45', 'sequence_id': 'auth-on-none'})
    assert none.status_code == 401 and none.json()['code'] == 1000


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


def test_v2_query_endpoints_state_results_alerts(client, monkeypatch):
    """V2.0 查询接口: Q1 状态/步骤, Q2 结果序列(对齐 IF-02 结构), Q3 告警分页"""
    validation.initialize()
    monkeypatch.setenv('BRIDGE_VALIDATION_TOKEN', 'test-token')
    monkeypatch.setattr(validation, '_bind', lambda *_: {
        'deployment_id': 'deploy-test', 'source_id': 1, 'target_id': 2,
    })
    headers = {'Authorization': 'Bearer test-token'}
    request = {'scenario_id': '41', 'sequence_id': 'v2-query', 'actions': [
        {'seq': 1, 'action_name': 'HttpServiceCheck', 'target_node': {'subnet': 'lab', 'ip': '10.0.0.2'}},
    ]}
    receipt = client.post('/api/v1/attack-sequences', json=request, headers=headers).json()
    assert receipt['code'] == 0

    # Q1: 任务查询（accepted 态）
    q1 = client.get('/api/v1/attack-sequences/v2-query?scenario_id=41')
    assert q1.status_code == 200
    data = q1.json()['data']
    assert data['state'] == 'accepted' and data['scenario_id'] == '41' and data['sequence_id'] == 'v2-query'
    assert data['steps'][0]['state'] == 'pending'

    # Q3: 告警查询（无告警 → 空数组 + is_last）
    q3 = client.get('/api/v1/snort-alerts', params={'scenario_id': '41', 'sequence_id': 'v2-query'})
    assert q3.json()['data']['alerts'] == []

    # 不存在任务 → 404/1001
    missing = client.get('/api/v1/attack-sequences/no-such?scenario_id=41')
    assert missing.status_code == 404 and missing.json()['code'] == 1001

    # 模拟执行完成（隔离环境无 bridge 执行器——直接置步骤终态再封存）
    with repo.connection() as connection:
        task = connection.execute('SELECT * FROM validation_tasks').fetchone()
        body = json.loads(connection.execute('SELECT body FROM validation_steps WHERE task_id=?',
                                             (task['task_id'],)).fetchone()[0])
        body.update(started=time.time(), ended=time.time() + 0.4, duration=0.4,
                    result=dict(exit_code=0, http_status=200, source_ip='10.0.0.1', source_port=40001))
        connection.execute('UPDATE validation_steps SET state=?,body=? WHERE task_id=?',
                           ('SUCCESS', json.dumps(body), task['task_id']))
    validation._freeze(task, json.loads(task['context']), 'test', interrupted=False)

    # Q1: finished 态
    q1 = client.get('/api/v1/attack-sequences/v2-query?scenario_id=41')
    assert q1.json()['data']['state'] == 'finished'
    # Q2: 结果序列（结构对齐 V1.0 IF-02 payload）
    q2 = client.get('/api/v1/attack-sequences/v2-query/results?scenario_id=41')
    payload = q2.json()['data']
    assert payload['dictionary_version'] == receipt['dictionary_version']
    r0 = payload['results'][0]
    assert r0['actual'] == 'SUCCESS' and r0['seq'] == 1 and 'duration_sec' in r0
    assert r0['started_at'].endswith('Z') and 'source_node_id' in r0


def test_q3_pagination_cursor_and_frozen_observation_status(client, monkeypatch):
    """Q3 分页游标(after_event_id/limit/is_last) + finished 后 observation_status 冻结。"""
    validation.initialize()
    monkeypatch.setenv('BRIDGE_VALIDATION_TOKEN', 'test-token')
    monkeypatch.setattr(validation, '_bind', lambda *_: {
        'deployment_id': 'deploy-test', 'drill_id': 'd-test', 'source_id': 1, 'target_id': 2, 'port': 8080,
    })
    headers = {'Authorization': 'Bearer test-token'}
    request = {'scenario_id': '41', 'sequence_id': 'v2-page', 'actions': [
        {'seq': 1, 'action_name': 'HttpServiceCheck', 'target_node': {'subnet': 'lab', 'ip': '10.0.0.2'}},
    ]}
    assert client.post('/api/v1/attack-sequences', json=request, headers=headers).json()['code'] == 0
    with repo.connection() as connection:
        task = connection.execute('SELECT * FROM validation_tasks').fetchone()
        task_id, context = task['task_id'], json.loads(task['context'])
        # 受理态 context 无观测字段——按执行器 (_run) 的形态补齐，模拟已进入观测
        context.update(run_id='run-test', cursor=0, observation_start=time.time(),
                       observation_end=time.time() + 1,
                       probes={'p': {'status': 'running', 'metrics': {'drops': 0}}})
        started = context['observation_start'] + 0.1
        ended = context['observation_start'] + 0.5
        body = json.loads(connection.execute('SELECT body FROM validation_steps WHERE task_id=?',
                                             (task_id,)).fetchone()[0])
        body.update(started=started, ended=ended, duration=0.4,
                    result=dict(exit_code=0, http_status=200, source_ip='10.0.0.1', source_port=40001))
        connection.execute('UPDATE validation_steps SET state=?,body=? WHERE task_id=?',
                           ('SUCCESS', json.dumps(body), task_id))
        connection.execute('UPDATE validation_tasks SET context=? WHERE task_id=?',
                           (json.dumps(context), task_id))
    # 事件表独立连接（platform_db.record_event 自管连接, 与 repo 连接并存会撞锁）
    # 三条窗口内告警：请求方向(归属 seq=1)、响应方向(对称匹配, 归属 seq=1)、无关五元组(归属不了)
    alerts_spec = [
        dict(src_addr='10.0.0.1', src_port=40001, dst_addr='10.0.0.2', dst_port=context['port'], at=0.2),
        dict(src_addr='10.0.0.2', src_port=context['port'], dst_addr='10.0.0.1', dst_port=40001, at=0.25),
        dict(src_addr='10.0.0.1', src_port=49999, dst_addr='10.0.0.2', dst_port=context['port'], at=0.3),
    ]
    for spec in alerts_spec:
        stamp = started + spec.pop('at')
        platform_db.record_event(41, {'type': 'snort', 'run_id': 'run-test',
                                     'probe_id': 'p', 'seconds': int(stamp),
                                     'timestamp': f'01/01-00:00:00.{int(stamp % 1 * 1e6):06d}',
                                     'proto': 'TCP', 'rule': '1:1000001:1', 'priority': 3, **spec},
                                 context['drill_id'])
    validation._freeze(task, context, 'test', interrupted=False)

    def alerts(**params):
        response = client.get('/api/v1/snort-alerts', params={'scenario_id': '41', 'sequence_id': 'v2-page', **params})
        assert response.status_code == 200, response.text
        return response.json()['data']

    # 全量：3 条（2 条归属 seq=1，1 条归属不了 seq=null）
    full = alerts()
    assert full['total'] == 3 and len(full['alerts']) == 3 and full['is_last'] is True
    assert [a['seq'] for a in full['alerts']] == [1, 1, None]
    cursor = full['after_event_id']
    assert cursor == full['alerts'][-1]['event_id']

    # limit=1 分页 + 游标推进
    page1 = alerts(limit=1)
    assert page1['total'] == 3 and len(page1['alerts']) == 1 and page1['is_last'] is False
    page2 = alerts(limit=2, after_event_id=page1['after_event_id'])
    assert len(page2['alerts']) == 2 and page2['is_last'] is True
    assert [a['event_id'] for a in page2['alerts']] == [a['event_id'] for a in full['alerts'][1:]]

    # 非法游标 400/1004；finished 后 observation_status 用封存值（实时推导会因探针不在库返回 unavailable）
    bad = client.get('/api/v1/snort-alerts', params={'scenario_id': '41', 'sequence_id': 'v2-page',
                                                     'after_event_id': 'bogus'})
    assert bad.status_code == 400 and bad.json()['code'] == 1004
    frozen = alerts()['observation_status']
    assert frozen == 'partial' != validation._observation_status({**context, 'probes': {}}, False)
