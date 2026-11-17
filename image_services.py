"""Image service inventories, staged edits and immutable executable modes.

The inventory is independent of node enablement. Legacy implementations remain
attachments; they are never guessed into groups or rewritten in place.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import io
import ipaddress
import json
import shlex
import uuid
import zipfile
import concurrent.futures
import threading
import tarfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

import delivery
import repository as repo
from settings import settings

router = APIRouter()
MAX_SCRIPT = 2 * 1024 * 1024
MAX_IMPORT = 32 * 1024 * 1024
_exports = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='image-export')
_export_slots = threading.BoundedSemaphore(4)


def key(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:24]


def inspect_image(ref):
    import target_admin as ta
    text, code = ta._docker('image inspect ' + shlex.quote(ref), 30)
    if code:
        raise ValueError('镜像不存在，请先导入：' + ref)
    raw = json.loads(text)
    image = raw[0] if isinstance(raw, list) else raw
    return image


def attachment_path(digest):
    if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('附件摘要无效')
    return settings.data_dir / 'image-services' / 'attachments' / digest


def store_attachment(content, name, role='startup'):
    if not content or len(content) > MAX_SCRIPT:
        raise ValueError('脚本不能为空，且每个文件不得超过 2 MiB')
    content.decode('utf-8')
    digest = hashlib.sha256(content).hexdigest()
    path = attachment_path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        import os, tempfile
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            stream.write(content)
            temporary = stream.name
        os.replace(temporary, path)
    return {'name': Path(name).name, 'role': role, 'sha256': digest, 'size': len(content)}


def read_attachment(item):
    content = attachment_path(item['sha256']).read_bytes()
    if hashlib.sha256(content).hexdigest() != item['sha256']:
        raise ValueError('附件校验失败：' + item['name'])
    return content.decode('utf-8')


def legacy_catalog(manifest, sources=None, templates=None):
    services = []
    for entry in manifest.get('services', []):
        sid = 'service-' + key(entry['name'])[:12]
        services.append({**copy.deepcopy(entry),'id': sid, 'name': entry.get('display_name') or entry['name'],
                         'software': entry.get('software', ''), 'description': '',
                         'ports': [{'port': p, 'protocol': 'tcp', 'address': '0.0.0.0'} for p in entry.get('ports', [])],
                         'modes': [{'id': 'mode-' + key(entry['name'])[:12], 'name': entry.get('mode') or '原有启动方式',
                                    'strategy': 'legacy', 'implementation': entry['name'], 'default': True,
                                    'interpreter': 'sh', 'workdir': '/', 'args': '', 'env': [],
                                    'ports': None, 'depends_on': [], 'files': []}]})
    owners={m['implementation']:(s['id'],m['id']) for s in services for m in s['modes']}
    for source in (sources or {}).values():
        for cls in ast.parse(source).body:
            if not isinstance(cls,ast.ClassDef): continue
            names=delivery.names(ast.unparse(cls))
            if not names or names[0] not in owners: continue
            dependencies=[]
            for statement in cls.body:
                if isinstance(statement,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='dependencies' for t in statement.targets):
                    try: dependencies=ast.literal_eval(statement.value)
                    except (ValueError,TypeError): pass
            sid,mid=owners[names[0]]
            mode=next(m for s in services if s['id']==sid for m in s['modes'] if m['id']==mid)
            mode['depends_on']=[{'service_id':owners[name][0],'mode_id':owners[name][1]} for name in dependencies if name in owners]
    return {'services': services, 'legacy_sources': sources or {}, 'legacy_templates': templates or {},'legacy_manifest':copy.deepcopy(manifest),
            'migration_note': '旧服务逐项保留。需要合并为同一服务的不同模式时，请在服务表中明确调整归属。' if services else ''}


def catalog_for(ref):
    import target_admin as ta
    image = inspect_image(ref)
    cid = 'image-' + key(image['Id'])
    record = repo.read('image_catalogs', cid)
    if record:
        record = copy.deepcopy(record)
        record['image_ref'] = ref
    else:
        manifest = ta._manifest_index().get(ref)
        published = repo.revision('target', manifest['name'], manifest.get('target_revision')) if manifest else None
        migrated = legacy_catalog(manifest or {}, (published or {}).get('sources'), (published or {}).get('templates'))
        if manifest and not published:
            root = Path(ta.CUSTOM_SERVICES_DIR) / manifest['name']
            migrated['legacy_sources'] = {p.name: p.read_text() for p in root.glob('*.py') if p.name != '__init__.py'}
            migrated['legacy_templates'] = {p.name: p.read_text() for p in (root / 'templates').glob('*') if p.is_file()}
        record = {'catalog_id': cid, 'image_ref': ref, 'image_id': image['Id'], 'revision': 0,
                  'target_name': (manifest or {}).get('name') or cid, 'state': 'applied' if published else 'unconfigured',
                  'applied_target_version': (published or {}).get('version'), **migrated}
    config = image.get('Config') or {}
    record['info'] = {'id': image['Id'], 'tags': image.get('RepoTags') or [], 'size': image.get('Size'),
                      'platform': image.get('Os', '') + '/' + image.get('Architecture', ''),
                      'created': image.get('Created'), 'workdir': config.get('WorkingDir') or '/',
                      'entrypoint': config.get('Entrypoint') or [], 'command': config.get('Cmd') or [],
                      'ports': list((config.get('ExposedPorts') or {}).keys()), 'labels': config.get('Labels') or {}}
    return record


def normalize(services):
    if not isinstance(services, list):
        raise ValueError('服务清单必须是表格记录')
    if any(not isinstance(service,dict) for service in services):
        raise ValueError('服务表中存在无效记录')
    result = copy.deepcopy(services)
    issues, identities, names = [], set(), set()
    for service in result:
        service.setdefault('id', 'service-' + uuid.uuid4().hex)
        if service['id'] in identities:
            raise ValueError('服务标识重复')
        identities.add(service['id'])
        name = str(service.get('name', '')).strip()
        service['name'] = name
        if not name or name.casefold() in names:
            issues.append('服务名称为空或重复：' + name)
        names.add(name.casefold())
        service.setdefault('ports', [])
        ports(service['ports'])
        modes = service.setdefault('modes', [])
        if not isinstance(modes,list) or any(not isinstance(mode,dict) for mode in modes):
            raise ValueError(name+' 的启动模式表无效')
        if not modes:
            issues.append(name + ' 尚未配置启动模式')
        mode_ids, mode_names = set(), set()
        defaults = 0
        for mode in modes:
            mode.setdefault('id', 'mode-' + uuid.uuid4().hex)
            mode.setdefault('name', '')
            if mode['id'] in mode_ids:
                raise ValueError(name + ' 的模式标识重复')
            mode_ids.add(mode['id'])
            mode['name'] = mode['name'].strip()
            if not mode['name'] or mode['name'].casefold() in mode_names:
                issues.append(name + ' 的模式名称为空或重复')
            mode_names.add(mode['name'].casefold())
            defaults += bool(mode.get('default'))
            if mode.get('strategy') not in ('script', 'manual', 'legacy'):
                raise ValueError(name + ' 的启动方式无效')
            if mode.get('interpreter', 'sh') not in ('sh', 'bash', 'python3', 'shebang'):
                raise ValueError('不支持的脚本执行器')
            if not str(mode.get('workdir') or '/').startswith('/'):
                issues.append(name + ' / ' + mode['name'] + ' 工作目录必须是绝对路径')
            shlex.split(mode.get('args') or '')
            ports(mode.get('ports') or [])
            files = mode.setdefault('files', [])
            if mode['strategy'] == 'script' and not any(f.get('role') == 'startup' for f in files):
                issues.append(name + ' / ' + mode['name'] + ' 缺少启动脚本')
            file_names = set()
            for item in files:
                if item.get('role') not in ('startup', 'shutdown', 'validate'):
                    raise ValueError('脚本用途无效')
                identity = (item['role'], item['name'])
                if identity in file_names:
                    issues.append(name + ' / ' + mode['name'] + ' 存在重复脚本文件')
                file_names.add(identity)
                read_attachment(item)
            for item in mode.get('env') or []:
                import re
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', item.get('name', '')):
                    raise ValueError('环境变量名称无效')
        if defaults > 1:
            issues.append(name + ' 只能有一个默认模式')
    for service in result:
        for mode in service['modes']:
            for dependency in mode.get('depends_on') or []:
                dep = next((s for s in result if s['id'] == dependency.get('service_id')), None)
                if not dep or (dependency.get('mode_id') and not any(m['id'] == dependency['mode_id'] for m in dep['modes'])):
                    issues.append(service['name'] + ' / ' + mode['name'] + ' 的依赖已被移除')
    return result, issues


def ports(rows):
    if not isinstance(rows,list):
        raise ValueError('端口必须使用行表格记录')
    result = []
    for row in rows:
        if not isinstance(row,dict) or isinstance(row.get('port'),bool) or isinstance(row.get('end'),bool):
            raise ValueError('端口记录无效')
        port, end = int(row['port']), int(row.get('end') or row['port'])
        if str(row['port']) not in (str(port),str(float(port))) or row.get('end') is not None and str(row['end']) not in (str(end),str(float(end))):
            raise ValueError('端口必须是整数')
        protocol = row.get('protocol', 'tcp').lower()
        address = ipaddress.ip_address(row.get('address') or '0.0.0.0')
        if not 1 <= port <= end <= 65535 or protocol not in ('tcp', 'udp'):
            raise ValueError('端口范围或协议无效')
        result.append((port, end, protocol, address, bool(row.get('ipv6_only', False))))
    return result


def overlaps(a, b):
    if a[2] != b[2] or a[1] < b[0] or b[1] < a[0]:
        return False
    x, y = a[3], b[3]
    if x.version != y.version:
        return (x.version == 6 and x.is_unspecified and not a[4]) or (y.version == 6 and y.is_unspecified and not b[4])
    return x == y or x.is_unspecified or y.is_unspecified


def combination(catalog, selection):
    if not isinstance(selection, dict):
        raise ValueError('服务选择必须按服务标识保存')
    available = {s['id']: s for s in catalog['services']}
    active, errors, warnings, occupied, order = {}, [], [], [], []
    for sid, mid in selection.items():
        if not mid:
            continue
        service = available.get(sid)
        mode = next((m for m in (service or {}).get('modes', []) if m['id'] == mid), None)
        if not mode:
            errors.append('服务或启动模式已失效：' + str(sid))
            continue
        active[sid] = (service, mode)
    for sid, (service, mode) in active.items():
        declared = service.get('ports', []) if mode.get('ports') is None else mode['ports']
        if not declared and mode['strategy'] != 'manual':
            warnings.append(service['name'] + ' 未声明监听端口，无法静态确认占用')
        for port in ports(declared):
            for other, occupied_port in occupied:
                if other != sid and overlaps(port, occupied_port):
                    errors.append(service['name'] + ' 与 ' + active[other][0]['name'] + ' 端口冲突：' + str(port[0]) + '/' + port[2])
            occupied.append((sid, port))
        for dep in mode.get('depends_on') or []:
            chosen = active.get(dep['service_id'])
            if not chosen:
                errors.append(service['name'] + ' 缺少依赖：' + available.get(dep['service_id'], {}).get('name', dep['service_id']))
            elif dep.get('mode_id') and chosen[1]['id'] != dep['mode_id']:
                errors.append(service['name'] + ' 的依赖模式不兼容')
    visited, visiting = set(), []

    def visit(sid):
        if sid in visiting:
            errors.append('依赖循环：' + ' → '.join(active[x][0]['name'] for x in visiting + [sid]))
            return
        if sid in visited:
            return
        visiting.append(sid)
        for dep in active[sid][1].get('depends_on') or []:
            if dep['service_id'] in active:
                visit(dep['service_id'])
        visiting.pop()
        visited.add(sid)
        order.append(sid)

    for sid in active:
        visit(sid)
    return {'errors': list(dict.fromkeys(errors)), 'warnings': list(dict.fromkeys(warnings)), 'order': order}


def mode_name(service, mode):
    return 'imgmode-' + key(service['id'])[:12] + '-' + key(mode['id'])[:12]


def compile_sources(catalog, selection=None, prefix=''):
    """Generate exact script bytes, avoiding template interpolation of shell text."""
    sources = copy.deepcopy(catalog.get('legacy_sources') or {})
    mapping = {n:prefix+'legacy-'+key(n) for source in sources.values() for n in delivery.names(source)}
    selected = selection if selection is not None else {s['id']: (next((m['id'] for m in s['modes'] if m.get('default')), None)) for s in catalog['services']}
    sources = {name: delivery.rewrite(source, mapping) for name, source in sources.items()}
    legacy_selected={mapping[m['implementation']]:prefix+mode_name(s,m) for s in catalog['services'] for m in s['modes'] if m['strategy']=='legacy' and selected.get(s['id'])==m['id'] and m['implementation'] in mapping}
    generated = ['from pathlib import Path\nfrom core.services.base import CoreService, ServiceMode\n']
    for service in catalog['services']:
        for mode in service['modes']:
            name = prefix + mode_name(service, mode)
            dependencies = []
            for dep in mode.get('depends_on') or []:
                ds = next((s for s in catalog['services'] if s['id'] == dep['service_id']), None)
                dm = next((m for m in (ds or {}).get('modes', []) if m['id'] == (selected.get(dep['service_id']) or dep.get('mode_id'))), None)
                if dm:
                    dependencies.append(prefix + mode_name(ds, dm))
            if mode['strategy'] == 'legacy':
                original=mode.get('implementation')
                found=False
                for filename,source in list(sources.items()):
                    tree=ast.parse(source)
                    class_name=next((cls.name for cls in tree.body if isinstance(cls,ast.ClassDef) and mapping.get(original) in delivery.names(ast.unparse(cls))),None)
                    if class_name:
                        suffix='\nclass ImageLegacy_'+key(name)+'('+class_name+'):\n    name = '+repr(name)+'\n    group = "Image services"\n'
                        suffix+='    dependencies = '+(repr(dependencies) if mode.get('depends_on') else '['+repr(legacy_selected)+'.get(dep, dep) for dep in '+class_name+'.dependencies]')+'\n'
                        sources[filename]=source+suffix
                        found=True
                        break
                if not found: raise ValueError('缺少原有服务实现：'+str(original))
                continue
            commands = {'startup': [], 'shutdown': [], 'validate': []}
            contents = {}
            directory = '/tmp/chb-' + key(name)
            for index, item in enumerate(mode.get('files') or []):
                path = directory + '/' + str(index) + '-' + Path(item['name']).name
                contents[path] = read_attachment(item)
                interpreter = mode.get('interpreter') or 'sh'
                command = (shlex.quote(path) if interpreter == 'shebang' else interpreter + ' ' + shlex.quote(path))
                if interpreter == 'shebang':
                    command = 'chmod +x ' + shlex.quote(path) + ' && ' + command
                env = ['%s=%s' % (x['name'], x.get('value', '')) for x in mode.get('env') or []]
                if env:
                    command = 'env ' + ' '.join(shlex.quote(x) for x in env) + ' ' + command
                command = 'cd ' + shlex.quote(mode.get('workdir') or '/') + ' && ' + command
                if item['role'] == 'startup' and mode.get('args'):
                    command += ' ' + ' '.join(shlex.quote(x) for x in shlex.split(mode['args']))
                commands[item['role']].append(command)
            if mode['strategy'] == 'manual':
                commands = {k: [] for k in commands}
            generated.append('\nclass ImageMode_' + key(name) + '(CoreService):\n' +
                             '    name = ' + repr(name) + '\n    group = "Image services"\n' +
                             '    directories = ' + repr([directory] if contents else []) + '\n' +
                             '    platform_origin = "image"\n' +
                             '    dependencies = ' + repr(dependencies) + '\n' +
                             ''.join('    ' + k + ' = ' + repr(v) + '\n' for k, v in commands.items()) +
                             ('    validation_mode = ServiceMode.NON_BLOCKING\n' if commands['validate'] else '') +
                             '    def create_files(self):\n' +
                             (''.join('        self.node.create_file(Path(' + repr(path) + '), ' + repr(content) + ')\n' for path, content in contents.items()) or '        pass\n'))
    sources['image_modes.py'] = '\n'.join(generated)
    return sources


def flattened(catalog):
    return [{'name': mode_name(service, mode), 'display_name': service['name'], 'service_id': service['id'],
             'mode_id': mode['id'], 'mode_name': mode['name'], 'software': service.get('software', ''),
             'origin': 'image', 'ports': [p['port'] for p in (mode.get('ports') if mode.get('ports') is not None else service.get('ports', []))]}
            for service in catalog['services'] for mode in service['modes']]


def selection_for(catalog, selections):
    check = combination(catalog, selections)
    if check['errors']:
        raise ValueError('；'.join(check['errors']))
    available = {s['id']: s for s in catalog['services']}
    chosen = []
    for sid in check['order']:
        service = available[sid]
        mode = next(m for m in service['modes'] if m['id'] == selections[sid])
        chosen.append({'name': mode_name(service, mode), 'service_id': sid, 'service_name': service['name'],
                       'mode_id': mode['id'], 'mode_name': mode['name'], 'origin': 'image',
                       'scripts': copy.deepcopy(mode.get('files') or [])})
    return chosen, check


def execution(published, selections):
    import target_admin as ta
    catalog = published['catalog']
    chosen, _ = selection_for(catalog, selections)
    identity = key(json.dumps({'selection':selections,'content':catalog.get('content_hash'),'implementation':published['sources']}, sort_keys=True))
    prefix = 'imgsvc-' + key(published['image_digest'])[:10] + '-r' + str(published['version']) + '-' + identity[:8] + '-'
    directory = Path(ta.CUSTOM_SERVICES_DIR) / ('image_exec_' + key(prefix))
    # Execute the frozen implementation, not a regeneration using today's compiler.
    sources = copy.deepcopy(published['sources'])
    names = {n:prefix+n for text in sources.values() for n in delivery.names(text)}
    dependency_overrides={}
    for service in catalog['services']:
        mode=next((m for m in service['modes'] if m['id']==selections.get(service['id'])),None)
        if not mode or mode['strategy']=='legacy' and not mode.get('depends_on'): continue
        dependencies=[]
        for dep in mode.get('depends_on') or []:
            other=next(s for s in catalog['services'] if s['id']==dep['service_id'])
            other_mode=next(m for m in other['modes'] if m['id']==selections[other['id']])
            dependencies.append(prefix+mode_name(other,other_mode))
        dependency_overrides[prefix+mode_name(service,mode)]=dependencies
    directory.mkdir(parents=True, exist_ok=True)
    for filename, source in sources.items():
        text = delivery.rewrite(source, names)
        tree=ast.parse(text)
        for cls in tree.body:
            if not isinstance(cls,ast.ClassDef): continue
            declared=delivery.names(ast.unparse(cls))
            if not declared or declared[0] not in dependency_overrides: continue
            cls.body=[statement for statement in cls.body if not isinstance(statement,ast.Assign) or not any(isinstance(t,ast.Name) and t.id=='dependencies' for t in statement.targets)]
            cls.body.append(ast.Assign(targets=[ast.Name(id='dependencies',ctx=ast.Store())],value=ast.parse(repr(dependency_overrides[declared[0]]),mode='eval').body))
        text=ast.unparse(ast.fix_missing_locations(tree))+'\n'
        path = directory / Path(filename).name
        if path.exists() and path.read_text() != text:
            raise ValueError('不可变实现目录内容不一致：' + str(path))
        if not path.exists():
            path.write_text(text)
    (directory / '__init__.py').touch(exist_ok=True)
    (directory / 'templates').mkdir(exist_ok=True)
    for filename, content in (catalog.get('legacy_templates') or {}).items():
        path = directory / 'templates' / Path(filename).name
        if not path.exists():
            path.write_text(content)
    return [{**s, 'execution_name': prefix + s['name']} for s in chosen], str(directory)


def stage_delivery(body, refs):
    """An import saves inventory only, never installs or activates services."""
    import target_admin as ta
    result = []
    for ref in refs:
        record = catalog_for(ref)
        manifest = body.get('manifest') or {}
        mapping={n:ta._prefixed_service_name(n,body.get('package','')) for source in (body.get('python_files') or {}).values() for n in delivery.names(source)}
        sources={fn:delivery.rewrite(source,mapping) for fn,source in (body.get('python_files') or {}).items()}
        incoming = copy.deepcopy(body.get('catalog')) if body.get('catalog') else legacy_catalog(manifest,sources,body.get('templates'))
        staged = {'import_id': repo.identifier('image-import'), 'image_ref': ref, 'catalog_id': record['catalog_id'],
                  'catalog': incoming, 'created_at': repo.now(), 'state': 'pending-confirmation'}
        repo.put('image_catalog_imports', staged['import_id'], staged)
        result.append(staged['import_id'])
    return result


def commit(cid, body):
    import target_admin as ta
    import app, core_adapter, topologies
    with topologies.mutation_lock, ta._store_lock:
        current = catalog_for(body['image_ref'])
        if current['catalog_id'] != cid:
            raise HTTPException(409, '镜像标签已指向其他内容，请重新打开详情')
        if body.get('request_id') and current.get('request_id') == body['request_id'] and current['state'] == 'applied':
            return current
        if body.get('base_revision') != current['revision']:
            raise HTTPException(409, '服务配置已被修改，请导出当前编辑内容后重新打开合并')
        services, issues = normalize(body.get('services', []))
        sources = body.get('legacy_sources', current.get('legacy_sources', {}))
        templates = body.get('legacy_templates', current.get('legacy_templates', {}))
        for service in services:
            for mode in service['modes']:
                if mode['strategy'] == 'legacy' and mode.get('implementation') not in {n for text in sources.values() for n in delivery.names(text)}:
                    issues.append(service['name'] + ' / ' + mode['name'] + ' 缺少原有实现附件')
        content_hash=key(json.dumps({'services':services,'sources':sources,'templates':templates,'image_id':current['image_id']},sort_keys=True))
        record = {**current, 'services': services, 'legacy_sources': sources, 'legacy_templates': templates,'content_hash':content_hash,
                  'revision': current['revision'] + 1, 'issues': issues, 'state': 'incomplete' if issues else 'pending',
                  'stage': 'saved', 'request_id': body.get('request_id'), 'updated_at': repo.now()}
        record.pop('info', None)
        if content_hash != current.get('content_hash'):
            record.pop('pending_target_version',None)
        repo.put('image_catalogs', cid, record)
        if body.get('action') != 'apply' or issues:
            return record
        if not body.get('acknowledge'):
            record['message'] = '配置已保存；请确认服务注册及可能的 CORE 重启影响后生效'
            repo.put('image_catalogs', cid, record)
            return record
        sessions = app.bridge.get_sessions()
        if sessions:
            record['message'] = '配置已保存待生效。存在 CORE 会话，请先在拓扑管理中处理全部会话'
            repo.put('image_catalogs', cid, record)
            return record
        try:
            record['stage'] = 'materializing'
            repo.put('image_catalogs', cid, record)
            ref = record['image_ref']
            name, tag = ref.rsplit(':', 1) if ':' in ref.rsplit('/', 1)[-1] else (ref, 'latest')
            manifest = {**record.get('legacy_manifest',{}),'schema_version': 3, 'name': record['target_name'], 'image': {**record.get('legacy_manifest',{}).get('image',{}),'name': name, 'tag': tag},
                        'services': flattened(record), 'catalog': {'services': services}, 'origin': 'image'}
            payload = {'manifest': manifest, 'image_ref': ref, 'image_digest': record['image_id'],
                       'catalog': copy.deepcopy(record), 'sources': compile_sources(record), 'templates': templates}
            payload['catalog'].pop('request_id', None)
            payload['catalog'].pop('stage', None)
            published = repo.revision('target',record['target_name'],record['pending_target_version']) if record.get('pending_target_version') else repo.publish('target', record['target_name'], payload)
            mapping, directory = __import__('versions').materialize(record['target_name'], published)
            required = [mapping[s['name']] for s in manifest['services']]
            record.update(stage='registering', pending_target_version=published['version'])
            repo.put('image_catalogs', cid, record)
            result = core_adapter.activate(app.bridge, [directory], required)
            if result['missing']:
                raise ValueError('CORE 未加载服务：' + '、'.join(result['missing']))
            record.update(stage='verified', state='applied', applied_target_version=published['version'],
                          message='配置已保存并核对注册结果；使用独立修订名加载，无需主动重启 CORE')
            manifest.update(target_revision=published['version'], target_id=published['entity_id'], publication_state='published')
            with repo.transaction():
                repo.put('image_catalog_activations',record['target_name']+':'+str(published['version']),{'state':'applied','verified_at':repo.now()})
                repo.put('manifests', record['target_name'], manifest)
                repo.put('image_catalogs', cid, record)
            ta._svc_cache['loaded'] = False
            ta._audit('image.services.apply', ref=ref, revision=record['revision'])
        except Exception as error:
            record.update(state='failed', failed_stage=record['stage'], message='配置已保存，生效失败：' + str(error))
            repo.put('image_catalogs', cid, record)
        return record


@router.get('/image-services/detail')
def detail(ref: str):
    try:
        record = catalog_for(ref)
        record['imports'] = [v for v in repo.read('image_catalog_imports').values() if v['catalog_id'] == record['catalog_id']]
        record['references'] = repo.references('target', record['target_name'])
        return {'ok': True, 'data': record}
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error


@router.get('/instances/{name}/service-options')
def template_service_options(name:str,version:int|None=None):
    template=repo.revision('template',name,version)
    if not template: raise HTTPException(404,'模板修订不存在')
    if template['runtime_type']=='docker':
        target=repo.revision('target',template['target_name'],template['target_version'])
        if not target or not target.get('catalog'): return {'ok':True,'data':{'template':template,'catalog':None,'selection':None}}
        return {'ok':True,'data':{'template':template,'catalog':target['catalog'],'selection':template.get('service_selection') or {}}}
    import target_admin as ta
    services=[{'id':n,'name':n,'modes':[{'id':'native','name':'原生实现','default':True}]} for n in ta._available_services()['system']]
    return {'ok':True,'data':{'template':template,'catalog':{'origin':'core','services':services},'selection':{s['name']:'native' for s in template.get('services',[])}}}


@router.post('/image-services/{cid}/validate')
def validate(cid: str, body: dict):
    try:
        services, issues = normalize(body.get('services', []))
        result = {'issues': issues}
        if 'selection' in body:
            result.update(combination({'services': services}, body['selection']))
        return {'ok': True, 'data': result}
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise HTTPException(400, str(error)) from error


@router.post('/image-services/{cid}/commit')
def save(cid: str, body: dict):
    try:
        return {'ok': True, 'data': commit(cid, body)}
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise HTTPException(400, str(error)) from error


@router.post('/image-services/attachments')
async def upload(request: Request, name: str, role: str = 'startup'):
    try:
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_SCRIPT:
                raise ValueError('每个脚本不得超过 2 MiB')
        if role not in ('startup', 'shutdown', 'validate'):
            raise ValueError('脚本用途无效')
        return {'ok': True, 'data': store_attachment(bytes(content), name, role)}
    except (ValueError, UnicodeError) as error:
        raise HTTPException(400, str(error)) from error


@router.get('/image-services/attachments/{digest}')
def download(digest: str, name: str = 'script.sh'):
    try:
        path = attachment_path(digest)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    if not path.is_file():
        raise HTTPException(404, '脚本附件不存在')
    return FileResponse(path, filename=Path(name).name)


def configuration_archive(catalog):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('catalog.json', json.dumps(catalog, ensure_ascii=False, indent=2))
        written = set()
        for service in catalog['services']:
            for mode in service['modes']:
                for item in mode.get('files') or []:
                    if item['sha256'] not in written:
                        archive.writestr('scripts/' + item['sha256'], read_attachment(item).encode())
                        written.add(item['sha256'])
    stream.seek(0)
    return stream


@router.get('/image-services/export')
def export(ref: str):
    try:
        catalog = catalog_for(ref)
        stream = configuration_archive(catalog)
        return StreamingResponse(stream, media_type='application/zip', headers={'Content-Disposition': 'attachment; filename="' + catalog['catalog_id'] + '-config.zip"'})
    except (ValueError, OSError) as error:
        raise HTTPException(400, str(error)) from error


@router.post('/image-services/export/config')
def export_current(body: dict):
    try:
        catalog=copy.deepcopy(body['catalog'])
        catalog['services'],_=normalize(catalog['services'])
        return StreamingResponse(configuration_archive(catalog),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="image-config.zip"'})
    except (ValueError,KeyError,OSError) as error:
        raise HTTPException(400,str(error)) from error


def run_export(job,catalog):
    import target_admin as ta, shutil
    root=settings.data_dir/'image-services'/'exports'/job['job_id']
    try:
        root.mkdir(parents=True,exist_ok=True)
        job.update(state='running',stage='saving-image')
        repo.put('image_export_jobs',job['job_id'],job)
        image=root/'image.tar'
        output,code=ta._docker('save -o '+shlex.quote(str(image))+' '+shlex.quote(catalog['image_id']),1200)
        if code: raise ValueError(output or '镜像导出失败')
        job['stage']='packing'
        repo.put('image_export_jobs',job['job_id'],job)
        config=configuration_archive(catalog)
        target=root/'image-bundle.tar.gz'
        with tarfile.open(target,'w:gz') as archive:
            archive.add(image,arcname='image.tar')
            with zipfile.ZipFile(config) as package:
                for item in package.infolist():
                    content=package.read(item.filename)
                    info=tarfile.TarInfo(item.filename);info.size=len(content)
                    archive.addfile(info,io.BytesIO(content))
        image.unlink()
        job.update(state='succeeded',stage='complete',size=target.stat().st_size,download='/image-services/exports/'+job['job_id']+'/download')
    except Exception as error:
        job.update(state='failed',message=str(error))
        shutil.rmtree(root,ignore_errors=True)
    finally:
        repo.put('image_export_jobs',job['job_id'],job)
        _export_slots.release()


@router.post('/image-services/export/full',status_code=202)
def export_full(body:dict):
    try: catalog=catalog_for(body['ref'])
    except (ValueError,KeyError) as error: raise HTTPException(400,str(error)) from error
    if not _export_slots.acquire(False): raise HTTPException(429,'导出任务已满，请稍后再试')
    job={'job_id':repo.identifier('image-export'),'state':'queued','image_ref':catalog['image_ref'],'created_at':repo.now()}
    try:
        repo.put('image_export_jobs',job['job_id'],job)
        _exports.submit(run_export,job,catalog)
    except Exception:
        _export_slots.release();raise
    return {'ok':True,'data':job}


@router.get('/image-services/exports/{job_id}')
def export_status(job_id:str):
    job=repo.read('image_export_jobs',job_id)
    if not job: raise HTTPException(404,'导出任务不存在')
    return {'ok':True,'data':job}


@router.get('/image-services/exports')
def export_jobs():
    return {'ok':True,'data':list(repo.read('image_export_jobs').values())}


@router.get('/image-services/exports/{job_id}/download')
def export_download(job_id:str):
    job=export_status(job_id)['data']
    path=settings.data_dir/'image-services'/'exports'/job_id/'image-bundle.tar.gz'
    if job['state']!='succeeded' or not path.is_file(): raise HTTPException(409,'完整包尚不可下载，请检查任务结果')
    return FileResponse(path,filename='image-bundle.tar.gz')


def recover():
    for catalog in repo.read('image_catalogs').values():
        if catalog.get('stage') in ('materializing','registering') and catalog['state']=='pending':
            catalog.update(state='failed',failed_stage=catalog['stage'],message='服务处理被 Bridge 重启中断，配置已保留；请重新确认以核对注册结果')
            repo.put('image_catalogs',catalog['catalog_id'],catalog)
    for job in repo.read('image_export_jobs').values():
        if job['state'] in ('queued','running'):
            job.update(state='interrupted',message='导出被 Bridge 重启中断，请重新导出')
            repo.put('image_export_jobs',job['job_id'],job)


def shutdown():
    _exports.shutdown(wait=False,cancel_futures=True)


@router.post('/image-services/import/preview')
async def import_preview(request: Request, ref: str):
    try:
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > MAX_IMPORT:
                raise ValueError('配置包不得超过 32 MiB；完整镜像请通过镜像导入')
        if zipfile.is_zipfile(io.BytesIO(content)):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if sum(i.file_size for i in archive.infolist()) > MAX_IMPORT:
                    raise ValueError('解压后的配置包过大')
                incoming = json.loads(archive.read('catalog.json'))
                for service in incoming['services']:
                    for mode in service['modes']:
                        for item in mode.get('files') or []:
                            data = archive.read('scripts/' + item['sha256'])
                            stored = store_attachment(data, item['name'], item['role'])
                            if stored['sha256'] != item['sha256']:
                                raise ValueError('导入脚本摘要不一致')
        else:
            incoming = json.loads(content)
        if 'image' in incoming and 'catalog_id' not in incoming:
            incoming = legacy_catalog(delivery.manifest(incoming))
        incoming['services'], issues = normalize(incoming.get('services', []))
        current = catalog_for(ref)
        rows = []
        for service in incoming['services']:
            existing = next((s for s in current['services'] if s['id'] == service['id'] or s['name'].casefold() == service['name'].casefold()), None)
            rows.append({'incoming': service, 'existing': existing, 'match_id': (existing or {}).get('id'), 'action': 'skip' if existing else 'add'})
        return {'ok': True, 'data': {'rows': rows, 'issues': issues, 'legacy_sources': incoming.get('legacy_sources', {}), 'legacy_templates': incoming.get('legacy_templates', {})}}
    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile) as error:
        raise HTTPException(400, str(error)) from error


@router.post('/image-services/import/merge')
def merge_import(body:dict):
    try:
        catalog=copy.deepcopy(body['catalog']);preview=body['preview'];mapping={};mode_mapping={}
        if all(row['action']=='skip' for row in preview['rows']):
            return {'ok':True,'data':{'catalog':catalog,'issues':[]}}
        existing={s['id']:s for s in catalog['services']}
        for row in preview['rows']:
            incoming=row['incoming'];action=row['action']
            if action=='skip':
                if row.get('match_id'):
                    mapping[incoming['id']]=row['match_id']
                    for mode in incoming['modes']:
                        matched=next((m for m in existing[row['match_id']]['modes'] if m['id']==mode['id'] or m['name']==mode['name']),None)
                        if matched: mode_mapping[(incoming['id'],mode['id'])]=matched['id']
                continue
            if action not in ('add','replace'): raise ValueError('导入处理方式无效')
            value=copy.deepcopy(incoming)
            if action=='replace':
                if row.get('match_id') not in existing: raise ValueError('替换目标已变化')
                value['id']=row['match_id']
            else:
                value['id']='service-'+uuid.uuid4().hex
                for mode in value['modes']:
                    old=mode['id'];mode['id']='mode-'+uuid.uuid4().hex
                    mode_mapping[(incoming['id'],old)]=mode['id']
            mapping[incoming['id']]=value['id'];existing[value['id']]=value
        previous=copy.deepcopy(catalog.get('legacy_sources',{}))
        needed={m['implementation'] for row in preview['rows'] if row['action']!='skip' for m in row['incoming']['modes'] if m['strategy']=='legacy'}
        aliases={}
        if needed:
            batch=uuid.uuid4().hex[:12]
            aliases={n:'import-'+batch+'-'+n for source in preview.get('legacy_sources',{}).values() for n in delivery.names(source)}
            for filename,source in preview.get('legacy_sources',{}).items():
                previous['import_'+batch+'_'+Path(filename).name]=delivery.rewrite(source,aliases)
            for row in preview['rows']:
                if row['action']=='skip': continue
                for mode in existing[mapping[row['incoming']['id']]]['modes']:
                    if mode['strategy']=='legacy': mode['implementation']=aliases.get(mode['implementation'],mode['implementation'])
            conflicting=[name for name,text in preview.get('legacy_templates',{}).items() if name in catalog.get('legacy_templates',{}) and catalog['legacy_templates'][name]!=text]
            retained=any(m['strategy']=='legacy' and m['implementation'] not in aliases.values() for service in existing.values() for m in service['modes'])
            if conflicting and retained:
                raise ValueError('脚本模板文件冲突：'+'、'.join(conflicting)+'。仍有旧服务保留，请更名关联附件后导入，避免覆盖跳过的服务')
        for row in preview['rows']:
            if row['action']=='skip': continue
            value=existing[mapping[row['incoming']['id']]]
            for mode in value['modes']:
                for dep in mode.get('depends_on') or []:
                    original_service=dep['service_id']
                    if dep.get('mode_id'): dep['mode_id']=mode_mapping.get((original_service,dep['mode_id']),dep['mode_id'])
                    dep['service_id']=mapping.get(dep['service_id'],dep['service_id'])
        catalog.update(services=list(existing.values()),legacy_sources=previous,legacy_templates={**catalog.get('legacy_templates',{}),**(preview.get('legacy_templates',{}) if needed else {})})
        catalog['services'],issues=normalize(catalog['services'])
        return {'ok':True,'data':{'catalog':catalog,'issues':issues}}
    except (ValueError,KeyError,TypeError,SyntaxError) as error:
        raise HTTPException(400,str(error)) from error
