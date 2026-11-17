"""Persisted version drafts, optimistic publish, references and activation visibility."""
import copy
import json
import shlex
from pathlib import Path
from fastapi import APIRouter,HTTPException
import delivery
import delivery_state as ds
import repository as repo
import versions

router=APIRouter()


def target_issues(body):
    issues=[]
    try: manifest=delivery.manifest(body.get('manifest'))
    except ValueError as e: return [str(e)]
    if not body.get('image_digest'): issues.append('本地镜像未就位，尚不能固定镜像 ID')
    declared={name for source in body.get('sources',{}).values() for name in delivery.names(source)}
    from core.services.manager import ServiceManager
    builtins=ServiceManager();builtins.load_locals()
    for service in manifest['services']:
        if service['name'] not in declared and service['name'] not in builtins.services:
            issues.append('缺少服务实现: '+service['name'])
    return issues


def draft_body(kind,name,body):
    import target_admin as ta
    body=copy.deepcopy(body)
    if kind=='target':
        manifest=delivery.manifest(body.get('manifest') or body)
        if manifest['name']!=name: raise ValueError('清单名称与靶标名称必须一致')
        prepared=versions.target_body(name,manifest,body.get('sources'),body.get('templates'))
        if body.get('image_digest') and body['image_digest']!=prepared['image_digest']:
            raise ValueError('镜像 tag 的实际 ID 已变化，请刷新交付内容并查看差异')
        return prepared
    if kind!='template': raise ValueError('kind 仅支持 target 或 template')
    for key in ('version','entity_id','publication_state','base_version'): body.pop(key,None)
    return body


def diagnostics(kind,name,body):
    if kind=='target': return target_issues(body)
    import target_admin as ta
    error=ta._validate_instance({**copy.deepcopy(body),'name':name},ta._available_services())
    if error: return [json.loads(error.body)['error']['message']]
    return []


def save_draft(kind,name,body,draft_id=None,revision=None,base_version=None,prepared=False):
    payload=copy.deepcopy(body) if prepared else draft_body(kind,name,body)
    problems=diagnostics(kind,name,payload)
    with repo.transaction():
        old=repo.read('publication_drafts',draft_id) if draft_id else None
        if draft_id and not old: raise HTTPException(404,'版本草稿不存在')
        if old and (old['status']!='draft' or revision!=old['revision']): raise HTTPException(409,'草稿已发布或已被修改，请重新打开')
        latest=repo.revision(kind,name)
        record={'draft_id':draft_id or repo.identifier('publication'),'kind':kind,'name':name,'status':'draft','revision':(old or {}).get('revision',0)+1,'base_version':old['base_version'] if old else (base_version if base_version is not None else (latest or {}).get('version',0)),'body':payload,'issues':problems,'updated_at':repo.now()}
        return repo.put('publication_drafts',record['draft_id'],record)


def pending_target(name,payload):
    for old in repo.read('publication_drafts').values():
        if old['status']=='draft' and old['kind']=='target' and old['name']==name and old['body']==payload: return old
    return save_draft('target',name,payload,prepared=True)


@router.get('/publications/drafts')
def drafts(): return {'ok':True,'data':list(repo.read('publication_drafts').values())}


@router.get('/publications/drafts/{draft_id}')
def get(draft_id:str):
    value=repo.read('publication_drafts',draft_id)
    if not value: raise HTTPException(404,'版本草稿不存在')
    return {'ok':True,'data':value}


@router.post('/publications/drafts',status_code=201)
def create(body:dict):
    try: return {'ok':True,'data':save_draft(body['kind'],body['name'],body['body'],base_version=body.get('base_version'))}
    except (ValueError,KeyError,SyntaxError) as e: raise HTTPException(400,str(e))


@router.put('/publications/drafts/{draft_id}')
def update(draft_id:str,body:dict):
    old=get(draft_id)['data']
    try: return {'ok':True,'data':save_draft(old['kind'],old['name'],body['body'],draft_id,body.get('revision'))}
    except (ValueError,KeyError,SyntaxError) as e: raise HTTPException(400,str(e))


