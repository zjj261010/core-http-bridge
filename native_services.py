"""CORE service selection and per-node configuration for native nodes."""
import base64
import binascii
import logging
from contextlib import contextmanager
from functools import lru_cache

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

router = APIRouter()
logger = logging.getLogger(__name__)
NATIVE_TYPES = {'default', 'host', 'router', 'pc'}
DEFAULT_MODELS = {'PC', 'host', 'router', 'mdr'}
# CORE renders custom file contents with Mako. Pass user files as literal data.
FILE_PREFIX = "${__import__('base64').b64decode('"
FILE_SUFFIX = "').decode('utf-8')}"


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    config: dict[str, str] = Field(default_factory=dict)
    files: dict[str, str] = Field(default_factory=dict)


@lru_cache(maxsize=1)
def builtin_names():
    from core.services.manager import ServiceManager

    manager = ServiceManager()
    manager.load_locals()
    return frozenset(manager.services)


def catalog(bridge):
    from target_admin import _load_manifests

    image_names = {
        service['name'] for manifest in _load_manifests().values() if isinstance(manifest, dict)
        for service in manifest.get('services') or [] if isinstance(service, dict) and service.get('name')
    }
    builtins = builtin_names()
    return {
        service.name: {
            'name': service.name,
            'group': service.group,
            'origin': 'builtin' if service.name in builtins else 'custom',
            'dependencies': list(service.dependencies),
        }
        for service in bridge.get_config().services
        if service.name in builtins or (
            service.name not in image_names and not service.name.startswith(('svc-', 'imgsvc-', 'imgmode-'))
        )
    }


def check_selection(names, configs=None, *, registered=None, check_dependencies=False):
    selected = set(names)
    extra = set(configs or {}) - selected
    if any(configs[name] is not None for name in extra):
        raise ValueError('先启用服务再配置：' + '、'.join(sorted(extra)))
    if not selected:
        return
    if registered is None and not selected.issubset(builtin_names()):
        import app
        try:
            registered = catalog(app.bridge)
        except Exception as error:
            raise ValueError('无法核对 CORE 自定义服务是否已注册') from error
    if registered is None:
        return
    missing = selected - registered.keys()
    if missing:
        raise ValueError('服务未注册或不能用于原生节点：' + '、'.join(sorted(missing)))
    if check_dependencies:
        for name in sorted(selected):
            dependencies = set(registered[name]['dependencies']) - selected
            if dependencies:
                raise ValueError(name + ' 缺少依赖：' + '、'.join(sorted(dependencies)))


def effective_configs(inherited, overrides, selected):
    merged = {**(inherited or {}), **(overrides or {})}
    return {name: merged[name] for name in selected if merged.get(name) is not None}


@contextmanager
def definition_node(bridge, names):
    from core.api.grpc import wrappers

    session = bridge.create_session()
    try:
        node = wrappers.Node(id=1, name='service-preview', model='bare', services=set(names))
        bridge.add_node(session.id, node)
        yield session.id, node.id
    finally:
        if not bridge.delete_session(session.id):
            logger.error('CORE service preview session %s could not be removed', session.id)
            raise RuntimeError('无法清理 CORE 服务预览会话')


def complete_config(values, defaults):
    """When CORE receives overrides it expects all declared parameter values."""
    extra = values.keys() - defaults.keys()
    if extra:
        raise ValueError('未知服务参数：' + '、'.join(sorted(extra)))
    result = {key: option.value for key, option in defaults.items()}
    for key, value in values.items():
        option = defaults[key]
        kind = int(getattr(option.type, 'value', option.type))
        choices = option.select or []
        try:
            if choices and value not in choices or kind == 11 and value not in ('0', '1'):
                raise ValueError()
            if 1 <= kind <= 8:
                bits = (8, 16, 32, 64)[(kind - 1) % 4]
                low = 0 if kind <= 4 else -(1 << (bits - 1))
                high = (1 << bits) - 1 if kind <= 4 else (1 << (bits - 1)) - 1
                if not low <= int(value) <= high:
                    raise ValueError()
            if kind == 9:
                from math import isfinite
                if not isfinite(float(value)):
                    raise ValueError()
        except (TypeError, ValueError) as error:
            raise ValueError('服务参数值无效：' + (option.label or key)) from error
        result[key] = value
    return result


