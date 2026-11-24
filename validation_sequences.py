"""Container-side integration for preconfigured HTTP service verification."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit

import grpc
from core.api.grpc.wrappers import NodeType
from fastapi import APIRouter, Header
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

import core_adapter
import network_activity as network
import platform_db
import repository as repo
import validation_actions

logger = logging.getLogger(__name__)
BATCH_SIZE = 100
OBSERVATION_GRACE = 2
PATHS = {'IF-02': '/api/v1/execution-results', 'IF-03': '/api/v1/snort-alerts'}
_stop = threading.Event()
_workers = []
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS validation_tasks(
    task_id TEXT PRIMARY KEY, scenario_id TEXT NOT NULL, sequence_id TEXT NOT NULL,
    deployment_id TEXT NOT NULL, execution_state TEXT NOT NULL, digest TEXT NOT NULL,
    context TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(scenario_id,sequence_id));
CREATE UNIQUE INDEX IF NOT EXISTS validation_active_session
    ON validation_tasks(scenario_id) WHERE execution_state IN ('accepted','executing');
CREATE INDEX IF NOT EXISTS validation_task_deployment ON validation_tasks(deployment_id,created_at);
CREATE TABLE IF NOT EXISTS validation_steps(
    task_id TEXT NOT NULL REFERENCES validation_tasks(task_id), seq INTEGER NOT NULL,
    state TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(task_id,seq));
CREATE TABLE IF NOT EXISTS validation_deliveries(
    task_id TEXT NOT NULL REFERENCES validation_tasks(task_id), interface TEXT NOT NULL,
    batch_no INTEGER NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0, error TEXT,
    PRIMARY KEY(task_id,interface,batch_no));
CREATE INDEX IF NOT EXISTS validation_due ON validation_deliveries(status,retry_at);
'''


class Rejected(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code


class ContractRoute(APIRoute):
    def get_route_handler(self):
        handle = super().get_route_handler()

        async def request_handler(request):
            try:
                return await handle(request)
            except RequestValidationError:
                return JSONResponse({'code': 1004, 'message': '请求格式无效'}, status_code=400)

        return request_handler


router = APIRouter(route_class=ContractRoute)


class NodeReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ip: ipaddress.IPv4Address
    subnet: str = Field(min_length=1, max_length=128)
    hostname: str | None = Field(default=None, max_length=128)


class Parameters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    check_id: Literal['web-health'] = 'web-health'


class Step(BaseModel):
    model_config = ConfigDict(extra='forbid')
    seq: int = Field(strict=True, ge=1, le=64)
    action_name: str = Field(min_length=1, max_length=64)
    source_node: NodeReference | None = None
    target_node: NodeReference
    params: Parameters = Field(default_factory=Parameters)


class Sequence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scenario_id: str = Field(pattern=r'^[1-9][0-9]{0,9}$')
    sequence_id: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,128}$')
    actions: list[Step] = Field(min_length=1, max_length=64)


def initialize():
    with repo.connection() as connection:
        connection.executescript(_SCHEMA)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _utc(stamp):
    if stamp is not None:
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    return None


def _key(task):
    return {name: task[name] for name in ('scenario_id', 'sequence_id')}


def _live(bridge, sid):
    try:
        current = bridge.get_session(sid)
    except grpc.RpcError as error:
        if error.code() == grpc.StatusCode.NOT_FOUND:
            raise Rejected(409, 1001, 'CORE 会话不存在或未运行') from error
        raise
    if current is None or getattr(current.state, 'value', current.state) != 4:
        raise Rejected(409, 1001, 'CORE 会话不存在或未运行')
    identity = core_adapter.identities(bridge).get(str(sid))
    if not identity:
        raise Rejected(503, 1501, '暂无法确认 CORE 会话身份')
    with repo.connection() as connection:
        row = connection.execute("""
            SELECT d.body FROM documents AS d JOIN drills AS a
              ON a.drill_id=json_extract(d.body,'$.drill_id')
            WHERE d.collection='deployments' AND a.core_sid=? AND a.identity=?
              AND a.status='active' AND json_extract(d.body,'$.status')='running'
        """, (sid, identity)).fetchone()
    if row is None:
        raise Rejected(409, 1001, '该 CORE 会话缺少运行中的 Bridge 部署')
    return current, identity, json.loads(row['body'])


def _live_node(bridge, session, node_id):
    try:
        node, interfaces, _ = bridge.get_node(session.id, node_id)
    except grpc.RpcError as error:
        if error.code() == grpc.StatusCode.NOT_FOUND:
            raise Rejected(409, 1003, '已标记的节点已离开 CORE 会话') from error
        raise
    if node.id != node_id or node.type not in (NodeType.DEFAULT, NodeType.DOCKER) or node.server:
        raise Rejected(409, 1003, '验证节点须是本机 CORE 原生节点或 Docker 节点')
    return interfaces


def _match_address(interfaces, reference):
    candidates = [item for item in interfaces if item.ip4 == str(reference.ip)]
    if len(candidates) != 1:
        raise Rejected(409, 1003, '请求地址与已标记节点的运行接口不匹配')
    if '/' in reference.subnet:
        try:
            real_subnet = ipaddress.ip_interface(f'{candidates[0].ip4}/{candidates[0].ip4_mask}').network
            matches = ipaddress.ip_network(reference.subnet) == real_subnet
        except ValueError:
            matches = False
        if not matches:
            raise Rejected(409, 1003, '请求网段与已标记节点的运行接口不匹配')


def _bind(bridge, sid, actions):
    session, identity, deployment = _live(bridge, sid)
    nodes = (deployment.get('execution_definition') or {}).get('nodes', [])
    sources = [node for node in nodes if node.get('validation_source')]
    targets = [node for node in nodes if node.get('validation_http_port')]
    if len(sources) != 1 or len(targets) != 1 or sources[0]['id'] == targets[0]['id']:
        raise Rejected(409, 1003, '请在启动前标记执行节点和 HTTP 验证目标')
    source, target = sources[0], targets[0]
    source_interfaces = _live_node(bridge, session, source['id'])
    target_interfaces = _live_node(bridge, session, target['id'])
    for step in actions:
        source_ref = step.source_node
        if source_ref:
            _match_address(source_interfaces, source_ref)
        _match_address(target_interfaces, step.target_node)
        try:
            network._target_address(str(step.target_node.ip))
        except ValueError as error:
            raise Rejected(409, 1003, '验证目标须使用拓扑内的私有 IPv4 地址') from error
        # A duplicate interface address would make the HTTP request ambiguous.
        attached = (iface for link in deployment['execution_definition'].get('links', [])
                    for iface in (link.get('iface1'), link.get('iface2')) if iface)
        if sum(iface.get('ip4') == str(step.target_node.ip) for iface in attached) != 1:
            raise Rejected(409, 1003, '验证目标地址须在部署中唯一')
    context = {name: deployment.get(name) for name in ('deployment_id', 'drill_id', 'validation_epoch')}
    context.update(identity=identity, source_id=source['id'], target_id=target['id'],
                   port=target['validation_http_port'])
    return context


def _accept(bridge, body):
    actions = sorted(body.actions, key=lambda step: step.seq)
    ids = body.scenario_id, body.sequence_id
    payload = [item.model_dump(mode='json') for item in actions]
    digest = hashlib.sha256(_json(payload).encode()).hexdigest()

    def existing(connection):
        row = connection.execute('SELECT digest,context FROM validation_tasks WHERE scenario_id=? AND sequence_id=?', ids).fetchone()
        if row and row['digest'] != digest:
            raise Rejected(409, 1005, '序列编号已用于不同内容')
        return json.loads(row['context'])['receipt'] if row else None

    def mapped_catalog():
        record = validation_actions.catalog()
        for step in actions:
            entry = record['items'].get(step.action_name)
            if entry is None:
                raise Rejected(400, 1002, '未配置动作映射：' + step.action_name)
            if 'simulation_steps' in entry:
                raise Rejected(400, 1002, '动作仅供文档对照，当前未接入执行：' + step.action_name)
        return record

    with repo.connection() as connection:
        if receipt := existing(connection):
            return receipt
    if int(ids[0]) > 2**31 - 1 or [step.seq for step in actions] != list(range(1, len(actions) + 1)):
        raise Rejected(400, 1004, '会话编号或步骤顺序无效')
    mapped_catalog()
    context = _bind(bridge, int(ids[0]), actions)
    with repo.transaction() as connection:
        if receipt := existing(connection):
            return receipt
        accepted = {'code': 0, 'message': 'accepted', 'scenario_id': ids[0], 'sequence_id': ids[1],
                    'dictionary_version': validation_actions.dictionary_version(mapped_catalog())}
        context['receipt'] = accepted
        if connection.execute("""SELECT 1 FROM validation_tasks WHERE scenario_id=?
                AND execution_state IN ('accepted','executing')""", (ids[0],)).fetchone():
            raise Rejected(409, 1006, '该会话已有未完成的验证序列')
        task_id = repo.identifier('validation')
        connection.execute('INSERT INTO validation_tasks VALUES(?,?,?,?,?,?,?,?)',
                           (task_id, *ids, context['deployment_id'], 'accepted', digest, _json(context), time.time()))
        connection.executemany('INSERT INTO validation_steps VALUES(?,?,?,?)',
                               ((task_id, item['seq'], 'pending', _json({'request': item})) for item in payload))
    return accepted


@router.post('/api/v1/attack-sequences')
def accept(body: Sequence, authorization: Annotated[str | None, Header()] = None):
    import app

    token = os.environ.get('BRIDGE_VALIDATION_TOKEN')
    if not token:
        return JSONResponse({'code': 1501, 'message': '序列验证接口未启用'}, status_code=503)
    if not hmac.compare_digest((authorization or '').encode(), ('Bearer ' + token).encode()):
        return JSONResponse({'code': 1000, 'message': '调用凭据无效'}, status_code=401)
    try:
        return _accept(app.bridge, body)
    except Rejected as error:
        return JSONResponse({**body.model_dump(include={'scenario_id', 'sequence_id'}), 'code': error.code,
                             'message': str(error)}, status_code=error.status)
    except (grpc.RpcError, OSError, TimeoutError):
        logger.warning('Could not confirm validation session %s', body.scenario_id, exc_info=True)
        return JSONResponse({'code': 1501, 'message': '暂无法确认 CORE 会话状态'}, status_code=503)


def _same_runtime(bridge, task, context):
    session, identity, deployment = _live(bridge, int(task['scenario_id']))
    if identity != context['identity'] or any(deployment.get(key) != context[key]
            for key in ('deployment_id', 'drill_id', 'validation_epoch')):
        raise Rejected(409, 1001, 'CORE 会话运行实例已变化')
    return session, deployment


def _check(bridge, session, deployment, context, step):
    target = NodeReference.model_validate(step['target_node'])
    address = network._target_address(str(target.ip))
    _match_address(_live_node(bridge, session, context['target_id']), target)
    _live_node(bridge, session, context['source_id'])
    docker, errors = network._docker_namespaces(deployment, session, bridge, [
        network.Traffic(id='validation', name='HTTP 验证', protocol='http',
                        source_id=context['source_id'], target_id=context['target_id'],
                        target_link=(context['source_id'], context['target_id']), port=context['port'], rate=1, duration=1),
    ])
    if errors:
        raise ValueError(next(iter(errors.values())))
    source_id = context['source_id']
    source = docker.get(source_id)
    fd = source.open() if source else network._native_namespace(session, source_id)
    try:
        proc = subprocess.run([str(network._tools()['nsenter']), '--net=/proc/self/fd/' + str(fd), '--',
                               sys.executable, str(Path(__file__).with_name('network_http.py')),
                               '--check', address, str(context['port'])],
                              pass_fds=(fd,), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
    finally:
        os.close(fd)
    output = network._json_line(proc.stdout) or {}
    output['exit_code'] = proc.returncode
    return output


def _steps(connection, task):
    return [dict(row) for row in connection.execute(
        'SELECT * FROM validation_steps WHERE task_id=? ORDER BY seq', (task['task_id'],))]


def _save_step(connection, row, state, body):
    connection.execute('UPDATE validation_steps SET state=?,body=? WHERE task_id=? AND seq=?',
                       (state, _json(body), row['task_id'], row['seq']))


def _run(bridge, task):
    context = json.loads(task['context'])
    with repo.connection() as connection:
        steps = _steps(connection, task)
    context.update(observation_start=time.time(), cursor=platform_db.event_cursor(context['drill_id']),
                   run_id=None, probes={})
    failure = None
    try:
        _, deployment = _same_runtime(bridge, task, context)
        context['run_id'] = (deployment.get('network_activity_start') or {}).get('run_id')
        context['probes'] = _probes(context['run_id'])
    except (Rejected, grpc.RpcError, OSError):
        failure = 'session_unavailable'
    with repo.transaction() as connection:
        connection.execute('UPDATE validation_tasks SET context=? WHERE task_id=?', (_json(context), task['task_id']))
    for row in steps:
        if failure or _stop.is_set():
            break
        body = json.loads(row['body'])
        try:
            session, deployment = _same_runtime(bridge, task, context)
        except (Rejected, grpc.RpcError, OSError):
            failure = 'session_unavailable'
            break
        body['started'] = time.time()
        baseline = time.monotonic()
        with repo.transaction() as connection:
            _save_step(connection, row, 'running', body)
        try:
            result = _check(bridge, session, deployment, context, body['request'])
            body.update(result=result, reason=None if result['exit_code'] == 0 else 'http_check_failed')
        except subprocess.TimeoutExpired:
            body['reason'] = 'http_check_timeout'
        except (Rejected, grpc.RpcError, OSError, ValueError) as error:
            logger.warning('validation step %s/%s unavailable: %s: %s',
                           task['sequence_id'], row['seq'], type(error).__name__, error)
            body['reason'] = 'node_or_interface_unavailable'
        body.update(ended=time.time(), duration=round(time.monotonic() - baseline, 3))
        with repo.transaction() as connection:
            _save_step(connection, row, 'FAIL' if body['reason'] else 'SUCCESS', body)
        failure = body['reason']
    context['observation_end'] = time.time()
    with repo.transaction() as connection:
        connection.execute('UPDATE validation_tasks SET context=? WHERE task_id=?', (_json(context), task['task_id']))
    interrupted = _stop.wait(OBSERVATION_GRACE)
    _freeze(task, context, failure or 'bridge_stopped', interrupted=interrupted)


def _probes(run_id):
    run = repo.read(network.RUN_COLLECTION, run_id) if run_id else None
    return {probe_id: item for probe_id, item in (run or {}).get('items', {}).items() if item['kind'] == 'probe'}


def _reported_status(row, context):
    """查询接口的观测状态：finished 任务回放封存值（数据冻结承诺），未结束才实时推导。"""
    if row['execution_state'] == 'finished':
        return context.get('observation_status')
    return _observation_status(context, False)


def _observation_status(context, interrupted):
    before = context.get('probes', {})
    after = _probes(context.get('run_id'))
    if not any(probe['status'] == 'running' for probe in (*before.values(), *after.values())):
        return 'unavailable'
    if interrupted or any(probe['status'] != 'running' or after.get(key, {}).get('status') != 'running'
                          or after[key].get('metrics', {}).get('drops', 0) > probe.get('metrics', {}).get('drops', 0)
                          for key, probe in before.items()):
        return 'partial'
    return 'complete'


def _alert_stamp(detail):
    stamp = detail.get('seconds')
    fraction = re.search(r'\.(\d{1,6})$', detail.get('timestamp') or '')
    return stamp + (float('0.' + fraction[1]) if fraction else 0) if isinstance(stamp, int) else None


def _alerts(connection, context, steps):
    if not context.get('run_id') or 'observation_start' not in context:
        return
    rows = connection.execute("""SELECT id,detail FROM events
        WHERE drill_id=? AND type='snort' AND json_valid(detail)
          AND json_extract(detail,'$.run_id')=? AND id>? ORDER BY id""",
        (context['drill_id'], context['run_id'], context['cursor']))
    for row in rows:
        detail = json.loads(row['detail'])
        stamp = _alert_stamp(detail)
        if stamp is None or not context['observation_start'] <= stamp <= context['observation_end']:
            continue
        src, dst = (detail.get('src_addr'), detail.get('src_port')), (detail.get('dst_addr'), detail.get('dst_port'))
        matches = []
        for body in steps:
            result = body.get('result', {})
            source = result.get('source_ip'), result.get('source_port')
            target = body['request']['target_node']['ip'], context['port']
            if (source[0] and source[1] and detail.get('proto') == 'TCP' and
                    body.get('started', float('inf')) <= stamp <= body.get('ended', 0) and
                    ((src, dst) == (source, target) or (src, dst) == (target, source))):
                matches.append(body['request']['seq'])
        yield dict(event_id=f"{context['run_id']}:{row['id']}", probe_id=detail.get('probe_id'),
                   seq=matches[0] if len(matches) == 1 else None, ts=_utc(stamp),
                   sid=':'.join(detail['rule'].split(':')[:2]), priority=detail.get('priority'),
                   src_ip=src[0], src_port=src[1], dst_ip=dst[0], dst_port=dst[1],
                   proto=detail.get('proto'), msg=detail.get('msg'))


def _queue(connection, task, interface, batch_no, payload):
    connection.execute("""INSERT INTO validation_deliveries(task_id,interface,batch_no,payload)
        VALUES(?,?,?,?)""", (task['task_id'], interface, batch_no, _json(payload)))


def _freeze(task, context, reason, *, interrupted):
    status = _observation_status(context, interrupted)
    context.setdefault('observation_end', time.time())
    with repo.transaction() as connection:
        results, steps = [], []
        for row in _steps(connection, task):
            body = json.loads(row['body'])
            step_state = row['state']
            if step_state in ('pending', 'running'):
                body['reason'] = 'interrupted_outcome_unknown' if step_state == 'running' else reason
                step_state = 'FAIL' if step_state == 'running' else 'SKIPPED'
                _save_step(connection, row, step_state, body)
            steps.append(body)
            result = body.get('result', {})
            results.append(dict(seq=row['seq'], action_name=body['request']['action_name'], actual=step_state,
                                exit_code=result.get('exit_code'), reason=body.get('reason'),
                                started_at=_utc(body.get('started')), ended_at=_utc(body.get('ended')),
                                duration_sec=body.get('duration', 0 if step_state == 'SKIPPED' else None),
                                source_node_id=context['source_id'], target_node_id=context['target_id'],
                                stdout_digest=f"HTTP {result['http_status']}" if 'http_status' in result else None))
        version = (context.get('receipt') or {}).get('dictionary_version', validation_actions.DEFAULT_VERSION)
        _queue(connection, task, 'IF-02', 0, {**_key(task), 'dictionary_version': version, 'results': results})
        batch_no, batch = 1, []
        base = {**_key(task), 'observation_status': status}
        for alert in _alerts(connection, context, steps):
            if len(batch) == BATCH_SIZE:
                _queue(connection, task, 'IF-03', batch_no, {**base, 'batch_no': batch_no, 'is_last': False, 'alerts': batch})
                batch_no, batch = batch_no + 1, []
            batch.append(alert)
        _queue(connection, task, 'IF-03', batch_no, {**base, 'batch_no': batch_no, 'is_last': True, 'alerts': batch})
        context['observation_status'] = status
        connection.execute("UPDATE validation_tasks SET execution_state='finished',context=? WHERE task_id=?",
                           (_json(context), task['task_id']))


def _execute_next(bridge):
    with repo.connection() as connection:
        row = connection.execute("SELECT * FROM validation_tasks WHERE execution_state='accepted' ORDER BY created_at LIMIT 1").fetchone()
    if row is None:
        return False
    with repo.transaction() as connection:
        claim = connection.execute("UPDATE validation_tasks SET execution_state='executing' WHERE task_id=? AND execution_state='accepted'",
                                   (row['task_id'],))
        if not claim.rowcount:
            return False
    try:
        _run(bridge, row)
    except Exception:
        logger.exception('Validation interrupted for %s', row['task_id'])
        _recover_task(row['task_id'])
    return True


def _recover_task(task_id):
    with repo.connection() as connection:
        row = connection.execute("SELECT * FROM validation_tasks WHERE task_id=? AND execution_state='executing'",
                                 (task_id,)).fetchone()
    if row:
        context = json.loads(row['context'])
        _freeze(row, context, 'bridge_interrupted', interrupted=True)


def _post(base_url, interface, payload):
    url = urlsplit(base_url)
    if url.scheme not in ('http', 'https') or not url.hostname or url.path not in ('', '/') or any(
            (url.username, url.password, url.query, url.fragment)):
        return 'invalid_callback_address', False
    transport = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
    connection = transport(url.hostname, url.port, timeout=10)
    headers = {'Content-Type': 'application/json; charset=utf-8'}
    token = os.environ.get('BRIDGE_VALIDATION_CALLBACK_TOKEN')
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        connection.request('POST', PATHS[interface], payload.encode(), headers)
        with connection.getresponse() as response:
            if response.status == 429 or response.status >= 500:
                return f'http_{response.status}', True
            if not 200 <= response.status < 300:
                return f'http_{response.status}', False
            raw = response.read(65537)
    except (OSError, http.client.HTTPException):
        return 'callback_unreachable', True
    finally:
        connection.close()
    field, collection = ('received_steps', 'results') if interface == 'IF-02' else ('received_alerts', 'alerts')
    try:
        ack = json.loads(raw) if len(raw) <= 65536 else None
        if (isinstance(ack, dict) and type(ack.get('code')) is int and ack['code'] == 0 and
                type(ack.get(field)) is int and ack[field] == len(json.loads(payload)[collection])):
            return None, False
    except (ValueError, UnicodeError):
        pass
    return 'invalid_acknowledgment', False


def _deliver_next(base_url):
    if not base_url:
        return False
    with repo.connection() as connection:
        row = connection.execute("""SELECT d.* FROM validation_deliveries AS d
            WHERE d.status='pending' AND d.retry_at<=? AND NOT EXISTS (
                SELECT 1 FROM validation_deliveries AS previous WHERE previous.task_id=d.task_id
                AND previous.batch_no<d.batch_no AND previous.status<>'confirmed')
            ORDER BY d.retry_at, d.task_id, d.batch_no LIMIT 1""", (time.time(),)).fetchone()
    if row is None:
        return False
    try:
        error, retry = _post(base_url, row['interface'], row['payload'])
    except ValueError:
        error, retry = 'invalid_callback_address', False
    attempts = row['attempts'] + 1
    status = 'confirmed' if error is None else 'pending' if retry else 'blocked'
    with repo.transaction() as connection:
        connection.execute("""UPDATE validation_deliveries SET status=?,attempts=?,retry_at=?,error=?
            WHERE task_id=? AND interface=? AND batch_no=?""",
            (status, attempts, time.time() + (min(300, 2**min(attempts, 9)) if retry else 0),
             error, row['task_id'], row['interface'], row['batch_no']))
    return True


def _query_payload(sequence_id: str, scenario_id: str, *, with_results: bool):
    """Q2 共用: 定位任务并组装 V1.0 IF-02 同构结果序列; 任务不存在返回 None"""
    with repo.connection() as connection:
        row = connection.execute("""SELECT * FROM validation_tasks
            WHERE scenario_id=? AND sequence_id=?""", (scenario_id, sequence_id)).fetchone()
        if row is None:
            return None
        context = json.loads(row['context'])
        results, steps = [], []
        for step in _steps(connection, row):
            body = json.loads(step['body'])
            result = body.get('result', {})
            state = step['state']
            if state in ('pending', 'running'):
                body['reason'] = 'interrupted_outcome_unknown' if state == 'running' else 'pending'
                state = 'FAIL' if state == 'running' else 'SKIPPED'
            results.append(dict(seq=step['seq'], action_name=body['request']['action_name'],
                                actual=state, exit_code=result.get('exit_code'), reason=body.get('reason'),
                                started_at=_utc(body.get('started')), ended_at=_utc(body.get('ended')),
                                duration_sec=body.get('duration', 0 if state == 'SKIPPED' else None),
                                source_node_id=context.get('source_id'), target_node_id=context.get('target_id'),
                                stdout_digest=f"HTTP {result['http_status']}" if 'http_status' in result else None))
            steps.append(body)
        alerts = list(_alerts(connection, context, steps))
        return {**_key(row), 'dictionary_version': (context.get('receipt') or {}).get('dictionary_version',
                                                                                   validation_actions.DEFAULT_VERSION),
                'state': row['execution_state'], 'observation_status': _reported_status(row, context),
                'results': results, 'alerts_total': len(alerts)}


@router.get('/api/v1/attack-sequences/{sequence_id}')
def query_task(sequence_id: str, scenario_id: str, authorization: Annotated[str | None, Header()] = None):
    """V2.0 Q1: 任务状态与步骤查询（含已完成步骤; state=finished 后结果冻结）"""
    with repo.connection() as connection:
        row = connection.execute("""SELECT * FROM validation_tasks
            WHERE scenario_id=? AND sequence_id=?""", (scenario_id, sequence_id)).fetchone()
        if row is None:
            return JSONResponse({'code': 1001, 'message': '任务不存在'}, status_code=404)
        context = json.loads(row['context'])
        steps = []
        for step in _steps(connection, row):
            body = json.loads(step['body'])
            steps.append(dict(seq=step['seq'], action_name=body['request']['action_name'],
                              state=step['state'], reason=body.get('reason'),
                              duration_sec=body.get('duration')))
        return {'data': {**_key(row), 'state': row['execution_state'],
                         'observation_status': context.get('observation_status'),
                         'dictionary_version': (context.get('receipt') or {}).get('dictionary_version',
                                                                                 validation_actions.DEFAULT_VERSION),
                         'source_node_id': context.get('source_id'), 'target_node_id': context.get('target_id'),
                         'steps': steps}}


@router.get('/api/v1/attack-sequences/{sequence_id}/results')
def query_results(sequence_id: str, scenario_id: str, authorization: Annotated[str | None, Header()] = None):
    """V2.0 Q2: 整条结果序列查询（结构对齐 V1.0 IF-02 payload, 数字平台入库可复用）"""
    payload = _query_payload(sequence_id, scenario_id, with_results=True)
    if payload is None:
        return JSONResponse({'code': 1001, 'message': '任务不存在'}, status_code=404)
    return {'data': payload}


@router.get('/api/v1/snort-alerts')
def query_alerts(sequence_id: str, scenario_id: str, after_event_id: str | None = None,
                 limit: int = 100,
                 authorization: Annotated[str | None, Header()] = None):
    """V2.0 Q3: 告警分页查询（after_event_id 增量游标; is_last 标识窗口末尾）"""
    limit = max(1, min(limit, 500))
    after = None
    if after_event_id:
        match = re.fullmatch(r'(.+):(\d+)', after_event_id)
        if match is None:
            return JSONResponse({'code': 1004, 'message': 'after_event_id 格式无效（须为 run_id:事件号）'},
                                status_code=400)
        after = int(match.group(2))
    with repo.connection() as connection:
        row = connection.execute("""SELECT * FROM validation_tasks
            WHERE scenario_id=? AND sequence_id=?""", (scenario_id, sequence_id)).fetchone()
        if row is None:
            return JSONResponse({'code': 1001, 'message': '任务不存在'}, status_code=404)
        context = json.loads(row['context'])
        # _alerts 期望已解析的步骤 body（按 result 五元组把告警归属到步骤 seq）
        steps = [json.loads(step['body']) for step in _steps(connection, row)]
        matched = [alert for alert in _alerts(connection, context, steps)
                   if after is None or int(alert['event_id'].rsplit(':', 1)[-1]) > after]
        alerts = matched[:limit]
        last = alerts[-1]['event_id'] if alerts else after_event_id
        return {'data': {**_key(row), 'observation_status': _reported_status(row, context),
                         'is_last': len(matched) <= limit, 'total': len(matched),
                         'after_event_id': last, 'alerts': alerts}}


@router.get('/deployments/{deployment_id}/validation-sequences')
def list_sequences(deployment_id: str):
    with repo.connection() as connection:
        if not repo.read('deployments', deployment_id):
            return JSONResponse({'error': '运行记录不存在'}, status_code=404)
        rows = connection.execute("""SELECT * FROM validation_tasks WHERE deployment_id=?
            ORDER BY created_at DESC LIMIT 50""", (deployment_id,)).fetchall()
        items = []
        for row in rows:
            context = json.loads(row['context'])
            steps = []
            for step in _steps(connection, row):
                body = json.loads(step['body'])
                steps.append(dict(seq=step['seq'], state=step['state'], reason=body.get('reason'),
                                  duration_sec=body.get('duration'), http_status=body.get('result', {}).get('http_status')))
            deliveries = [dict(entry) for entry in connection.execute("""SELECT interface,batch_no,status,attempts,error
                FROM validation_deliveries WHERE task_id=? ORDER BY batch_no""", (row['task_id'],))]
            items.append({**_key(row), 'task_id': row['task_id'], 'state': row['execution_state'], 'steps': steps,
                          'deliveries': deliveries, 'observation_status': context.get('observation_status'),
                          'source_node_id': context['source_id'], 'target_node_id': context['target_id']})
    return {'ok': True, 'data': {'items': items, 'callback_configured': bool(os.environ.get('BRIDGE_VALIDATION_CALLBACK_URL'))}}


@router.post('/deployments/{deployment_id}/validation-sequences/{task_id}/retry')
def retry_delivery(deployment_id: str, task_id: str):
    with repo.transaction() as connection:
        count = connection.execute("""UPDATE validation_deliveries SET status='pending',retry_at=0,error=NULL
            WHERE task_id IN (SELECT task_id FROM validation_tasks WHERE task_id=? AND deployment_id=?)
              AND status='blocked'""", (task_id, deployment_id)).rowcount
    return {'ok': True, 'data': {'retried': count}}


def _loop(operation):
    while not _stop.is_set():
        try:
            if operation():
                continue
        except Exception:
            logger.exception('Validation background operation failed')
        _stop.wait(0.5)


def start(bridge):
    initialize()
    with repo.connection() as connection:
        active = connection.execute("SELECT task_id FROM validation_tasks WHERE execution_state='executing'").fetchall()
    for row in active:
        _recover_task(row['task_id'])
    _stop.clear()
    for operation, name in ((lambda: _execute_next(bridge), 'validation-execution'),
                            (lambda: _deliver_next(os.environ.get('BRIDGE_VALIDATION_CALLBACK_URL')), 'validation-delivery')):
        worker = threading.Thread(target=_loop, args=(operation,), name=name, daemon=True)
        _workers.append(worker)
        worker.start()


def shutdown():
    _stop.set()
    for worker in _workers:
        worker.join(timeout=12)
    _workers.clear()
