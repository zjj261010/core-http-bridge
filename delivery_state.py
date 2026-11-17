"""Content identities and receipts for independently resumable delivery pieces."""
import ast
import copy
import hashlib
import json
import os
import shlex
import tarfile
from pathlib import Path
import delivery


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def file_digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(4*1024*1024),b''): h.update(chunk)
    return h.hexdigest()


def clean_manifest(value):
    result=copy.deepcopy(value)
    for key in ('imported_at','target_revision','target_id','publication_state'): result.pop(key,None)
    return result


def prepare(body):
    import target_admin as ta
    body=copy.deepcopy(body)
    if body.get('dir'):
        body=ta._bundle_body(body)
        body['source_dir']=body.pop('dir')
    body.pop('completed_pieces',None)
    package=(body.get('package') or '').strip()
    man=delivery.manifest(body['manifest']) if body.get('manifest') else None
    if man:
        if package and package!=man['name']: raise ValueError('package 与 manifest.name 必须一致')
        package=man['name']
    sources=dict(body.get('python_files') or {})
    if body.get('py'): sources['editor.py']=body['py']
    names=[n for text in sources.values() for n in delivery.names(text)]
    if len(names)!=len(set(names)): raise ValueError('本次提交内有重复服务定义')
    mapping={n:ta._prefixed_service_name(n,package) for n in names}
    if man:
        for svc in man['services']: svc['name']=mapping.get(svc['name'],svc['name'])
    body.update(package=package,manifest=man,python_files=sources,py='')
    return body,mapping


def identities(body,previous=None):
    previous=previous or {}
    path=body.get('image_path')
    if path and os.path.isfile(path): archive=file_digest(path)
    elif previous.get('image_path')==path: archive=previous.get('archive_sha256')
    else: archive=None
    m=body.get('manifest') or {};image=m.get('image') or {}
    ref=image['name']+':'+image.get('tag','latest') if image.get('name') else None
    values={'image':{'archive_sha256':archive,'declared_ref':ref},'services':{'package':body['package'],'sources':body['python_files'],'templates':body.get('templates') or {},'declared_services':([] if body['python_files'] else [s['name'] for s in m.get('services',[])])},'manifest':{'manifest':clean_manifest(m),'image':{'archive_sha256':archive,'declared_ref':ref},'services':{'package':body['package'],'sources':body['python_files'],'templates':body.get('templates') or {},'declared_services':([] if body['python_files'] else [s['name'] for s in m.get('services',[])])}}}
    return {k:digest(v) for k,v in values.items()}, {'image_path':path,'archive_sha256':archive,'declared_ref':ref}


def archive_refs(path):
    try:
        with tarfile.open(path,'r:*') as archive:
            member=archive.getmember('manifest.json')
            if member.size>4*1024*1024: raise ValueError('镜像 manifest.json 过大')
            manifests=json.load(archive.extractfile(member))
            return sorted({tag for m in manifests for tag in (m.get('RepoTags') or [])})
    except KeyError:
        return []  # OCI/unlabelled archives are checked from docker load's resulting identity.


def image_receipt(refs):
    import target_admin as ta
    result={}
    for ref in refs:
        value,code=ta._docker('image inspect --format {{.Id}} '+shlex.quote(ref),30)
        if code: raise ValueError('镜像导入后引用不存在: '+ref)
        result[ref]=value.strip()
    return {'images':result}


def service_receipt(package,names,templates):
    import target_admin as ta
    root=Path(ta.CUSTOM_SERVICES_DIR)/package
    files={}
    for p in root.glob('*.py'):
        if set(delivery.names(p.read_text())) & set(names): files[p.name]=file_digest(p)
    tpls={name:file_digest(root/'templates'/Path(name).name) for name in templates}
    return {'package':package,'files':files,'templates':tpls,'names':list(names)}


def reusable(kind,piece,key):
    if not piece or piece.get('ok') is not True or piece.get('input_key')!=key: return False
    receipt=piece.get('receipt') or {}
    try:
        if kind=='image':
            return bool(receipt.get('images')) and image_receipt(list(receipt['images']))==receipt
        if kind=='services':
            return service_receipt(receipt['package'],receipt['names'],receipt['templates'])==receipt
        import repository
        published=repository.revision('target',receipt['name'],receipt.get('version')) if receipt.get('version') else None
        if published: return digest(clean_manifest(published['manifest']))==receipt['manifest_key']
        draft=repository.read('publication_drafts',receipt.get('draft_id',''))
        return bool(draft and draft['status']=='draft' and digest(clean_manifest(draft['body']['manifest']))==receipt['manifest_key'])
    except (OSError,ValueError,KeyError,SyntaxError):
        return False
