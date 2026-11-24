"""
靶标包管理路由（Tab1 镜像管理 + Tab2 独立容器管理）
设计文档：/opt/artifacts/core-drill/管理平台改造设计文档.md / 镜像管理模块设计文档.md
路线 B：基座镜像不自启服务，CORE 声明启动；本模块管理交付物（镜像/服务定义/清单）和独立容器

响应统一为 {"ok": bool, "data": {...}} / {"ok": False, "error": {"code", "message"}}（设计 §3.2）
"""
import asyncio
import json
import logging
import os
import pty
from pathlib import Path
import re
import signal
import subprocess
import tarfile

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from settings import settings
import storage
import delivery
import threading
import functools
import repository
import versions
_store_lock = threading.RLock()
_dirty_packages=set()

logger = logging.getLogger("core.target_admin")

router = APIRouter()

# ── 路径常量 ──
MANIFEST_STORE = str(settings.data_dir / "manifests.json")
CUSTOM_SERVICES_DIR = str(settings.services_dir)
IMAGE_IMPORT_DIR = str(settings.import_dir)
INSTANCE_STORE = str(settings.data_dir / "instances.json")
TOPOLOGY_STORE = str(settings.data_dir / "topologies.json")
AUDIT_LOG = str(settings.data_dir / "audit.jsonl")
STANDALONE_PREFIX = "standalone-"



# ═══════════════════════════════════════════════
# 内部工具
# ═══════════════════════════════════════════════

def _load_manifests() -> dict:
    return _load_store(MANIFEST_STORE)


def _save_manifests(data: dict):
    _save_store(MANIFEST_STORE, data)


def _collection(path):
    return {MANIFEST_STORE:'manifests',INSTANCE_STORE:'instances',TOPOLOGY_STORE:'legacy_topologies'}.get(path)


def _load_store(path: str) -> dict:
    collection=_collection(path)
    if not collection: return storage.read_json(path)
    repository.migrate(collection,path)
    return repository.read(collection)


def _save_store(path: str, data: dict):
    collection=_collection(path)
    if not collection: return storage.write_json(path,data)
    repository.migrate(collection,path)
    with _store_lock, repository.transaction():
        if collection == 'manifests':
            existing=repository.read(collection)
            for name,body in data.items():
                if body==existing.get(name) and name not in _dirty_packages and repository.revision('target',name): continue
                try:
                    validated=delivery.manifest(body)
                except ValueError:
                    continue  # Retain legacy records for correction, never silently delete them.
                import publications
                payload=versions.target_body(name,validated)
                problems=publications.target_issues(payload)
                if problems:
                    draft=publications.pending_target(name,payload)
                    body.update(publication_state='draft',pending_draft_id=draft['draft_id'])
                    body.pop('target_revision',None);body.pop('target_id',None)
                else:
                    published=repository.publish('target',name,payload)
                    versions.materialize(name,published)
                    body.update(target_revision=published['version'],target_id=published['entity_id'],publication_state='published')
                    previous_draft=body.pop('pending_draft_id',None)
                    if previous_draft:
                        old=repository.read('publication_drafts',previous_draft)
                        if old and old['status']=='draft' and __import__('delivery_state').clean_manifest(old['body']['manifest'])==__import__('delivery_state').clean_manifest(validated):
                            old.update(status='published',body=payload,published_version=published['version'],published_at=repository.now(),revision=old['revision']+1)
                            repository.put('publication_drafts',previous_draft,old)
                _dirty_packages.discard(name)
        elif collection == 'instances':
            existing = repository.read(collection)
            for name,body in data.items():
                if body == existing.get(name): continue
                if not isinstance(body,dict) or body.get('runtime_type') not in ('docker','netns'): continue
                published=versions.template(name,body)
                body.update(published)
        repository.replace_collection(collection,data)


def _serialized(fn):
    @functools.wraps(fn)
    def call(*args,**kwargs):
        with _store_lock, repository.transaction(): return fn(*args,**kwargs)
    return call


_docker_guard=threading.Lock()
_docker_processes=set()
_docker_stopping=False


def shutdown_docker():
    global _docker_stopping
    with _docker_guard:
        _docker_stopping=True
        processes=list(_docker_processes)
    for process in processes:
        try: os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError: pass


def _docker(cmd: str, timeout: int = 120) -> tuple[str, int]:
    with _docker_guard:
        if _docker_stopping: return 'Bridge 正在停止，操作中断',130
        process=subprocess.Popen(f'docker {cmd}',shell=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
        _docker_processes.add(process)
    try:
        stdout,stderr=process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGKILL)
        process.communicate()
        raise
    finally:
        with _docker_guard: _docker_processes.discard(process)
    out=stdout.strip()
    if process.returncode and stderr.strip(): out=out or stderr.strip()
    return out,process.returncode


def _container_net_bytes(name: str) -> tuple[int, int] | None:
    """容器内 eth* 接口累计 rx/tx 字节。
    docker stats 的 NetIO 对 --net=none 容器（CORE 方式）恒为 0B——它只统计 docker 自建网络,
    后注入的 veth 接口须直接读容器 netns 的 /sys/class/net 计数器"""
    import shlex
    script='for f in /sys/class/net/eth[0-9]*/statistics; do [ -d "$f" ] || continue; cat "$f/rx_bytes" "$f/tx_bytes" || exit 1; done'
    out,code=_docker('exec '+shlex.quote(name)+' sh -c '+shlex.quote(script),10)
    if code: return None
    try:
        values=[int(x) for x in out.split()]
        if not values or len(values)%2: return None
        return sum(values[::2]),sum(values[1::2])
    except ValueError:
        return None


def _fmt_bytes(n: float) -> str:
    for unit, div in (("TB", 2**40), ("GB", 2**30), ("MB", 2**20), ("KB", 2**10)):
        if n >= div:
            return f"{n/div:.1f}{unit}"
    return f"{n:.0f}B"


def _err(code: str, message: str, status: int = 400, **extra):
    body = {"ok": False, "error": {"code": code, "message": message}}
    if extra:
        body["error"].update(extra)
    return JSONResponse(body, status_code=status)


