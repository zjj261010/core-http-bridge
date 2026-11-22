"""Use an isolated Bridge, real CORE Docker nodes and Snort, and a mock receiver."""
import argparse
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


def main(root):
    fixture = json.loads((root / 'topology-fixture.json').read_text())
    token = (root / 'validation-token').read_text().strip()
    opener = build_opener(ProxyHandler({}))

    def api(method, path, body=None, expected=200, authenticated=True):
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + token
        request = Request('http://127.0.0.1:18080' + path, method=method, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
        try:
            response = opener.open(request, timeout=10 if path == incoming else 90)
        except HTTPError as error:
            response = error
        with response:
            payload = json.load(response)
            assert response.status == expected, (method, path, response.status, payload)
            return payload.get('data', payload)

    def poll(operation, ready, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = operation()
            if ready(value):
                return value
            time.sleep(0.2)
        raise AssertionError(value)

    incoming = '/api/v1/attack-sequences'
    template = '验证节点-' + uuid4().hex[:8]
    catalog = fixture['catalog']
    api('POST', '/instances', {'name': template, 'runtime_type': 'docker', 'image_tag': fixture['ref'],
                               'target_name': catalog['target_name'], 'target_version': catalog['applied_target_version'],
                               'service_selection': {'http': 'default'}, 'services': []})
    run = api('POST', '/topologies', {'definition': {
        'name': '序列验证', 'nodes': [
            {'id': 1, 'name': '执行端', 'instance': template, 'type': 'docker', 'validation_source': True},
            {'id': 2, 'name': '目标端', 'instance': template, 'type': 'docker', 'validation_http_port': 8080},
        ], 'links': [{'node1_id': 1, 'node2_id': 2,
                      'iface1': {'id': 0, 'ip4': '10.93.0.1', 'ip4_mask': 24},
                      'iface2': {'id': 0, 'ip4': '10.93.0.2', 'ip4_mask': 24}}],
        'network_activity': {'probes': [
            {'id': 'validation', 'name': '验证探针', 'node_id': 2, 'link': [1, 2], 'ruleset': 'verification'},
        ]},
    }}, expected=201)
    deployment = api('POST', f"/topologies/{run['topology_id']}/deploy",
                     {'revision': run['revision'], 'start': True}, expected=201)
    route = '/deployments/' + deployment['deployment_id']
    sid = str(deployment['core_sid'])
    probe = poll(lambda: api('GET', route + '/network-activity/batches'),
                 lambda data: (data.get('run') or {}).get('items', {}).get('validation', {}).get('status') == 'running')
    assert probe['run']['items']['validation']['node_id'] == 2
    action = {'seq': 1, 'action_name': 'HttpServiceCheck',
              'target_node': {'ip': '10.93.0.2', 'subnet': '10.93.0.0/24'}}
    body = {'scenario_id': sid, 'sequence_id': 'closed-loop-' + uuid4().hex[:8], 'actions': [action]}
    bad_target = {**action, 'target_node': {**action['target_node'], 'ip': '10.93.0.1'}}
    assert api('POST', incoming, {**body, 'actions': [bad_target]}, expected=409)['code'] == 1003
    assert api('POST', incoming, body, expected=401, authenticated=False)['code'] == 1000
    started = time.monotonic()
    reply = api('POST', incoming, body)
    assert reply['code'] == 0 and time.monotonic() - started < 10
    assert api('POST', incoming, body) == reply
    assert api('POST', incoming, {**body, 'actions': [{**action, 'seq': 2}]}, expected=409)['code'] == 1005

    visible = poll(lambda: api('GET', route + '/validation-sequences'),
                   lambda data: any(item['deliveries'] and all(row['status'] == 'confirmed'
                                   for row in item['deliveries']) for item in data['items']))
    assert len(visible['items']) == 1, visible
    item = visible['items'][0]
    assert item['state'] == 'finished' and item['steps'][0]['http_status'] == 200, item
    assert item['source_node_id'] == 1 and item['target_node_id'] == 2, item
    assert [row['attempts'] for row in item['deliveries']] == [2, 2], item

    with sqlite3.connect(root / 'evidence/receiver.db') as database:
        receipts = [(batch, json.loads(payload), attempts) for batch, payload, attempts in database.execute(
            'SELECT batch_no,payload,attempts FROM receipts WHERE scenario_id=? AND sequence_id=? ORDER BY rowid',
            (sid, body['sequence_id']))]
    assert [receipt[0] for receipt in receipts] == [0, 1], receipts
    assert receipts[0][1]['results'][0]['actual'] == 'SUCCESS'
    tail = receipts[1][1]
    assert tail['observation_status'] == 'complete' and tail['is_last'], tail
    assert any(alert.get('msg') == 'Bridge IDS verification' and alert['seq'] == 1 and
               isinstance(alert['priority'], int) for alert in tail['alerts']), tail
    (root / 'evidence/validation-sequences.json').write_text(json.dumps(
        {'progress': visible, 'receipts': receipts}, indent=2, ensure_ascii=False))
    print('PASS: real HTTP, real Snort, IF-02 before IF-03, duplicates and both callback retries')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    main(parser.parse_args().run_dir)
