"""One authorized focused retest of failures, followed by distinct recovery/migration cases."""
import importlib.util,json,sys,os,time,subprocess,sqlite3
from pathlib import Path
spec=importlib.util.spec_from_file_location('acceptance',Path(__file__).with_name('real_acceptance.py'));s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
s.state.update(json.loads((s.ROOT/'runtime-context.json').read_text()))
s.owned.update(s.state.get('owned_sessions',[]));s.results=[]
# Preserve first-run evidence; focused cases have their own run namespace.
s.ROOT=s.ROOT/'focused';s.ROOT.mkdir(exist_ok=True);(s.ROOT/'evidence').mkdir(exist_ok=True)
parent=s.ROOT.parent
original_case=s.case
from core.api.grpc.client import CoreGrpcClient

def case(name,fn):
    original_case(name,fn)
    (s.ROOT/'runtime-context.json').replace(parent/'focused-runtime-context.json')

def xml():
    d=s.api('POST','/api/topology',raw=s.need('xml').encode(),ctype='application/xml');s.owned.add(d['session_id']);s.check(d['nodes']==3,d);return d

def connectivity():
    d=s.api('POST',s.session(s.need('sid'))+'/connectivity',{});s.check(d['all_reachable'],d);return d

def export():
    d=s.api('GET',s.session(s.need('sid'))+'/topology');n=next(n for n in d['nodes'] if n['id']==1)
    s.check(n['type']=='docker' and n['instance_version']==1 and any(l['node1_id']==1 and l['options']['delay']==1000 for l in d['links']),d);s.state['export']=d;return d

def json_import():
    d=s.api('POST','/api/topology',s.need('export'));s.owned.add(d['session_id']);return s.api('GET',s.session(d['session_id']))

def replaced():
    sid=s.need('position_sid');d=s.api('GET',s.session(sid));node=next(n for n in d['nodes'] if n['id']==1)
    s.check(node['position']=={'x':200,'y':200} and node['instance']==s.need('netns_instance') and len(d['links'])==1,d)
    return d

def nonempty_migration():
    directory=s.ROOT/'legacy';directory.mkdir()
    code='''import json,os
from pathlib import Path
import repository as r
root=Path(os.environ['BRIDGE_DATA_DIR']);source=root/'instances.json'
legacy={'template-a':{'runtime_type':'netns','model':'PC','services':[{'name':'DefaultRoute'}]},'unresolved':{'original':'retained'}}
source.write_text(json.dumps(legacy));r.migrate('legacy-verification',str(source));assert r.read('legacy-verification')==legacy
assert json.loads(Path(str(source)+'.pre-sqlite.bak').read_text())==legacy
r.put('legacy-verification','template-a',{'changed':True});assert json.loads(source.read_text())==legacy
r.migrate('legacy-verification',str(source));assert r.read('legacy-verification','template-a')=={'changed':True}
print(json.dumps({'backup_verified':True,'idempotent':True,'sqlite_only_writes':True,'unresolved_preserved':True}))
'''
    env=dict(os.environ,BRIDGE_DATA_DIR=str(directory),PYTHONPATH='/opt/core-http-bridge',PYTHONDONTWRITEBYTECODE='1')
    r=subprocess.run(['/opt/core/venv/bin/python','-c',code],env=env,capture_output=True,text=True);s.check(r.returncode==0,r.stdout+r.stderr);return json.loads(r.stdout)

def offline_draft():
    # Pause only the isolated daemon, then verify a draft can be read without CORE replying.
    from core.api.grpc.client import CoreGrpcClient
    control=CoreGrpcClient(address='127.0.0.1:50051');control.connect()
    script=s.ROOT/'pause.py';resume=s.ROOT/'resume.py';output=s.ROOT/'paused-pid'
    code="""import os,signal
from pathlib import Path
supervisor=__SUPERVISOR__
for candidate in Path(f'/proc/{supervisor}/task/{supervisor}/children').read_text().split():
    pid=int(candidate)
    try: cmd=Path(f'/proc/{pid}/cmdline').read_bytes()
    except FileNotFoundError: continue
    if __CONF__.encode() in cmd:
        Path(__OUTPUT__).write_text(str(pid))
        os.kill(pid,signal.SIGSTOP)
""".replace('__SUPERVISOR__',str(json.loads((parent/'processes.json').read_text())['supervisor'])).replace('__CONF__',repr(str(parent/'core.conf'))).replace('__OUTPUT__',repr(str(output)))
    script.write_text(code);control.execute_script(str(script),'')
    try:
        s.check(output.exists(),'isolated daemon PID not found')
        start=time.monotonic();d=s.api('GET','/topologies/'+s.need('topology')['topology_id']);elapsed=time.monotonic()-start
        s.check(elapsed<1.5 and d['revision']==s.need('topology')['revision'],d)
        return {'read_seconds_while_daemon_paused':elapsed}
    finally:
        resume.write_text('import os,signal\nos.kill('+output.read_text()+',signal.SIGCONT)\n')
        control.execute_script(str(resume),'');control.close()


