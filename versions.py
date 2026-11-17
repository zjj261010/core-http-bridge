"""Published target/template revisions and version-specific executable definitions."""
import copy
import hashlib
import json
import shutil
from pathlib import Path
import delivery
import repository as repo


def target_body(name, manifest, sources=None, templates=None):
    import target_admin as ta
    m = copy.deepcopy(manifest)
    m.pop('imported_at',None)
    m.pop('target_revision',None)
    m.pop('target_id',None)
    m.pop('publication_state',None)
    supplied_sources=sources
    sources = {} if sources is None else copy.deepcopy(sources)
    package = Path(ta.CUSTOM_SERVICES_DIR)/name
    for p in sorted(package.glob('*.py')):
        if supplied_sources is None and p.name != '__init__.py':
            sources[p.name] = p.read_text()
    if templates is None: templates = {p.name:p.read_text() for p in (package/'templates').glob('*') if p.is_file()}
    import shlex
    image = m['image']
    ref = image['name']+':'+image.get('tag','latest')
    raw, code = ta._docker('image inspect --format {{.Id}} '+shlex.quote(ref),30)
    digest = raw.strip() if code == 0 else None
    return {'manifest':m,'image_ref':ref,'image_digest':digest,'sources':sources,'templates':templates}


def target(name,manifest):
    body=target_body(name,manifest)
    import publications
    issues=publications.target_issues(body)
    if issues: raise ValueError('; '.join(issues))
    with repo.transaction():
        published = repo.publish('target',name,body)
        materialize(name,published)
        return published


def execution_mapping(name,published):
    import target_admin as ta
    key = hashlib.sha256(name.encode()).hexdigest()[:12]
    prefix = (f'imgsvc-{key}-r{published["version"]}-' if published.get('catalog') else f'svc-{key}-v{published["version"]}-')
    mapping = {n:prefix+n.removeprefix('svc-') for src in published.get('sources',{}).values() for n in delivery.names(src)}
    dst = Path(ta.CUSTOM_SERVICES_DIR)/f'version_{key}_{published["version"]}'
    return mapping,str(dst)


def materialize(name,published):
    mapping,directory=execution_mapping(name,published)
    dst=Path(directory)
    existed=dst.exists()
    dst.mkdir(parents=True,exist_ok=True)
    if not existed:
        import database,platform_db
        database.on_rollback(platform_db.DB_PATH,lambda:shutil.rmtree(dst,ignore_errors=True))
    for fn,source in published.get('sources',{}).items():
        path=dst/Path(fn).name
        content=delivery.rewrite(source,mapping)
        if not path.exists(): path.write_text(content)
    (dst/'__init__.py').touch()
    (dst/'templates').mkdir(exist_ok=True)
    for fn,text in published.get('templates',{}).items():
        path=dst/'templates'/Path(fn).name
        if not path.exists(): path.write_text(text)
    return mapping,str(dst)


def template_body(name,body):
    import target_admin as ta
    if not isinstance(body,dict) or body.get('runtime_type') not in ('docker','netns'):
        raise ValueError('旧模板缺少有效运行时类型，请先修正')
    body=copy.deepcopy(body)
    if body.get('runtime_type') == 'docker':
        m=ta._manifest_index().get(body.get('image_tag'))
        if not m: raise ValueError('模板绑定的镜像缺少能力清单')
        requested=body.get('target_version') or m.get('target_revision')
        target_name=body.get('target_name') or m['name']
        t=repo.revision('target',target_name,requested)
        if not t and not requested and target_name==m['name']: t=target(target_name,m)
        if not t: raise ValueError('靶标版本不存在')
        if not t.get('image_digest'): raise ValueError('此靶标版本未固定本地镜像 ID，请先导入镜像再发布靶标版本')
        if t['image_ref'] != body['image_tag']: raise ValueError('靶标版本与镜像不一致')
        body.update(target_name=target_name,target_version=t['version'])
        if t.get('catalog'):
            import image_services
            if not repo.read('image_catalog_activations',target_name+':'+str(t['version'])):
                raise ValueError('镜像配置尚未核对注册成功，请先在镜像管理中生效')
            selection=body.get('service_selection')
            if selection is None:
                declared={s['name'] for s in body.get('services',[])}
                selection={s['id']:mode['id'] for s in t['catalog']['services'] for mode in s['modes'] if image_services.mode_name(s,mode) in declared}
            selected,_=image_services.selection_for(t['catalog'],selection)
            body.update(service_selection=selection,services=selected)
    for key in ('version','entity_id','publication_state','base_version'): body.pop(key,None)
    return body


def template(name,body):
    return repo.publish('template',name,template_body(name,body))


def resolve(name,version=None,service_overrides=None):
    import target_admin as ta
    t=repo.revision('template',name,version)
    if not t:
        if version is not None: raise ValueError('节点模板版本不存在')
        raw=ta._load_store(ta.INSTANCE_STORE).get(name)
        if not raw: raise ValueError('节点模板不存在: '+name)
        t=template(name,raw)
    services=[s['name'] for s in t.get('services',[])]
    out={'instance':name,'instance_version':t['version'],'model':t.get('model') or ('target' if t['runtime_type']=='docker' else 'bare'),'type':'docker' if t['runtime_type']=='docker' else 'default','services':services}
    dirs=[]
    if t['runtime_type']=='netns' and service_overrides is not None:
        allowed=set(ta._available_services()['system'])
        if any(name not in allowed or value not in (None,'','native') for name,value in service_overrides.items()):
            raise ValueError('原生节点只能选择 CORE 原生服务')
        services=[name for name,value in service_overrides.items() if value]
        if not services and out['model'] in ('PC','host','router','mdr'):
            raise ValueError('该原生型号会回填默认服务，不能关闭全部服务；请使用裸节点模板')
        out.update(services=services,service_overrides=copy.deepcopy(service_overrides),effective_services=[{'name':n,'origin':'core','mode_id':'native'} for n in services])
    if t['runtime_type']=='docker':
        target_revision=repo.revision('target',t['target_name'],t['target_version'])
        if not target_revision: raise ValueError('绑定的靶标版本不存在')
        if target_revision.get('catalog'):
            import image_services
            selection=copy.deepcopy(t.get('service_selection') or {}) if service_overrides is None else copy.deepcopy(service_overrides)
            chosen,directory=image_services.execution(target_revision,selection)
            out.update(image=target_revision['image_digest'],image_ref=target_revision['image_ref'],target_name=t['target_name'],target_version=t['target_version'],
                       service_overrides=copy.deepcopy(service_overrides),effective_services=chosen,service_selection=selection,
                       services=[s['execution_name'] for s in chosen])
            return out,[directory]
        if service_overrides is not None:
            raise ValueError('此旧模板尚未迁移服务模式清单，请先在镜像管理中确认配置后保存新模板')
        mapping,directory=materialize(t['target_name'],target_revision)
        dirs.append(directory)
        out.update(image=target_revision.get('image_digest') or target_revision['image_ref'],target_name=t['target_name'],target_version=t['target_version'],image_ref=target_revision['image_ref'])
        out['services']=[mapping.get(s,s) for s in services]
    return out,dirs
