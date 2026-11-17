"""Persistent topology drafts, deployments, and authoritative template bindings."""
import copy
import threading
from fastapi import APIRouter, HTTPException
import repository as repo
import versions
import platform_db

router=APIRouter()
mutation_lock=threading.RLock()


@router.get('/topology-workspace')
def workspace():
    """Join saved configurations and live execution identities without rewriting history."""
    import app
    import core_adapter
    from core.services.manager import ServiceManager
    saved=list(repo.read('topologies').values())
    history=copy.deepcopy(list(repo.read('deployments').values()))
    sessions=[]
    error=None
    try:
        summaries=app.bridge.get_sessions()
        tokens=core_adapter.identities(app.bridge) if summaries else {}
        with repo.connection() as connection:
            active={row['core_sid']:dict(row) for row in connection.execute(
                "SELECT core_sid,drill_id,identity FROM drills WHERE status='active'")}
        for summary in summaries:
            session=app._session_summary_to_dict(summary)
            token=tokens.get(str(summary.id))
            owner=active.get(summary.id)
            session.update(identity=token,drill_id=owner['drill_id'] if owner and token and owner['identity']==token else None)
            sessions.append(session)
    except Exception:
        # Saved definitions/history remain useful when CORE is unavailable.
        sessions=[]
        error='无法读取 CORE 实时状态；已保存的拓扑和历史记录仍可查看。'
    by_sid={s['id']:s for s in sessions}
    labels={1:'未启动',2:'配置中',3:'启动中',4:'运行中',5:'停止中',6:'已关闭'}
    for run in history:
        run['saved_topology_id']=run.get('topology_id') or run.get('captured_topology_id')
        live=by_sid.get(run.get('core_sid'))
        run['live']=bool(live and live.get('drill_id') and live['drill_id']==run.get('drill_id')
                         and run.get('status') not in ('superseded','failed','closed','interrupted'))
        run['current_state']=live['state'] if run['live'] else None
        run['current_status']=labels.get(live['state'],'未知') if run['live'] else ('状态待确认' if error else '历史记录')
        if run['live'] and live['state']==1 and run.get('status')=='stopped': run['current_status']='已停止'
    return {'ok':True,'data':{'topologies':saved,'deployments':history,'sessions':sessions,
                            'runtime_error':error,'native_models':ServiceManager().defaults,'updated_at':repo.now()}}


def _checked_session(sid,identity):
    import app
    import core_adapter
    if not identity or core_adapter.identities(app.bridge).get(str(sid))!=identity:
        raise HTTPException(409,'该会话已结束或被替换，请刷新拓扑列表。')
    return app.bridge.get_session(sid)


@router.get('/topology-workspace/sessions/{sid}')
def workspace_session(sid:int,identity:str):
    _checked_session(sid,identity)
    return {'ok':True,'data':snapshot_session(sid)}


@router.post('/topology-workspace/sessions/{sid}/{action}')
def workspace_action(sid:int,action:str,body:dict):
    import app
    with mutation_lock:
        session=_checked_session(sid,body.get('identity'))
        if action=='stop':
            if app._enum_val(session.state)!=4:
                raise HTTPException(409,'会话当前不在运行中，请刷新状态。')
            result=app.stop_session(sid)
        elif action=='delete':
            result=app.delete_session(sid)
        elif action=='connectivity':
            return connectivity(sid,body)
        elif action=='capture':
            try:
                with repo.transaction():
                    record=save({'definition':body.get('definition') or snapshot_session(sid)})
                    run=for_session(sid)
                    run['captured_topology_id']=record['topology_id']
                    repo.put('deployments',run['deployment_id'],run)
                return {'ok':True,'data':record}
            except ValueError as exc:
                raise HTTPException(400,str(exc)) from exc
        else:
            raise HTTPException(404,'未知拓扑操作')
        if not isinstance(result,dict) or not result.get('result'):
            raise HTTPException(502,'CORE 未完成操作，请刷新状态后查看。')
        return {'ok':True,'data':result}


