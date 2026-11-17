"""Native CORE XML round trip, with optional platform bindings in CORE metadata."""
import copy
import json
import xml.etree.ElementTree as ET
from fastapi import HTTPException
from settings import request_file
import repository as repo
import platform_db
import versions

KEY='http_bridge_bindings_v1'


def export(bridge,sid):
    import topologies
    with request_file('.xml') as path:
        bridge.save_xml(sid,str(path))
        root=ET.fromstring(path.read_text())
    bound=topologies.bindings(sid)
    if bound:
        metadata=root.find('session_metadata')
        if metadata is None: metadata=ET.SubElement(root,'session_metadata')
        for item in list(metadata):
            if item.get('name')==KEY: metadata.remove(item)
        ET.SubElement(metadata,'configuration',name=KEY,value=json.dumps(bound))
    return ET.tostring(root,encoding='unicode')


def load(bridge,text,start=False):
    import topologies
    from core_adapter import activate
    try:
        root=ET.fromstring(text)
        if root.tag!='scenario': raise ValueError('原生 CORE XML 根节点必须是 scenario')
        metadata=root.find('session_metadata')
        bound={}
        if metadata is not None:
            item=next((x for x in metadata if x.get('name')==KEY),None)
            if item is not None: bound=json.loads(item.get('value','{}'))
        if not isinstance(bound,dict): raise ValueError('XML 中的平台绑定必须是对象')
        deployment_id=repo.identifier('deploy')
        directories=set();required=set()
        for device in root.findall('./devices/device'):
            nid=str(int(device.get('id')))
            binding=bound.get(nid,{})
            if not isinstance(binding,dict): raise ValueError('XML 节点绑定必须是对象')
            old_name=device.get('name')
            binding.setdefault('display_name',old_name)
            if device.get('class') in ('docker','podman'):
                device.set('name',f'chb-{deployment_id[-12:]}-{nid}')
            binding['runtime_name']=device.get('name')
            if binding.get('instance'):
                resolved,dirs=versions.resolve(binding['instance'],binding.get('instance_version'),binding.get('service_overrides'))
                directories.update(dirs)
                if resolved.get('image') and device.get('image') not in (resolved['image'],resolved.get('image_ref')):
                    raise ValueError('XML 镜像与其模板版本不一致')
                declared={e.get('name') for e in device.findall('./services/service')}
                if declared!=set(resolved['services']): raise ValueError('XML 服务与其模板版本不一致')
            bound[nid]=binding
            required.update(e.get('name') for e in device.findall('./services/service'))
        if directories:
            result=activate(bridge,sorted(directories),sorted(required))
            if result['missing']: raise ValueError('XML 所需服务尚未注册: '+str(result['missing']))
        if metadata is None: metadata=ET.SubElement(root,'session_metadata')
        for item in list(metadata):
            if item.get('name')==KEY: metadata.remove(item)
        ET.SubElement(metadata,'configuration',name=KEY,value=json.dumps(bound))
        prepared=ET.tostring(root,encoding='unicode')
    except (ValueError,TypeError,ET.ParseError) as e:
        raise HTTPException(400,str(e))
    with request_file('.xml') as path:
        path.write_text(prepared)
        result,sid=bridge.open_xml(path,False)
    if not result: return {'result':False,'session_id':sid}
    drill=platform_db.ensure_drill(sid)
    record={'deployment_id':deployment_id,'core_sid':sid,'drill_id':drill,'status':'defined','bindings':bound,'snapshot':None,'native_xml':prepared,'created_at':repo.now()}
    with repo.transaction():
        repo.put('deployments',deployment_id,record)
        for nid,binding in bound.items():
            if binding.get('instance'): platform_db.register_deploy_ref(drill,sid,int(nid),binding['instance'],binding['instance_version'])
    platform_db.subscribe_events(sid)
    if start:
        import app
        outcome=app.start_session(sid,app.StartSessionRequest())
        if not outcome['result']: return {'result':False,'session_id':sid,'exceptions':outcome['exceptions']}
    return {'result':True,'session_id':sid,'deployment_id':deployment_id}
