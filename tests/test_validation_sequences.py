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
