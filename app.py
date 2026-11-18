"""
Core HTTP Bridge — FastAPI 服务，将 CORE gRPC 接口暴露为 HTTP REST API。

- 一元 RPC → RESTful 端点 (POST / GET / PATCH / DELETE)
- 服务端流 → SSE (Server-Sent Events)
- 客户端流 → WebSocket

错误响应统一格式:
  {"error": "<human readable>", "detail": "<技术细节>", "hint": "<可操作建议>"}

运行: uvicorn app:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pty
import re
import signal
import subprocess
import threading
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from core.api.grpc import wrappers
from core.api.grpc import client as grpc_client

from bridge import CoreGrpcBridge
from native_services import ServiceConfig
from network_activity import Plan
from settings import settings, request_file

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("core-http-bridge")

# ---------------------------------------------------------------------------
# 统一错误处理
# ---------------------------------------------------------------------------

_GRPC_TO_HTTP = {
    "NOT_FOUND": 404,
    "INVALID_ARGUMENT": 400,
    "ALREADY_EXISTS": 409,
    "PERMISSION_DENIED": 403,
    "UNAUTHENTICATED": 401,
    "UNIMPLEMENTED": 501,
    "UNAVAILABLE": 503,
    "CANCELLED": 499,
    "DEADLINE_EXCEEDED": 504,
    "OUT_OF_RANGE": 400,
    "FAILED_PRECONDITION": 400,
    "ABORTED": 409,
    "RESOURCE_EXHAUSTED": 429,
    "INTERNAL": 500,
    "DATA_LOSS": 500,
    "UNKNOWN": 500,
}


def _parse_grpc_error(exc: Exception) -> dict | None:
    """从 gRPC _InactiveRpcError 提取 status/detail/http_code。"""
    if type(exc).__name__ != "_InactiveRpcError":
        return None
    msg = str(exc)
    code_map = {
        0: "OK", 1: "CANCELLED", 2: "UNKNOWN", 3: "INVALID_ARGUMENT",
        4: "DEADLINE_EXCEEDED", 5: "NOT_FOUND", 6: "ALREADY_EXISTS",
        7: "PERMISSION_DENIED", 8: "RESOURCE_EXHAUSTED",
        9: "FAILED_PRECONDITION", 10: "ABORTED", 11: "OUT_OF_RANGE",
        12: "UNIMPLEMENTED", 13: "INTERNAL", 14: "UNAVAILABLE",
        15: "DATA_LOSS", 16: "UNAUTHENTICATED",
    }
    status_code = "UNKNOWN"
    m = re.search(r"grpc_status:(\d+)", msg)
    if m:
        status_code = code_map.get(int(m.group(1)), "UNKNOWN")
    detail = msg
    m = re.search(r'grpc_message:"([^"]+)"', msg)
    if m:
        detail = m.group(1)
    http_code = _GRPC_TO_HTTP.get(status_code, 500)
    return {"http": http_code, "status": status_code, "detail": detail}


_HINTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"session.*not found", re.I), "session_id 不存在，请先 POST /api/sessions 创建"),
    (re.compile(r"node.*not found", re.I), "node_id 不存在或不属于该会话"),
    (re.compile(r"interface.*not", re.I), "iface_id 不存在于该节点"),
    (re.compile(r"does not have mobility"), "该节点没有配置移动模型（仅 WLAN/EMANE 节点支持）"),
    (re.compile(r"not using BasicRangeModel"), "WLAN 节点需要先启动会话才能使用 BasicRangeModel"),
    (re.compile(r"has no attribute|NoneType.*to_proto", re.I), "请求参数格式有误 — 缺少必填字段或字段类型错误"),
    (re.compile(r"does not have.*interface", re.I), "iface_id 不存在于该节点"),
    (re.compile(r"unknown", re.I), "请检查请求的资源是否存在"),
    (re.compile(r"ptp links support at most"), "该节点已有足够接口，不要重复添加"),
    (re.compile(r"NoneType.*to_proto", re.I), "请求体格式有误 — 缺少必填字段或字段类型不匹配（例如缺少 iface1/iface2 或 type 字段）"),
]


def _hint_from_detail(detail: str) -> str:
    for pattern, hint in _HINTS:
        if pattern.search(detail):
            return hint
    return ""


def _error_response(status_code: int, error: str, detail: str = "", hint: str = "") -> JSONResponse:
    body: dict[str, str] = {"error": error}
    if detail:
        body["detail"] = detail
    if hint:
        body["hint"] = hint
    return JSONResponse(status_code=status_code, content=body)


# ---------------------------------------------------------------------------
# Bridge lifecycle
# ---------------------------------------------------------------------------

bridge: CoreGrpcBridge | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global bridge
    bridge = CoreGrpcBridge(settings.core_address)
    bridge.connect()
    logger.info("bridge connected to core-daemon")
    try:
        import platform_db
        platform_db.init()
        import jobs
        jobs.recover()
        __import__('image_services').recover()
        __import__('network_activity').recover()
        platform_db.start_sampler()   # reconciler + 事件落库 + 指标采样（5s）
        logger.info("platform db sampler started")
    except Exception:
        logger.exception("platform_db startup failed")
    yield
    await asyncio.to_thread(__import__("jobs").shutdown)
    __import__('image_services').shutdown()
    await asyncio.to_thread(__import__('network_activity').shutdown)
    bridge.close()
    logger.info("bridge closed")


app = FastAPI(
    title="CORE HTTP Bridge",
    description="HTTP REST API 包装 CORE 网络仿真器的 gRPC 接口",
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    grpc_err = _parse_grpc_error(exc)
    if grpc_err:
        detail = grpc_err["detail"]
        hint = _hint_from_detail(detail)
        logger.warning("gRPC %s %s %s -> %s: %s", request.method, request.url.path, grpc_err["status"], detail)
        return _error_response(
            status_code=grpc_err["http"],
            error=detail,
            detail="gRPC status: " + grpc_err["status"],
            hint=hint,
        )
    logger.exception("unhandled exception on %s %s", request.method, request.url.path)
    return _error_response(
        status_code=500,
        error="internal server error",
        detail=str(exc)[:200],
        hint="check server logs for details",
    )


# ---------------------------------------------------------------------------
# Pydantic 模型
# ---------------------------------------------------------------------------

class PositionModel(BaseModel):
    x: float = 0
    y: float = 0

class GeoModel(BaseModel):
    lat: float = 0
    lon: float = 0
    alt: float = 0

class InterfaceModel(BaseModel):
    model_config = {'extra': 'allow'}
    id: int | None = None
    name: str | None = None
    mac: str | None = None
    ip4: str | None = None
    ip4_mask: int | None = None
    ip6: str | None = None
    ip6_mask: int | None = None
    mtu: int | None = None
    node_id: int | None = None

class LinkOptionsModel(BaseModel):
    model_config = {'extra': 'allow'}
    jitter: int | None = None
    key: int | None = None
    mburst: int | None = None
    mer: int | None = None
    loss: float | None = None
    bandwidth: int | None = None
    burst: int | None = None
    delay: int | None = None
    dup: int | None = None
    unidirectional: bool | None = None
    buffer: int | None = None

class LinkModel(BaseModel):
    node1_id: int
    node2_id: int
    iface1: InterfaceModel | None = None
    iface2: InterfaceModel | None = None
    options: LinkOptionsModel | None = None

class NodeModel(BaseModel):
    model_config = {'extra': 'allow'}
    id: int | None = None
    name: str | None = None
    type: int | None = 0
    model: str | None = None
    position: PositionModel | None = None
    services: list[str] | None = None
    emane: str | None = None
    icon: str | None = None
    image: str | None = None
    server: str | None = None
    geo: GeoModel | None = None
    instance_version: int | None = None
    service_overrides: dict[str, str | None] | None = None
    service_configs: dict[str, ServiceConfig | None] | None = None
    instance: str | None = None   # 来源实例名——部署即登记引用（实例锁定扳机, 设计 §5.2）

class StartSessionRequest(BaseModel):
    definition: bool = False

class LinkedRequest(BaseModel):
    node1_id: int
    node2_id: int
    iface1_id: int
    iface2_id: int
    linked: bool

class NodeCommandRequest(BaseModel):
    command: str
    wait: bool = True
    shell: bool = False

class MobilityActionRequest(BaseModel):
    node_id: int
    action: str  # START | PAUSE | STOP

class ServiceActionRequest(BaseModel):
    node_id: int
    service: str
    action: str  # START | STOP | RESTART | VALIDATE

class CreateServiceRequest(BaseModel):
    name: str
    group: str = "Custom"
    executables: list[str] = []
    dependencies: list[str] = []
    directories: list[str] = []
    files: list[str] = []
    startup: list[str] = []
    validate: list[str] = []
    shutdown: list[str] = []
    templates: dict[str, str] = {}
    recreate: bool = False

class AlertBody(BaseModel):
    level: str  # DEFAULT|FATAL|ERROR|WARNING|NOTICE
    source: str
    text: str
    node_id: int | None = None

class OpenXmlBody(BaseModel):
    data: str
    start: bool = False

class ExecuteScriptBody(BaseModel):
    script: str
    args: str = ""

class SetWlanConfigBody(BaseModel):
    wlan_id: int
    config: dict[str, str]

class SetMobilityConfigBody(BaseModel):
    node_id: int
    config: dict[str, str]

class SetEmaneModelConfigBody(BaseModel):
    node_id: int
    model: str
    config: dict[str, str]
    iface_id: int | None = None

class WlanLinkBody(BaseModel):
    wlan_id: int
    node1_id: int
    node2_id: int
    linked: bool

class EmaneLinkBody(BaseModel):
    nem1: int
    nem2: int
    linked: bool

class WirelessLinkedBody(BaseModel):
    wireless_id: int
    node1_id: int
    node2_id: int
    linked: bool

class EmanePathlossesBody(BaseModel):
    node1_id: int
    iface1_id: int = 0
    node2_id: int
    iface2_id: int = 0
    rx1: float = 0.0
    rx2: float = 0.0


# ---------------------------------------------------------------------------
# 拓扑导入 模型
# ---------------------------------------------------------------------------

class TopologyNode(BaseModel):
    model_config = {'extra': 'allow'}
    """拓扑中的节点定义。"""
    id: int
    name: str
    model: str | None = "PC"
    type: str = "default"  # default | switch | wlan | wireless | hub | docker | emane
    x: float = 200
    y: float = 200
    services: list[str] | None = None
    icon: str | None = None
    image: str | None = None
    image_ref: str | None = None
    instance: str | None = None
    instance_version: int | None = None
    service_overrides: dict[str, str | None] | None = None
    service_configs: dict[str, ServiceConfig | None] | None = None
    target_name: str | None = None
    target_version: int | None = None
    emane: str | None = None

class TopologyLink(BaseModel):
    model_config = {'extra': 'allow'}
    """拓扑中的链路定义。支持自定义接口IP。"""
    node1_id: int
    node2_id: int
    iface1: InterfaceModel | None = None
    iface2: InterfaceModel | None = None
    options: LinkOptionsModel | None = None
    iface1_ip4: str | None = None
    iface1_ip4_mask: int | None = None
    iface2_ip4: str | None = None
    iface2_ip4_mask: int | None = None
    bandwidth: int | None = None
    delay: int | None = None
    loss: float | None = None
    jitter: int | None = None
    dup: int | None = None

class NativeServicePreviewRequest(BaseModel):
    node_id: int = Field(default=1, ge=1)
    model: str = 'bare'
    links: list[TopologyLink] = Field(default_factory=list)
    peer_types: dict[int, Literal['switch', 'hub', 'wlan']] = Field(default_factory=dict)
    config: dict[str, str] = Field(default_factory=dict)


class TopologyWlan(BaseModel):
    model_config = {'extra': 'allow'}
    """WLAN 节点定义（含配置）。"""
    id: int
    name: str
    x: float = 200
    y: float = 200
    range: str = "275"
    bandwidth: str = "54000000"
    delay: str = "5000"
    jitter: str = "0"
    error: str = "0"

class TopologyInput(BaseModel):
    model_config = {'extra': 'allow'}
    """完整拓扑描述（JSON/XML 均可）。"""
    schema_version: int = 2
    name: str = "topology"
    options: dict[str, str] | None = None
    nodes: list[TopologyNode] = []
    links: list[TopologyLink] = []
    wlans: list[TopologyWlan] = []
    network_activity: Plan | None = None


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------

def _enum_val(e: Any) -> int:
    return e.value if hasattr(e, "value") else e

def _session_to_dict(s: wrappers.Session) -> dict:
    return {
        "id": s.id,
        "state": _enum_val(s.state),
        "nodes": [_node_to_dict(n) for n in s.nodes.values()],
        "links": [_link_to_dict(l) for l in (s.links or [])],
        "dir": s.dir,
        "user": s.user,
        "file": str(s.file) if s.file else None,
    }

def _session_summary_to_dict(s: wrappers.SessionSummary) -> dict:
    return {"id": s.id, "state": _enum_val(s.state), "nodes": s.nodes, "file": s.file, "dir": s.dir}

def _node_to_dict(n: wrappers.Node) -> dict:
    from native_services import from_core

    overrides, external = from_core(getattr(n, 'service_configs', {}) or {})
    return {
        "id": n.id,
        "name": n.name,
        "type": _enum_val(n.type),
        "model": n.model,
        "position": {"x": n.position.x, "y": n.position.y} if n.position else None,
        "geo": {"lat": n.geo.lat, "lon": n.geo.lon, "alt": n.geo.alt} if n.geo else None,
        "services": list(n.services) if n.services else [],
        **({"service_configs": overrides} if overrides else {}),
        **({"external_service_files": external} if external else {}),
        "emane": n.emane,
        "icon": n.icon,
        "image": n.image,
        "server": n.server,
        "dir": n.dir,
        "channel": n.channel,
        "canvas": n.canvas,
    }

def _link_to_dict(l: wrappers.Link) -> dict:
    return {
        "node1_id": l.node1_id,
        "node2_id": l.node2_id,
        "type": _enum_val(l.type),
        "iface1": _iface_to_dict(l.iface1) if l.iface1 else None,
        "iface2": _iface_to_dict(l.iface2) if l.iface2 else None,
        "options": _link_options_to_dict(l.options) if l.options else None,
    }

def _iface_to_dict(i: wrappers.Interface) -> dict:
    return {
        "id": i.id, "name": i.name, "mac": i.mac, "ip4": i.ip4, "ip4_mask": i.ip4_mask,
        "ip6": i.ip6, "ip6_mask": i.ip6_mask, "mtu": i.mtu, "node_id": i.node_id,
    }

def _link_options_to_dict(o: wrappers.LinkOptions) -> dict:
    return {
        "jitter": o.jitter, "key": o.key, "mburst": o.mburst, "mer": o.mer,
        "loss": o.loss, "bandwidth": o.bandwidth, "burst": o.burst, "delay": o.delay,
        "dup": o.dup, "unidirectional": o.unidirectional, "buffer": o.buffer,
    }

def _config_option_to_dict(co: wrappers.ConfigOption) -> dict:
    return {
        "label": co.label, "name": co.name, "value": co.value, "type": co.type,
        "select": list(co.select) if co.select else [], "group": co.group,
    }

def _config_option_dict_to_dict(d: dict[str, wrappers.ConfigOption]) -> dict[str, dict]:
    return {k: _config_option_to_dict(v) for k, v in d.items()}

def _wrappers_node(m: NodeModel) -> wrappers.Node:
    from native_services import to_core

    pos = wrappers.Position(x=m.position.x, y=m.position.y) if m.position else wrappers.Position(x=200,y=200)
    geo = wrappers.Geo(lat=m.geo.lat, lon=m.geo.lon, alt=m.geo.alt) if m.geo else None
    return wrappers.Node(
        id=m.id, name=m.name,
        type=wrappers.NodeType(m.type) if m.type is not None else wrappers.NodeType.DEFAULT,
        model=m.model, position=pos, geo=geo,
        services=set(m.services) if m.services else None,
        service_configs=to_core(bridge, m.service_configs),
        emane=m.emane, icon=m.icon, image=m.image, server=m.server,
    )

def _wrappers_iface(m: InterfaceModel | None) -> wrappers.Interface | None:
    if m is None:
        return None
    return wrappers.Interface(
        id=m.id, name=m.name, mac=m.mac, ip4=m.ip4, ip4_mask=m.ip4_mask,
        ip6=m.ip6, ip6_mask=m.ip6_mask, mtu=m.mtu, node_id=m.node_id,
    )

def _wrappers_link_opts(m: LinkOptionsModel | None) -> wrappers.LinkOptions | None:
    if m is None:
        return None
    return wrappers.LinkOptions(
        jitter=m.jitter, key=m.key, mburst=m.mburst, mer=m.mer, loss=m.loss,
        bandwidth=m.bandwidth, burst=m.burst, delay=m.delay, dup=m.dup,
        unidirectional=m.unidirectional, buffer=m.buffer,
    )

# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    if bridge is None:
        return _error_response(503, "bridge not initialized", hint="check server startup logs")
    try:
        sessions = bridge.get_sessions()
        return {"status": "ok", "sessions": len(sessions)}
    except Exception as e:
        return _error_response(503, "bridge unavailable", detail=str(e)[:100],
                               hint="ensure core-daemon is running on localhost:50051")

# ── Frontend (SPA) ──
# ── Static files ──
import os

@app.middleware("http")
async def _no_cache_html(request, call_next):
    """HTML 页面禁止缓存，避免前端更新后浏览器仍用旧页面（fetch 接口对不上）"""
    response = await call_next(request)
    p = request.url.path
    if p == "/" or (p.startswith("/static/") and p.endswith(".html")):
        response.headers["Cache-Control"] = "no-cache"
    return response

_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# ── Frontend SPA ──

@app.get("/term.html", include_in_schema=False)
def term_page():
    """Redirect to static term page."""
    import os
    path = os.path.join(os.path.dirname(__file__), "static", "term.html")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>Terminal page not found</h1>")

@app.get("/", include_in_schema=False)
def index():
    """Serve the frontend SPA from static/."""
    path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>CORE HTTP Bridge</h1><p>Frontend not found.</p>")
@app.get("/api/sessions")
def list_sessions():
    """获取所有会话摘要列表"""
    sessions = bridge.get_sessions()
    import repository
    pending=repository.read('identity_diagnostics')
    return [{**_session_summary_to_dict(s),'identity_status':pending.get(str(s.id),{}).get('status','unchecked')} for s in sessions]

@app.post("/api/sessions", status_code=201)
def create_session(body: dict | None = None):
    """创建新会话。同时签发演练身份 drill_id（数据主键; CORE 会话号可回收, 见 platform_db）"""
    session_id = body.get("session_id") if body else None
    s = bridge.create_session(session_id)
    d = _session_to_dict(s)
    try:
        import platform_db
        d["drill_id"] = platform_db.ensure_drill(s.id)
        platform_db.subscribe_events(s.id)  # 创建即订阅, 头几秒动作不漏录
    except Exception as e:
        d.update(drill_id=None,identity_status="pending",identity_message=str(e))
        logger.exception("issue drill failed")
    return d

@app.get("/api/sessions/{session_id}")
def get_session(session_id: int):
    """获取指定会话详情"""
    s = bridge.get_session(session_id)
    data = _session_to_dict(s)
    import topologies
    import platform_db
    try:
        data['drill_id']=platform_db.ensure_drill(session_id)
        data['identity_status']='confirmed'
        bound=topologies.bindings(session_id)
        for node in data['nodes']: node.update(bound.get(str(node['id']),{}))
    except platform_db.IdentityUnconfirmed as e:
        data.update(drill_id=None,identity_status='pending',identity_message=str(e))
    return data

@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: int):
    """删除会话（演练身份正常关闭; 该会话的 metrics/events 保留供复盘, audit 永久保留）。
    stop 是异步收尾: 会话仍在 RUNTIME/DATACOLLECT 时先补 stop 并等状态退出（实测立即 delete 会失败）"""
    import time as _t
    __import__('network_activity').stop(session_id)
    try:
        s = bridge.get_session(session_id)
        st = getattr(s.state, "value", s.state)
        if st in (4, 5):
            try:
                bridge.stop_session(session_id)
            except Exception:
                pass
            for _ in range(20):  # 最多等 10s 让节点收尾
                _t.sleep(0.5)
                st2 = getattr(bridge.get_session(session_id).state, "value", None)
                if st2 not in (4, 5):
                    break
    except Exception:
        pass  # 会话已不存在等情况 → 直接尝试删除
    result = bridge.delete_session(session_id)
    if not result:
        return {"result":False}
    import topologies
    topologies.state(session_id,"closed")
    try:
        import platform_db
        platform_db.unsubscribe_events(session_id)
        platform_db.release_deploy_refs(core_sid=session_id)  # 会话删除 → 实例引用解除
        platform_db.close_drill(session_id, status="closed")
    except Exception:
        logger.exception("close drill failed")
    return {"result": result}

@app.get("/api/sessions/{session_id}/check")
def check_session(session_id: int):
    """检查会话是否存在"""
    return {"result": bridge.check_session(session_id)}

@app.post("/api/sessions/{session_id}/start")
def start_session(session_id: int, body: StartSessionRequest):
    """启动会话 (definition=True 仅定义, False 实例化并启动服务)"""
    s = bridge.get_session(session_id)
    if not body.definition:
        import topologies
        deployment=topologies.prepare_start(session_id)
        for nid,node in s.nodes.items():
            runtime=deployment.get('bindings',{}).get(str(nid),{}).get('runtime_name')
            if runtime: node.name=runtime
    result, exceptions = bridge.start_session(s, definition=body.definition)
    if result:
        import topologies
        topologies.for_session(session_id)
        topologies.state(session_id,'defined' if body.definition else 'running')
        if not body.definition:
            try:
                __import__('network_activity').start(topologies.for_session(session_id), bridge)
            except Exception:
                logger.exception('network activity could not start for session %s', session_id)
    return {"result": result, "exceptions": exceptions}

@app.post("/api/sessions/{session_id}/stop")
def stop_session(session_id: int):
    """停止运行中的会话。停止后保留拓扑定义（节点/链路不丢失），
    状态重置为 DEFINITION_STATE，可再次 start。"""
    try:
        # 1. 保存拓扑定义（在 stop 之前获取，因为 stop 会销毁会话）
        session_data = bridge.get_session(session_id)
        __import__('network_activity').stop(session_id)
        # 2. 停止（销毁运行时资源）
        bridge.stop_session(session_id)
        # 3. 从保存的定义中恢复拓扑（节点/链路仍保留）
        result, exceptions = bridge.start_session(session_data, definition=True)
        if not result: return {'result':False,'exceptions':exceptions}
        import topologies
        topologies.state(session_id,'stopped')
        return {"result": True}
    except Exception as e:
        grpc_err = _parse_grpc_error(e)
        if grpc_err:
            return _error_response(
                grpc_err["http"], grpc_err["detail"],
                detail="gRPC status: " + grpc_err["status"],
                hint=_hint_from_detail(grpc_err["detail"]),
            )
        logger.exception("stop_session failed")
        return _error_response(500, "internal server error", detail=str(e)[:200])

@app.post("/api/sessions/{session_id}/alert")
def session_alert(session_id: int, body: AlertBody):
    """发送告警到会话"""
    level_map = {
        "DEFAULT": wrappers.AlertLevel.DEFAULT,
        "FATAL": wrappers.AlertLevel.FATAL,
        "ERROR": wrappers.AlertLevel.ERROR,
        "WARNING": wrappers.AlertLevel.WARNING,
        "NOTICE": wrappers.AlertLevel.NOTICE,
    }
    level = level_map.get(body.level.upper())
    if level is None:
        return _error_response(400, "invalid alert level",
                               detail=f"supported: {', '.join(level_map)}")
    return {"result": bridge.alert(session_id, level, body.source, body.text, body.node_id)}


# ===================================================================
# Node RPCs
# ===================================================================

@app.post("/api/sessions/{session_id}/nodes", status_code=201)
def add_node(session_id: int, body: NodeModel):
    """添加节点 (type: 0=DEFAULT, 1=PHYSICAL, 4=SWITCH, 6=WIRELESS_LAN, 10=EMANE, 15=DOCKER, 17=WIRELESS, 18=PODMAN)。
    body.instance 为来源实例名时自动登记部署引用（实例锁定扳机, 设计 §5.2）"""
    import native_services, topologies, versions
    from core_adapter import activate
    with topologies.mutation_lock:
        binding = {}
        if body.instance:
            try:
                resets = [name for name, value in (body.service_configs or {}).items() if value is None]
                resolved, dirs = versions.resolve(body.instance,body.instance_version,body.service_overrides)
                # Incoming resolved fields are compatibility hints, never a second authority.
                template = __import__('repository').revision('template',body.instance,resolved['instance_version'])
                if body.image and body.image not in (resolved.get('image'),resolved.get('image_ref'),template.get('image_tag')):
                    raise ValueError('请求镜像与模板绑定不一致')
                if body.services is not None and set(body.services) not in ({x['name'] for x in template.get('services',[])},set(resolved['services'])):
                    raise ValueError('请求服务与模板绑定不一致')
                if body.type not in (None,0,15 if resolved['type']=='docker' else 0):
                    raise ValueError('节点类型与模板绑定不一致')
                raw = resolved.pop('service_configs', {})
                inherited = {name: ServiceConfig.model_validate(value) for name, value in raw.items()}
                if inherited or body.service_configs:
                    if resolved['type'] != 'default':
                        raise ValueError('只有 CORE 原生节点支持服务配置覆盖')
                    native_services.check_selection(resolved['services'], body.service_configs)
                    resolved['service_configs'] = native_services.effective_configs(inherited, body.service_configs, resolved['services'])
                dep = topologies.for_session(session_id)
                if body.id is None:
                    body.id = max(bridge.get_session(session_id).nodes.keys(),default=0)+1
                display = body.name or body.instance
                body = body.model_copy(update={**resolved,'type':15 if resolved['type']=='docker' else 0,'name':f'chb-{dep["deployment_id"][-12:]}-{body.id}' if resolved['type']=='docker' else display})
                if dirs:
                    result=activate(bridge,dirs,body.services or [])
                    if result['missing']: raise ValueError('服务未在 daemon 注册: '+', '.join(result['missing']))
                binding={k:resolved[k] for k in ('instance','instance_version','target_name','target_version','image_ref','service_overrides','service_selection','effective_services') if k in resolved}
                if resets:
                    binding['native_service_resets'] = resets
                binding.update(display_name=display,runtime_name=body.name)
            except ValueError as e:
                raise HTTPException(400,str(e))
        if body.type in (None,0):
            if body.id is None:
                body.id=max(bridge.get_session(session_id).nodes.keys(),default=0)+1
            display=binding.get('display_name') or body.name or f'n{body.id}'
            body=body.model_copy(update={'name':f'n{body.id}'})
            binding.update(display_name=display,runtime_name=body.name)
        try:
            if body.service_configs:
                if body.type not in (None,0):
                    raise ValueError('只有 CORE 原生节点支持服务配置覆盖')
                native_services.check_selection(body.services or [], body.service_configs)
            node_id = bridge.add_node(session_id, _wrappers_node(body))
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        if binding:
            import platform_db
            import repository
            try:
                drill=platform_db.ensure_drill(session_id)
                with repository.transaction():
                    topologies.bind(session_id,node_id,binding)
                    if body.instance:
                        platform_db.register_deploy_ref(drill,session_id,node_id,body.instance,body.instance_version)
            except Exception:
                bridge.delete_node(session_id,node_id)
                raise
        return {"node_id": node_id}

@app.get("/api/sessions/{session_id}/nodes/{node_id}")
def get_node(session_id: int, node_id: int):
    """获取节点详情（含接口和链路）"""
    node, ifaces, links = bridge.get_node(session_id, node_id)
    return {
        "node": _node_to_dict(node),
        "interfaces": [_iface_to_dict(i) for i in ifaces],
        "links": [_link_to_dict(l) for l in links],
    }

@app.patch("/api/sessions/{session_id}/nodes/{node_id}")
def edit_node(session_id: int, node_id: int, icon: str | None = None):
    """编辑节点图标"""
    return {"result": bridge.edit_node(session_id, node_id, icon=icon)}

@app.delete("/api/sessions/{session_id}/nodes/{node_id}")
def delete_node(session_id: int, node_id: int):
    """删除节点。CORE delete_node 不级联删链——孤儿链路在节点 id 复用后会"嫁接"到新节点
    （实测：删节点→新拖节点同 id→链路视觉自动连上且删不掉）。先删该节点全部链路再删节点"""
    try:
        _node, _ifaces, links = bridge.get_node(session_id, node_id)
        for l in links:
            try:
                bridge.delete_link(session_id, wrappers.Link(
                    node1_id=l.node1_id, node2_id=l.node2_id,
                    iface1=l.iface1, iface2=l.iface2))
            except Exception as e:
                logger.warning("cascade delete link %s-%s failed: %s", l.node1_id, l.node2_id, e)
    except Exception:
        pass  # 节点已不存在等情况 → 直接删
    result = bridge.delete_node(session_id,node_id)
    if result:
        import platform_db, topologies
        platform_db.release_deploy_ref_node(session_id,node_id)
        topologies.unbind(session_id,node_id)
    return {'result':result}

@app.post("/api/sessions/{session_id}/nodes/{node_id}/move")
def move_node(session_id: int, node_id: int,
                    position: PositionModel | None = None, geo: GeoModel | None = None):
    """移动节点（坐标或经纬度）"""
    if not position and not geo:
        return _error_response(400, "must provide position or geo")
    pos = wrappers.Position(x=position.x, y=position.y) if position else None
    g = wrappers.Geo(lat=geo.lat, lon=geo.lon, alt=geo.alt) if geo else None
    return {"result": bridge.move_node(session_id, node_id, position=pos, geo=g)}

@app.post("/api/sessions/{session_id}/nodes/{node_id}/command")
def node_command(session_id: int, node_id: int, body: NodeCommandRequest):
    """在节点上执行命令（需要会话已启动）。同步 def 交由 FastAPI 线程池执行，不阻塞事件循环。"""
    rc, output = bridge.node_command(session_id, node_id, body.command, body.wait, body.shell)
    return {"return_code": rc, "output": output}

@app.get("/api/sessions/{session_id}/nodes/{node_id}/terminal")
def get_node_terminal(session_id: int, node_id: int):
    """获取节点的终端命令字符串"""
    return {"terminal": bridge.get_node_terminal(session_id, node_id)}


# ===================================================================
# Link RPCs
# ===================================================================

@app.post("/api/sessions/{session_id}/links", status_code=201)
def add_link(session_id: int, body: LinkModel):
    """添加链路。iface 未指定时按 CORE GUI 行为自动取节点下一个空闲接口 id
    （否则 proto3 默认实例让 daemon 收到 id=0，第二条链路报 interface already exists）"""
    def _next_iface_id(node_id: int) -> int:
        _node, ifaces, _links = bridge.get_node(session_id, node_id)
        used = {i.id for i in ifaces}
        i = 0
        while i in used:
            i += 1
        return i

    iface1 = body.iface1
    iface2 = body.iface2
    if iface1 is None:
        iface1 = InterfaceModel(id=_next_iface_id(body.node1_id))
    if iface2 is None:
        iface2 = InterfaceModel(id=_next_iface_id(body.node2_id))
    link = wrappers.Link(
        node1_id=body.node1_id, node2_id=body.node2_id,
        iface1=_wrappers_iface(iface1), iface2=_wrappers_iface(iface2),
        options=_wrappers_link_opts(body.options),
    )
    result, iface1, iface2 = bridge.add_link(session_id, link)
    return {
        "result": result,
        "iface1": _iface_to_dict(iface1) if iface1 else None,
        "iface2": _iface_to_dict(iface2) if iface2 else None,
    }

@app.patch("/api/sessions/{session_id}/links")
def edit_link(session_id: int, body: LinkModel):
    """编辑链路参数（bandwidth/delay/loss/jitter）及(未运行时)接口 IP。

    与 CORE-GUI 行为对齐：
    - 仅改 QoS options：任何状态都支持（CORE 运行中可改链路 QoS）；
    - 会话运行中（RUNTIME/DATACOLLECT）请求改接口 IP → 拒绝 409
      （CORE 协议 EditLinkRequest 无 ip4 字段，运行中不可在线改 IP）；
    - 会话未运行 → 允许改 IP，桥后端内部先删链、再以新 IP 重建
      （保留合并后的 QoS），前端一次调用即可完成"改 IP + 改 QoS"。
    """
    # --- 1. 合并式 options: 以当前链路 options 为基准, 只覆盖传入字段 ---
    cur_dict: dict = {}
    cur_if1: wrappers.Interface | None = None
    cur_if2: wrappers.Interface | None = None
    try:
        s = bridge.get_session(session_id)
        for l in (s.links or []):
            if l.node1_id == body.node1_id and l.node2_id == body.node2_id:
                if l.options:
                    cur_dict = _link_options_to_dict(l.options)
                cur_if1 = l.iface1
                cur_if2 = l.iface2
                break
    except Exception:
        pass
    if body.options:
        cur_dict.update(body.options.model_dump(exclude_unset=True))
    merged = LinkOptionsModel(**cur_dict) if cur_dict else None

    def _iface_with_ip(m: InterfaceModel | None, cur: wrappers.Interface | None) -> wrappers.Interface | None:
        """重建接口：以当前接口为基准保留 id/name/mac/node_id/mtu,
        覆盖 body 传入的 ip4/ip4_mask（未传的字段保留当前值）。"""
        if m is None:
            return cur
        base = cur if cur else wrappers.Interface(id=m.id)
        return wrappers.Interface(
            id=base.id,
            name=m.name if m.name is not None else base.name,
            mac=m.mac if m.mac is not None else base.mac,
            ip4=m.ip4 if m.ip4 is not None else base.ip4,
            ip4_mask=m.ip4_mask if m.ip4_mask is not None else base.ip4_mask,
            ip6=m.ip6 if m.ip6 is not None else base.ip6,
            ip6_mask=m.ip6_mask if m.ip6_mask is not None else base.ip6_mask,
            mtu=m.mtu if m.mtu is not None else base.mtu,
            node_id=base.node_id,
        )

    # --- 2. 是否请求了接口 IP 变更 ---
    want_ip = bool(
        (body.iface1 and (body.iface1.ip4 is not None or body.iface1.ip4_mask is not None))
        or (body.iface2 and (body.iface2.ip4 is not None or body.iface2.ip4_mask is not None))
    )

    if want_ip:
        state = None
        try:
            state = bridge.get_session(session_id).state
        except Exception:
            pass
        running = state in (wrappers.SessionState.RUNTIME, wrappers.SessionState.DATACOLLECT)
        if running:
            return _error_response(
                409,
                f"会话运行中({state.name})不可在线修改接口 IP",
                detail="CORE 协议 EditLinkRequest 无 ip4 字段，运行中接口 IP 被锁定；"
                       "要改 IP 请先停止会话（POST /api/sessions/{id}/stop）再 PATCH，"
                       "桥后端会自动删链重建；或运行中直接删链后以新 IP 重建。"
                       "仅 QoS（带宽/时延/丢包等）在运行中可改。",
                hint="先停止会话，再改 IP；QoS 可直接改",
            )
        # 未运行：删链 + 以新 IP 重建（保留原接口 id/name/mac 与合并后的 QoS）
        nif1 = _iface_with_ip(body.iface1, cur_if1)
        nif2 = _iface_with_ip(body.iface2, cur_if2)
        bridge.delete_link(session_id, wrappers.Link(
            node1_id=body.node1_id, node2_id=body.node2_id,
            iface1=nif1, iface2=nif2,
        ))
        bridge.add_link(session_id, wrappers.Link(
            node1_id=body.node1_id, node2_id=body.node2_id,
            iface1=nif1, iface2=nif2,
            options=_wrappers_link_opts(merged),
        ))
        return {"result": True, "action": "replace_ip", "state": state.name}

    # --- 3. 仅 QoS：走 EditLink（保留 id，不含 ip4）---
    link = wrappers.Link(
        node1_id=body.node1_id, node2_id=body.node2_id,
        # 协议只用 iface 的 id(接口序号), ip4 等字段传不上去, 保留 id 即可
        iface1=_wrappers_iface(body.iface1) if body.iface1 else None,
        iface2=_wrappers_iface(body.iface2) if body.iface2 else None,
        options=_wrappers_link_opts(merged),
    )
    return {"result": bridge.edit_link(session_id, link)}

@app.delete("/api/sessions/{session_id}/links")
def delete_link(session_id: int, body: LinkModel):
    """删除链路"""
    link = wrappers.Link(
        node1_id=body.node1_id, node2_id=body.node2_id,
        iface1=_wrappers_iface(body.iface1), iface2=_wrappers_iface(body.iface2),
    )
    return {"result": bridge.delete_link(session_id, link)}

@app.post("/api/sessions/{session_id}/links/linked")
def linked(session_id: int, body: LinkedRequest):
    """切换链路通断（Linked=False 断开, True 接通）"""
    bridge.linked(session_id, body.node1_id, body.node2_id,
                  body.iface1_id, body.iface2_id, body.linked)
    return {"result": True}


# ===================================================================
# WLAN RPCs
# ===================================================================

@app.get("/api/sessions/{session_id}/wlan/{node_id}")
def get_wlan_config(session_id: int, node_id: int):
    """获取 WLAN 配置"""
    return _config_option_dict_to_dict(bridge.get_wlan_config(session_id, node_id))

@app.post("/api/sessions/{session_id}/wlan")
def set_wlan_config(session_id: int, body: SetWlanConfigBody):
    """设置 WLAN 配置（range/bandwidth/delay/jitter/error等）"""
    return {"result": bridge.set_wlan_config(session_id, body.wlan_id, body.config)}

@app.post("/api/sessions/{session_id}/wlan/link")
def wlan_link(session_id: int, body: WlanLinkBody):
    """WLAN 节点间链路控制"""
    return {"result": bridge.wlan_link(session_id, body.wlan_id, body.node1_id,
                                        body.node2_id, body.linked)}


# ===================================================================
# Mobility RPCs
# ===================================================================

@app.get("/api/sessions/{session_id}/mobility/{node_id}")
def get_mobility_config(session_id: int, node_id: int):
    """获取移动模型配置"""
    return _config_option_dict_to_dict(bridge.get_mobility_config(session_id, node_id))

@app.post("/api/sessions/{session_id}/mobility")
def set_mobility_config(session_id: int, body: SetMobilityConfigBody):
    """设置移动模型配置"""
    return {"result": bridge.set_mobility_config(session_id, body.node_id, body.config)}

@app.post("/api/sessions/{session_id}/mobility/action")
def mobility_action(session_id: int, body: MobilityActionRequest):
    """移动模型动作控制 (START/PAUSE/STOP)"""
    action_map = {"START": wrappers.MobilityAction.START,
                  "PAUSE": wrappers.MobilityAction.PAUSE,
                  "STOP": wrappers.MobilityAction.STOP}
    action = action_map.get(body.action.upper())
    if action is None:
        return _error_response(400, "invalid mobility action",
                               detail=f"supported: {', '.join(action_map)}")
    return {"result": bridge.mobility_action(session_id, body.node_id, action)}


# ===================================================================
# Service RPCs
# ===================================================================

@app.get("/api/sessions/{session_id}/services/defaults")
def get_service_defaults(session_id: int, node_id: int, name: str):
    """获取服务默认配置"""
    d = bridge.get_service_defaults(session_id, node_id, name)
    return {
        "templates": d.templates,
        "config": _config_option_dict_to_dict(d.config),
        "modes": [{"name": m.name, "config": m.config} for m in (d.modes or [])],
    }

@app.get("/api/sessions/{session_id}/services/node")
def get_node_service(session_id: int, node_id: int, name: str):
    """获取节点上某服务的配置值"""
    return {"config": bridge.get_node_service(session_id, node_id, name)}

@app.get("/api/sessions/{session_id}/services/rendered")
def get_service_rendered(session_id: int, node_id: int, name: str):
    """获取服务渲染后的文件内容"""
    return {"rendered": bridge.get_service_rendered(session_id, node_id, name)}


@app.post("/native-services/{name}/preview")
def preview_native_service(name: str, body: NativeServicePreviewRequest):
    from native_services import render_preview

    links = [link for link in _topology_links(body.links)
             if body.node_id in (link.node1_id, link.node2_id) and link.node1_id != link.node2_id]
    return {'ok': True, 'data': {'files': render_preview(
        bridge, name, body.node_id, body.model, links, body.peer_types, body.config
    )}}

@app.post("/api/sessions/{session_id}/services/action")
def service_action(session_id: int, body: ServiceActionRequest):
    """服务动作: START/STOP/RESTART/VALIDATE"""
    action_map = {"START": wrappers.ServiceAction.START,
                  "STOP": wrappers.ServiceAction.STOP,
                  "RESTART": wrappers.ServiceAction.RESTART,
                  "VALIDATE": wrappers.ServiceAction.VALIDATE}
    action = action_map.get(body.action.upper())
    if action is None:
        return _error_response(400, "invalid service action",
                               detail=f"supported: {', '.join(action_map)}")
    return {"result": bridge.service_action(session_id, body.node_id, body.service, action)}

@app.post("/api/services/create")
def create_service(body: CreateServiceRequest):
    """运行时创建自定义服务"""
    service = wrappers.Service(
        name=body.name, group=body.group,
        executables=body.executables, dependencies=body.dependencies,
        directories=body.directories, files=body.files,
        startup=body.startup, validate=body.validate, shutdown=body.shutdown,
    )
    return {"result": bridge.create_service(service, body.templates, body.recreate)}


# ===================================================================
# Wireless RPCs
# ===================================================================

@app.post("/api/sessions/{session_id}/wireless/linked")
def wireless_linked(session_id: int, body: WirelessLinkedBody):
    """控制 Wireless 节点间链路通断"""
    bridge.wireless_linked(session_id, body.wireless_id, body.node1_id, body.node2_id, body.linked)
    return {"result": True}

@app.get("/api/sessions/{session_id}/wireless/{node_id}")
def get_wireless_config(session_id: int, node_id: int):
    """获取 Wireless 节点配置"""
    return _config_option_dict_to_dict(bridge.get_wireless_config(session_id, node_id))


# ===================================================================
# EMANE RPCs
# ===================================================================

@app.get("/api/sessions/{session_id}/emane/model")
def get_emane_model_config(session_id: int, node_id: int, model: str, iface_id: int = -1):
    """获取 EMANE 模型配置"""
    return _config_option_dict_to_dict(
        bridge.get_emane_model_config(session_id, node_id, model, iface_id))

@app.post("/api/sessions/{session_id}/emane/model")
def set_emane_model_config(session_id: int, body: SetEmaneModelConfigBody):
    """设置 EMANE 模型配置"""
    emc = wrappers.EmaneModelConfig(node_id=body.node_id, model=body.model,
                                     config=body.config, iface_id=body.iface_id)
    return {"result": bridge.set_emane_model_config(session_id, emc)}

@app.get("/api/sessions/{session_id}/emane/channel")
def get_emane_event_channel(session_id: int, nem_id: int):
    """获取 EMANE 事件通道信息"""
    ch = bridge.get_emane_event_channel(session_id, nem_id)
    return {"group": ch.group, "port": ch.port, "device": ch.device}

@app.post("/api/sessions/{session_id}/emane/link")
def emane_link(session_id: int, body: EmaneLinkBody):
    """EMANE NEM 链路控制"""
    return {"result": bridge.emane_link(session_id, body.nem1, body.nem2, body.linked)}

@app.post("/api/sessions/{session_id}/emane/pathlosses")
def emane_pathlosses(session_id: int, body: EmanePathlossesBody):
    """发布 EMANE 路径损耗（单次）"""
    pl = client.EmanePathlossesStreamer()
    import threading
    t = threading.Thread(target=lambda: bridge.emane_pathlosses(pl), daemon=True)
    t.start()
    pl.send(wrappers.EmanePathlossesRequest(
        session_id=session_id, node1_id=body.node1_id, iface1_id=body.iface1_id,
        node2_id=body.node2_id, iface2_id=body.iface2_id, rx1=body.rx1, rx2=body.rx2))
    pl.send(None)
    t.join(timeout=2)
    return {"result": True}


# ===================================================================
# XML RPCs
# ===================================================================

@app.get("/api/sessions/{session_id}/xml")
def save_xml(session_id: int):
    """导出会话为 EmulationScript XML"""
    import native_xml
    return {'data':native_xml.export(bridge,session_id),'session_id':session_id}

@app.post('/api/xml/open')
def open_xml(body: OpenXmlBody):
    """Native XML with platform bindings, a new deployment identity and unique runtime names."""
    import native_xml
    return native_xml.load(bridge,body.data,body.start)


# ===================================================================
# Utility RPCs
# ===================================================================

@app.get("/api/interfaces")
def get_interfaces():
    """获取宿主机可用网络接口"""
    return {"interfaces": bridge.get_interfaces()}

@app.post("/api/execute")
def execute_script(body: ExecuteScriptBody):
    """在 CoreEmu 上下文中执行 Python 脚本"""
    with request_file(".py") as path:
        path.write_text(body.script, encoding="utf-8")
        session_id = bridge.execute_script(str(path), body.args)
    return {"session_id": session_id}


# ===================================================================
# Global RPCs
# ===================================================================

@app.get("/api/config")
def get_config():
    """获取系统全局配置（可用服务列表 + EMANE 模型列表）"""
    cfg = bridge.get_config()
    return {
        "services": [{
            "name": s.name, "group": s.group, "executables": s.executables,
            "directories": s.directories, "files": s.files,
            "startup": s.startup, "validate": s.validate, "shutdown": s.shutdown,
        } for s in cfg.services],
        "emane_models": cfg.emane_models,
    }


# ===================================================================
# Server-Streaming → SSE
# ===================================================================

def _event_to_dict(event: wrappers.Event) -> dict:
    """会话事件 → 结构化详情（实现收敛到 platform_db.event_to_dict, 与落库共用一份）"""
    import platform_db
    return platform_db.event_to_dict(event)


@app.get('/api/sessions/{session_id}/events/stream')
def events_stream(session_id:int,request:Request):
    """Stream persisted event IDs; history and live data share one journal and cursor."""
    import platform_db,time
    try: drill_id=platform_db.ensure_drill(session_id)
    except platform_db.IdentityUnconfirmed as e: raise HTTPException(503,str(e))
    if not platform_db.subscribe_events(session_id):
        raise HTTPException(503,'CORE 事件订阅暂未就绪，正在退避重连')
    cursor=platform_db.event_cursor(drill_id)
    header=request.headers.get('last-event-id')
    if header and header.isdigit(): cursor=min(int(header),cursor)
    def generate():
        nonlocal cursor
        yield 'retry: 1000\n\n'
        yield 'event: ready\ndata: '+json.dumps({'ready':True,'drill_id':drill_id,'cursor':cursor})+'\n\n'
        check_at=0;heartbeat_at=0
        while True:
            if time.monotonic()>=check_at:
                check_at=time.monotonic()+1
                try:
                    current=platform_db.ensure_drill(session_id)
                    stream=platform_db._event_subs.get(session_id)
                    if current!=drill_id:
                        yield 'event: identity-changed\ndata: '+json.dumps({'drill_id':current})+'\n\n'
                        return
                    if stream is None or not stream.is_active():
                        yield 'event: upstream-error\ndata: {"message":"CORE 事件订阅中断，正在重连"}\n\n'
                        return
                except Exception as e:
                    yield 'event: identity-pending\ndata: '+json.dumps({'message':str(e)},ensure_ascii=False)+'\n\n'
                    return
            rows=platform_db.query_events(drill_id=drill_id,after_id=cursor,limit=250)
            for row in rows:
                event={**row['detail'],'event_id':row['id'],'drill_id':row['drill_id'],'ts':row['ts']}
                cursor=row['id']
                yield f'id: {cursor}\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n'
            if time.monotonic()>=heartbeat_at:
                heartbeat_at=time.monotonic()+5
                yield ': heartbeat\n\n'
            time.sleep(.1)
    return StreamingResponse(generate(),media_type='text/event-stream')

@app.get("/api/sessions/{session_id}/throughputs/stream")
async def throughputs_stream(session_id: int):
    """SSE 吞吐量流（每 3 秒）。注意 handler 必须传给 bridge（CORE 客户端后台线程驱动）"""
    def generate():
        tps = deque(maxlen=32)
        lock = threading.Lock()
        def handler(event: wrappers.ThroughputsEvent):
            with lock:
                tps.append(json.dumps({
                    "session_id": event.session_id,
                    "ifaces": [{"node_id": f.node_id, "iface_id": f.iface_id,
                                "throughput": f.throughput} for f in event.iface_throughputs],
                    "bridges": [{"node_id": getattr(b,"node_id",None), "node1_id": getattr(b,"node1_id",None), "node2_id": getattr(b,"node2_id",None),
                                 "throughput": b.throughput} for b in event.bridge_throughputs],
                }))
        stream = bridge.throughputs(session_id, handler)
        try:
            while True:
                if hasattr(stream,'is_active') and not stream.is_active():
                    yield 'event: upstream-error\ndata: {"message":"CORE 订阅中断，正在重连"}\n\n'
                    return
                with lock:
                    batch=list(tps)
                    tps.clear()
                for item in batch:
                    yield f"data: {item}\n\n"
                yield ": heartbeat\n\n"
                import time; time.sleep(1)
        finally:
            stream.cancel()
    return StreamingResponse(generate(), media_type="text/event-stream")

@app.get("/api/cpu-usage/stream")
async def cpu_usage_stream(delay: int = 1):
    """SSE CPU 使用率流。含 1s 心跳兜底（daemon 不发事件时前端也知道流是活的）"""
    def generate():
        cpus = deque(maxlen=32)
        lock = threading.Lock()
        def handler(event: wrappers.CpuUsageEvent):
            with lock:
                cpus.append(json.dumps({"usage": event.usage}))
        stream = bridge.cpu_usage(delay, handler)
        try:
            while True:
                if hasattr(stream,'is_active') and not stream.is_active():
                    yield 'event: upstream-error\ndata: {"message":"CORE 订阅中断，正在重连"}\n\n'
                    return
                with lock:
                    batch=list(cpus)
                    cpus.clear()
                for item in batch:
                    yield f"data: {item}\n\n"
                yield ": heartbeat\n\n"
                import time; time.sleep(1)
        finally:
            stream.cancel()
    return StreamingResponse(generate(), media_type="text/event-stream")


# ===================================================================
# Client-Streaming → WebSocket
# ===================================================================

@app.websocket("/api/sessions/{session_id}/move-nodes/ws")
async def move_nodes_ws(websocket: WebSocket, session_id: int):
    await websocket.accept()
    streamer = bridge.move_nodes_streamer(session_id)
    t = threading.Thread(target=lambda: bridge.move_nodes(streamer), daemon=True)
    t.start()
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("stop"):
                break
            if "position" in data:
                p = data["position"]
                streamer.send_position(data["node_id"], p.get("x", 0), p.get("y", 0))
            elif "geo" in data:
                g = data["geo"]
                streamer.send_geo(data["node_id"], g.get("lon", 0), g.get("lat", 0), g.get("alt", 0))
    except WebSocketDisconnect:
        pass
    finally:
        streamer.stop(); t.join(timeout=2)

@app.websocket("/api/sessions/{session_id}/emane-pathlosses/ws")
async def emane_pathlosses_ws(websocket: WebSocket, session_id: int):
    await websocket.accept()
    streamer = bridge.emane_pathlosses_streamer()
    t = threading.Thread(target=lambda: bridge.emane_pathlosses(streamer), daemon=True)
    t.start()
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("stop"):
                break
            streamer.send(wrappers.EmanePathlossesRequest(
                session_id=session_id, node1_id=data["node1_id"],
                iface1_id=data.get("iface1_id", 0), node2_id=data["node2_id"],
                iface2_id=data.get("iface2_id", 0),
                rx1=data.get("rx1", 0.0), rx2=data.get("rx2", 0.0)))
    except WebSocketDisconnect:
        pass
    finally:
        streamer.send(None); t.join(timeout=2)

@app.websocket("/api/sessions/{session_id}/emane-events/ws")
async def emane_events_ws(websocket: WebSocket, session_id: int):
    await websocket.accept()
    streamer = bridge.emane_events_streamer()
    t = threading.Thread(target=lambda: bridge.emane_events(streamer), daemon=True)
    t.start()
    try:
        while True:
            data = await websocket.receive_json()
            if data.get("stop"):
                break
            event_type = data.get("type", "location")
            if event_type == "location":
                loc = data.get("location", {})
                event = wrappers.EmaneEventsRequest(
                    session_id=session_id,
                    location=wrappers.LocationEvent(
                        node_id=loc.get("node_id", 0), iface_id=loc.get("iface_id", 0),
                        lon=loc.get("lon", 0.0), lat=loc.get("lat", 0.0), alt=loc.get("alt", 0.0)))
            elif event_type == "comm_effect":
                ce = data.get("comm_effect", {})
                event = wrappers.EmaneEventsRequest(
                    session_id=session_id,
                    comm_effect=wrappers.CommEffectEvent(
                        node1_id=ce.get("node1_id", 0), iface1_id=ce.get("iface1_id", 0),
                        node2_id=ce.get("node2_id", 0), iface2_id=ce.get("iface2_id", 0),
                        delay=ce.get("delay", 0), jitter=ce.get("jitter", 0),
                        loss=ce.get("loss", 0.0), dup=ce.get("dup", 0)))
            else:
                await websocket.send_json({"error": f"unknown event type: {event_type}"})
                continue
            streamer.send(event)
    except WebSocketDisconnect:
        pass
    finally:
        streamer.send(None); t.join(timeout=2)



# ===================================================================
# 拓扑导入（JSON / XML）
# ===================================================================

_NODE_TYPE_MAP = {
    "default": 0, "router": 0, "host": 0, "PC": 0, "pc": 0,
    "switch": 4, "hub": 5,
    "wlan": 6, "wireless_lan": 6,
    "rj45": 7, "tunnel": 8,
    "emane": 10, "tap_bridge": 11,
    "docker": 15,
    "wireless": 17, "podman": 18,
}


def _build_topology(session_id: int, topo: "TopologyInput"):
    """将 TopologyInput 转换为 session 中的节点和链路。"""
    from native_services import to_core

    if topo.options:
        session=bridge.get_session(session_id)
        session.set_options(topo.options)
        bridge.start_session(session,definition=True)
    # 1. Create nodes
    for node in topo.nodes:
        ntype = _NODE_TYPE_MAP.get(node.type.lower(), 0)
        node_obj = wrappers.Node(
            id=node.id, name=node.name,
            model=node.model,
            type=wrappers.NodeType(ntype),
            position=wrappers.Position(x=node.x, y=node.y),
            services=set(node.services) if node.services else None,
            icon=node.icon or None, image=node.image, emane=node.emane,
            service_configs=to_core(bridge, node.service_configs),
        )
        bridge.add_node(session_id, node_obj)
        if node.instance:
            import platform_db
            platform_db.register_deploy_ref(platform_db.ensure_drill(session_id),session_id,node.id,node.instance,node.instance_version)

    # 2. Create WLAN nodes (separate because they have config)
    for wlan in topo.wlans:
        node_obj = wrappers.Node(
            id=wlan.id, name=wlan.name,
            type=wrappers.NodeType.WIRELESS_LAN,
            position=wrappers.Position(x=wlan.x, y=wlan.y),
        )
        bridge.add_node(session_id, node_obj)
        # Set WLAN config
        bridge.set_wlan_config(session_id, wlan.id, {
            "range": wlan.range, "bandwidth": wlan.bandwidth,
            "delay": wlan.delay, "jitter": wlan.jitter, "error": wlan.error,
        })

    for link in _topology_links(topo.links):
        bridge.add_link(session_id, link)


def _topology_links(links: list[TopologyLink]):
    """Use the same interface allocation for previews and deployments."""
    iface_id_counter: dict[int, int] = {}

    def next_iface_id(node_id: int) -> int:
        value = iface_id_counter.get(node_id, 0)
        iface_id_counter[node_id] = value + 1
        return value

    helper = grpc_client.InterfaceHelper(ip4_prefix="10.0.0.0/24")
    for link in links:
        opts = None
        if any([link.bandwidth, link.delay, link.loss, link.jitter, link.dup]):
            opts = wrappers.LinkOptions(
                bandwidth=link.bandwidth or 0,
                delay=link.delay or 0,
                loss=link.loss or 0.0,
                jitter=link.jitter or 0,
                dup=link.dup or 0,
            )
        if link.options: opts = _wrappers_link_opts(link.options)
        for nid,iface in ((link.node1_id,link.iface1),(link.node2_id,link.iface2)):
            if iface and iface.id is not None:
                iface_id_counter[nid]=max(iface_id_counter.get(nid,0),iface.id+1)
        # Use custom IPs if provided, else auto-assign
        if link.iface1_ip4:
            iface1 = wrappers.Interface(
                id=next_iface_id(link.node1_id),
                ip4=link.iface1_ip4,
                ip4_mask=link.iface1_ip4_mask or 24,
            )
        else:
            iface1 = helper.create_iface(link.node1_id, next_iface_id(link.node1_id))
        if link.iface2_ip4:
            iface2 = wrappers.Interface(
                id=next_iface_id(link.node2_id),
                ip4=link.iface2_ip4,
                ip4_mask=link.iface2_ip4_mask or 24,
            )
        else:
            iface2 = helper.create_iface(link.node2_id, next_iface_id(link.node2_id))
        yield wrappers.Link(
            node1_id=link.node1_id, node2_id=link.node2_id,
            iface1=_wrappers_iface(link.iface1) if link.iface1 else iface1, iface2=_wrappers_iface(link.iface2) if link.iface2 else iface2, options=opts,
        )


def _parse_xml_topology(xml_str: str) -> "TopologyInput":
    """解析 XML 格式的拓扑描述。"""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml_str)
    if root.tag != "topology":
        raise ValueError("自定义 XML 根节点须为 topology；原生 XML 使用原生导入")
    name = root.get("name", "topology")
    nodes: list = []
    links: list = []
    wlans: list = []
    for elem in root.findall("node"):
        nid = int(elem.get("id", 0))
        nname = elem.get("name", f"n{nid}")
        ntype_raw = elem.get("type", "default")
        nx = float(elem.get("x", 200))
        ny = float(elem.get("y", 200))
        nmodel = elem.get("model", "PC")
        services_str = elem.get("services", "")
        services = [s.strip() for s in services_str.split(",") if s.strip()]
        if ntype_raw.lower() in ("wlan", "wireless_lan"):
            wlans.append(TopologyWlan(
                id=nid, name=nname, x=nx, y=ny,
                range=elem.get("range", "275"),
                bandwidth=elem.get("bandwidth", "54000000"),
                delay=elem.get("delay", "5000"),
            ))
        else:
            nodes.append(TopologyNode(
                id=nid, name=nname, type=ntype_raw,
                model=nmodel, x=nx, y=ny,
                services=services or None,
            ))
    for elem in root.findall("link"):
        node1 = int(elem.get("node1", 0))
        node2 = int(elem.get("node2", 0))
        link = TopologyLink(node1_id=node1, node2_id=node2)
        ip1 = elem.get("iface1_ip4")
        if ip1: link.iface1_ip4 = ip1
        ip1m = elem.get("iface1_ip4_mask")
        if ip1m: link.iface1_ip4_mask = int(ip1m)
        ip2 = elem.get("iface2_ip4")
        if ip2: link.iface2_ip4 = ip2
        ip2m = elem.get("iface2_ip4_mask")
        if ip2m: link.iface2_ip4_mask = int(ip2m)
        bw = elem.get("bandwidth")
        if bw: link.bandwidth = int(bw)
        dl = elem.get("delay")
        if dl: link.delay = int(dl)
        ls = elem.get("loss")
        if ls: link.loss = float(ls)
        links.append(link)
    return TopologyInput(name=name, nodes=nodes, links=links, wlans=wlans)


@app.post("/api/topology", status_code=201)
async def import_topology(request: Request):
    """导入网络拓扑（JSON 或 XML）。

    JSON (Content-Type: application/json):
    {
      "name": "my-top",
      "nodes": [{"id": 1, "name": "r1", "model": "router", "x": 100, "y": 100}],
      "links": [{"node1_id": 1, "node2_id": 2}],
      "wlans": [{"id": 3, "name": "w1", "x": 200, "y": 200}]
    }

    XML (Content-Type: application/xml):
    <topology name="my-top">
      <node id="1" name="r1" model="router" x="100" y="100"/>
      <link node1="1" node2="2"/>
      <node id="3" name="w1" type="wlan" x="200" y="200" range="300"/>
    </topology>
    """
    content_type = request.headers.get("content-type", "").lower()
    if "xml" in content_type or content_type.endswith("+xml"):
        body = await request.body()
        try:
            import xml.etree.ElementTree as ET
            text = body.decode('utf-8')
            if ET.fromstring(text).tag != 'topology':
                response = await asyncio.to_thread(open_xml,OpenXmlBody(data=text,start=False))
                if not response['result']: return _error_response(400,'原生 XML 导入失败')
                session=await asyncio.to_thread(bridge.get_session,response['session_id'])
                return {**response,'name':session.file,'nodes':len(session.nodes),'links':len(session.links or [])}
            topo = _parse_xml_topology(text)
        except Exception as e:
            return _error_response(400, "invalid XML", detail=str(e),
                                   hint="请检查 XML 格式是否正确")
    elif "json" in content_type or not content_type:
        try:
            body_dict = await request.json()
            topo = TopologyInput(**body_dict)
        except Exception as e:
            return _error_response(400, "invalid JSON", detail=str(e),
                                   hint="请检查 JSON 格式是否正确")
    else:
        return _error_response(415, "unsupported media type",
                               detail=f"received: {content_type}",
                               hint="supported: application/json, application/xml")

    try:
        import topologies
        deployment = await asyncio.to_thread(topologies.deploy_definition,topo.model_dump(exclude_none=True))
        return {'session_id':deployment['core_sid'],'deployment_id':deployment['deployment_id'],'name':topo.name,'nodes':len(topo.nodes)+len(topo.wlans),'links':len(topo.links)}
    except Exception as e:
        if isinstance(e,ValueError): return _error_response(400,str(e))
        grpc = _parse_grpc_error(e)
        if grpc:
            return _error_response(grpc["http"], grpc["detail"],
                                   detail="gRPC status: " + grpc["status"],
                                   hint=_hint_from_detail(grpc["detail"]))
        raise


# ===================================================================
# 入口
# ===================================================================

@app.websocket("/api/sessions/{session_id}/nodes/{node_id}/terminal/ws")
async def terminal_ws(websocket: WebSocket, session_id: int, node_id: int):
    """WebSocket 交互式终端。通过 vcmd 连接到节点的 bash，实现双向交互。

    用法:
      const ws = new WebSocket("ws://host:8080/api/sessions/1/nodes/1/terminal/ws");
      ws.onmessage = (e) => console.log(e.data);     // 收到终端输出
      ws.send("ping 10.0.0.2\r\n");                  // 发送命令
      ws.send("\x03");                                 // Ctrl+C 中断
    """
    await websocket.accept()
    process: subprocess.Popen | None = None
    try:
        # 获取 vcmd 终端命令
        terminal = await asyncio.to_thread(bridge.get_node_terminal,session_id,node_id)
        if not terminal:
            await websocket.send_json({"error": "terminal not available (session not instantiated?)"})
            await websocket.close(code=1011)
            return

        # 从 "vcmd -c <channel> -- /bin/bash" 构建交互式命令
        # vcmd 的 -i (PTY 交互) 模式要求 stdin/stdout 是真实终端,
        # 否则 termioraw()/tcgetattr()/SIGWINCH ioctl() 全失败并打印报错。
        # 所以用 pty.openpty() 生成一对真实终端, vcmd 挂到 slave 上,
        # 后端只读写 master 端。
        parts = terminal.split(" -- ", 1)
        if len(parts) == 2:
            vcmd_part = parts[0]
            shell_cmd = parts[1]
            # 在 -c 前插入 -i (PTY 交互), 不加 -q(否则静默无回显)
            vcmd_part = vcmd_part.replace(" -c ", " -i -c ")
            cmd = f"{vcmd_part} -- {shell_cmd}"
        else:
            cmd = terminal

        logger.info("terminal_ws: running %s", cmd)

        # 创建真实 PTY 对: vcmd -i 需要 tty, 否则 tcgetattr/ioctl 报错
        master_fd, slave_fd = pty.openpty()

        # 启动 vcmd 子进程, stdin/stdout/stderr 都挂到 PTY slave 上
        # start_new_session=True: 让 vcmd 成为独立进程组组长, 便于断开时整组回收
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)  # 父进程只保留 master 端
        os.set_blocking(master_fd, False)

        loop = asyncio.get_running_loop()
        from buffers import PtyQueue
        read_queue = PtyQueue(loop,master_fd)

        def _on_master_readable():
            """PTY master 可读回调: 读数据入队, EOF/IO 错 → None 终止"""
            try:
                data = os.read(master_fd, 4096)
            except BlockingIOError:
                return
            except OSError:
                read_queue.put_nowait(None)
                return
            if not data:
                read_queue.put_nowait(None)
            else:
                read_queue.put_nowait(data)

        read_queue.callback = _on_master_readable
        loop.add_reader(master_fd, _on_master_readable)

        async def pipe_stdout_to_ws():
            """后台任务：PTY master 输出持续转发到 WebSocket（同时喂给命令留痕的输出缓冲）"""
            try:
                while True:
                    data = await read_queue.get()
                    if data is None:
                        break
                    text = data.decode(errors="replace")
                    try:
                        _feed_output(text)
                    except Exception:
                        pass
                    await websocket.send_text(text)
            except (WebSocketDisconnect, ConnectionResetError):
                pass
            except Exception:
                logger.exception("terminal_ws stdout pipe error")

        reader_task = asyncio.create_task(pipe_stdout_to_ws())

        # 终端命令留痕+结果（设计 §8.1）：输入行与其后的输出配对入库。
        # 输出按行缓冲(剥 ANSI), 直到下一命令输入 / Ctrl+C / 断开时 flush 上一条。
        # 交互式程序(top/vim)的输出归并不保证可读——演练"命令→结果"形态为准。
        import re as _re
        _ansi_re = _re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[=>]')
        node_name = ""
        try:
            node, _ifaces, _links = bridge.get_node(session_id, node_id)
            node_name = node.name
        except Exception:
            pass
        pending = {"input": None, "output": []}

        def _flush_cmd():
            if not pending["input"]:
                return
            out = "\n".join(pending["output"])[:2000]
            try:
                import platform_db
                platform_db.write_audit("term.node", session_id=session_id, node_id=node_id,
                                         node=node_name or str(node_id),
                                         input=pending["input"][:500], output=out)
            except Exception:
                logger.exception("terminal audit failed")
            pending["input"] = None
            pending["output"] = []

        def _on_line(line: str):
            line = line.replace("\x7f", "").strip()
            _flush_cmd()                      # 上一条命令连同其输出落库
            if line:
                pending["input"] = line
                pending["output"] = []

        def _feed_output(chunk: str):
            for ln in _ansi_re.sub('', chunk.replace('\r', '')).split('\n'):
                ln = ln.strip()
                if ln:
                    if sum(map(len,pending['output'])) < 4000:
                        pending['output'].append(ln[:2000])
                    elif not pending['output'][-1].endswith('[输出已截断]'):
                        pending['output'].append('[输出已截断]')

        input_buf: list[str] = []

        # 主循环：从 WebSocket 读取 → 写入 PTY master (等价真实键盘输入)
        try:
            while True:
                raw = await websocket.receive_text()
                if process.returncode is not None:
                    break
                # 处理 Ctrl+C / Ctrl+D
                if raw == "\x03":
                    os.write(master_fd, b"\x03")  # PTY 会把它变成前台进程的 SIGINT
                    _on_line("".join(input_buf) + "^C")
                    input_buf.clear()
                elif raw == "\x04":
                    os.write(master_fd, b"\x04")
                else:
                    os.write(master_fd, raw.encode())
                    for ch in raw:
                        if ch in ("\n", "\r"):
                            _on_line("".join(input_buf))
                            input_buf.clear()
                        elif ch >= " ":
                            if len(input_buf)<8192: input_buf.append(ch)
                            elif input_buf[-1]!='[输入已截断]': input_buf.append('[输入已截断]')
        except WebSocketDisconnect:
            pass
        finally:
            reader_task.cancel()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):
                pass
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass
            # 关键: vcmd 对 SIGTERM 不响应(见 CLAUDE.md 坑#2), 必须 SIGKILL。
            # 且 vcmd 会 fork 出 /bin/bash 子进程, 单杀父进程会留孤儿。
            # 因为启动时挂了 start_new_session=True, 这里整进程组 SIGKILL,
            # 确保同组的 vcmd + /bin/bash 全部回收。
            try:
                _flush_cmd()  # 断开时把最后一条命令(含其输出)落库
            except Exception:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            try:
                await process.wait()
            except Exception:
                pass
            try:
                os.close(master_fd)
            except Exception:
                pass
    except Exception as e:
        logger.exception("terminal_ws error")
        try:
            await websocket.send_json({"error": str(e)[:200]})
            await websocket.close(code=1011)
        except Exception:
            pass

# ── 靶标包管理路由（Tab1 镜像管理 + Tab2 独立容器管理）──
from target_admin import router as target_admin_router
app.include_router(target_admin_router, tags=["靶标管理"])
from topologies import router as topology_router
app.include_router(topology_router, tags=["拓扑草稿与部署"])
from publications import router as publications_router
app.include_router(publications_router,tags=["版本草稿与发布"])
from jobs import router as jobs_router
app.include_router(jobs_router, tags=["导入任务"])
from image_services import router as image_services_router
app.include_router(image_services_router, tags=["镜像服务管理"])
from native_services import router as native_services_router
app.include_router(native_services_router, tags=["CORE 原生服务"])
from network_activity import router as network_activity_router
app.include_router(network_activity_router, tags=["背景流量与 Snort"])

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=settings.host, port=settings.port)