def validate(definition, pin=True):
    import app
    import ipaddress
    if definition.get('format')=='core-xml':
        import xml.etree.ElementTree as ET
        try: root=ET.fromstring(definition['xml_text'])
        except (ET.ParseError,KeyError,TypeError) as e: raise ValueError('原生 XML 内容无效: '+str(e)) from e
        if root.tag!='scenario': raise ValueError('原生 XML 根节点必须是 scenario')
        return copy.deepcopy(definition)
    topo=app.TopologyInput.model_validate(definition)
    ids=[n.id for n in topo.nodes]+[w.id for w in topo.wlans]
    if len(ids)!=len(set(ids)): raise ValueError('节点 ID 重复')
    for node in topo.nodes:
        if node.type.lower() not in app._NODE_TYPE_MAP: raise ValueError('未知节点类型: '+node.type)
        if node.instance:
            resolved,_=versions.resolve(node.instance,node.instance_version,node.service_overrides)
            if node.image and node.image not in (resolved.get('image'),resolved.get('image_ref')):
                raise ValueError('节点镜像与模板版本不一致: '+str(node.id))
            if node.services is not None:
                template=repo.revision('template',node.instance,resolved['instance_version'])
                canonical={s['name'] for s in template.get('services',[])}
                if set(node.services) not in (set(resolved['services']),canonical):
                    raise ValueError('节点服务与模板版本不一致: '+str(node.id))
            if pin: node.instance_version=resolved['instance_version']
    for link in topo.links:
        if link.node1_id not in ids or link.node2_id not in ids or link.node1_id==link.node2_id:
            raise ValueError('链路端点不存在或相同')
        for key in ('iface1','iface2'):
            iface=getattr(link,key)
            if not iface: continue
            for family,maximum in (('ip4',32),('ip6',128)):
                address=getattr(iface,family)
                mask=getattr(iface,family+'_mask')
                if mask is not None and not 0<=mask<=maximum:
                    raise ValueError(f'{key} 的 {family} 掩码位须为 0–{maximum}')
                if address:
                    try:
                        parsed=ipaddress.ip_address(address)
                        if parsed.version!=(4 if family=='ip4' else 6): raise ValueError()
                    except ValueError as exc:
                        raise ValueError(f'{key} 的 {family} 地址无效: {address}') from exc
            if iface.id is not None and iface.id<0: raise ValueError('接口 ID 不能为负数')
            if iface.mtu is not None and not 68<=iface.mtu<=65535: raise ValueError('MTU 须为 68–65535')
        if link.options:
            for key in ('bandwidth','delay','jitter','buffer','burst'):
                value=getattr(link.options,key)
                if value is not None and value<0: raise ValueError(f'链路参数 {key} 不能为负数')
            for key in ('loss','dup'):
                value=getattr(link.options,key)
                if value is not None and not 0<=value<=100: raise ValueError(f'链路参数 {key} 须为 0–100')
    return topo.model_dump(exclude_none=True)


def save(body, topology_id=None):
    definition=validate(body.get('definition') or body)
    topology_id=topology_id or repo.identifier('topo')
    existing=repo.read('topologies',topology_id)
    if existing and body.get('revision') != existing['revision']:
        raise HTTPException(409,'草稿已被其他操作修改，请重新打开')
    record={'topology_id':topology_id,'revision':(existing or {}).get('revision',0)+1,'name':definition.get('name','topology'),'definition':definition,'updated_at':repo.now()}
    with repo.transaction():
        repo.put('topology_revisions',topology_id+':'+str(record['revision']),record)
        return repo.put('topologies',topology_id,record)


@router.get('/topologies')
def listing():
    return {'ok':True,'data':list(repo.read('topologies').values())}


@router.post('/topologies',status_code=201)
def create(body:dict):
    with mutation_lock:
        try: return {'ok':True,'data':save(body)}
        except ValueError as e: raise HTTPException(400,str(e))


@router.get('/topologies/{topology_id}')
def get(topology_id:str):
    d=repo.read('topologies',topology_id)
    if not d: raise HTTPException(404,'草稿不存在')
    return {'ok':True,'data':d}


@router.put('/topologies/{topology_id}')
def update(topology_id:str,body:dict):
    with mutation_lock:
        try: return {'ok':True,'data':save(body,topology_id)}
        except ValueError as e: raise HTTPException(400,str(e))


@router.delete('/topologies/{topology_id}')
def delete(topology_id:str):
    repo.remove('topologies',topology_id)
    return {'ok':True}