def to_core(bridge, configs):
    from core.api.grpc.wrappers import ServiceData

    enabled = {name: value for name, value in (configs or {}).items() if value and (value.config or value.files)}
    if not enabled:
        return {}
    result = {}
    with definition_node(bridge, enabled) as (sid, nid):
        for name, value in enabled.items():
            defaults = bridge.get_service_defaults(sid, nid, name)
            extra = value.files.keys() - defaults.templates.keys()
            if extra:
                raise ValueError(name + ' 不提供这些配置文件：' + '、'.join(sorted(extra)))
            files = {
                path: FILE_PREFIX + base64.b64encode(content.encode()).decode('ascii') + FILE_SUFFIX
                for path, content in value.files.items()
            }
            result[name] = ServiceData(
                config=complete_config(value.config, defaults.config) if value.config else {},
                templates=files,
            )
    return result


def from_core(configs):
    overrides, external = {}, {}
    for service, data in configs.items():
        files = {}
        for path, template in data.templates.items():
            if template.startswith(FILE_PREFIX) and template.endswith(FILE_SUFFIX):
                encoded = template[len(FILE_PREFIX):-len(FILE_SUFFIX)]
                try:
                    files[path] = base64.b64decode(encoded, validate=True).decode()
                    continue
                except (ValueError, binascii.Error):
                    pass
            external.setdefault(service, []).append(path)
        if data.config or files:
            overrides[service] = {'config': dict(data.config), 'files': files}
    return overrides, external


def render_preview(bridge, name, node_id, model, links, peer_types, values):
    """Render installed templates with the draft's adjacent links and parameters."""
    from core.api.grpc import wrappers

    if name not in catalog(bridge):
        raise HTTPException(404, '该服务未注册或不能用于原生节点')
    with definition_node(bridge, [name]) as (sid, preview_id):
        defaults = bridge.get_service_defaults(sid, preview_id, name)
        try:
            config = complete_config(values, defaults.config) if values else {}
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        session = bridge.get_session(sid)
        session.nodes = {
            node_id: wrappers.Node(id=node_id, name=f'n{node_id}', model=model,
                                   services={name}, service_configs={name: wrappers.ServiceData(config=config)})
        }
        network_types = {'switch': wrappers.NodeType.SWITCH, 'hub': wrappers.NodeType.HUB,
                         'wlan': wrappers.NodeType.WIRELESS_LAN}
        for link in links:
            peer_id = link.node2_id if link.node1_id == node_id else link.node1_id
            if peer_id not in session.nodes:
                peer_type = network_types.get(peer_types.get(peer_id), wrappers.NodeType.DEFAULT)
                session.nodes[peer_id] = wrappers.Node(id=peer_id, name=f'n{peer_id}', type=peer_type, model='bare')
        session.links = links
        ready, _ = bridge.start_session(session, definition=True)
        if not ready:
            raise HTTPException(422, 'CORE 无法根据当前节点和接口定义生成脚本')
        return bridge.get_service_rendered(sid, node_id, name)


@router.get('/native-services')
def list_native_services():
    import app
    return {'ok': True, 'data': sorted(catalog(app.bridge).values(), key=lambda item: item['name'].casefold())}


@router.get('/native-services/{name}/defaults')
def service_defaults(name: str):
    import app

    if name not in catalog(app.bridge):
        raise HTTPException(404, '该服务未注册或不能用于原生节点')
    with definition_node(app.bridge, [name]) as (sid, nid):
        defaults = app.bridge.get_service_defaults(sid, nid, name)
    return {'ok': True, 'data': {
        'config': app._config_option_dict_to_dict(defaults.config),
        'files': sorted(defaults.templates),
        'templates': defaults.templates,
        'modes': defaults.modes,
    }}
