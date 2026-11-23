"""Editable IF-01 action catalog and V1.0 reference entries."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

import repository as repo

router = APIRouter(prefix='/validation/action-mappings')
COLLECTION = 'validation_action_mappings'
CATALOG = 'catalog'
DEFAULT_VERSION = 'validation/1'
REFERENCE_ACTIONS = {
    'DiscoverRemoteSystems': ('发现远程系统', '1'),
    'ServiceDiscovery': ('服务发现', '激进 1 / 隐蔽 3'),
    'DiscoverDeception': ('发现诱饵', '2'),
    'ExploitRemoteService': ('远程服务利用', '4'),
    'PrivilegeEscalate': ('权限提升', '2'),
    'DegradeServices': ('服务降质', '2'),
    'Impact': ('影响', '2'),
    'Withdraw': ('撤离', '1'),
    'Sleep': ('等待', '1'),
}


class ActionMapping(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    action_name: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_.-]{0,63}$')
    label: str = Field(min_length=1, max_length=64)
    description: str = Field(default='', max_length=400)


def catalog():
    record = repo.read(COLLECTION, CATALOG)
    if record is None:
        defaults = {'revision': 2, 'reference_seeded': True, 'items': {
            'HttpServiceCheck': {
                'action_name': 'HttpServiceCheck',
                'label': 'HTTP 服务验证',
                'description': '检查拓扑中已标记的 HTTP 目标服务。',
            },
        }}
        _add_reference_entries(defaults)
        return defaults
    if record.get('reference_seeded'):
        return record
    with repo.transaction():
        record = repo.read(COLLECTION, CATALOG)
        if not record.get('reference_seeded'):
            _add_reference_entries(record)
            record['reference_seeded'] = True
            _save(record)
    return record


def _add_reference_entries(record):
    for name, (label, steps) in REFERENCE_ACTIONS.items():
        entry = record['items'].setdefault(name, {'action_name': name, 'label': label, 'description': ''})
        entry['simulation_steps'] = steps


def _entry(mapping, previous=None):
    payload = mapping.model_dump()
    reference = REFERENCE_ACTIONS.get(mapping.action_name)
    steps = reference[1] if reference else (previous or {}).get('simulation_steps')
    if steps:
        payload['simulation_steps'] = steps
    return payload


def dictionary_version(record):
    return f"validation/{record['revision']}"


def _save(record):
    record['revision'] += 1
    repo.put(COLLECTION, CATALOG, record)


@router.get('')
def list_mappings():
    record = catalog()
    items = sorted(record['items'].values(), key=lambda item: item['action_name'].casefold())
    return {'data': {'dictionary_version': dictionary_version(record), 'items': items}}


@router.post('', status_code=201)
def create_mapping(mapping: ActionMapping):
    # 动作映射编辑已关闭（字典只读）：IF-01 受理语义依赖稳定的字典版本，线上不接受运行期增删
    raise HTTPException(403, '动作映射编辑已关闭（字典只读）')


@router.put('/{action_name}')
def update_mapping(action_name: str, mapping: ActionMapping):
    # 动作映射编辑已关闭（字典只读）：IF-01 受理语义依赖稳定的字典版本，线上不接受运行期增删
    raise HTTPException(403, '动作映射编辑已关闭（字典只读）')


@router.delete('/{action_name}')
def delete_mapping(action_name: str):
    # 动作映射编辑已关闭（字典只读）：IF-01 受理语义依赖稳定的字典版本，线上不接受运行期增删
    raise HTTPException(403, '动作映射编辑已关闭（字典只读）')
