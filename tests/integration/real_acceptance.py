"""One-pass real acceptance; requires an explicitly isolated host environment."""
import argparse,asyncio,concurrent.futures,copy,json,sqlite3,subprocess,threading,time,traceback,urllib.request,urllib.error
from pathlib import Path
import websockets

parser=argparse.ArgumentParser();parser.add_argument('--url',required=True);parser.add_argument('--run-dir',required=True);args=parser.parse_args()
BASE=args.url.rstrip('/');ROOT=Path(args.run_dir);C=json.loads((ROOT/'context.json').read_text());P=C['prefix']
assert BASE.startswith('http://127.0.0.1:') and not BASE.endswith(':8080'), 'Dedicated local test service required'
results=[];state={};case_id='SETUP';sequence=0;owned=set()
class Blocked(Exception): pass

def need(key):
    if key not in state: raise Blocked('上游步骤尚未成功: '+key)
    return state[key]

def check(value,detail):
    if not value: raise AssertionError(json.dumps(detail,ensure_ascii=False,default=str))

def case(name,fn):
    global case_id,sequence
    case_id='ACC-'+str(len(results)+1).zfill(3);sequence=0;started=time.monotonic()
    try: detail=fn();status='PASS'
    except Blocked as e: detail=str(e);status='BLOCKED'
    except Exception as e: detail={'error':str(e),'trace':traceback.format_exc()[-4500:]};status='FAIL'
    results.append({'id':case_id,'name':name,'status':status,'seconds':round(time.monotonic()-started,3),'detail':detail})
    (ROOT/'api-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,default=str))
    (ROOT/'runtime-context.json').write_text(json.dumps({**C,**state,'owned_sessions':sorted(owned)},ensure_ascii=False,indent=2,default=str))
    print(case_id,status,name,flush=True)


def api(method,path,body=None,raw=None,ctype='application/json',timeout=60,expected=None):
    global sequence
    request=urllib.request.Request(BASE+path,data=raw if raw is not None else json.dumps(body).encode() if body is not None else None,method=method,headers={'Content-Type':ctype})
    started=time.monotonic()
    try:
        with urllib.request.urlopen(request,timeout=timeout) as response: code=response.status;text=response.read().decode()
    except urllib.error.HTTPError as e: code=e.code;text=e.read().decode()
    try: data=json.loads(text)
    except ValueError: data=text
    sequence+=1;directory=ROOT/'evidence'/case_id;directory.mkdir(exist_ok=True)
    (directory/f'request-{sequence:04}.json').write_text(json.dumps({'method':method,'path':path,'request':body if raw is None else {'bytes':len(raw)},'status':code,'seconds':time.monotonic()-started,'response':data},ensure_ascii=False,indent=2,default=str))
    if expected is not None: check(code==expected,{'status':code,'response':data,'expected':expected})
    else: check(code<400 and (not isinstance(data,dict) or data.get('ok',True)),{'status':code,'response':data})
    if isinstance(data,dict):
        sid=data.get('session_id') or (data.get('id') if path=='/api/sessions' and method=='POST' else None)
        if sid: owned.add(sid)
        return data.get('data',data) if isinstance(data.get('data'),(list,dict)) else data
    return data


def session(sid): return '/api/sessions/'+str(sid)
def cmd(nid,command,sid=None):
    d=api('POST',session(sid or need('sid'))+f'/nodes/{nid}/command',{'command':command,'wait':True,'shell':True});check(d['return_code']==0,d);return d

def control(path):
    token=json.loads((ROOT/'control.json').read_text())['token']
    q=urllib.request.Request('http://127.0.0.1:18081'+path,data=b'',method='POST',headers={'X-Test-Key':token})
    with urllib.request.urlopen(q,timeout=40) as r:return json.load(r)

def wait_job(job):
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        d=api('GET','/jobs/'+job['job_id'])
        if d['status'] not in ('queued','running'): return d
        time.sleep(.25)
    raise AssertionError('job timeout')


def stream(path,trigger,match,limit=12):
    ready=threading.Event();received=[];errors=[]
    def read():
        try:
            with urllib.request.urlopen(BASE+path,timeout=limit) as r:
                kind='message'
                for raw in r:
                    line=raw.decode().strip()
                    if line.startswith('event:'): kind=line[6:].strip()
                    elif line.startswith('data:'):
                        value=json.loads(line[5:])
                        if kind=='ready': ready.set()
                        elif kind=='message':
                            received.append(value)
                            if match(value): break
                        kind='message'
                    elif path.endswith('cpu-usage/stream') or '/throughputs/' in path: ready.set()
        except Exception as e: errors.append(str(e));ready.set()
    thread=threading.Thread(target=read,daemon=True);thread.start();check(ready.wait(6),'stream readiness timeout')
    trigger();thread.join(limit)
    check(any(match(d) for d in received),{'events':received[-4:],'errors':errors});return received[-4:]

async def terminal(path,marker):
    output=''
    async with websockets.connect(BASE.replace('http','ws',1)+path,open_timeout=8) as ws:
        await ws.send('printf "'+marker+'\\n"\n')
        end=time.monotonic()+8
        while time.monotonic()<end:
            output+=str(await asyncio.wait_for(ws.recv(),end-time.monotonic()))
            if '\r\n'+marker in output or '\n'+marker in output: break
    check(marker in output,output[-1500:]);return output[-1500:]


def setup_delivery():
    state['upload']=api('POST','/targets/import/bundle/upload?name='+P,raw=(ROOT/'fixtures/bundle.tar.gz').read_bytes(),ctype='application/octet-stream',timeout=120)
    return state['upload']
def draft():
    d=api('GET','/targets/import/bundle/draft?dir='+need('upload')['dir']);check(d['draft']['python_files'] and d['inventory']['has_image_tar'],d);state['draft']=d;return d['inventory']
def confirm():
    d=need('draft');m=copy.deepcopy(C['manifest']);m['notes']='edited draft accepted'
    result=api('POST','/targets/import/bundle/confirm',{'dir':d['dir'],'manifest':m,'python_files':d['draft']['python_files'],'templates':d['draft']['templates']},timeout=120)
    check(all(x['ok'] for x in result['pieces'].values()),result);state['registered']=True;return result

def append():
    py="from core.services.base import CoreService\nclass One(CoreService):\n name='svc-one'\n group='Acceptance'\n startup=[]\n validate=['true']\nclass Two(CoreService):\n name: str = 'svc-two'\n group='Acceptance'\n startup=[]\n validate=['true']\n"
    result=api('POST','/targets/import/services',{'package':P,'py':py,'templates':{}})
    check(result['pieces']['services']['ok'],result)
    m=api('GET','/targets/manifest/'+P)
    m['services'] += [{'name':'svc-'+P+'-one','software':'httpd'},{'name':'svc-'+P+'-two','software':'httpd'},{'name':'DefaultRoute'}]
    api('POST','/targets/manifest/'+P,{'manifest':m})
    state['manifest']=m
    return result

def templates():
    need('registered');web=P+'-web';ns=P+'-ns'
    api('POST','/instances',{'name':web,'runtime_type':'docker','image_tag':C['image'],'services':[{'name':'svc-'+P+'-httpd-normal'}, {'name':'DefaultRoute'}]})
    api('POST','/instances',{'name':ns,'runtime_type':'netns','model':'PC','services':[{'name':'DefaultRoute'}]})
    state.update(web_instance=web,netns_instance=ns)
    return api('GET','/instances')
def saved_draft():
    w=need('web_instance');n=need('netns_instance')
    d=api('POST','/topologies',{'name':P+'-mixed','nodes':[{'id':1,'name':'web','type':'docker','instance':w,'x':100,'y':200},{'id':2,'name':'client','instance':n,'x':400,'y':200},{'id':3,'name':'switch','type':'switch','x':250,'y':200}], 'links':[{'node1_id':1,'node2_id':3,'iface1':{'id':0,'ip4':'10.230.10.1','ip4_mask':24},'delay':1000,'bandwidth':100000000},{'node1_id':2,'node2_id':3,'iface1':{'id':0,'ip4':'10.230.10.2','ip4_mask':24}}]})
    check(d['definition']['nodes'][0]['instance_version']==1,d);state['topology']=d;return d

def deployment():
    t=need('topology');d=api('POST','/topologies/'+t['topology_id']+'/deploy',{'revision':t['revision'],'start':False},timeout=120)
    state.update(deployment=d,sid=d['core_sid'],drill=d['drill_id']);owned.add(d['core_sid']);return d

def start():
    sid=need('sid');r=api('POST',session(sid)+'/start',{'definition':False},timeout=90);check(r['result'],r);state['runtime']=True;return r

def version_update():
    t=need('topology');w=need('web_instance')
    api('POST','/instances/'+w,{'runtime_type':'docker','image_tag':C['image'],'services':[{'name':'svc-'+P+'-httpd-vuln'}]})
    old=api('GET','/topologies/'+t['topology_id']);check(old['definition']['nodes'][0]['instance_version']==1,old)
    vs=api('GET','/versions/template/'+w);check([v['version'] for v in vs]==[2,1],vs);return vs

def upgrade():
    t=need('topology');path='/topologies/'+t['topology_id']+'/upgrade';diff=api('POST',path,{'revision':t['revision']});check(diff['changes'] and diff['changes'][0]['from']==1 and diff['changes'][0]['to']==2,diff)
    d=api('POST',path,{'revision':t['revision'],'confirm':True});state['topology']=d;check(d['definition']['nodes'][0]['instance_version']==2,d);return diff

def export():
    sid=need('sid');d=api('GET',session(sid)+'/topology');node=d['nodes'][0]
    check(node['type']=='docker' and node['image'] and node['instance_version']==1 and any(l['node1_id']==1 and l['options']['delay']==1000 for l in d['links']),d);state['export']=d;return d

def native_xml():
    sid=need('sid');xml=api('GET',session(sid)+'/xml')['data'];state['xml']=xml;return {'bytes':len(xml)}

def same_sid():
    # External delete/create between reconciliation ticks must produce a fresh drill immediately.
    from core.api.grpc.client import CoreGrpcClient
    b=CoreGrpcClient(address='127.0.0.1:15051');b.connect();s=b.create_session(session_id=32991)
    old=api('GET',session(s.id))['drill_id'];b.delete_session(s.id);b.create_session(session_id=s.id)
    new=api('GET',session(s.id))['drill_id'];b.close();owned.add(s.id)
    check(new!=old,{'before':old,'after':new});state['identity_sid']=s.id;return {'before':old,'after':new}

def jobs():
    m=copy.deepcopy(C['manifest']);m['name']=P+'-job'
    payload={'package':m['name'],'image_path':str(Path(C['bundle'])/'image.tar'),'manifest':m}
    started=time.monotonic();j=api('POST','/jobs/import',payload);latency=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        def health(_):
            t=time.monotonic();api('GET','/health');return time.monotonic()-t
        latency=list(pool.map(health,range(4)))
    d=wait_job(j);check(max(latency)<1.5 and d['status']=='partial',{'latency':latency,'job':d});state['job']=d
    return {'submit_seconds':time.monotonic()-started,'health_seconds':latency,'job':d}

def resume():
    old=need('job');d=wait_job(api('POST','/jobs/'+old['job_id']+'/resume',{}));check(d['result']['pieces']['image']['detail'].startswith('沿用'),d);return d

def stopped():
    sid=need('sid');drill=need('drill');r=api('POST',session(sid)+'/stop',{},timeout=90);check(r['result'],r);d=api('GET',session(sid));check(d['drill_id']==drill and len(d['nodes'])==3,d);return d

def browser_session():
    d=api('POST','/api/sessions',{});state['browser_sid']=d['id'];owned.add(d['id']);return d

def restart_bridge():
    state['pre_restart_drill']=need('drill');check(control('/restart-bridge')['ready'],'Bridge restart failed')
    d=api('GET',session(need('sid')));check(d['drill_id']==need('pre_restart_drill'),d)
    check(api('GET','/topologies/'+need('topology')['topology_id'])['revision']==need('topology')['revision'],'draft disappeared')
    return d


def main():
    case('专用 Bridge 与空 CORE 健康',lambda:check(api('GET','/health')['sessions']==0,'专用 daemon 不为空'))
    for page in ('/','/static/target-admin.html','/static/instances.html','/static/containers.html','/static/monitor.html','/static/audit.html','/static/topologies.html','/static/term.html','/static/cterm.html'):
        case('真实页面 '+page,lambda page=page:check(bool(api('GET',page)),'empty page'))
    case('真实 CORE 配置与服务注册',lambda:api('GET','/api/config'))
    case('Docker 镜像列表与不同 tag 可见',lambda:check(any(i['ref']==C['image'] for i in api('GET','/targets')['images']),'fixture missing'))
    case('拒绝 image 字符串清单且不污染列表',lambda:(api('POST','/targets/import/manifest',{'manifest':{'name':P+'bad','image':'invalid'}},expected=400),api('GET','/targets')))
    case('真实整包上传并解包',setup_delivery)
    case('读取多文件草稿与模板',draft)
    case('确认注册采用草稿编辑值',confirm)
    case('清单编辑值真实持久化',lambda:check(api('GET','/targets/manifest/'+P)['notes']=='edited draft accepted','edited draft lost'))
    case('追加单引号/无类型注解服务且保留原定义',append)
    case('CORE 内置服务不被添加包前缀',lambda:check('DefaultRoute' in [s['name'] for s in api('GET','/targets/manifest/'+P)['services']],'builtin rewritten'))
    case('服务池包含两项同软件单模式与原模式对',lambda:check(len([s for s in api('GET','/instances/available-services')['drill'] if s['name'].startswith('svc-'+P+'-')])==4,'services overwritten'))
    case('伪造/省略模式无法同时启用 normal 与 vuln',lambda:api('POST','/instances',{'name':P+'-mutex','runtime_type':'docker','image_tag':C['image'],'services':[{'name':'svc-'+P+'-httpd-normal'},{'name':'svc-'+P+'-httpd-vuln'}]},expected=409))
    case('Docker 模板拒绝清单外基础服务',lambda:api('POST','/instances',{'name':P+'-outside','runtime_type':'docker','image_tag':C['image'],'services':[{'name':'SSH'}]},expected=400))
    case('netns 空服务与默认回填不产生假保存',lambda:api('POST','/instances',{'name':P+'-empty','runtime_type':'netns','model':'PC','services':[]},expected=400))
    case('创建真实 Docker/netns 模板并发布初版',templates)
    case('独立保存混合拓扑草稿与固定版本',saved_draft)
    case('草稿乐观并发冲突有明确反馈',lambda:api('PUT','/topologies/'+need('topology')['topology_id'],{'definition':need('topology')['definition'],'revision':0},expected=409))
    case('部署冻结完整配置并创建独立身份',deployment)
    case('查询保留模板/靶标版本与运行名映射',lambda:check(all('instance_version' in n for n in api('GET',session(need('sid')))['nodes'] if n['type'] in (0,15)),'binding missing'))
    case('同模板第二次部署使用不同容器运行名',lambda:second_deployment())
    case('无坐标建节点得到默认位置',lambda:default_position())
    case('不存在的模板拒绝部署',lambda:api('POST',session(need('browser_sid'))+'/nodes',{'instance':P+'-absent'},expected=400)) if 'browser_sid' in state else None
    case('真实交换机混合网络启动服务',start)
    case('运行节点本机 HTTP 返回 200',lambda:check('200' in cmd(1,"curl -s --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1/")['output'],'local HTTP unavailable'))
    case('交换机跨节点真实 ICMP',lambda:cmd(2,'ping -n -c 1 -W 2 10.230.10.1'))
    case('交换机跨节点真实 HTTP',lambda:check('200' in cmd(2,"curl -s --max-time 5 -o /dev/null -w '%{http_code}' http://10.230.10.1/")['output'],'cross-node HTTP unavailable'))
    case('用户主动触发的拓扑联通检查',lambda:check(api('POST',session(need('sid'))+'/connectivity',{})['all_reachable'],'connectivity failed'))
    case('启用服务声明/就绪检查与网络检查分开',lambda:precheck())
    case('真实 CORE WebSocket 终端',lambda:asyncio.run(terminal(session(need('sid'))+'/nodes/2/terminal/ws','ACC_CORE_TERMINAL_'+P)))
    case('真实事件订阅完成握手后收到告警',lambda:stream(session(need('sid'))+'/events/stream',lambda:api('POST',session(need('sid'))+'/alert',{'source':P,'text':'ACCEPT_EVENT','level':'NOTICE'}),lambda d:d.get('text')=='ACCEPT_EVENT'))
    case('持久事件按 drill 查询且按新到旧返回',lambda:event_history())
    case('真实 CPU SSE',lambda:stream('/api/cpu-usage/stream',lambda:None,lambda d:'usage' in d))
    case('真实吞吐量 SSE 支持本机 CORE 网桥结构',lambda:stream(session(need('sid'))+'/throughputs/stream',lambda:None,lambda d:'bridges' in d or 'ifaces' in d))
    case('容器分组与真实 netns 字节统计',lambda:api('GET','/monitor/containers'))
    case('历史指标写入真实网络字节',lambda:metrics())
    case('拓扑 JSON 保留 Docker/绑定/接口和 QoS',export)
    case('原生 XML 从真实会话导出',native_xml)
    case('原生 XML 走原生 UI 导入路径且非空',lambda:native_import())
    case('导出的混合 JSON 可再次真实部署',lambda:json_import())
    case('模板编辑生成新版本且不修改已有草稿',version_update)
    case('草稿显式升级展示并确认版本差异',upgrade)
    case('既有运行部署保持初版服务',lambda:check(api('GET',session(need('sid')))['nodes'][0]['instance_version']==1,'deployment version changed'))
    case('后台导入期间并行健康检查不被阻塞',jobs)
    case('部分导入续办复用同一文件已完成镜像',resume)
    case('创建独立浏览器用定义态会话',browser_session)
    case('定义态替换绑定保留链路与位置',lambda:replace_binding())
    case('外部删除并重用相同 sid 不混演练身份',same_sid)
    case('停止保留原部署与 drill 身份',stopped)
    case('同一部署重新启动服务且身份不变',lambda:restart_same())
    case('Bridge 重启后草稿/版本/身份恢复',restart_bridge)
    case('Bridge 重启后事件采集恢复',lambda:stream(session(need('sid'))+'/events/stream',lambda:api('POST',session(need('sid'))+'/alert',{'source':P,'text':'AFTER_RESTART','level':'NOTICE'}),lambda d:d.get('text')=='AFTER_RESTART'))
    case('实际数据库迁移记录与唯一写入来源',lambda:migration())
    case('实际独立容器生命周期与终端',lambda:standalone())
    case('EMANE 环境能力',lambda:emane())


def second_deployment():
    t=need('topology');d=api('POST','/topologies/'+t['topology_id']+'/deploy',{'revision':t['revision']});owned.add(d['core_sid']);state['second_sid']=d['core_sid']
    check(d['bindings']['1']['runtime_name']!=need('deployment')['bindings']['1']['runtime_name'],d)
    api('DELETE',session(d['core_sid']));return d['bindings']
def default_position():
    d=api('POST','/api/sessions',{});owned.add(d['id']);state['position_sid']=d['id'];n=api('POST',session(d['id'])+'/nodes',{'name':P+'-default','model':'bare'});actual=api('GET',session(d['id'])+'/nodes/'+str(n['node_id']))['node'];check(actual['position']=={'x':200,'y':200},actual);return actual
def precheck():
    d=api('POST','/precheck?session_id='+str(need('sid'))+'&wait=0',{});check(d['verdict']=='READY',d);check(all(n.get('network',{}).get('result')=='not-tested' for n in d['nodes']),d);return d

def event_history():
    time.sleep(.2);d=api('GET','/events?session_id='+str(need('sid'))+'&limit=20');check(d['entries'] and all(e['drill_id']==need('drill') for e in d['entries']),d);return d

def metrics():
    time.sleep(5);d=api('GET','/metrics?drill_id='+need('drill'));check(any(e.get('net_io') and 'RX' in e['net_io'] for e in d['entries']),d);return d

def native_import():
    d=api('POST','/api/topology',raw=need('xml').encode(),ctype='application/xml');owned.add(d['session_id']);check(d['nodes']==3,d);return d

def json_import():
    d=api('POST','/api/topology',need('export'));owned.add(d['session_id']);n=api('GET',session(d['session_id']))['nodes'];check(n[0]['type']==15 and n[0]['instance_version']==1,n);return d

def replace_binding():
    sid=need('position_sid');nid=api('GET',session(sid))['nodes'][0]['id'];api('POST',session(sid)+'/nodes',{'id':2,'model':'bare','position':{'x':400,'y':200}})
    api('POST',session(sid)+'/links',{'node1_id':nid,'node2_id':2,'iface1':{'id':0,'ip4':'10.231.1.1','ip4_mask':24},'iface2':{'id':0,'ip4':'10.231.1.2','ip4_mask':24}})
    r=api('POST',session(sid)+f'/nodes/{nid}/replace-instance',{'instance':need('netns_instance'),'instance_version':1});d=api('GET',session(sid));check(len(d['links'])==1 and next(n for n in d['nodes'] if n['id']==nid)['position']=={'x':200,'y':200},d);return r

def restart_same():
    r=api('POST',session(need('sid'))+'/start',{'definition':False},timeout=90);check(r['result'],r);check(api('GET',session(need('sid')))['drill_id']==need('drill'),'drill changed');return r

def migration():
    rows=api('GET','/storage/migrations');check({r['source'] for r in rows}>={'manifests','instances','legacy_topologies'},rows)
    c=sqlite3.connect('file:'+str(ROOT/'state/platform.db')+'?mode=ro',uri=True);r=c.execute("SELECT count(*) FROM revisions").fetchone()[0];c.close();check(r>=4,r);return rows

def standalone():
    name='standalone-'+P;d=api('POST','/containers/standalone/run',{'name':name,'image':C['image'],'command':'tail -f /dev/null'});state['standalone']=name
    check(api('POST','/containers/standalone/'+name+'/exec',{'cmd':'echo STANDALONE'})['return_code']==0,d)
    output=asyncio.run(terminal('/ws/containers/standalone/'+name+'/terminal','ACC_STANDALONE_'+P));return output

def emane():
    if not subprocess.run(['sh','-c','command -v emane'],capture_output=True).returncode==0: raise Blocked('未安装 EMANE，未伪造通过')
    return api('GET','/api/config')['emane_models']

if __name__=='__main__':main()
