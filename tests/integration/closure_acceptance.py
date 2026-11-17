"""Distinct final acceptance: XML metadata, same-image versions, network protocols and discard."""
import importlib.util,json,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('acceptance',Path(__file__).with_name('real_acceptance.py'));s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
parent=s.ROOT;s.state.update(json.loads((parent/'focused-runtime-context.json').read_text()));s.owned.update(s.state.get('owned_sessions',[]));s.results=[]
s.ROOT=parent/'closure';s.ROOT.mkdir(exist_ok=True);(s.ROOT/'evidence').mkdir(exist_ok=True)


def native_binding():
    dep=s.need('new_deployment');sid=dep['core_sid'];xml=s.api('GET',s.session(sid)+'/xml')['data'];s.check('http_bridge_bindings_v1' in xml,'missing binding metadata')
    response=s.api('POST','/api/xml/open',{'data':xml,'start':False});s.owned.add(response['session_id']);d=s.api('GET',s.session(response['session_id']))
    old=s.api('GET',s.session(sid));actual=next(n for n in d['nodes'] if n['id']==1);original=next(n for n in old['nodes'] if n['id']==1)
    s.check(actual['instance_version']==original['instance_version'] and actual['name']!=original['name'] and actual['display_name']==original['display_name'],{'new':actual,'old':original})
    redeployed=s.api('POST','/deployments/'+response['deployment_id']+'/redeploy',{});s.owned.add(redeployed['core_sid']);s.check(redeployed['deployment_id']!=response['deployment_id'],redeployed);return {'imported':response,'new_runtime_name':actual['name'],'redeployed':redeployed['deployment_id']}


def discard():
    uploaded=s.api('POST','/targets/import/upload?rel_path=cancel/archive.tar',raw=b'uploaded draft',ctype='application/octet-stream')
    original=parent/'fixtures/user-original.txt';original.write_text('user original')
    result=s.api('POST','/targets/import/discard',{'paths':[uploaded['path'],str(original)]})
    s.check(not Path(uploaded['path']).exists() and original.exists(),result);return result


def service_source(marker):
    return "from core.services.base import CoreService\nclass VersionProof(CoreService):\n name = 'svc-version-proof'\n group='Acceptance'\n startup = [\"sh -c 'echo "+marker+" > /tmp/chb-version-proof'\"]\n validate=['test -f /tmp/chb-version-proof']\n"


def versions_side_by_side():
    package=s.P+'-version-proof';service='svc-'+package+'-version-proof'
    m={'name':package,'software':'version-proof','version':'software-1.0','image':{'name':s.C['image'].rsplit(':',1)[0],'tag':'v1','ports':[]},'services':[{'name':service}]}
    s.api('POST','/targets/import/services',{'package':package,'py':service_source('OLD')})
    s.api('POST','/targets/import/manifest',{'manifest':m})
    template=s.P+'-proof-template'
    declaration={'runtime_type':'docker','image_tag':s.C['image'],'target_name':package,'services':[{'name':service}]}
    initial=s.api('GET','/versions/target/'+package)[0];declaration['target_version']=initial['version']
    s.api('POST','/instances',{'name':template,**declaration})
    s.api('POST','/targets/import/services',{'package':package,'py':service_source('NEW')})
    latest=s.api('GET','/versions/target/'+package)[0]
    s.check(latest['version']>initial['version'] and s.api('GET','/versions/target/'+package)[-1]['manifest']['version']=='software-1.0',latest)
    declaration['target_version']=latest['version'];s.api('POST','/instances/'+template,declaration)
    topology=s.api('POST','/topologies',{'name':package,'nodes':[{'id':1,'name':'old','instance':template,'instance_version':1},{'id':2,'name':'new','instance':template,'instance_version':2}],'links':[]})
    d=s.api('POST','/topologies/'+topology['topology_id']+'/deploy',{'revision':topology['revision'],'start':True});s.owned.add(d['core_sid'])
    a=s.cmd(1,'cat /tmp/chb-version-proof',d['core_sid'])['output'].strip();b=s.cmd(2,'cat /tmp/chb-version-proof',d['core_sid'])['output'].strip()
    s.check(a=='OLD' and b=='NEW',{'old':a,'new':b});s.state['proof_deployment']=d
    return {'old':a,'new':b,'target_versions':[initial['version'],latest['version']],'template_versions':[1,2]}