def job_interruption():
    j=s.api('POST','/jobs/import',{'package':s.P+'-interruption','image_path':str(Path(s.C['bundle'])/'image.tar')})
    token=json.loads((parent/'control.json').read_text())['token']
    request=s.urllib.request.Request('http://127.0.0.1:18081/restart-bridge',data=b'',method='POST',headers={'X-Test-Key':token})
    with s.urllib.request.urlopen(request,timeout=40) as response: s.check(json.load(response)['ready'],'restart failed')
    d=s.api('GET','/jobs/'+j['job_id'])
    if d['status']!='interrupted':
        if d['status'] in ('partial','succeeded'): raise s.Blocked('真实 docker load 在关停前已完成，未强行伪造中断')
        s.check(False,d)
    return d


def daemon_restart():
    old=s.api('GET',s.session(s.need('sid')))['drill_id'];t=s.need('topology')
    token=s.json.loads((parent/'control.json').read_text())['token']
    request=s.urllib.request.Request('http://127.0.0.1:18081/restart-daemon',data=b'',method='POST',headers={'X-Test-Key':token})
    with s.urllib.request.urlopen(request,timeout=40) as r: s.check(s.json.load(r)['ready'],'test daemon restart failed')
    client=CoreGrpcClient(address='127.0.0.1:15051');client.connect();client.create_session(session_id=s.need('sid'));client.close()
    new=s.api('GET',s.session(s.need('sid')))['drill_id'];s.check(old!=new,{'old':old,'new':new})
    saved=s.api('GET','/topologies/'+t['topology_id']);s.check(saved['revision']==t['revision'],saved)
    s.state['new_epoch_drill']=new
    return {'old':old,'new':new,'saved_draft_revision':saved['revision']}

def recover_deployment():
    t=s.need('topology');d=s.api('POST','/topologies/'+t['topology_id']+'/deploy',{'revision':t['revision']})
    s.owned.add(d['core_sid']);s.check(d['drill_id']!=s.need('drill'),d);s.state['new_deployment']=d
    old=next(x for x in s.api('GET','/deployments') if x['deployment_id']==s.need('deployment')['deployment_id'])
    s.check(old['status']=='interrupted',old);return d

def audit_identity():
    d=s.need('new_deployment');sid=d['core_sid'];s.api('POST',s.session(sid)+'/start',{'definition':False})
    s.asyncio.run(s.terminal(s.session(sid)+'/nodes/2/terminal/ws','AUDIT_IDENTITY_'+s.P))
    rows=s.api('GET','/audit?drill_id='+d['drill_id'])['entries'];s.check(any('AUDIT_IDENTITY_' in str(x) for x in rows),rows)
    s.state['audit_drill']=d['drill_id'];return rows

if __name__=='__main__':
    case('补验：最小镜像使用同 netns 宿主 ping 联通检查',connectivity)
    case('补验：拓扑 JSON 按节点/链路身份校验完整内容',export)
    case('补验：原生 XML 通过正确 Path 对象导入',xml)
    case('补验：此前受阻的混合 JSON 再部署',json_import)
    case('补验：替换绑定后按节点 ID 校验位置与链路',replaced)
    case('新增：非空 JSON 备份、迁移校验、幂等与唯一写入',nonempty_migration)
    case('新增：CORE daemon 暂停时草稿仍可读取',offline_draft)
    case('新增：真实导入被 Bridge 重启中断后状态可恢复',job_interruption)
    case('新增：CORE daemon 重启后相同 sid 签发新身份',daemon_restart)
    case('新增：重启后原草稿新建部署，旧部署标记中断',recover_deployment)
    case('新增：真实终端审计按演练身份过滤',audit_identity)