def for_session(sid):
    drill=platform_db.ensure_drill(sid)
    for d in repo.read('deployments').values():
        if d.get('drill_id')==drill and d.get('status') in ('defined','running','stopped'):
            return d
    d={'deployment_id':repo.identifier('deploy'),'core_sid':sid,'drill_id':drill,'status':'defined','created_at':repo.now(),'bindings':{},'snapshot':None}
    return repo.put('deployments',d['deployment_id'],d)


def bindings(sid):
    drill=platform_db.get_active_drill(sid)
    workspace=repo.read('session_workspaces',str(sid))
    if workspace and workspace.get('drill_id')==drill: return workspace['bindings']
    return next((d.get('bindings',{}) for d in repo.read('deployments').values() if d.get('drill_id')==drill),{})


def _workspace(sid):
    current=for_session(sid)
    existing=repo.read('session_workspaces',str(sid))
    if existing and existing.get('drill_id')==current['drill_id']: return existing
    return {'core_sid':sid,'drill_id':current['drill_id'],'deployment_id':current['deployment_id'],'bindings':copy.deepcopy(current.get('bindings',{}))}


def bind(sid,nid,data):
    with repo.transaction():
        workspace=_workspace(sid);workspace['bindings'][str(nid)]=data
        repo.put('session_workspaces',str(sid),workspace)


def unbind(sid,nid):
    with repo.transaction():
        workspace=_workspace(sid);workspace['bindings'].pop(str(nid),None)
        repo.put('session_workspaces',str(sid),workspace)


def snapshot_session(sid):
    import app
    session=app.bridge.get_session(sid)
    bound=bindings(sid)
    nodes=[]
    for n in session.nodes.values():
        b=bound.get(str(n.id),{})
        raw=app._node_to_dict(n)
        raw.update(b)
        raw.update(type=next((k for k,v in app._NODE_TYPE_MAP.items() if v==app._enum_val(n.type)),'default'),x=n.position.x,y=n.position.y)
        raw['name']=b.get('display_name') or n.name
        nodes.append(raw)
    links=[app._link_to_dict(l) for l in session.links or []]
    return {'schema_version':2,'name':f'session-{sid}','nodes':nodes,'links':links,'options':{k:getattr(v,'value',str(v)) for k,v in (getattr(session,'options',{}) or {}).items()}}


def state(sid,status):
    drill=platform_db.get_active_drill(sid)
    for d in repo.read('deployments').values():
        if d.get('drill_id')==drill:
            d['status']=status
            if status=='running' and not d.get('has_run'):
                execution=snapshot_session(sid)
                d.update(has_run=True,source=execution,snapshot=execution,execution_definition=execution,bindings=copy.deepcopy(bindings(sid)))
                if d.get('native_xml'): d['native_xml']=__import__('native_xml').export(__import__('app').bridge,sid)
            repo.put('deployments',d['deployment_id'],d)


def _signature(definition):
    import delivery_state
    nodes=[]
    for node in definition.get('nodes',[]):
        nodes.append({k:node.get(k) for k in ('id','name','type','model','image','instance','instance_version','target_name','target_version','service_overrides','service_selection','effective_services')})
        nodes[-1]['services']=sorted(node.get('services') or [])
    links=copy.deepcopy(definition.get('links') or [])
    for link in links:
        for key in ('iface1','iface2'):
            if link.get(key): link[key].pop('name',None)
    return delivery_state.digest({'nodes':sorted(nodes,key=lambda n:n['id']),'links':sorted(links,key=lambda l:(l['node1_id'],l['node2_id'])),'options':definition.get('options') or {}})


