"""Bounded traffic generation and passive Snort sensing in local CORE namespaces."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import platform_db
import repository as repo
from settings import settings

router = APIRouter()
logger = logging.getLogger(__name__)
NATIVE_TYPES = {'default', 'host', 'router', 'pc'}
ACTIVE = {'pending', 'waiting', 'starting', 'running'}
Key = Annotated[str, Field(pattern=r'^[a-zA-Z0-9_-]{1,48}$')]
ItemName = Annotated[str, Field(min_length=1, max_length=80)]
VERIFY_RULE = (
    'alert tcp any any -> any any (msg:"Bridge IDS verification"; '
    'content:"BRIDGE_IDS_CHECK"; sid:1000001; rev:1;)\n'
)
SNORT_LUA = (
    "alert_json = {fields = 'timestamp proto src_ap dst_ap rule action msg class'};"
    "perf_monitor = {seconds=1, packets=1, output='console', format='csv', "
    "modules={{name='daq',pegs='analyzed dropped'}}}"
)
TOOL_ROOT = Path(os.environ.get('CORE_BRIDGE_NETWORK_ROOT', '/opt/core-network-tools'))
RUN_COLLECTION = 'network_activity'
RULE_MAX_BYTES = 8 * 1024 * 1024
MAX_INTERVAL_SECONDS = 3


class Traffic(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: Key
    name: ItemName
    protocol: Literal['http', 'tcp', 'udp']
    source_id: int
    target_id: int
    target_link: tuple[int, int]
    port: int = Field(ge=1, le=65535)
    path: str = Field(default='/', max_length=512, pattern=r'^/[!-~]*$')
    rate: int = Field(ge=1, le=10_000_000)
    delay: int = Field(default=0, ge=0, le=3600)
    duration: int = Field(ge=1, le=3600)


class Probe(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: Key
    name: ItemName
    node_id: int
    link: tuple[int, int]
    ruleset: Key = 'verification'


class Plan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    traffic: list[Traffic] = Field(default_factory=list, max_length=8)
    probes: list[Probe] = Field(default_factory=list, max_length=4)


def rulesets():
    result = {}
    root = settings.data_dir / 'snort-rules'
    for path in sorted(root.glob('*.rules')):
        if path.is_file() and re.fullmatch(r'[a-zA-Z0-9_-]{1,48}', path.stem) and path.stem != 'verification':
            result[path.stem] = path
    result['verification'] = None
    return result


def _ruleset(name):
    path = rulesets().get(name)
    if path is None:
        if name != 'verification':
            raise ValueError('Snort 规则集不存在：' + name)
        return VERIFY_RULE.encode()
    with path.open('rb') as stream:
        data = stream.read(RULE_MAX_BYTES + 1)
    if len(data) > RULE_MAX_BYTES:
        raise ValueError('Snort 规则集超过大小限制')
    return data


def _interface(link, node_id):
    if node_id == link.get('node1_id'):
        return link.get('iface1')
    if node_id == link.get('node2_id'):
        return link.get('iface2')
    return None


def _link(definition, endpoints):
    pair = set(endpoints)
    return next((link for link in definition.get('links', []) if
                 {link['node1_id'], link['node2_id']} == pair), None)


def validate(plan, definition, kinds):
    """Resolve topology references at save time. Resolve real addresses at runtime."""
    seen, listeners, captures = set(), set(), set()
    catalog = rulesets()
    for item in [*plan.traffic, *plan.probes]:
        if item.id in seen:
            raise ValueError('流量任务或探针 ID 重复')
        seen.add(item.id)
        if isinstance(item, Traffic):
            if item.source_id == item.target_id or kinds.get(item.source_id) not in NATIVE_TYPES:
                raise ValueError(item.name + '：请选择不同的源与目标，源必须为 CORE 原生节点')
            target = kinds.get(item.target_id)
            if target not in NATIVE_TYPES | {'docker', 'podman'}:
                raise ValueError(item.name + '：请选择有 IP 接口的业务节点')
            if item.protocol != 'http' and target not in NATIVE_TYPES:
                raise ValueError(item.name + '：iperf3 接收端须为 CORE 原生节点')
            link = _link(definition, item.target_link)
            if not link or item.target_id not in item.target_link:
                raise ValueError(item.name + '：请选择目标节点所连的接口')
            if item.protocol == 'http' and item.rate > 50:
                raise ValueError(item.name + '：HTTP 背景流量上限为 50 请求/秒')
            if item.protocol == 'http' and item.path.startswith('//'):
                raise ValueError(item.name + '：请填写站内请求路径')
            if item.protocol != 'http':
                listener = (item.target_id, item.port)
                if listener in listeners:
                    raise ValueError(item.name + '：同一节点的 iperf3 接收端口须各不相同')
                listeners.add(listener)
        else:
            if kinds.get(item.node_id) not in NATIVE_TYPES or item.node_id not in item.link or not _link(definition, item.link):
                raise ValueError(item.name + '：请选择 CORE 原生节点对应的链路接口')
            if item.ruleset not in catalog:
                raise ValueError(item.name + '：Snort 规则集不存在')
            capture = (item.node_id, frozenset(item.link))
            if capture in captures:
                raise ValueError(item.name + '：接口已有 Snort 探针')
            captures.add(capture)


def _tools():
    return {
        'nsenter': Path('/usr/bin/nsenter'),
        'ss': Path('/usr/bin/ss'),
        'iperf3': TOOL_ROOT / 'iperf/bin/iperf3',
        'snort': TOOL_ROOT / 'snort/bin/snort',
        'snort_config': TOOL_ROOT / 'snort/etc/snort/snort.lua',
        'daq': TOOL_ROOT / 'daq/lib/daq',
    }


@router.get('/network-activity/capabilities')
def capabilities():
    tools = _tools()
    return {'ok': True, 'data': {
        'rulesets': [{'id': name, 'name': '无害功能验收' if name == 'verification' else name}
                     for name in rulesets()],
        'http': tools['nsenter'].is_file(),
        'iperf3': all(tools[key].is_file() for key in ('nsenter', 'ss', 'iperf3')),
        'snort': all(tools[key].is_file() for key in ('nsenter', 'snort', 'snort_config')) and tools['daq'].is_dir(),
    }}


def _namespace(session, node_id):
    """Bind to the local vnoded process opened by CORE, then pin its namespace FD."""
    node = session.nodes.get(node_id)
    if node is None or node.server or node.type.value != 0 or not re.fullmatch(r'n\d+', node.name or ''):
        raise ValueError('该任务需要本机部署的 CORE 原生节点')
    channel = Path(session.dir) / node.name
    pid = int(channel.with_suffix('.pid').read_text().strip())
    process = Path('/proc') / str(pid)
    argv = (process / 'cmdline').read_bytes().split(b'\0')
    if not argv or Path(os.fsdecode(argv[0])).name != 'vnoded' or os.fsencode(channel) not in argv:
        raise ValueError('CORE 节点进程已退出或被替换')
    return os.open(process / 'ns/net', os.O_RDONLY | os.O_CLOEXEC)


def _resolve_interface(bridge, sid, definition, endpoints, node_id):
    _, ifaces, _ = bridge.get_node(sid, node_id)
    link = _link(definition, endpoints)
    if link is None:
        raise ValueError('该链路已移除')
    declared = _interface(link, node_id) or {}
    # Frozen execution definitions contain CORE-assigned interface IDs, even for automatic addresses.
    ident = declared.get('id')
    if ident is None:
        if len(ifaces) != 1:
            raise ValueError('无法确认运行接口')
        return ifaces[0]
    iface = next((item for item in ifaces if item.id == ident), None)
    if iface is None:
        raise ValueError('运行接口已移除')
    return iface


def _target_address(iface):
    address = ipaddress.IPv4Address(iface.ip4 or '')
    private = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')
    if not any(address in ipaddress.IPv4Network(network) for network in private):
        raise ValueError('背景流量目标须为拓扑节点的私有 IPv4 地址')
    return str(address)


def _kill(proc, sig):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass


class Worker:
    def __init__(self, run, spec):
        self.run = run
        self.spec = spec
        self.cancelled = threading.Event()
        self.processes = []
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.path = run.path / spec.id

    def update(self, **changes):
        self.run.update(self.spec.id, **changes)

    def spawn(self, node_id, args, *, output=subprocess.PIPE, env=None):
        with self.lock:
            if self.cancelled.is_set():
                raise InterruptedError()
            fd = _namespace(self.run.session, node_id)
            try:
                proc = subprocess.Popen(
                    [str(_tools()['nsenter']), '--net=/proc/self/fd/' + str(fd), '--', *map(str, args)],
                    pass_fds=(fd,), start_new_session=True, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                    cwd=self.path, env=env,
                )
            finally:
                os.close(fd)
            self.processes.append(proc)
            return proc

    def terminate(self):
        with self.lock:
            self.cancelled.set()
            for proc in self.processes:
                _kill(proc, signal.SIGTERM)
                if proc.poll() is None:
                    timer = threading.Timer(2, _kill, args=(proc, signal.SIGKILL))
                    timer.daemon = True
                    timer.start()

    def cleanup(self):
        with self.lock:
            children = list(self.processes)
        limit = time.monotonic() + 2
        for proc in children:
            _kill(proc, signal.SIGTERM)
        for proc in children:
            try:
                proc.wait(timeout=max(0, limit - time.monotonic()))
            except subprocess.TimeoutExpired:
                _kill(proc, signal.SIGKILL)
                proc.wait(timeout=2)
            if proc.stdout:
                proc.stdout.close()

    def lines(self, proc, process):
        recent = ''
        with (self.path / 'runtime.log').open('w', encoding='utf-8') as log:
            for line in proc.stdout:
                if process(line):
                    continue
                recent = line.strip()[:300] or recent
                log.write(line)
        result = proc.wait()
        if result and not self.cancelled.is_set():
            raise RuntimeError(recent or f'进程以状态 {result} 退出')

    def serve(self, address):
        spec = self.spec
        tools = _tools()
        # Check the listening socket without opening a connection to the one-off server.
        probe = [tools['ss'], '-Hlt', 'sport', '=', ':' + str(spec.port)]
        check = self.spawn(spec.target_id, probe)
        if check.communicate(timeout=3)[0].strip():
            raise ValueError('目标节点的端口已被占用')
        server = self.spawn(spec.target_id, [tools['iperf3'], '-s', '-1', '-B', address, '-p', spec.port],
                            output=subprocess.DEVNULL)
        for _ in range(20):
            if self.cancelled.wait(0.1):
                raise InterruptedError()
            if server.poll() is not None:
                raise ValueError('iperf3 接收端启动失败')
            check = self.spawn(spec.target_id, probe)
            if check.communicate(timeout=3)[0].strip():
                return
        raise ValueError('iperf3 接收端未在目标端口就绪')

    def traffic(self):
        spec = self.spec
        self.update(status='waiting')
        if self.cancelled.wait(max(0, self.run.started + spec.delay - time.monotonic())):
            raise InterruptedError()
        iface = _resolve_interface(self.run.bridge, self.run.sid, self.run.definition, spec.target_link, spec.target_id)
        address = _target_address(iface)
        self.update(status='starting', endpoint=address)
        if spec.protocol == 'http':
            args = [sys.executable, str(Path(__file__).with_name('network_http.py')), address, spec.port,
                    spec.path, spec.rate, spec.duration]
            consume = self.http_sample
        else:
            tool = _tools()['iperf3']
            if not tool.is_file():
                raise ValueError('宿主未准备 iperf3')
            self.serve(address)
            args = [tool, '-c', address, '-p', spec.port, '-b', spec.rate, '-t', spec.duration,
                    '-i', '1', '--json-stream', '--connect-timeout', '2000']
            if spec.protocol == 'udp':
                args.append('-u')
            else:
                # The default 128 KiB TCP write size produces bursts at low background rates.
                args.extend(('-l', max(256, min(16384, spec.rate // 80))))
            consume = self.iperf_sample
        # A stalled peer must not leave a finite traffic job running indefinitely.
        proc = self.spawn(spec.source_id, ['/usr/bin/timeout', '--foreground', '--kill-after=2s',
                                           str(spec.duration + 18) + 's', *args])
        self.lines(proc, consume)

    def http_sample(self, line):
        data = _json_line(line)
        if data is None:
            return False
        self.update(status=data.pop('status', 'running'), metrics=data)
        return True

    def iperf_sample(self, line):
        data = _json_line(line)
        if data is None:
            return False
        event, report = data.get('event'), data.get('data') or {}
        if event == 'start':
            self.update(status='running')
        elif event in ('interval', 'end'):
            stats = report.get('sum_received') or report.get('sum') or report.get('sum_sent') or {}
            self.update(status='running', metrics={key: stats[key] for key in
                        ('bits_per_second', 'bytes', 'lost_packets', 'lost_percent', 'jitter_ms') if key in stats})
        elif event == 'error':
            raise ValueError(str(report)[:300])
        return True

    def probe(self):
        spec = self.spec
        tools = _tools()
        if not capabilities()['data']['snort']:
            raise ValueError('宿主未准备 Snort 3 和 LibDAQ')
        self.update(status='starting', metrics={'packets': 0, 'drops': 0, 'alerts': 0})
        iface = _resolve_interface(self.run.bridge, self.run.sid, self.run.definition, spec.link, spec.node_id)
        if not iface.name or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,15}', iface.name):
            raise ValueError('CORE 没有返回有效的监测接口')
        rules = _ruleset(spec.ruleset)
        rule_path = self.path / 'loaded.rules'
        rule_path.write_bytes(rules)
        version = hashlib.sha256(rules).hexdigest()
        self.update(interface=iface.name, rules_sha256=version)
        proc = self.spawn(spec.node_id, [
            tools['snort'], '-c', tools['snort_config'], '-R', rule_path, '--daq-dir', tools['daq'],
            '--daq', 'pcap', '--daq-mode', 'passive', '-i', iface.name, '-A', 'alert_json', '--lua', SNORT_LUA,
        ], env={**os.environ, 'LD_LIBRARY_PATH': str(tools['daq'].parent)})
        columns, packets, drops, alerts = [], 0, 0, 0

        def consume(line):
            nonlocal columns, packets, drops, alerts
            if line.startswith('#timestamp,'):
                columns = line.strip().split(',')
                return True
            if columns and re.match(r'^\d+,', line):
                stats = dict(zip(columns, line.strip().split(',')))
                packets += int(stats.get('daq.analyzed', '0'))
                drops += int(stats.get('daq.dropped', '0'))
            elif match := _json_line(line):
                if not match.get('rule'):
                    return False
                alerts += 1
                platform_db.record_event(self.run.sid, {
                    'type': 'snort', 'action': 'detect', 'node_id': spec.node_id, 'probe_id': spec.id,
                    'probe': spec.name, 'interface': iface.name, 'run_id': self.run.id,
                    'rules_sha256': version,
                    **{key: match.get(key) for key in ('timestamp','proto','src_ap','dst_ap','rule','msg','class')},
                }, self.run.drill)
            elif 'packet processing' not in line.lower():
                return False
            self.update(status='running', metrics={'packets': packets, 'drops': drops, 'alerts': alerts})
            return True

        self.lines(proc, consume)
        if not self.cancelled.is_set():
            raise ValueError('Snort 探针提前退出')

    def execute(self):
        result = {'status': 'completed'}
        try:
            self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if isinstance(self.spec, Traffic):
                self.traffic()
            else:
                self.probe()
        except InterruptedError:
            result = {'status': 'stopped'}
        except Exception as error:
            result = {'status': 'failed', 'error': str(error)[:350]}
            logger.info('network activity %s/%s: %s', self.run.id, self.spec.id, error)
        finally:
            try:
                self.cleanup()
                self.update(**({'status': 'stopped'} if self.cancelled.is_set() else result), ended_at=repo.now())
            finally:
                self.finished.set()


def _json_line(line):
    try:
        result = json.loads(line)
    except (ValueError, TypeError):
        return None
    return result if isinstance(result, dict) else None


class Run:
    def __init__(self, record, session, bridge, plan):
        self.id = repo.identifier('net')
        self.sid = session.id
        self.drill = record['drill_id']
        self.definition = record['execution_definition']
        self.session = session
        self.bridge = bridge
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.path = settings.data_dir / 'network-activity' / self.id
        self.workers = {spec.id: Worker(self, spec) for spec in [*plan.traffic, *plan.probes]}
        self.data = {
            'run_id': self.id, 'deployment_id': record['deployment_id'], 'drill_id': self.drill,
            'core_sid': self.sid, 'created_at': repo.now(), 'started_ns': time.time_ns(),
            'node_names': {str(node['id']): node['name'] for node in self.definition.get('nodes', [])},
            'items': {key: {**worker.spec.model_dump(mode='json'), 'status': 'pending',
                            'kind': 'traffic' if isinstance(worker.spec, Traffic) else 'probe'}
                      for key, worker in self.workers.items()},
        }
        repo.put(RUN_COLLECTION, self.id, self.data)
        self.last_write = {}

    def update(self, key, **changes):
        with self.lock:
            item = self.data['items'][key]
            previous = item['status']
            item.update(changes)
            now = time.monotonic()
            if previous != item['status'] or now - self.last_write.get(key, 0) >= 1:
                self.last_write[key] = now
                repo.put(RUN_COLLECTION, self.id, self.data)

    def start(self):
        for key, worker in reversed(tuple(self.workers.items())):
            threading.Thread(target=worker.execute, name=f'network-{key}', daemon=True).start()

    def stop(self):
        for worker in self.workers.values():
            worker.terminate()
        until = time.monotonic() + MAX_INTERVAL_SECONDS + 3
        for worker in self.workers.values():
            worker.finished.wait(max(0, until - time.monotonic()))


_lock = threading.RLock()
_runs = {}


def start(record, bridge):
    definition = record.get('execution_definition') or {}
    plan = Plan.model_validate(definition.get('network_activity') or {})
    if not plan.traffic and not plan.probes:
        return
    sid = record['core_sid']
    with _lock:
        if sid in _runs:
            return
        run = Run(record, bridge.get_session(sid), bridge, plan)
        _runs[sid] = run
        run.start()


def stop(sid, drill=None):
    with _lock:
        run = _runs.get(sid)
        if run and (drill is None or run.drill == drill):
            _runs.pop(sid)
            run.stop()


def reconcile(sessions):
    with _lock:
        for sid, run in list(_runs.items()):
            session = sessions.get(sid)
            if not session or session.state.value != 4 or platform_db.get_active_drill(sid) != run.drill:
                stop(sid, run.drill)


def shutdown():
    with _lock:
        for sid in list(_runs):
            stop(sid)


def recover():
    for key, run in repo.read(RUN_COLLECTION).items():
        active = [item for item in run.get('items', {}).values() if item['status'] in ACTIVE]
        if active:
            for item in active:
                item.update(status='interrupted', ended_at=repo.now())
            repo.put(RUN_COLLECTION, key, run)


@router.get('/deployments/{deployment_id}/network-activity')
def history(deployment_id: str, run_id: str | None = None):
    deployment = repo.read('deployments', deployment_id)
    if deployment is None:
        raise HTTPException(404, '运行记录不存在')
    runs = [record for record in repo.read(RUN_COLLECTION).values() if record['deployment_id'] == deployment_id]
    runs.sort(key=lambda item: (item['created_at'], item.get('started_ns', 0)), reverse=True)
    if run_id and not any(item['run_id'] == run_id for item in runs):
        raise HTTPException(404, '该运行批次不存在')
    selected = run_id or (runs[0]['run_id'] if runs else None)
    alerts = platform_db.query_events(drill_id=deployment['drill_id'], limit=200,
                                     event_type='snort', detail_run_id=selected) if selected else []
    return {'ok': True, 'data': {'runs': runs, 'alerts': alerts}}


@router.post('/deployments/{deployment_id}/network-activity/{run_id}/{item_id}/stop')
def stop_item(deployment_id: str, run_id: str, item_id: str):
    with _lock:
        run = next((run for run in _runs.values() if run.id == run_id and run.data['deployment_id'] == deployment_id), None)
        item = run.workers.get(item_id) if run else None
        if not item or item.finished.is_set():
            raise HTTPException(409, '任务已经结束，或当前运行已变化')
        if not isinstance(item.spec, Traffic):
            raise HTTPException(400, '探针随拓扑停止')
        item.terminate()
    return {'ok': True, 'data': {'stopping': True}}