def redis_fixture():
    package=s.P+'-redis';service='svc-'+package+'-server-vuln'
    source="from core.services.base import CoreService\nclass RedisProof(CoreService):\n name='svc-server-vuln'\n group='Acceptance'\n startup=['redis-server --daemonize yes --protected-mode no --bind 0.0.0.0']\n validate=['pidof redis-server']\n shutdown=['pkill redis-server']\n"
    s.api('POST','/targets/import/services',{'package':package,'py':source})
    s.api('POST','/targets/import/manifest',{'manifest':{'name':package,'software':'redis','image':{'name':'target-inn-base','tag':'v3','ports':[6379]},'services':[{'name':service,'mode':'vuln','ports':[6379]}]}})
    target=s.api('GET','/versions/target/'+package)[0]
    s.api('POST','/instances',{'name':package,'runtime_type':'docker','image_tag':'target-inn-base:v3','target_name':package,'target_version':target['version'],'services':[{'name':service}]})
    s.state['redis_instance']=package;return {'service':service}


def network(kind):
    nodes=[{'id':1,'name':'web','instance':s.need('web_instance'),'instance_version':1},{'id':2,'name':'redis','instance':s.need('redis_instance'),'instance_version':1},{'id':3,'name':'client','instance':s.need('netns_instance'),'instance_version':1}]
    links=[]
    if kind=='switch':
        nodes.append({'id':4,'name':'switch','type':'switch'})
        for nid in (1,2,3):links.append({'node1_id':nid,'node2_id':4,'iface1':{'id':0,'ip4':'10.234.10.'+str(nid),'ip4_mask':24}})
        web,redis='10.234.10.1','10.234.10.2'
    else:
        links=[{'node1_id':1,'node2_id':2,'iface1':{'id':0,'ip4':'10.233.1.1','ip4_mask':24},'iface2':{'id':0,'ip4':'10.233.1.2','ip4_mask':24}}, {'node1_id':1,'node2_id':3,'iface1':{'id':1,'ip4':'10.233.2.1','ip4_mask':24},'iface2':{'id':0,'ip4':'10.233.2.2','ip4_mask':24}}]
        web,redis='10.233.2.1','10.233.1.2'
    t=s.api('POST','/topologies',{'name':s.P+'-'+kind,'nodes':nodes,'links':links})
    d=s.api('POST','/topologies/'+t['topology_id']+'/deploy',{'revision':t['revision'],'start':True});sid=d['core_sid'];s.owned.add(sid)
    ping=s.cmd(3,'ping -n -c 1 -W 2 '+web,sid)
    http=s.cmd(3,"curl -s --max-time 5 -o /dev/null -w '%{http_code}' http://"+web+'/',sid);s.check('200' in http['output'],http)
    command="timeout 5 bash -c 'exec 3<>/dev/tcp/"+redis+"/6379; printf \"PING\\r\\n\" >&3; head -n 1 <&3'"
    pong=s.cmd(1,command,sid);s.check('PONG' in pong['output'],pong)
    s.state[kind+'_sid']=sid
    return {'sid':sid,'icmp':ping['return_code'],'http':http['output'],'redis':pong['output']}


def throughput():
    sid=s.need('switch_sid')
    return s.stream(s.session(sid)+'/throughputs/stream',lambda:s.cmd(3,'ping -n -c 3 -i .3 -W 2 10.234.10.1',sid),lambda d:any(i.get('throughput',0)>0 for i in d.get('bridges',[])+d.get('ifaces',[])))

if __name__=='__main__':
    s.case('新增：原生 XML 携带绑定、唯一运行名与独立重部署',native_binding)
    s.case('新增：取消上传草稿只删除平台暂存文件',discard)
    s.case('新增：同镜像两个服务实现版本同时真实运行 OLD/NEW',versions_side_by_side)
    s.case('新增：真实 Redis 交付和模板',redis_fixture)
    s.case('新增：直连网络完整 ICMP / HTTP 200 / Redis PONG',lambda:network('direct'))
    s.case('新增：交换机网络完整 ICMP / HTTP 200 / Redis PONG',lambda:network('switch'))
    s.case('新增：交换机真实流量对应非零吞吐量',throughput)