@router.post('/publications/drafts/{draft_id}/publish')
def publish(draft_id:str,body:dict):
    import target_admin as ta
    with ta._store_lock,repo.transaction():
        draft=get(draft_id)['data']
        if draft['status']!='draft' or body.get('revision')!=draft['revision']: raise HTTPException(409,'草稿状态或版本已变化')
        latest=repo.revision(draft['kind'],draft['name'])
        if (latest or {}).get('version',0)!=draft['base_version']: raise HTTPException(409,'已有更新的发布版本，请从最新版新建草稿并合并修改')
        issues=diagnostics(draft['kind'],draft['name'],draft['body'])
        if issues: raise HTTPException(400,issues)
        if draft['kind']=='target':
            payload=draft['body'];ref=payload['image_ref']
            actual,code=ta._docker('image inspect --format {{.Id}} '+shlex.quote(ref),30)
            if code or actual.strip()!=payload['image_digest']: raise HTTPException(409,'镜像 ID 已变化，发布前请刷新草稿')
            published=repo.publish('target',draft['name'],payload)
            versions.materialize(draft['name'],published)
            manifest={**payload['manifest'],'target_revision':published['version'],'target_id':published['entity_id'],'publication_state':'published'}
            repo.put('manifests',draft['name'],manifest)
        else:
            try: published=versions.template(draft['name'],draft['body'])
            except ValueError as e: raise HTTPException(400,str(e))
            repo.put('instances',draft['name'],published)
        draft.update(status='published',published_version=published['version'],published_at=repo.now(),revision=draft['revision']+1)
        repo.put('publication_drafts',draft_id,draft)
        return {'ok':True,'data':{'draft':draft,'published':published}}


@router.get('/versions/{kind}/{name}/references')
def references(kind:str,name:str,version:int | None=None):
    if kind not in ('template','target'): raise HTTPException(400,'未知版本类型')
    return {'ok':True,'data':repo.references(kind,name,version)}


@router.get('/versions/target/{name}/{version}/activation')
def activation(name:str,version:int):
    import core_adapter,app
    target=repo.revision('target',name,version)
    if not target: raise HTTPException(404,'靶标发布版本不存在')
    mapping,directory=versions.execution_mapping(name,target)
    names=[mapping.get(s['name'],s['name']) for s in target['manifest']['services']]
    on_disk=Path(directory).is_dir() and all((Path(directory)/filename).exists() for filename in target['sources']) and all((Path(directory)/'templates'/filename).exists() for filename in target['templates'])
    try:
        registered=set(core_adapter.registered(app.bridge))
        missing=sorted(set(names)-registered)
        state='registered' if not missing else 'pending-daemon'
        diagnostic=None
    except Exception as e:
        missing=names;state='unknown';diagnostic=str(e)
    refs=repo.references('target',name,version)
    # Referencing template versions lead to exact deployment/history usage.
    dependents=[]
    for ref in refs['revisions']:
        dependents.extend(repo.references('template',ref['owner_name'],ref['owner_version'])['documents'])
    return {'ok':True,'data':{'state':state,'on_disk':on_disk,'services':names,'missing':missing,'diagnostic':diagnostic,'references':refs,'deployments':[r for r in dependents if r['collection']=='deployments']}}


@router.post('/targets/import/impact')
def import_impact(body:dict):
    import target_admin as ta
    try: normalized,mapping=ds.prepare(body)
    except (ValueError,SyntaxError) as e: raise HTTPException(400,str(e))
    package=normalized['package'];root=Path(ta.CUSTOM_SERVICES_DIR)/package
    old={}
    for source in root.glob('*.py'):
        try:
            text=source.read_text()
            for name in delivery.names(text): old[name]=delivery.rewrite(text,{})
        except (OSError,SyntaxError): continue
    changed=[]
    for source in normalized['python_files'].values():
        text=delivery.rewrite(source,mapping)
        for name in delivery.names(text):
            if name in old and old[name]!=text: changed.append(name)
    templates=[name for name,text in (normalized.get('templates') or {}).items() if (root/'templates'/Path(name).name).exists() and (root/'templates'/Path(name).name).read_text()!=text]
    references=repo.references('target',package)
    dependents=[]
    for ref in references['revisions']:
        dependents.extend(repo.references('template',ref['owner_name'],ref['owner_version'])['documents'])
    return {'ok':True,'data':{'package':package,'updated_services':sorted(set(changed)),'updated_templates':templates,'references':references,'dependent_documents':dependents,'message':'同名更新会产生新的交付版本；已有模板版本和历史部署继续使用固定的旧实现'}}


@router.delete('/publications/drafts/{draft_id}')
def delete_draft(draft_id:str,revision:int):
    with repo.transaction():
        draft=get(draft_id)['data']
        if draft['status']!='draft' or draft['revision']!=revision: raise HTTPException(409,'仅能删除当前未发布草稿')
        repo.remove('publication_drafts',draft_id)
    return {'ok':True}
