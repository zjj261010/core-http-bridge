"""Small CORE 9.x adapter; does not modify installed CORE packages."""
import json
import threading
from settings import request_file
_lock = threading.RLock()


def call(bridge, body):
    with _lock, request_file('.py') as script:
        output = script.with_suffix('.json')
        script.write_text('import json, uuid\nfrom pathlib import Path\n'+body+'\nPath('+repr(str(output))+').write_text(json.dumps(result))\n')
        bridge.execute_script(str(script), '')
        if not output.exists():
            raise RuntimeError('CORE adapter 未返回结果，请检查 daemon 日志')
        return json.loads(output.read_text())


def identities(bridge):
    return call(bridge, '''if not getattr(coreemu, '_http_bridge_epoch', None):
    coreemu._http_bridge_epoch = uuid.uuid4().hex
result = {}
for sid, session in list(coreemu.sessions.items()):
    if not getattr(session, '_http_bridge_identity', None):
        session._http_bridge_identity = uuid.uuid4().hex
    result[str(sid)] = coreemu._http_bridge_epoch + ':' + session._http_bridge_identity
''')


def activate(bridge, directories, required):
    return call(bridge, '''for directory in '''+repr(directories)+''':
    coreemu.service_manager.load(Path(directory))
result = {'missing': sorted(set('''+repr(required)+''') - set(coreemu.service_manager.services))}
''')


def events(bridge, sid, handler):
    """Wait for an actual round trip before advertising a live event subscription."""
    import time, uuid
    from core.api.grpc import wrappers
    ready=threading.Event()
    marker=uuid.uuid4().hex
    def receive(event):
        alert=getattr(event,'alert_event',None)
        if alert and alert.source=='__bridge_subscription__':
            if alert.text==marker: ready.set()
            return
        handler(event)
    stream=bridge.events(sid,handler=receive)
    deadline=time.monotonic()+3
    while not ready.is_set() and time.monotonic()<deadline:
        bridge.alert(sid,wrappers.AlertLevel.NOTICE,'__bridge_subscription__',marker)
        ready.wait(.05)
    if not ready.is_set():
        stream.cancel()
        raise RuntimeError('CORE 事件订阅未就绪')
    return stream


def registered(bridge):
    return call(bridge,"result = sorted(coreemu.service_manager.services)")