def _audit(action: str, **kw):
    """审计留痕（设计 §8.1）：主写 platform.db（audit 表, 可按会话/节点/动作过滤）,
    JSONL 双写一个版本期（历史兼容）; bridge.log 文本同步"""
    logger.info("AUDIT %s %s", action, kw)
    try:
        import platform_db
        platform_db.write_audit(action, **kw)
    except Exception as e:
        logger.warning("audit db write failed: %s", e)
    try:
        import time as _t
        entry = {"ts": _t.strftime("%Y-%m-%d %H:%M:%S"), "action": action, **kw}
        os.makedirs(os.path.dirname(AUDIT_LOG), exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        logger.warning("audit jsonl write failed: %s", e)


def _manifest_index() -> dict:
    """镜像 ref -> manifest（按 manifest.image.name/tag 索引）"""
    idx = {}
    for m in _load_manifests().values():
        if isinstance(m,dict) and m.get('publication_state')=='draft':
            published=repository.revision('target',m.get('name',''))
            if not published: continue
            m=published['manifest']
        if not isinstance(m, dict):
            continue
        try:
            m=delivery.manifest(m)
        except ValueError as e:
            logger.warning("legacy manifest requires correction: %s",e)
            continue
        img = m.get("image") or {}
        if not isinstance(img, dict):
            logger.warning("invalid legacy manifest image: %s", m.get("name"))
            continue
        nm, tg = img.get("name"), img.get("tag")
        if nm:
            key = f"{nm}:{tg or 'latest'}"
            # 「分开导入只导镜像件」会生成 services 为空的占位清单——与已编目清单同 ref 时不得遮蔽有内容的
            previous = idx.get(key)
            if previous is not None and not m.get('services') and previous.get('services'):
                continue
            idx[key] = m
    return idx


_svc_cache = {"loaded": False, "names": set(), "registry": {}}


def _service_registry() -> dict:
    """服务注册表（name -> 类对象）：内置 + custom_services——带缓存"""
    if _svc_cache["loaded"]:
        return _svc_cache["registry"]
    names, registry = set(), {}
    import sys, importlib
    importlib.invalidate_caches()
    prefixes={p.name for p in Path(CUSTOM_SERVICES_DIR).glob('*') if p.is_dir()}
    for module in list(sys.modules):
        if module.split('.')[0] in prefixes: sys.modules.pop(module,None)
    try:
        from core.services.manager import ServiceManager
        mgr = ServiceManager()
        mgr.load_locals()
        if os.path.isdir(CUSTOM_SERVICES_DIR):
            mgr.load(os.path.abspath(CUSTOM_SERVICES_DIR))
        registry = dict(mgr.services)
        names = set(registry.keys())
    except Exception as e:
        logger.warning("service registry load failed: %s", e)
    # 兜底: 内置名硬保底（ServiceManager 异常时 DefaultRoute 等不至于误报缺失）
    names.update({"DefaultRoute", "SSH", "NAT", "IPForward"})
    _svc_cache["names"] = names
    _svc_cache["registry"] = registry
    _svc_cache["loaded"] = True
    return registry


def _registered_service_names() -> set:
    """CORE 认识的服务名全集（内置 27 个 + custom_services 注册的）——带缓存"""
    return set(_service_registry().keys())


def _svc_dir_exists(svc_name: str) -> bool:
    """服务定义是否就位（设计 §4.1 services 件）：
    ① CORE 认识的服务名（内置 + custom_services 注册，前提⑤⁺ 基础服务条目算就位）
    ② 兜底扫描 custom_services .py 内容"""
    if svc_name in _registered_service_names():
        return True
    if not os.path.isdir(CUSTOM_SERVICES_DIR):
        return False
    needle = f'"{svc_name}"'
    for root, _dirs, files in os.walk(CUSTOM_SERVICES_DIR):
        for fn in files:
            if fn.endswith(".py"):
                try:
                    with open(os.path.join(root, fn), encoding="utf-8", errors="ignore") as f:
                        if needle in f.read():
                            return True
                except OSError:
                    continue
    return False


def _pieces(ref: str, mindex: dict) -> dict:
    """三件完整性（设计 §4.1）：镜像(docker 在列) / 服务定义(落地 custom_services) / 清单(已编目)"""
    repo = ref.rsplit(":", 1)[0]
    man = mindex.get(ref) or mindex.get(repo)
    svcs_ok = True
    svc_list = []
    for s in (man or {}).get("services") or []:
        # 漏洞标注（纯声明元数据）随 service_pairs 带出
        pair = {"name": s.get("name"), "mode": s.get("mode")}
        if s.get("cve") is not None: pair["cve"] = s.get("cve")
        if s.get("vulnerable") is not None: pair["vulnerable"] = s.get("vulnerable")
        svc_list.append(pair)
        if not _svc_dir_exists(s.get("name", "")):
            svcs_ok = False
    has_svcs = bool(svc_list)
    return {
        "image": True,
        "services": has_svcs and svcs_ok,
        "manifest": bool(man),
        "complete": bool(man) and has_svcs and svcs_ok,
        "manifest_data": man,
        "service_pairs": svc_list,
    }


def _enrich_pairs(pairs: list) -> list:
    """服务对附加实现层信息（设计 §8.2 服务表格数据源）：
    startup/validate 来自服务注册表；owner = 落位包（None=CORE 内置或未落位）"""
    reg = _service_registry()
    out = []
    for p in pairs:
        p = dict(p)
        cls = reg.get(p.get("name"))
        if cls:
            p["impl"] = {"startup": list(cls.startup or []),
                         "validate": list(getattr(cls, "validate", []) or []),
                         "files": list(cls.files or [])}
            p["owner"] = _service_name_owner(p.get("name")) or "CORE 内置"
        else:
            p["impl"] = None
            p["owner"] = "CORE 内置" if not str(p.get("name", "")).startswith("svc-") else None
        out.append(p)
    return out


# ═══════════════════════════════════════════════
# Tab1 镜像管理

# ═══════════════════════════════════════════════
# Tab1 镜像管理
# ═══════════════════════════════════════════════

@router.get("/targets")
def targets_list():
    """靶标镜像列表（按 image ID 去重别名，含三件完整性 + 编目状态）"""
    manifests = _load_manifests()
    mindex = _manifest_index()
    out, _code = _docker("images --format '{{json .}}'", 30)
    by_id = {}
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except json.JSONDecodeError:
            continue
        repo, tag = o.get("Repository") or "", o.get("Tag") or ""
        if not repo or repo == "<none>" or tag == "<none>":
            continue
        ref = f"{repo}:{tag}"
        iid = o.get("ID") or ref
        iid = ref  # Every tag remains selectable, including aliases of one image ID.
        pc = _pieces(ref, mindex)
        by_id[iid] = {
            "ref": ref,
            "size": o.get("Size", ""),
            "import_time": (pc.get("manifest_data") or {}).get("imported_at"),
            "created_at": o.get("CreatedAt"),
            "aliases": [],
            "pieces": {k: pc[k] for k in ("image", "services", "manifest", "complete")},
            "has_manifest": pc["manifest"],
        }
    # Keep the legacy Docker CLI size string; add the same byte count used by image details.
    if by_id:
        import shlex
        inspected,code=_docker("image inspect --format '{{json .}}' "+' '.join(shlex.quote(ref) for ref in by_id),30)
        if code==0:
            for line in inspected.splitlines():
                try:
                    item=json.loads(line)
                    if not isinstance(item,dict): continue
                    size=item.get('Size')
                    for ref in item.get('RepoTags') or []:
                        if ref not in by_id: continue
                        by_id[ref]['image_digest'] = item.get('Id')
                        if isinstance(size,int) and size>=0: by_id[ref]['size_bytes']=size
                except (ValueError,TypeError): continue
    # 规则（用户定稿）: 镜像必须导入（列表行存在的前提）; 服务定义/清单可缺, 进详情补录。
    # 纯 manifest 的孤儿条目不进列表（镜像未导入则整个靶标条目不存在）。
    images = sorted(by_id.values(), key=lambda i: i["import_time"] or "", reverse=True)
    return JSONResponse({"ok": True, "data": {"images": images, "manifests": manifests}})


@router.post("/targets/import/image")
def targets_import_image(body: dict):
    """导入镜像 tar（docker load）。body: {path} 服务器路径"""
    path = (body.get("path") or "").strip()
    if not path or not os.path.exists(path):
        return _err("FILE_NOT_FOUND", f"文件不存在: {path}")
    out, code = _docker(f"load -i '{path}'", 600)
    _audit("image.import", path=path, ok=code == 0)
    if code != 0:
        return _err("LOAD_FAILED", out or "docker load 失败", status=500)
    return JSONResponse({"ok": True, "data": {"output": out}})


def _import_delivery(body:dict,checkpoint=None,reuse=None,input_keys=None):
    import copy,time,shlex
    import delivery_state as ds
    try:
        body,mapping=ds.prepare(body)
        keys=input_keys or ds.identities(body)[0]
    except (ValueError,SyntaxError,TypeError) as e:
        return _err('DELIVERY_INVALID',str(e))
    package=body['package'];m=body.get('manifest');sources=body['python_files']
    result={'package':package,'pieces':{},'warnings':[],'hint':None}
    reuse=reuse or {}
    def progress(kind,value):
        result['pieces'][kind]={**value,'input_key':keys[kind]}
        if checkpoint: checkpoint(kind,copy.deepcopy(result))
    def cached(kind):
        previous=reuse.get(kind)
        if ds.reusable(kind,previous,keys[kind]):
            progress(kind,{**previous,'reused':True,'detail':'沿用内容和实际结果均未改变的已完成件'})
            return True
        return False
    if not cached('image'):
        progress('image',{'ok':None,'state':'running','detail':'正在加载镜像，完成后保存镜像 ID'})
        path=body.get('image_path')
        if body.get('services_only') or body.get('manifest_only'):
            progress('image',{'ok':None,'state':'not-requested','detail':'本次未处理镜像'})
        elif not path or not os.path.isfile(path):
            progress('image',{'ok':False,'state':'failed','detail':'有效镜像归档缺失，请补齐后续办'})
            return JSONResponse({'ok':True,'data':result})
        else:
            try:
                refs=ds.archive_refs(path)
                declared=(m['image']['name']+':'+m['image'].get('tag','latest')) if m else None
                if declared and refs and declared not in refs: raise ValueError('清单声明镜像不在本次归档内: '+declared)
                out,code=_docker('load -i '+shlex.quote(path),600)
                if code: raise ValueError(out or 'docker load 失败')
                if declared: refs=[declared]
                if not refs:
                    refs=[line.split(': ',1)[1] for line in out.splitlines() if line.startswith(('Loaded image: ','Loaded image ID: '))]
                receipt=ds.image_receipt(refs)
                progress('image',{'ok':True,'state':'done','detail':out[:1000],'receipt':receipt})
            except (ValueError,OSError,subprocess.TimeoutExpired,tarfile.TarError) as e:
                progress('image',{'ok':False,'state':'failed','detail':str(e)})
                return JSONResponse({'ok':True,'data':result})
    if body.get('defer_services'):
        import image_services
        receipt=(result['pieces'].get('image') or {}).get('receipt') or {}
        refs=list(receipt.get('images') or {})
        if not refs and m: refs=[m['image']['name']+':'+m['image'].get('tag','latest')]
        if not refs and body.get('image_ref'): refs=[body['image_ref']]
        staged=image_services.stage_delivery(body,refs)
        progress('services',{'ok':True,'state':'pending-confirmation','detail':'配置已接收，等待镜像详情最后确认；尚未注册','imports':staged})
        progress('manifest',{'ok':True,'state':'pending-confirmation','detail':'清单待确认，未改动已生效配置'})
        result['hint']='请打开镜像详情，预览并合入导入内容，最后确定后统一生效'
        return JSONResponse({'ok':True,'data':result})
    if not cached('services'):
        if sources:
            progress('services',{'ok':None,'state':'running','detail':'正在保存服务定义和模板'})
            try:
                with _store_lock,repository.transaction():
                    installed,dst=delivery.install(CUSTOM_SERVICES_DIR,package,sources,body.get('templates') or {},_prefixed_service_name)
                    _svc_cache['loaded']=False
                    _dirty_packages.add(package)
                    if not m:
                        current=_load_manifests()
                        if package in current: _save_manifests(current)
                    receipt=ds.service_receipt(package,installed.values(),body.get('templates') or {})
                    progress('services',{'ok':True,'state':'done','detail':str(dst),'activation':'saved','receipt':receipt})
            except (ValueError,SyntaxError,OSError) as e:
                progress('services',{'ok':False,'state':'failed','detail':str(e)})
        else:
            from core.services.manager import ServiceManager
            builtins=ServiceManager();builtins.load_locals()
            required=[svc['name'] for svc in (m or {}).get('services',[]) if svc['name'] not in builtins.services]
            available={name for path in (Path(CUSTOM_SERVICES_DIR)/package).glob('*.py') for name in delivery.names(path.read_text())}
            if m and set(required)<=available:
                receipt=ds.service_receipt(package,required,{})
                progress('services',{'ok':True,'state':'done','detail':'声明服务已保存或仅使用 CORE 内置服务','receipt':receipt})
            else: progress('services',{'ok':None,'state':'missing','detail':'缺少声明服务定义，可补录后续办'})
    if not cached('manifest'):
        if m:
            progress('manifest',{'ok':None,'state':'running','detail':'正在编目并固定交付版本'})
            try:
                with _store_lock,repository.transaction():
                    # A concurrent tag change must not silently bind another image to this delivery.
                    image_piece=result['pieces']['image']
                    if image_piece.get('ok') and not ds.reusable('image',image_piece,keys['image']):
                        raise ValueError('镜像引用已变化，请恢复原镜像后续办')
                    m['imported_at']=time.strftime('%Y-%m-%d %H:%M:%S')
                    store=_load_manifests();store[m['name']]=m;_save_manifests(store)
                    receipt={'name':m['name'],'version':store[m['name']].get('target_revision'),'manifest_key':ds.digest(ds.clean_manifest(m)),'draft_id':store[m['name']].get('pending_draft_id')}
                    progress('manifest',{'ok':True,'state':'done','detail':'已保存版本草稿，缺件补齐后发布' if receipt.get('draft_id') else '人工能力清单已发布并固定版本','receipt':receipt})
            except (ValueError,OSError) as e:
                progress('manifest',{'ok':False,'state':'failed','detail':str(e)})
        else: progress('manifest',{'ok':None,'state':'missing','detail':'未提供清单，可补录后续办'})
    result['hint']='件完成表示已保存；daemon 注册状态可在靶标版本详情查询'
    _audit('delivery.import',package=package,pieces=result['pieces'])
    return JSONResponse({'ok':True,'data':result})


def _bundle_body(body):
    directory = Path(body.get('dir') or '')
    if not body.get('dir') or not directory.is_dir():
        raise ValueError('包目录不存在')
    draft = _read_bundle(directory)
    merged = {**draft, **body}
    merged['package'] = body.get('package') or (merged.get('manifest') or {}).get('name') or draft['package']
    # 镜像归档: image.tar 优先, 兼容 image.tar.gz（docker load 原生支持 gzip 归档, 体积减半）
    merged['image_path'] = str(next((directory / n for n in ('image.tar', 'image.tar.gz')
                                     if (directory / n).is_file()), directory / 'image.tar'))
    return merged


@router.post('/targets/import/bundle')
def targets_import_bundle(body: dict):
    try:
        return _import_delivery(_bundle_body(body))
    except ValueError as e:
        return _err('DELIVERY_INVALID', str(e))


@router.post('/targets/import/pieces')
def targets_import_pieces(body: dict):
    return _import_delivery(body)


@router.post('/targets/import/services')
def targets_import_services(body: dict):
    if not body.get('py'):
        return _err('EMPTY_PY', '缺少 .py 内容')
    return _import_delivery({**body, 'services_only': True})


@router.post('/targets/import/manifest')
def targets_import_manifest(body: dict):
    return _import_delivery({**body, 'manifest_only': True})


@router.get("/targets/manifest/{name}")
def targets_get_manifest(name: str):
    store = _load_manifests()
    if name not in store:
        return _err("NOT_FOUND", f"未编目: {name}", status=404)
    return JSONResponse({"ok": True, "data": store[name]})


@router.post("/targets/manifest/{name}")
@_serialized
def targets_edit_manifest(name: str, body: dict):
    """手动补录/编辑清单（详情抽屉 JSON 表单保存入口）"""
    manifest = body.get('manifest') if 'manifest' in body else body
    try:
        manifest = delivery.manifest(manifest)
        if manifest['name'] != name:
            raise ValueError('清单名称与登记名称不一致')
    except ValueError as e:
        return _err('MANIFEST_INVALID', str(e))
    store = _load_manifests()
    store[name] = manifest
    _save_manifests(store)
    _audit("manifest.edit", name=name)
    return JSONResponse({"ok": True, "data": manifest})


@router.post("/targets/image/detail")
def targets_image_detail(body: dict):
    """靶标详情（设计 §8.2）：三件完整性 + manifest + docker inspect 摘要。body: {ref}"""
    ref = (body.get("ref") or "").strip()
    if not ref:
        return _err("NO_REF", "缺少 ref")
    mindex = _manifest_index()
    pc = _pieces(ref, mindex)
    selected = None
    if body.get('target_name') or body.get('target_version'):
        name=body.get('target_name') or (pc.get('manifest_data') or {}).get('name')
        t=repository.revision('target',name,body.get('target_version'))
        if not t or not versions.matches_image(t,ref): return _err('TARGET_INVALID','靶标版本与镜像不一致')
        pc['manifest_data']=t['manifest'];pc['manifest']=True
        pc['service_pairs']=t['manifest']['services']
        selected={'name':name,'version':t['version']}
    elif pc.get('manifest_data'):
        name=pc['manifest_data'].get('name')
        latest=repository.revision('target',name)
        if latest: selected={'name':name,'version':latest['version']}
    out, code = _docker("inspect --format '{{json .}}' " + ref, 30)
    if code != 0:
        # 镜像必须导入（用户定稿）: 镜像不存在则条目不存在, 无"缺镜像可补"状态
        return _err("NOT_FOUND", f"镜像不存在: {ref}", status=404)
    info = {}
    try:
        raw = json.loads(out.splitlines()[0])
        cfg = raw.get("Config") or {}
        info = {
            "id": (raw.get("Id") or "").split(":")[-1][:12],
            "created": raw.get("Created"),
            "os_arch": f'{raw.get("Os", "?")}/{raw.get("Architecture", "?")}',
            "workdir": cfg.get("WorkingDir") or "/",
            "cmd": cfg.get("Cmd"),
            "entrypoint": cfg.get("Entrypoint"),
            "ports": sorted((cfg.get("ExposedPorts") or {}).keys()),
            "env_count": len(cfg.get("Env") or []),
            "labels": cfg.get("Labels") or {},
            "repo_tags": raw.get("RepoTags") or [],
        }
    except (json.JSONDecodeError, IndexError):
        return _err("INSPECT_FAILED", "docker inspect 输出解析失败", status=500)
    return JSONResponse({"ok": True, "data": {"ref": ref, "pieces": pc, "manifest": pc["manifest_data"],
                                              "service_pairs": _enrich_pairs(pc["service_pairs"]), "inspect": info,
                                              "selected_target": selected}})


@router.post("/targets/image/delete")
def targets_image_delete(body: dict):
    """删除镜像（先占用检查，设计 §3.1 IMAGE_CONFLICT）。body: {ref}"""
    ref = (body.get("ref") or "").strip()
    if not ref:
        return _err("NO_REF", "缺少 ref")
    from image_services import inspect_image

    try:
        digest = inspect_image(ref).get('Id')
    except (ValueError, OSError):
        digest = None
    affected=[]
    for name,manifest in _load_manifests().items():
        for version in repository.history('target',name):
            exact = version.get('image_ref') == ref
            if not exact and (not digest or not version.get('catalog') or version.get('image_digest') != digest):
                continue
            refs = repository.references('target',name,version['version'])
            consumers = [r for r in refs['documents'] if exact and r['collection'] != 'manifests']
            templates = refs['revisions']
            if not exact:
                templates = [r for r in templates if r['owner_kind'] == 'template' and
                             (repository.revision('template',r['owner_name'],r['owner_version']) or {}).get('image_tag') == ref]
            if consumers or templates:
                affected.append({'name':name,'version':version['version'],'documents':consumers,'templates':templates})
    if affected: return _err('IMAGE_VERSION_REFERENCED','镜像仍被模板版本、草稿或历史部署引用，未删除',status=409,references=affected)
    out, _c = _docker("ps -a --filter ancestor='" + ref + "' --format '{{.Names}}'", 30)
    used = [x for x in out.splitlines() if x.strip()]
    if used:
        return _err("IMAGE_CONFLICT", f"镜像被容器引用，删除被拒: {', '.join(used[:5])}", status=409,
                    containers=used)
    out, code = _docker(f"rmi -f '{ref}'", 120)
    _audit("image.delete", ref=ref, ok=code == 0)
    if code != 0:
        return _err("RMI_FAILED", out or "docker rmi 失败", status=500)
    return JSONResponse({"ok": True, "data": {"ref": ref, "output": out}})


# ═══════════════════════════════════════════════
# Tab2 独立容器管理
# ═══════════════════════════════════════════════

def _ensure_standalone(name: str):
    """命名隔离硬规则（设计 §4.2）：Tab2 只允许操作 standalone- 前缀容器，
    物理上看不到/动不到 CORE 拓扑容器（防止误删正在运行的演练）。
    返回错误响应（拒绝）或 None（放行）"""
    if not name.startswith(STANDALONE_PREFIX):
        return _err("NOT_STANDALONE", f"仅允许操作 {STANDALONE_PREFIX}* 独立容器，{name} 不是独立容器", status=403)
    return None


@router.get("/containers/standalone")
def standalone_containers_list():
    """独立容器列表（standalone- 前缀，普通隔离，验证用；CORE 拓扑容器不在此列）"""
    out, _code = _docker("ps -a --format '{{.Names}}\\t{{.Image}}\\t{{.Status}}' --filter name=standalone-", 30)
    items = []
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 3:
            items.append({"name": parts[0], "image": parts[1], "status": parts[2]})
    return JSONResponse({"ok": True, "data": items})


@router.post("/containers/standalone/run")
def standalone_container_run(body: dict):
    """独立启动容器（普通隔离，验证用；不接 CORE 网络）。
    body: {image, name?, ports?: ["8080:80"], command?}
    command: 可选保活/初始命令——路线 B 镜像无 CMD, 独立验证需带 `tail -f /dev/null` 否则立即退出"""
    image = (body.get("image") or "").strip()
    if not image:
        return _err("NO_IMAGE", "缺少 image")
    name = (body.get("name") or f"standalone-{image.split('/')[-1].split(':')[0]}").strip()
    if not name.startswith(STANDALONE_PREFIX):
        name = STANDALONE_PREFIX + name
    ports = body.get("ports") or []
    port_args = " ".join(f"-p {p}" for p in ports)
    cmd_args = ""
    command = (body.get("command") or "").strip()
    if command:
        cmd_args = " " + command
    dup, _c = _docker("ps -a --format '{{.Names}}' --filter name=^" + name + "$", 30)
    if dup.strip():
        return _err("NAME_CONFLICT", f"容器名已存在: {name}（先删除或改名）", status=409)
    out, code = _docker(f"run -d --name {name} {port_args} {image}{cmd_args}", 120)
    _audit("container.run", name=name, image=image, ok=code == 0)
    if code != 0:
        return _err("RUN_FAILED", out, status=500)
    # 秒退检测: 无常驻进程的镜像（如 kali 官方镜像）不填保活命令会立即 Exited
    hint = None
    import time as _t
    _t.sleep(1.2)
    st, _c = _docker("inspect --format '{{.State.Running}} {{.State.ExitCode}}' " + name, 15)
    if st.startswith("false"):
        hint = ("容器已立即退出（Exited）——该镜像没有常驻进程。"
                "请删除后重新创建，并在「启动命令」填 tail -f /dev/null 保活")
    data = {"name": name, "image": image}
    if hint:
        data["hint"] = hint
    return JSONResponse({"ok": True, "data": data, **({"hint": hint} if hint else {})})


@router.post("/containers/standalone/{name}/stop")
def standalone_container_stop(name: str):
    denied = _ensure_standalone(name)
    if denied:
        return denied
    out, code = _docker(f"stop {name}", 60)
    _audit("container.stop", name=name, ok=code == 0)
    return JSONResponse({"ok": code == 0, "data": {"name": name, "output": out}})


@router.post("/containers/standalone/{name}/start")
def standalone_container_start(name: str):
    denied = _ensure_standalone(name)
    if denied:
        return denied
    out, code = _docker(f"start {name}", 60)
    _audit("container.start", name=name, ok=code == 0)
    return JSONResponse({"ok": code == 0, "data": {"name": name, "output": out}})


@router.delete("/containers/standalone/{name}")
def standalone_container_delete(name: str):
    denied = _ensure_standalone(name)
    if denied:
        return denied
    out, code = _docker(f"rm -f {name}", 60)
    _audit("container.delete", name=name, ok=code == 0)
    return JSONResponse({"ok": code == 0, "data": {"name": name, "output": out}})


@router.post("/containers/standalone/{name}/exec")
def standalone_container_exec(name: str, body: dict):
    """容器内执行命令（审计留痕）。body: {cmd}"""
    denied = _ensure_standalone(name)
    if denied:
        return denied
    cmd = (body.get("cmd") or "").strip()
    if not cmd:
        return _err("NO_CMD", "缺少 cmd")
    out, code = _docker(f"exec {name} {cmd}", 60)
    _audit("container.exec", name=name, cmd=cmd)
    return JSONResponse({"ok": code == 0, "data": {"output": out, "return_code": code}})


# ═══════════════════════════════════════════════
# Tab3 节点实例管理（设计 §5）
# 实例 = 模板：拓扑绑定实例名（引用），改实例所有引用拓扑下次部署生效；
# 被引用锁定（locked）；同软件 normal/vuln 互斥、跨软件可组合
# ═══════════════════════════════════════════════

_GENERATED_SERVICE_PACKAGE = re.compile(r'version_[0-9a-f]{12}_[0-9]+|image_exec_[0-9a-f]{24}')


def _available_services() -> dict:
    """List declarations and selectable registrations, excluding generated implementations."""
    from core.services.manager import ServiceManager
    manager=ServiceManager();manager.load_locals()
    native=set(manager.services)
    declared={}
    for manifest in _load_manifests().values():
        if not isinstance(manifest,dict): continue
        for service in manifest.get('services') or []:
            declared[service['name']]={'name':service['name'],'mode':service.get('mode'),'software':service.get('software') or manifest.get('software') or manifest.get('name'),'source':'image'}
    registry = _service_registry()
    runtime_aliases = {
        name for name, service in registry.items()
        if _GENERATED_SERVICE_PACKAGE.fullmatch(service.__module__.partition('.')[0])
    }
    custom=[]
    for name in sorted(registry.keys() - native - runtime_aliases):
        custom.append(declared.get(name,{'name':name,'mode':None,'software':name,'source':'image' if name.startswith(('svc-','imgsvc-','imgmode-')) else 'unknown'}))
    for name,service in declared.items():
        if name not in native and name not in registry: custom.append(service)
    return {'drill':custom,'system':sorted(native),'image':[item for item in custom if item['source']=='image'],'unknown':[item for item in custom if item['source']=='unknown']}


def _mutex_conflict(services: list, available: dict) -> dict | None:
    """同软件 normal/vuln 互斥校验（设计 §5.3）；跨软件可组合。
    返回冲突对描述或 None"""
    by_sw: dict = {}
    for s in services:
        sw = next((d["software"] for d in available["drill"] if d["name"] == s.get("name")), None)
        if sw is None:
            continue
        mode = next((d.get("mode") for d in available["drill"] if d["name"] == s.get("name")), None)
        by_sw.setdefault(sw, []).append(mode)
    for sw, modes in by_sw.items():
        if "normal" in modes and "vuln" in modes:
            return {"software": sw,
                    "detail": f"软件 {sw} 的 normal 与 vuln 声明互斥（二选一）"}
    return None


def _validate_instance(inst: dict, available: dict) -> JSONResponse | None:
    """实例创建/更新公共校验。返回错误响应或 None。
    服务来源过滤（清单驱动声明 ⑤⁺ + Tab3 分组规则）:
    - netns 实例: 仅 system 基础服务（宿主 rootfs 可用）; drill 演练服务是容器靶标专属 → 拒绝
    - docker 实例: 镜像清单（manifest.services）内的条目 + CORE 内置服务；
      声明清单外的 drill 服务 → SERVICE_NOT_IN_MANIFEST（声明时拦截错配, 比预检更前置）"""
    name = (inst.get("name") or "").strip()
    if not name:
        return _err("NO_NAME", "实例缺少 name")
    if "/" in name or "\\" in name or name in (".",".."):
        return _err("NAME_INVALID","模板名不能包含路径分隔符")
    rt = inst.get("runtime_type")
    if rt not in ("docker", "netns"):
        return _err("RUNTIME_INVALID", "runtime_type 仅允许 docker | netns")
    img = (inst.get("image_tag") or "").strip()
    if rt == "docker" and not img:
        return _err("IMAGE_REQUIRED", "docker 型实例必须绑定 image_tag")
    if rt == "netns" and img:
        return _err("NETNS_NO_IMAGE", "netns 型共享宿主 rootfs，不绑定镜像")
    if rt == 'netns' and not inst.get('services') and inst.get('model','PC') in ('PC','host','router','mdr'):
        return _err('CORE_DEFAULT_SERVICES', 'CORE 会为该型号回填默认服务；请至少明确选择一项服务，或选无默认服务的型号')
    if rt == 'netns':
        from native_services import ServiceConfig, check_selection
        from pydantic import TypeAdapter

        names = []
        for service in inst.get('services') or []:
            if not isinstance(service, dict) or not isinstance(service.get('name'), str) or service.get('mode'):
                return _err('SERVICE_INVALID', '请选择已注册的 CORE 原生服务')
            names.append(service['name'])
        if len(names) != len(set(names)):
            return _err('SERVICE_DUPLICATE', '重复声明原生服务')
        try:
            raw = inst.get('service_configs')
            parsed = TypeAdapter(dict[str, ServiceConfig]).validate_python({} if raw is None else raw)
            check_selection(names, parsed)
        except ValueError as error:
            return _err('SERVICE_INVALID', str(error))
        return None
    if inst.get('service_configs'):
        return _err('SERVICE_INVALID', 'Docker 模板的配置请在镜像服务模式中管理')
    available={k:list(v) for k,v in available.items()}
    manifest_ok=None
    if rt=='docker':
        from image_services import catalog_for

        name, version = inst.get('target_name'), inst.get('target_version')
        man = None
        if not (name and version):
            man = _manifest_index().get(img)
            if man:
                name = name or man['name']
                version = version or man.get('target_revision')
            else:
                try:
                    catalog = catalog_for(img)
                except (ValueError, OSError):
                    catalog = {}
                name = name or catalog.get('target_name')
                version = version or catalog.get('applied_target_version')
        target = repository.revision('target',name,version) if name and (version or man) else None
        if target:
            if not versions.matches_image(target,img):
                return _err('TARGET_INVALID','靶标版本不存在或与镜像不一致')
            man = target['manifest']
        elif version:
            return _err('TARGET_INVALID','靶标版本不存在或与镜像不一致')
        if not man: return _err('MANIFEST_REQUIRED','docker 模板须绑定已编目的靶标版本')
        if target and target.get('catalog'):
            import image_services
            try:
                selection=inst.get('service_selection')
                if selection is None:
                    declared={s['name'] for s in inst.get('services',[])}
                    selection={s['id']:mode['id'] for s in target['catalog']['services'] for mode in s['modes'] if image_services.mode_name(s,mode) in declared}
                chosen,_=image_services.selection_for(target['catalog'],selection)
                inst.update(service_selection=selection,services=chosen,target_name=name,target_version=target['version'])
                return None
            except (ValueError,KeyError,TypeError) as e:
                return _err('SERVICE_COMBINATION_INVALID',str(e))
        manifest_ok={s['name'] for s in man['services']}
        available['drill']=[{'name':s['name'],'mode':s.get('mode') or None,'software':s.get('software') or man.get('software') or man['name']} for s in man['services'] if s['name'] not in available['system']]
    drill_names={d['name'] for d in available['drill']}
    system_names=set(available['system'])
    seen_services = set()
    for s in inst.get("services") or []:
        if not isinstance(s, dict):
            return _err('SERVICE_INVALID', 'services 条目必须是对象')
        sn = s.get("name", "")
        if sn in seen_services:
            return _err('SERVICE_DUPLICATE', '重复声明服务: '+sn)
        seen_services.add(sn)
        declared_mode = next((d.get('mode') for d in available['drill'] if d['name'] == sn), None)
        if s.get('mode') and s['mode'] != declared_mode:
            return _err('MODE_INVALID', '服务模式与清单不一致: '+sn)
        s['mode'] = declared_mode
        if sn not in drill_names and sn not in system_names:
            return _err("SERVICE_UNKNOWN", f"服务未注册: {sn}（清单驱动声明，请从可用服务中选择）",
                        service=sn)
        if sn not in drill_names and s.get("mode"):
            return _err("MODE_INVALID", f"系统服务 {sn} 无 normal/vuln 之分", service=sn)
        if rt == "netns" and sn in drill_names:
            return _err("NETNS_DRILL_DENIED",
                        f"netns 节点不可声明演练服务 {sn}（演练服务为容器靶标专属；netns 仅基础服务）",
                        service=sn)
        if rt == "docker" and sn not in manifest_ok:
            return _err("SERVICE_NOT_IN_MANIFEST",
                        f"服务 {sn} 不在该镜像的清单声明中（清单如实声明原则 ⑤⁺——先编目再声明）",
                        service=sn)
    conflict = _mutex_conflict(inst.get("services") or [], available)
    if conflict:
        return _err("SERVICES_MUTEX", conflict["detail"], status=409, conflict=conflict)
    return None


def _instance_refs(name: str, topologies: dict) -> list:
    """实例的全部引用方 = 活跃部署引用（platform.db, 自动登记）+ 想定登记表（topologies.json, 手动）"""
    import platform_db
    refs = [f"{r['drill_id']}（会话{r['core_sid']}）" for r in platform_db.query_deploy_refs(name)]
    # topologies.json 值必须为 dict; 跳过注释/损坏条目
    refs += [t for t, td in topologies.items()
             if isinstance(td, dict) and name in (td.get("instances") or [])]
    refs += [d['topology_id'] for d in repository.read('topologies').values() if any(n.get('instance')==name for n in d.get('definition',{}).get('nodes',[]))]
    history=repository.references('template',name)
    refs += [f"{r['collection']}/{r['name']}（v{r['version']}）" for r in history['documents'] if r['collection'] not in ('instances','manifests')]
    return sorted(set(refs))


@router.get("/instances")
def instances_list():
    """节点实例列表（含引用/锁定状态，设计 §5.2）"""
    instances = _load_store(INSTANCE_STORE)
    topologies = _load_store(TOPOLOGY_STORE)
    out = []
    for name, inst in instances.items():
        if not isinstance(inst,dict):
            out.append({'name':name,'runtime_type':'unknown','services':[],'migration_error':'旧记录不是对象，请修正原始记录'})
            continue
        refs = _instance_refs(name, topologies)
        if not repository.revision('template',name):
            try:
                inst=versions.template(name,inst)
            except ValueError as e:
                inst={**inst,'migration_error':str(e)}
        out.append({**inst, "name": name, "locked": False, "delete_locked":bool(refs), "referenced_by": refs})
    return JSONResponse({"ok": True, "data": out})


@router.get("/instances/available-services")
def instances_available_services():
    """可声明服务总表（drill 成对 + system 基础），实例编辑器的服务选择来源"""
    return JSONResponse({"ok": True, "data": _available_services()})


@router.post("/instances")
@_serialized
def instances_create(body: dict):
    """创建节点实例。body: {name, runtime_type, image_tag?, services?, ports?, cve?, flags?}"""
    inst = dict(body)
    name = (inst.get("name") or "").strip()
    available = _available_services()
    err = _validate_instance(inst, available)
    if err:
        return err
    instances = _load_store(INSTANCE_STORE)
    if name in instances:
        return _err("INSTANCE_DUPLICATE", f"实例已存在: {name}", status=409)
    instances[name] = {k: v for k, v in inst.items() if k != "name"}
    _save_store(INSTANCE_STORE, instances)
    _audit("instance.create", name=name, runtime_type=inst.get("runtime_type"))
    return JSONResponse({"ok": True, "data": {"name": name}})


@router.post("/instances/{name}")
@_serialized
def instances_update(name: str, body: dict):
    """更新实例（模板式：引用它的拓扑下次部署生效）。被引用锁定 → 409（设计 §5.2）"""
    instances = _load_store(INSTANCE_STORE)
    if name not in instances:
        return _err("NOT_FOUND", f"实例不存在: {name}", status=404)
    if body.get('base_version') is not None and body['base_version']!=(repository.revision('template',name) or {}).get('version'):
        return _err('VERSION_CONFLICT','模板已有更新版本，请重新打开并合并修改',status=409)
    inst = dict(body)
    inst.pop('base_version',None)
    inst["name"] = name  # 实例名不可改（引用锚点）
    err = _validate_instance(inst, _available_services())
    if err:
        return err
    instances[name] = {k: v for k, v in inst.items() if k != "name"}
    _save_store(INSTANCE_STORE, instances)
    _audit("instance.update", name=name)
    return JSONResponse({"ok": True, "data": {"name": name}})


@router.post("/instances/{name}/clone")
@_serialized
def instances_clone(name: str, body: dict):
    """复制实例（被引用锁定的出路之二，设计 §5.2）。body: {name?: 新名}"""
    instances = _load_store(INSTANCE_STORE)
    if name not in instances:
        return _err("NOT_FOUND", f"实例不存在: {name}", status=404)
    base = name + "-copy"
    new = (body.get("name") or "").strip()
    if not new:
        i = 1
        new = base
        while new in instances:
            i += 1
            new = f"{base}{i}"
    if new in instances:
        return _err("INSTANCE_DUPLICATE", f"实例已存在: {new}", status=409)
    instances[new] = {k:v for k,v in instances[name].items() if k not in ('version','entity_id')}
    _save_store(INSTANCE_STORE, instances)
    _audit("instance.clone", name=name, new=new)
    return JSONResponse({"ok": True, "data": {"name": new}})


@router.delete("/instances/{name}")
@_serialized
def instances_delete(name: str):
    """删除实例（被引用锁定 → 409）"""
    instances = _load_store(INSTANCE_STORE)
    if name not in instances:
        return _err("NOT_FOUND", f"实例不存在: {name}", status=404)
    topologies = _load_store(TOPOLOGY_STORE)
    refs = _instance_refs(name, topologies)
    if refs:
        return _err("INSTANCE_LOCKED", f"被拓扑引用，禁止删除: {', '.join(refs)}",
                    status=409, referenced_by=refs)
    del instances[name]
    _save_store(INSTANCE_STORE, instances)
    _audit("instance.delete", name=name)
    return JSONResponse({"ok": True, "data": {"name": name}})


@router.websocket("/ws/containers/standalone/{name}/terminal")
async def standalone_terminal_ws(websocket: WebSocket, name: str):
    """独立容器交互式终端。docker exec -it 挂真实 PTY，双向转发 WebSocket。

    用法（与 CORE 节点终端一致）:
      const ws = new WebSocket("ws://host:8080/ws/containers/standalone/standalone-dmz/terminal");
      ws.onmessage = (e) => console.log(e.data);   // 终端输出
      ws.send("ls /\n");                            // 发送命令
      ws.send("\\x03");                              // Ctrl+C
    """
    # 命名隔离：非 standalone 容器拒绝进入终端（设计 §4.2）
    if not name.startswith(STANDALONE_PREFIX):
        await websocket.accept()
        await websocket.send_json({"error": f"仅允许进入 {STANDALONE_PREFIX}* 独立容器终端"})
        await websocket.close(code=1008)
        return

    await websocket.accept()
    master_fd = None
    process = None
    loop = asyncio.get_running_loop()
    try:
        # 同 app.py 节点终端: docker exec 需要真实 PTY（-t 校验 + readline 行编辑），
        # 用 pty.openpty() 生成对端，exec 进程挂 slave，后端读写 master
        master_fd, slave_fd = pty.openpty()
        cmd = f"docker exec -i -t {name} env TERM=xterm bash"
        logger.info("standalone_terminal_ws: %s", cmd)
        process = await asyncio.create_subprocess_shell(
            cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, start_new_session=True)
        os.close(slave_fd)
        os.set_blocking(master_fd, False)

        from buffers import PtyQueue
        read_queue = PtyQueue(loop,master_fd)

        def _on_readable():
            try:
                data = os.read(master_fd, 4096)
            except BlockingIOError:
                return
            except OSError:
                read_queue.put_nowait(None)
                return
            read_queue.put_nowait(data if data else None)

        read_queue.callback = _on_readable
        loop.add_reader(master_fd, _on_readable)

        async def pipe_to_ws():
            try:
                while True:
                    data = await read_queue.get()
                    if data is None:
                        break
                    await websocket.send_text(data.decode(errors="replace"))
            except (WebSocketDisconnect, ConnectionResetError):
                pass
            except Exception:
                logger.exception("standalone terminal stdout pipe error")

        reader_task = asyncio.create_task(pipe_to_ws())
        try:
            while True:
                raw = await websocket.receive_text()
                if process.returncode is not None:
                    break
                if raw in ("\x03", "\x04"):
                    os.write(master_fd, raw.encode())
                else:
                    os.write(master_fd, raw.encode())
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
            # exec + bash 整进程组 SIGKILL（同 vcmd 教训: 单杀父进程留孤儿）
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
        logger.exception("standalone_terminal_ws error")
        try:
            await websocket.send_json({"error": str(e)[:200]})
            await websocket.close(code=1011)
        except Exception:
            pass


# ═══════════════════════════════════════════════
# Tab5 数据监测（设计 §7）
# 三分组: CORE 会话容器 / 独立容器(不深监测) / 其他容器(列表可见不深监测)
# 宿主载荷: /proc 直读; CORE SSE 三流 bridge 已有, 前端直连透传
# ═══════════════════════════════════════════════

@router.get("/monitor/host")
def monitor_host():
    """宿主载荷：CPU ticks(前端两次采样算 %) + loadavg + 内存 + 磁盘"""
    def _disk(path: str) -> dict | None:
        try:
            st = os.statvfs(path)
            total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
            return {"path": path, "total_gb": round(total / 2**30, 1),
                    "used_gb": round((total - free) / 2**30, 1),
                    "used_pct": round((total - free) / total * 100, 1) if total else 0}
        except OSError:
            return None

    cpu_ticks = {}
    try:
        with open("/proc/stat") as f:
            for ln in f:
                if ln.startswith("cpu "):
                    cpu_ticks["total"] = [int(x) for x in ln.split()[1:8]]
                    break
    except OSError:
        pass
    mem = {}
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for ln in f:
                parts = ln.split(":")
                if len(parts) == 2:
                    info[parts[0]] = int(parts[1].strip().split()[0])  # kB
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        mem = {"total_gb": round(total / 2**20, 2), "used_gb": round((total - avail) / 2**20, 2),
               "used_pct": round((total - avail) / total * 100, 1) if total else 0}
    except (OSError, ValueError):
        pass
    return JSONResponse({"ok": True, "data": {
        "cpu_ticks": cpu_ticks,
        "loadavg": [round(x, 2) for x in os.getloadavg()],
        "mem": mem,
        "disks": [d for d in (_disk("/"), _disk("/opt")) if d],
    }})


@router.get("/monitor/containers")
def monitor_containers():
    """容器三分组视图（设计 §7.2）。CORE 会话容器按会话节点名匹配（DockerNode 容器名=节点名）"""
    # 全量容器列表
    out, _c = _docker("ps -a --format '{{.Names}}\\t{{.Image}}\\t{{.Status}}'", 30)
    containers = []
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 3:
            containers.append({"name": parts[0], "image": parts[1], "status": parts[2]})

    # 运行中容器 stats（独立容器按设计只列不深监测, 但 stats 一次拿到无妨——分组决定展示深度）
    stats = {}
    sout, _c = _docker("stats --no-stream --format '{{.Name}}\\t{{.CPUPerc}}\\t{{.MemUsage}}\\t{{.MemPerc}}\\t{{.NetIO}}'", 30)
    for ln in sout.splitlines():
        p = ln.split("\t")
        if len(p) >= 5:
            stats[p[0]] = {"cpu": p[1], "mem_usage": p[2], "mem_pct": p[3], "net_io": p[4]}

    # CORE 会话容器名集合（RUNTIME/DATACOLLECT 会话的 docker 型节点名）
    # 注意 wrappers 的 SessionState/NodeType 是普通 Enum, 与 int 比较恒 False, 统一取 .value
    core_names = set()
    core_drills: dict = {}  # 容器名 -> ["d-xxx（会话N）", ...]（多会话归属标注, 设计 §7.2）
    try:
        import platform_db
        import app as _app
        b = getattr(_app, "bridge", None)
        if b is not None:
            for s in b.get_sessions():
                st = getattr(s.state, "value", s.state)
                if st in (4, 5):
                    sess = b.get_session(s.id)
                    drill_id = platform_db.ensure_drill(s.id)
                    label = f"{drill_id}（会话{s.id}）"
                    # sess.nodes 是 {node_id: Node} 字典
                    for n in (sess.nodes.values() if isinstance(sess.nodes, dict) else sess.nodes):
                        nt = getattr(n.type, "value", n.type)
                        if nt == 15 or (getattr(n, "image", None) or ""):
                            core_names.add(n.name)
                            core_drills.setdefault(n.name, []).append(label)
    except Exception as e:
        logger.warning("core session lookup failed: %s", e)

    groups = {"core": [], "standalone": [], "others": []}
    for c in containers:
        c = dict(c)
        c["stats"] = stats.get(c["name"]) if c["status"].startswith("Up") else None
        if c["name"].startswith(STANDALONE_PREFIX):
            groups["standalone"].append(c)
        elif c["name"] in core_names:
            c["drills"] = sorted(set(core_drills.get(c["name"], [])))
            # docker stats NetIO 对 --net=none 容器恒为 0B → core 组改读容器内接口计数器（真实累计收发）
            if c["stats"] is not None:
                nb = _container_net_bytes(c["name"])
                if nb:
                    c["stats"]["net_io"] = f"RX {_fmt_bytes(nb[0])} / TX {_fmt_bytes(nb[1])}"
            groups["core"].append(c)
        else:
            groups["others"].append(c)
    return JSONResponse({"ok": True, "data": {"groups": groups}})


@router.get("/audit")
def audit_list(action: str | None = None, session_id: int | None = None,
                     node_id: int | None = None, limit: int = 100, drill_id: str | None = None):
    """审计查询（platform.db audit 表）：动作/会话/节点任意组合过滤"""
    import platform_db
    entries = platform_db.query_audit(action=action, session_id=session_id,
                                      node_id=node_id, limit=max(1,min(limit,1000)),drill_id=drill_id)
    # 兼容旧前端字段: ok/fail 从 detail 提取
    for e in entries:
        if isinstance(e.get("detail"), dict) and "ok" in e["detail"]:
            e["ok"] = e["detail"]["ok"]
    return JSONResponse({"ok": True, "data": {"total": len(entries), "entries": entries}})


@router.get("/drills")
def drills_list(limit: int = 200, display_names: bool = False):
    """演练身份列表（drill_id 为数据主键; CORE 会话号可回收, 仅作展示）"""
    import platform_db
    rows = platform_db.query_drills(limit=limit)
    if display_names:
        import topologies
        names = {}
        for deployment in repository.deployment_rows(limit=-1, drill_ids=[row['drill_id'] for row in rows]):
            if deployment['drill_id'] not in names:
                names[deployment['drill_id']] = topologies.display_name(deployment)
        for row in rows:
            row['display_name'] = names.get(row['drill_id'], row['name'])
    return JSONResponse({"ok": True, "data": rows})


@router.delete("/drills/{drill_id}/data")
def drills_purge_data(drill_id: str):
    """手动清除某演练的 metrics/events（drills 行与 audit 保留; 自动保留策略见 platform_db 配置）"""
    import platform_db
    return JSONResponse({"ok": True, "data": platform_db.purge_drill_data(drill_id)})


@router.get("/metrics")
def metrics_query(drill_id: str | None = None, node_id: int | None = None, limit: int = 500):
    """历史监测指标查询（采样线程仅 RUNTIME 会话期间 5s 一拍落库）"""
    import platform_db
    rows = platform_db.query_metrics(drill_id=drill_id, node_id=node_id, limit=limit)
    return JSONResponse({"ok": True, "data": {"total": len(rows), "entries": rows}})


@router.get("/events")
def events_query(drill_id: str | None = None, limit: int = 200, session_id: int | None = None, before_id: int | None = None):
    """持久化会话事件查询（动作时间线复盘; 实时视图走 SSE /events/stream）"""
    import platform_db
    rows = platform_db.query_events(drill_id=drill_id, limit=max(1,min(limit,1000)),core_sid=session_id,before_id=before_id)
    return JSONResponse({"ok": True, "data": {"total": len(rows), "entries": rows}})


# ═══════════════════════════════════════════════
# A1 服务/端口就绪度预检（设计 §8.1 环境与声明一致性检查）
# 原则: 一切检查项来自"声明"——节点声明的 CORE 服务（validate 命令）+ 编目清单端口。
# 平台不窥探镜像内容; 外源镜像自动降级到其声明范围（L1 存活 + L2 服务）。
# ═══════════════════════════════════════════════

def _service_validate_map() -> dict:
    """服务名 -> validate 命令列表（CORE 内置 + custom_services 统一加载）"""
    from core.services.manager import ServiceManager
    mgr = ServiceManager()
    mgr.load_locals()
    if os.path.isdir(CUSTOM_SERVICES_DIR):
        mgr.load(os.path.abspath(CUSTOM_SERVICES_DIR))
    return {name: list(getattr(cls, "validate", []) or []) for name, cls in mgr.services.items()}


def _manifest_ports_map() -> dict:
    """镜像 ref -> 编目端口列表（L3 仅编目镜像有）。ports 兼容 image.ports 与顶层两种写法"""
    out = {}
    for m in _load_manifests().values():
        if not isinstance(m, dict):
            continue
        img = m.get("image") or {}
        if not isinstance(img,dict): continue
        nm = img.get("name")
        if nm:
            ports = [str(p) for p in (img.get("ports") or m.get("ports") or [])]
            out[f"{nm}:{img.get('tag') or 'latest'}"] = ports
    return out


@router.post("/precheck")
def precheck(session_id: int, wait: int = 8):
    """环境与声明一致性预检（RUNTIME 后、红方开打前）。
    L1 容器存活 / L2 声明服务的 validate 命令 / L3 编目端口探测。
    wait: 失败项宽限重试窗口（秒）——刚实例化时服务进程可能尚未完成端口绑定（慢机器实测踩过）。
    不回答漏洞是否可利用（无验证门禁原则, 设计 §1.3）。"""
    import platform_db
    import time as _t
    import app as _app
    b = getattr(_app, "bridge", None)
    if b is None:
        return _err("BRIDGE_UNAVAILABLE", "bridge 未连接", status=503)
    try:
        sess = b.get_session(session_id)
    except Exception as e:
        return _err("SESSION_NOT_FOUND", f"会话不存在: {e}", status=404)
    st = getattr(sess.state, "value", sess.state)
    if st not in (4, 5):
        return _err("NOT_RUNNING", f"会话未运行（state={st}）——节点实例化后预检才有意义", status=409)
    wait = max(0, min(int(wait), 60))

    drill_id = platform_db.ensure_drill(session_id)
    validate_map = _service_validate_map()
    ports_map = _manifest_ports_map()

    # L1: 容器状态（DockerNode 容器名=节点名）
    out, _c = _docker("ps --format '{{.Names}}\\t{{.Status}}'", 30)
    up_map = {}
    for ln in out.splitlines():
        p = ln.split("\t")
        if len(p) >= 2:
            up_map[p[0]] = p[1]

    nodes = sess.nodes.values() if isinstance(sess.nodes, dict) else sess.nodes
    report_nodes = []
    ready = True

    def _grace(fn, *args):
        """失败时在 wait 窗口内重试（刚实例化的服务可能尚未完成端口绑定）"""
        deadline = _t.time() + wait
        attempts = 0
        while True:
            ok, item = fn(*args)
            attempts += 1
            if ok or _t.time() >= deadline:
                return ok, item, attempts
            _t.sleep(1.5)

    for n in nodes:
        nt = getattr(n.type, "value", n.type)
        entry = {"node_id": n.id, "name": n.name,
                 "runtime": "docker" if nt == 15 else "netns",
                 "container": None, "services": [], "ports": [], "notes": []}
        if nt == 15:
            status = up_map.get(n.name)
            entry["container"] = status
            if not status:
                entry["notes"].append("容器未运行")
                ready = False
        else:
            entry["notes"].append("netns 节点（无容器实体, 仅校验服务）")

        # L2: 声明服务的 validate 命令（宽限重试）
        for svc in sorted(n.services or []):
            vcmds = validate_map.get(svc)
            if vcmds is None:
                entry["services"].append({"name": svc, "validate": None, "result": "unknown",
                                          "detail": "服务定义未注册（bridge 侧 ServiceManager 加载不到）"})
                ready = False
                continue
            if not vcmds:
                entry["services"].append({"name": svc, "validate": [], "result": "no-validate",
                                          "detail": "服务定义无校验命令, 跳过"})
                continue
            results = []
            ok_all = True
            for vcmd in vcmds:
                def run_validate(v=vcmd):
                    try:
                        # shell=True: 目录模型生成的 validate 含 shell 语法（cd / && sh ...），裸 exec 会 127
                        rc, output = b.node_command(session_id, n.id, v, True, True)
                    except Exception as e:
                        return False, {"cmd": v, "result": "error", "detail": str(e)[:120]}
                    return rc == 0, {"cmd": v, "result": "ok" if rc == 0 else "missing",
                                     "detail": (output or "")[:120]}
                ok, item, _att = _grace(run_validate)
                results.append(item)
                if not ok:
                    ok_all = False
            entry["services"].append({"name": svc, "validate": vcmds,
                                      "result": "ok" if ok_all else "missing",
                                      "detail": results})
            if not ok_all:
                ready = False

        # L3: 编目端口探测（镜像有 manifest 才有；宽限重试）
        img = getattr(n, "image", None) or ""
        import topologies, repository
        binding=topologies.bindings(session_id).get(str(n.id),{})
        target=repository.revision('target',binding.get('target_name',''),binding.get('target_version')) if binding.get('target_name') else None
        manifest=(target or {}).get('manifest') or _manifest_index().get(img) or {}
        enabled=set(n.services or [])
        if target:
            import versions
            mapping,_=versions.materialize(binding['target_name'],target)
            enabled={original for original,runtime in mapping.items() if runtime in enabled} | enabled
        # 目录模型下启用名是 imgsvc-* 执行名，清单条目名是 imgmode-*——按后缀对齐再匹配
        ports=sorted({str(port) for svc in manifest.get('services',[])
                      if svc['name'] in enabled or any(e.endswith(svc['name']) for e in enabled)
                      for port in svc.get('ports',[])})
        entry['declaration']={'result':'consistent' if manifest or nt != 15 else 'unknown','enabled':sorted(enabled)}
        entry['network']={'result':'not-tested','detail':'服务预检不代表拓扑联通，请主动执行联通检查'}
        if not ports and (getattr(n, "image", None) or ""):
            entry["notes"].append("镜像未编目（无端口声明）: 仅完成存活/服务检查")
        for port in ports:
            probe = f'bash -c "echo > /dev/tcp/127.0.0.1/{port}"'

            def run_port(pp=port, pr=probe):
                try:
                    rc, output = b.node_command(session_id, n.id, pr)
                except Exception as e:
                    return False, {"port": pp, "result": "error", "detail": str(e)[:120]}
                return rc == 0, {"port": pp, "result": "open" if rc == 0 else "closed",
                                 "detail": (output or "")[:80]}
            ok, item, _att = _grace(run_port)
            entry["ports"].append(item)
            if not ok:
                ready = False
        report_nodes.append(entry)

    platform_db.write_audit("precheck", session_id=session_id, drill_id=drill_id,
                            verdict="READY" if ready else "NOT_READY")
    return JSONResponse({"ok": True, "data": {
        "drill_id": drill_id, "session_id": session_id,
        "verdict": "READY" if ready else "NOT_READY",
        "nodes": report_nodes}})


# ═══════════════════════════════════════════════
# 导入事务模型（P-导入1）：流式上传 + 草稿读取 + 重写注入 + 重名拦截
# 流程: 导入(镜像立即 load + 草稿载入弹窗) → 详情编辑 → 确定注册(写 custom_services/清单)
# ═══════════════════════════════════════════════

def _sanitize_rel(rel: str) -> str:
    """包内相对路径消毒: 逐段 basename; 含 .. / 空段 → 直接拒绝（不静默改写）"""
    parts = []
    for seg in rel.strip().strip("/").split("/"):
        seg = os.path.basename(seg.strip())
        if not seg or seg in (".", "..") or ".." in seg:
            raise ValueError(f"路径段非法: {seg!r}")
        parts.append(seg)
    if not parts:
        raise ValueError("rel_path 为空")
    rel2 = "/".join(parts)
    base = os.path.realpath(IMAGE_IMPORT_DIR)
    full = os.path.realpath(os.path.join(base, rel2))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("路径逃逸")
    return rel2


@router.post("/targets/import/upload")
async def targets_import_upload(request: Request, rel_path: str = ""):
    """页面直传（raw body 流式, 零新依赖）。query rel_path = 包内相对路径
    （如 web-tiny-bundle/image.tar 或 web-tiny-bundle/services/service.py）。
    落位 IMAGE_IMPORT_DIR/<rel_path>；上限 20GB；前端 fetch(url, {method:'POST', body: file})"""
    try:
        rel = _sanitize_rel(rel_path)
    except ValueError as e:
        return _err("BAD_PATH", str(e))
    upload_id=repository.identifier('upload')
    upload_root=os.path.join(os.path.realpath(IMAGE_IMPORT_DIR),upload_id)
    target = os.path.join(upload_root, rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    size = 0
    limit = 20 * 1024**3
    try:
        with open(target, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    f.close()
                    os.remove(target)
                    return _err("TOO_LARGE", f"上传超过 {limit // 2**30}GB 上限，已中止", status=413)
                await asyncio.to_thread(f.write,chunk)
    except Exception as e:
        try:
            os.remove(target)
        except OSError:
            pass
        return _err("UPLOAD_FAILED", str(e)[:200], status=500)
    repository.put('uploads',upload_id,{'path':target,'root':upload_root,'created_at':repository.now()})
    _audit("upload", rel_path=rel, size=size, ok=True)
    return JSONResponse({"ok": True, "data": {"path": target, "rel_path": rel, "size": size}})


def _read_bundle(directory):
    sources = {p.name:p.read_text() for p in sorted((directory/'services').glob('*.py')) if p.name != '__init__.py'}
    templates = {p.name:p.read_text() for p in sorted((directory/'services'/'templates').glob('*')) if p.is_file()}
    man_path = directory/'manifest.json'
    result={'package':directory.name, 'python_files':sources, 'py':'', 'templates':templates,
            'manifest':json.loads(man_path.read_text()) if man_path.exists() else None}
    catalog_path=directory/'catalog.json'
    if catalog_path.exists():
        import image_services
        catalog=json.loads(catalog_path.read_text())
        for service in catalog.get('services',[]):
            for mode in service.get('modes',[]):
                for item in mode.get('files',[]):
                    stored=image_services.store_attachment((directory/'scripts'/item['sha256']).read_bytes(),item['name'],item['role'])
                    if stored['sha256']!=item['sha256']: raise ValueError('导入脚本摘要不一致')
        result['catalog']=catalog
    return result


@router.get('/targets/import/bundle/draft')
def bundle_draft(dir: str = ''):
    try:
        directory = Path(dir)
        if not dir or not directory.is_dir():
            raise ValueError('包目录不存在')
        draft = _read_bundle(directory)
        return JSONResponse({'ok':True, 'data':{'package':directory.name, 'dir':str(directory), 'draft':draft,
          'inventory':{'py':list(draft['python_files']), 'templates':list(draft['templates']),
                       'has_image_tar':any((directory/n).is_file() for n in ('image.tar', 'image.tar.gz'))}}})
    except (ValueError, OSError) as e:
        return _err('DRAFT_INVALID', str(e))


@router.post('/targets/import/bundle/confirm')
def bundle_confirm(body: dict):
    return targets_import_bundle(body)


# ═══ 服务名前缀重写 + 跨包重名拦截（B1+B2 定稿）═══

def _sanitize_manifest_prefix(name: str) -> str:
    """清单名 → 服务名前缀片段（小写化, 非法字符转 -）"""
    import re as _re
    return _re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-") or "pkg"


def _prefixed_service_name(name: str, manifest_name: str) -> str:
    """name → svc-<清单名>-<原名去svc->；已带本清单前缀则原样（重复导入幂等）"""
    mn = _sanitize_manifest_prefix(manifest_name)
    if name.startswith(f"svc-{mn}-"):
        return name
    base = name[4:] if name.startswith("svc-") else name
    return f"svc-{mn}-{base}"


def _rewrite_py_names(py_text: str, manifest_name: str) -> str:
    return delivery.rewrite(py_text, {n:_prefixed_service_name(n,manifest_name) for n in delivery.names(py_text)})


def _rewrite_manifest_names(manifest: dict, manifest_name: str) -> dict:
    # CORE builtins retain their registered names. Custom names alone receive a prefix.
    for svc in manifest.get('services') or []:
        if svc.get('mode') or svc.get('name','').startswith('svc-'):
            svc['name'] = _prefixed_service_name(svc['name'], manifest_name)
    return manifest


def _svc_name_conflicts(package: str, names: list) -> list:
    """服务名冲突（重名时 CORE 先扫描者静默胜出——必须在导入时拦截, 实测 2026-11-15）：
    跨包同名（owner≠package）→ 归属混乱；同包内重复定义（owner=package）→ 打包错误。都拒绝"""
    seen_in_submission = set()
    conflicts = []
    for name in names:
        if name in seen_in_submission:
            conflicts.append({"service": name, "owner_package": package, "reason": "本次提交内重复定义"})
        seen_in_submission.add(name)
        owner = _service_name_owner(name)
        if owner and owner != package:
            conflicts.append({"service": name, "owner_package": owner, "reason": "已被其它包定义"})
    return conflicts


def _extract_service_names(py_text: str) -> list:
    return delivery.names(py_text)


def _service_name_owner(name: str) -> str | None:
    for path in Path(CUSTOM_SERVICES_DIR).glob('**/*.py'):
        try:
            if name in delivery.names(path.read_text()):
                return path.relative_to(CUSTOM_SERVICES_DIR).parts[0]
        except (SyntaxError, OSError):
            continue
    return None


def unpack_uploaded_delivery(upload_id, cancelled):
    import shutil
    import tempfile
    from pathlib import PurePosixPath

    upload = repository.read('uploads', upload_id)
    if not upload or not upload.get('archive_path'):
        raise ValueError('上传的交付包不存在，请重新上传')
    root = Path(upload['root'])
    archive = Path(upload['archive_path'])
    destination = root / 'content'
    if destination.is_dir():
        return destination
    staging = Path(tempfile.mkdtemp(prefix='.unpack-', dir=root))
    try:
        with tarfile.open(archive, 'r|*') as package:
            for member in package:
                path = PurePosixPath(member.name)
                if path.is_absolute() or '..' in path.parts or not (member.isdir() or member.isfile()):
                    raise ValueError('交付包仅支持普通文件和包内目录：' + member.name)
                target = staging.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.extractfile(member) as source, target.open('xb') as output:
                    while chunk := source.read(4 * 1024 * 1024):
                        if cancelled():
                            raise InterruptedError('交付包解包已中断')
                        output.write(chunk)
        entries = list(staging.iterdir())
        if not entries:
            raise ValueError('交付包为空')
        extracted = entries[0] if len(entries) == 1 and entries[0].is_dir() else staging
        try:
            extracted.rename(destination)
        except OSError:
            if not destination.is_dir():
                raise
        archive.unlink(missing_ok=True)
        return destination
    except FileNotFoundError:
        if destination.is_dir():
            return destination
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


@router.post("/targets/import/bundle/upload")
async def bundle_upload(request: Request, name: str = "", defer_extract: bool = False):
    """整体导入-本机上传：上传整包压缩包（.tar.gz，内含 image.tar/services/manifest.json），
    服务端解包落位 IMAGE_IMPORT_DIR/<包名>/ 并返回清单草稿。确认注册走 /targets/import/bundle/confirm"""
    import re as _re
    pkg = _re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "").strip()).strip("-")
    if not pkg:
        return _err("BAD_NAME", "包名非法（仅允许字母数字-_）")
    target_dir = os.path.join(os.path.realpath(IMAGE_IMPORT_DIR), pkg+'-'+__import__('uuid').uuid4().hex[:12])
    os.makedirs(target_dir, exist_ok=True)
    archive = os.path.join(target_dir, "bundle.tar.gz")
    size = 0
    limit = 20 * 1024**3
    try:
        with open(archive, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    f.close()
                    os.remove(archive)
                    return _err("TOO_LARGE", f"超过 {limit // 2**30}GB 上限", status=413)
                await asyncio.to_thread(f.write,chunk)
    except Exception as e:
        return _err("UPLOAD_FAILED", str(e)[:200], status=500)
    if defer_extract:
        upload_id = repository.identifier('upload')
        repository.put('uploads', upload_id, {
            'path': target_dir, 'root': target_dir, 'archive_path': archive, 'created_at': repository.now(),
        })
        _audit('bundle.upload', package=pkg, size=size, ok=True)
        return JSONResponse({'ok': True, 'data': {'package': pkg, 'upload_id': upload_id, 'size': size}})
    # 解包（--no-same-owner; GNU tar 拒绝绝对路径成员; 解包后校验无逃逸）
    rc = await asyncio.to_thread(subprocess.run, f"tar -xzf '{archive}' -C '{target_dir}' --no-same-owner 2>&1",
                        shell=True, capture_output=True, text=True, timeout=120)
    if rc.returncode != 0:
        os.remove(archive)
        return _err("EXTRACT_FAILED", (rc.stdout + rc.stderr)[:300], status=400)
    # 归一化: 若解包出唯一顶层目录则上移一层
    entries = [e for e in os.listdir(target_dir) if e != "bundle.tar.gz"]
    if len(entries) == 1 and os.path.isdir(os.path.join(target_dir, entries[0])):
        inner = os.path.join(target_dir, entries[0])
        for e in os.listdir(inner):
            os.rename(os.path.join(inner, e), os.path.join(target_dir, e))
        os.rmdir(inner)
    # 逃逸校验
    for root, _dirs, files in os.walk(target_dir):
        for fn in files:
            fp = os.path.realpath(os.path.join(root, fn))
            if not fp.startswith(os.path.realpath(target_dir)):
                return _err("EXTRACT_UNSAFE", "压缩包含越界路径，已中止", status=400)
    os.remove(archive)
    inv = {
        "has_image_tar": os.path.exists(os.path.join(target_dir, "image.tar")),
        "py": sorted(f for f in os.listdir(os.path.join(target_dir, "services"))
                     if f.endswith(".py")) if os.path.isdir(os.path.join(target_dir, "services")) else [],
        "has_manifest": os.path.exists(os.path.join(target_dir, "manifest.json")),
    }
    repository.put('uploads',repository.identifier('upload'),{'path':target_dir,'root':target_dir,'created_at':repository.now()})
    _audit("bundle.upload", package=pkg, size=size, ok=True)
    return JSONResponse({"ok": True, "data": {"package": pkg, "dir": target_dir, "inventory": inv}})


@router.get('/versions/{kind}/{name}')
def version_history(kind:str,name:str):
    if kind not in ('target','template'): return _err('KIND_INVALID','仅支持 target/template')
    if kind=='template' and not repository.revision(kind,name):
        raw=_load_store(INSTANCE_STORE).get(name)
        if raw:
            try: versions.template(name,raw)
            except ValueError as e: return _err('TEMPLATE_INVALID',str(e))
    if kind=='target' and not repository.revision(kind,name):
        raw=_load_manifests().get(name)
        if raw:
            try: versions.target(name,delivery.manifest(raw))
            except ValueError as e: return _err('MANIFEST_INVALID',str(e))
    history=repository.history(kind,name)
    if kind=='target':
        history=[{**v,'activation_state':('applied' if repository.read('image_catalog_activations',name+':'+str(v['version'])) else 'pending') if v.get('catalog') else 'legacy'} for v in history]
    return {'ok':True,'data':history}


@router.get('/storage/migrations')
def migration_status():
    for path in (MANIFEST_STORE,INSTANCE_STORE,TOPOLOGY_STORE): _load_store(path)
    with repository.connection() as c:
        rows=[dict(r) for r in c.execute('SELECT * FROM migrations')]
        for row in rows:
            report=c.execute('SELECT report FROM migration_reports WHERE collection=?',(row['source'],)).fetchone()
            row['validation']=json.loads(report[0]) if report else {'note':'旧迁移标记，原始记录仍保留'}
    return {'ok':True,'data':rows}


@router.get('/targets/registry')
def target_registry():
    out=[]
    for name,m in _load_manifests().items():
        try:
            published=repository.revision('target',name,m.get('target_revision')) or versions.target(name,delivery.manifest(m))
            out.append({'name':name,'version':published['version'],'entity_id':published['entity_id'],'image_ref':published['image_ref'],'image_digest':published.get('image_digest')})
        except ValueError as e:
            image=m.get('image') or {}
            ref=(image.get('name','')+':'+image.get('tag','latest')) if isinstance(image,dict) else image
            out.append({'name':name,'image_ref':ref,'publication_state':'draft','migration_error':str(e)})
    return {'ok':True,'data':out}


@router.post('/targets/import/discard')
def discard_uploads(body:dict):
    import shutil
    removed=[]
    for requested in body.get('paths',[]):
        record=next(((key,value) for key,value in repository.read('uploads').items() if value['path']==requested),None)
        if record is None: continue  # User-provided server directories are never deleted.
        for job in repository.read('jobs').values():
            job_input = job['input']
            if job['status'] in ('queued','running') and (
                requested in (job_input.get('dir'), job_input.get('source_dir'), job_input.get('image_path')) or
                job_input.get('bundle_upload_id') == record[0]
            ):
                return _err('UPLOAD_IN_USE','上传文件正在被后台任务使用',status=409)
        key,value=record
        shutil.rmtree(value['root'],ignore_errors=False)
        repository.remove('uploads',key);removed.append(requested)
    return {'ok':True,'data':{'removed':removed}}