def prepare_start(sid):
    """Freeze first execution; changed definitions become a new logical deployment."""
    import platform_db,native_xml
    with mutation_lock:
        current=for_session(sid)
        # Older deployments predate has_run; preserve their recorded execution.
        current.setdefault('has_run',current.get('status') in ('running','stopped'))
        definition=snapshot_session(sid)
        bound=copy.deepcopy(bindings(sid))
        native=native_xml.export(__import__('app').bridge,sid) if current.get('native_xml') else None
        changed=current.get('has_run',False) and _signature(current.get('execution_definition') or current.get('snapshot') or {})!=_signature(definition)
        if current.get('has_run') and not changed: return current
        with repo.transaction() as c:
            if changed:
                row=c.execute('SELECT identity FROM drills WHERE drill_id=?',(current['drill_id'],)).fetchone()
                old=current
                old['status']='superseded';repo.put('deployments',old['deployment_id'],old)
                platform_db.unsubscribe_events(sid)
                platform_db.close_drill(sid,'superseded');platform_db.release_deploy_refs(core_sid=sid)
                drill=platform_db.issue_drill(sid,identity=row['identity'])
                current={'deployment_id':repo.identifier('deploy'),'parent_deployment_id':old['deployment_id'],'core_sid':sid,'drill_id':drill,'status':'defined','created_at':repo.now(),'bindings':bound,'has_run':False}
                for node in definition['nodes']:
                    if node['type']=='docker':
                        runtime=f'chb-{current["deployment_id"][-12:]}-{node["id"]}'
                        bound.setdefault(str(node['id']),{})['runtime_name']=runtime
                        node['runtime_name']=runtime
            current.update(source=definition,snapshot=definition,execution_definition=definition,bindings=bound)
            if native: current['native_xml']=native
            repo.put('deployments',current['deployment_id'],current)
            repo.put('session_workspaces',str(sid),{'core_sid':sid,'drill_id':current['drill_id'],'deployment_id':current['deployment_id'],'bindings':bound})
            for nid,binding in bound.items():
                if binding.get('instance'): platform_db.register_deploy_ref(current['drill_id'],sid,int(nid),binding['instance'],binding['instance_version'])
        if changed: platform_db.subscribe_events(sid)
        return current


@router.get('/api/sessions/{sid}/topology')
def export(sid:int):
    return snapshot_session(sid)


@router.post('/api/sessions/{sid}/draft',status_code=201)
def capture(sid:int):
    return {'ok':True,'data':save(snapshot_session(sid))}


def deploy_definition(definition,topology_id=None,revision=None,start=False):
    import app
    from core_adapter import activate
    normalized=validate(definition)
    if normalized.get('format')=='core-xml':
        import native_xml
        response=native_xml.load(app.bridge,normalized['xml_text'],start)
        if not response['result']: raise ValueError('原生 XML 草稿部署失败')
        record=repo.read('deployments',response['deployment_id'])
        record.update(topology_id=topology_id,revision=revision)
        repo.put('deployments',record['deployment_id'],record)
        return record
    resolved=copy.deepcopy(normalized)
    deployment_id=repo.identifier('deploy')
    directories=set();required=set();bound={}
    for node in resolved['nodes']:
        display=node['name']
        if node.get('instance'):
            fields,dirs=versions.resolve(node['instance'],node.get('instance_version'),node.get('service_overrides'))
            node.update(fields);directories.update(dirs)
        if node['type']=='docker': node['name']=f'chb-{deployment_id[-12:]}-{node["id"]}'
        elif node['type'].lower() in ('default','router','host','pc'):
            # vnoded builds Unix socket/command paths from this value. Keep the user label in bindings.
            node['name']=f'n{node["id"]}'
        bound[str(node['id'])]={k:node[k] for k in ('instance','instance_version','target_name','target_version','image_ref','service_overrides','service_selection','effective_services') if k in node}
        bound[str(node['id'])].update(display_name=display,runtime_name=node['name'])
        required.update(node.get('services') or [])
    import target_admin,shlex
    for node in resolved['nodes']:
        if node.get('image'):
            _,code=target_admin._docker('image inspect '+shlex.quote(node['image']),30)
            if code: raise ValueError('固定的镜像已不在本机，请重新导入: '+node['image'])
    if directories:
        activation=activate(app.bridge,sorted(directories),sorted(required))
        if activation['missing']: raise ValueError('daemon 服务未注册: '+', '.join(activation['missing']))
    session=app.bridge.create_session()
    drill=platform_db.ensure_drill(session.id)
    platform_db.subscribe_events(session.id)
    record={'deployment_id':deployment_id,'topology_id':topology_id,'revision':revision,'snapshot':resolved,'source':normalized,'bindings':bound,'drill_id':drill,'core_sid':session.id,'status':'defined','created_at':repo.now()}
    repo.put('deployments',deployment_id,record)
    try:
        app._build_topology(session.id,app.TopologyInput.model_validate(resolved))
        if start:
            result=app.start_session(session.id,app.StartSessionRequest())
            if not result.get('result'):
                record['diagnostics']=[str(item) for item in result.get('exceptions') or []]
                if any('name too long' in item for item in record['diagnostics']):
                    raise ValueError('原生节点启动失败：CORE 运行目录过长，请缩短运行目录。')
                raise ValueError('节点启动失败，请展开本次运行记录中的启动诊断。')
        return repo.read('deployments',deployment_id)
    except Exception as exc:
        try:
            if app.bridge.delete_session(session.id):
                platform_db.close_drill(session.id,'interrupted');platform_db.release_deploy_refs(core_sid=session.id)
                platform_db.unsubscribe_events(session.id)
        finally:
            record.update(status='failed',error=str(exc));repo.put('deployments',deployment_id,record)
        raise


@router.post('/topologies/{topology_id}/deploy',status_code=201)
def deploy(topology_id:str,body:dict):
    record=get(topology_id)['data']
    if body.get('revision')!=record['revision']: raise HTTPException(409,'请重新打开并确认当前草稿版本')
    try:
        with mutation_lock:
            return {'ok':True,'data':deploy_definition(record['definition'],topology_id,record['revision'],body.get('start',False))}
    except ValueError as e: raise HTTPException(400,str(e))


@router.get('/deployments')
def deployments():
    return {'ok':True,'data':list(repo.read('deployments').values())}


@router.post('/deployments/{deployment_id}/redeploy',status_code=201)
def redeploy(deployment_id:str):
    d=repo.read('deployments',deployment_id)
    if not d: raise HTTPException(404,'部署不存在')
    if d.get('native_xml'):
        import native_xml,app
        response=native_xml.load(app.bridge,d['native_xml'])
        if not response['result']: raise HTTPException(400,'XML 快照重新部署失败')
        return {'ok':True,'data':repo.read('deployments',response['deployment_id'])}
    return {'ok':True,'data':deploy_definition(d.get('source') or d['snapshot'],d.get('topology_id'),d.get('revision'))}


@router.post('/deployments/{deployment_id}/draft',status_code=201)
def draft_from_deployment(deployment_id:str):
    with mutation_lock:
        original=repo.read('deployments',deployment_id)
        if not original: raise HTTPException(404,'部署不存在')
        if original.get('native_xml'):
            definition={'schema_version':2,'format':'core-xml','name':'history-'+deployment_id,'xml_text':original['native_xml']}
        else: definition=copy.deepcopy(original.get('source') or original.get('snapshot'))
        if not definition: raise HTTPException(409,'该旧部署没有可恢复的定义，需从原始文件补录')
        return {'ok':True,'data':save(definition)}


@router.post('/topologies/{topology_id}/upgrade')
def upgrade(topology_id:str,body:dict):
    with mutation_lock:
        d=get(topology_id)['data']
        if body.get('revision')!=d['revision']: raise HTTPException(409,'草稿版本已变化')
        if d['definition'].get('format')=='core-xml': raise HTTPException(400,'原生 XML 请通过文件重新导入；原始扩展配置将完整保留')
        changes=[]
        for n in d['definition']['nodes']:
            if n.get('instance') and (not body.get('node_ids') or n['id'] in body['node_ids']):
                latest=repo.revision('template',n['instance'])
                if latest and latest['version']!=n.get('instance_version'):
                    changes.append({'node_id':n['id'],'from':n.get('instance_version'),'to':latest['version'],'before':repo.revision('template',n['instance'],n.get('instance_version')),'after':latest})
                    n['instance_version']=latest['version']
                    for field in ('services','model','type','image','image_ref','target_name','target_version','runtime_name','display_name'): n.pop(field,None)
        if not body.get('confirm'): return {'ok':True,'data':{'changes':changes}}
        return {'ok':True,'data':save({'definition':d['definition'],'revision':d['revision']},topology_id)}


@router.post('/api/sessions/{sid}/connectivity')
def connectivity(sid:int,body:dict):
    """User-triggered IPv4 reachability, distinct from declaration/service readiness."""
    import app
    import ipaddress
    session=app.bridge.get_session(sid)
    if app._enum_val(session.state) not in (4,5): raise HTTPException(409,'请先启动会话')
    probes=body.get('probes') or []
    if not probes:
        addresses={}
        for n in session.nodes.values():
            if app._enum_val(n.type) not in (0,15,18): continue
            _,ifaces,_=app.bridge.get_node(sid,n.id)
            addresses[n.id]=[f.ip4 for f in ifaces if f.ip4]
        probes=[{'source':source,'target':ips[0]} for source in addresses for target,ips in addresses.items() if source!=target and ips][:30]
    if len(probes)>30: raise HTTPException(400,'每次最多检查 30 项')
    results=[]
    for p in probes:
        try:
            source=int(p['source']);target=str(ipaddress.IPv4Address(p['target']))
            if source not in session.nodes: raise ValueError('源节点不存在')
        except (ValueError,KeyError) as e: raise HTTPException(400,str(e))
        rc,out=app.bridge.node_command(sid,source,f'ping -n -c 1 -W 2 {target}',True,True)
        transport='node'
        if rc==127 and app._enum_val(session.nodes[source].type)==15:
            # Minimal images may omit ping; use the host binary in the same network namespace.
            import subprocess
            import target_admin
            import shlex
            runtime=session.nodes[source].name
            pid,code=target_admin._docker('inspect --format {{.State.Pid}} '+shlex.quote(runtime),10)
            if code==0 and pid.strip().isdigit() and int(pid)>0:
                probe=subprocess.run(['nsenter','-t',pid.strip(),'-n','--','ping','-n','-c','1','-W','2',target],capture_output=True,text=True,timeout=5)
                rc,out=probe.returncode,probe.stdout+probe.stderr
                transport='host-binary-in-node-netns'
        result='reachable' if rc==0 else 'unsupported' if rc==127 or 'Operation not permitted' in out else 'unreachable'
        results.append({'source':source,'target':target,'result':result,'transport':transport,'detail':out[-1000:]})
    return {'ok':True,'data':{'results':results,'complete':all(r['result']!='unsupported' for r in results),'all_reachable':bool(results) and all(r['result']=='reachable' for r in results)}}


@router.post('/api/sessions/{sid}/nodes/{nid}/replace-instance')
def replace_instance(sid:int,nid:int,body:dict):
    import app
    with mutation_lock:
        session=app.bridge.get_session(sid)
        if app._enum_val(session.state)!=1: raise HTTPException(409,'只能在定义态替换节点模板；请先停止会话')
        old,_,links=app.bridge.get_node(sid,nid)
        if app._enum_val(old.type) not in (0,15,18): raise HTTPException(400,'网络构件不能直接替换为终端节点')
        try:
            resolved,dirs=versions.resolve(body['instance'],body.get('instance_version'))
            if dirs:
                from core_adapter import activate
                result=activate(app.bridge,dirs,resolved['services'])
                if result['missing']: raise ValueError('服务尚未注册: '+str(result['missing']))
        except (ValueError,KeyError) as e: raise HTTPException(400,str(e))
        old_binding=bindings(sid).get(str(nid),{})
        removed=[];created=False
        try:
            for link in links:
                if not app.bridge.delete_link(sid,link): raise RuntimeError('删除原链路失败')
                removed.append(link)
            if not app.bridge.delete_node(sid,nid): raise RuntimeError('删除原节点失败')
            created=True
            node=app.NodeModel(id=nid,name=old_binding.get('display_name') or old.name,position=app.PositionModel(x=old.position.x,y=old.position.y),icon=old.icon,instance=body['instance'],instance_version=resolved['instance_version'])
            app.add_node(sid,node)
            for link in links: app.bridge.add_link(sid,link)
            platform_db.release_deploy_ref_node(sid,nid)
            platform_db.register_deploy_ref(platform_db.ensure_drill(sid),sid,nid,body['instance'],resolved['instance_version'])
            return {'ok':True,'data':{'node_id':nid,'instance':body['instance'],'instance_version':resolved['instance_version']}}
        except Exception as error:
            try:
                if created:
                    for link in links:
                        try: app.bridge.delete_link(sid,link)
                        except Exception: pass
                    app.bridge.delete_node(sid,nid)
                    app.bridge.add_node(sid,old)
                    if old_binding: bind(sid,nid,old_binding)
                    platform_db.release_deploy_ref_node(sid,nid)
                    if old_binding.get('instance'): platform_db.register_deploy_ref(platform_db.ensure_drill(sid),sid,nid,old_binding['instance'],old_binding['instance_version'])
                for link in removed: app.bridge.add_link(sid,link)
            except Exception as rollback:
                raise HTTPException(500,f'替换失败: {error}；恢复失败: {rollback}，请保留部署快照排查')
            raise HTTPException(400,f'替换失败，已恢复原节点和链路: {error}')
