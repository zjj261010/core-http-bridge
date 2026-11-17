"""One pass over remaining-plan behavior using real HTTP, CORE, Docker and faults."""
import argparse,copy,json,os,sqlite3,threading,time,traceback,urllib.request,urllib.error
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('--run-dir',required=True);args=p.parse_args();ROOT=Path(args.run_dir);C=json.loads((ROOT/'context.json').read_text());BASE='http://127.0.0.1:18080';P=C['prefix'];results=[];V={};current='SETUP';sequence=0
class Blocked(Exception):pass

def need(key):
    if key not in V:raise Blocked('前置未完成: '+key)
    return V[key]

def check(ok,value):
    if not ok:raise AssertionError(json.dumps(value,ensure_ascii=False,default=str))

def api(method,path,body=None,expected=None,headers=None):
    global sequence
    q=urllib.request.Request(BASE+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers={'Content-Type':'application/json',**(headers or {})})
    start=time.monotonic()
    try:
        with urllib.request.urlopen(q,timeout=90) as response:code=response.status;text=response.read().decode()
    except urllib.error.HTTPError as e:code=e.code;text=e.read().decode()
    try:value=json.loads(text)
    except ValueError:value=text
    sequence+=1;directory=ROOT/'evidence'/current;directory.mkdir(parents=True,exist_ok=True)
    (directory/f'{sequence:04}.json').write_text(json.dumps({'method':method,'path':path,'request':body,'status':code,'response':value,'seconds':time.monotonic()-start},ensure_ascii=False,indent=2))
    check(code==expected if expected is not None else code<400 and (not isinstance(value,dict) or value.get('ok',True)),{'code':code,'response':value})
    return value.get('data',value) if isinstance(value,dict) and isinstance(value.get('data'),(dict,list)) else value

def case(name,fn):
    global current,sequence
    current='REMAIN-'+str(len(results)+1).zfill(3);sequence=0
    try:detail=fn();status='PASS'
    except Blocked as e:detail=str(e);status='BLOCKED'
    except Exception as e:detail={'error':str(e),'trace':traceback.format_exc()[-4000:]};status='FAIL'
    results.append({'id':current,'name':name,'status':status,'detail':detail});(ROOT/'remaining-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,default=str));(ROOT/'remaining-context.json').write_text(json.dumps({**C,**V},ensure_ascii=False,indent=2,default=str));print(current,status,name,flush=True)

def control(path):
    token=json.loads((ROOT/'control.json').read_text())['token']
    with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:18081'+path,data=b'',method='POST',headers={'X-Test-Key':token}),timeout=45) as response:return json.load(response)

def root_script(name,source):
    from core.api.grpc.client import CoreGrpcClient
    path=ROOT/(name+'.py');path.write_text(source)
    client=CoreGrpcClient(address='127.0.0.1:50051');client.connect();client.execute_script(str(path),'');client.close()

def wait_job(job):
    end=time.monotonic()+120
    while time.monotonic()<end:
        value=api('GET','/jobs/'+job['job_id'])
        if value['status'] not in ('queued','running'):return value
        time.sleep(.08)
    raise AssertionError('job timeout')

def deploy(t,start=False):return api('POST','/topologies/'+t['topology_id']+'/deploy',{'revision':t['revision'],'start':start})
def session(sid):return '/api/sessions/'+str(sid)

def setup():
    check(api('GET','/health')['sessions']==0,'专用 CORE 不为空')
    job=wait_job(api('POST','/jobs/import',{'dir':C['bundle'],'manifest':C['manifest']}))
    check(job['status']=='succeeded',job)
    for piece in job['result']['pieces'].values():check(piece['ok'] and piece.get('receipt') and piece.get('input_key'),piece)
    V['main_job']=job
    for name,body in [(P+'-web',{'runtime_type':'docker','image_tag':C['image'],'target_name':P,'target_version':1,'services':[{'name':'svc-'+P+'-httpd-normal'}]}),(P+'-ns',{'runtime_type':'netns','model':'PC','services':[{'name':'DefaultRoute'}]})]:api('POST','/instances',{'name':name,**body})
    V.update(web=P+'-web',ns=P+'-ns');return {'job':job['job_id'],'receipts':list(job['result']['pieces'])}

def partial():
    path=ROOT/'fixtures/resume-image.tar';os.link(Path(C['bundle'])/'image.tar',path)
    package=P+'-resume';py="from core.services.base import CoreService\nclass Helper(CoreService):\n name='svc-helper'\n group='Acceptance'\n startup=[]\n validate=['true']\n"
    job=wait_job(api('POST','/jobs/import',{'image_path':str(path),'package':package,'py':py}))
    check(job['status']=='partial' and job['result']['pieces']['image']['ok'] and job['result']['pieces']['services']['ok'],job)
    V.update(partial_job=job,partial_source=py,partial_package=package)
    path.unlink();return job

def resume_complete():
    manifest={'name':need('partial_package'),'software':'helper','image':C['manifest']['image'],'services':[{'name':'svc-'+need('partial_package')+'-helper'}]}
    value=wait_job(api('POST','/jobs/'+need('partial_job')['job_id']+'/resume',{'manifest':manifest}))
    check(value['status']=='succeeded' and value['result']['pieces']['image'].get('reused') and value['result']['pieces']['services'].get('reused'),value)
    V['resume_manifest']=manifest;return value

def mismatched_image():
    manifest=copy.deepcopy(need('resume_manifest'));manifest['image']={'name':'target-dmz-base','tag':'v3','ports':[]}
    value=wait_job(api('POST','/jobs/'+need('partial_job')['job_id']+'/resume',{'manifest':manifest}))
    check(value['status']=='failed' and not value['result']['pieces']['image']['ok'],value)
    check(api('GET','/targets/manifest/'+need('partial_package'))['image']['name']==C['manifest']['image']['name'],'清单被错误覆盖');return value

def changed_services():
    source=need('partial_source').replace("validate=['true']","validate=['echo NEW_IMPLEMENTATION']")
    preview=api('POST','/targets/import/impact',{'package':need('partial_package'),'py':source,'manifest':need('resume_manifest')})
    check(preview['updated_services'],preview)
    value=wait_job(api('POST','/jobs/'+need('partial_job')['job_id']+'/resume',{'manifest':need('resume_manifest'),'py':source}))
    check(value['status']=='succeeded' and value['result']['pieces']['image'].get('reused') and not value['result']['pieces']['services'].get('reused'),value)
    V['impact']=preview;return value

def drafts():
    before=api('GET','/versions/target/'+P)
    values=[]
    for note in ('draft-A','draft-B'):
        m=copy.deepcopy(C['manifest']);m['notes']=note
        values.append(api('POST','/publications/drafts',{'kind':'target','name':P,'body':{'manifest':m}}))
    check(len(api('GET','/versions/target/'+P))==len(before),'保存草稿提前发布')
    check(all(d['status']=='draft' and not d['issues'] for d in values),values)
    V['target_drafts']=values;return values

def persisted_draft():
    control('/restart-bridge');value=api('GET','/publications/drafts/'+need('target_drafts')[0]['draft_id'])
    check(value['status']=='draft' and value['body']['manifest']['notes']=='draft-A',value);return value

def publish_target():
    d=need('target_drafts')[0];value=api('POST','/publications/drafts/'+d['draft_id']+'/publish',{'revision':d['revision']})
    check(value['draft']['status']=='published' and value['published']['version']==2,value)
    V['target_version']=2;return value

def stale_publish():
    d=need('target_drafts')[1];return api('POST','/publications/drafts/'+d['draft_id']+'/publish',{'revision':d['revision']},expected=409)

def template_draft():
    name=P+'-draft-only';draft=api('POST','/publications/drafts',{'kind':'template','name':name,'body':{'runtime_type':'docker','services':[]}})
    check(draft['issues'] and draft['status']=='draft',draft)
    api('POST','/publications/drafts/'+draft['draft_id']+'/publish',{'revision':draft['revision']},expected=400)
    body={'runtime_type':'netns','model':'PC','services':[{'name':'DefaultRoute'}]}
    saved=api('PUT','/publications/drafts/'+draft['draft_id'],{'revision':draft['revision'],'body':body})
    published=api('POST','/publications/drafts/'+saved['draft_id']+'/publish',{'revision':saved['revision']})
    check(published['published']['version']==1,published);V['ui_draft']=saved['draft_id'];return published

def publish_template_update():
    body={'runtime_type':'docker','image_tag':C['image'],'target_name':P,'target_version':need('target_version'),'services':[{'name':'svc-'+P+'-httpd-vuln'}]}
    draft=api('POST','/publications/drafts',{'kind':'template','name':need('web'),'body':body})
    value=api('POST','/publications/drafts/'+draft['draft_id']+'/publish',{'revision':draft['revision']})
    check(value['published']['version']==2,value);return value

def topology():
    value=api('POST','/topologies',{'name':P+'-snapshot','nodes':[{'id':1,'name':'web','instance':need('web'),'instance_version':1},{'id':2,'name':'client','instance':need('ns'),'instance_version':1}], 'links':[{'node1_id':1,'node2_id':2,'iface1':{'id':0,'ip4':'10.245.1.1','ip4_mask':24},'iface2':{'id':0,'ip4':'10.245.1.2','ip4_mask':24}}]})
    d=deploy(value,True);V.update(topology=value,old_deployment=d,sid=d['core_sid'],old_drill=d['drill_id']);return d

def unchanged_restart():
    sid=need('sid');api('POST',session(sid)+'/stop',{});start=api('POST',session(sid)+'/start',{'definition':False})
    check(start['result'],start)
    check(api('GET',session(sid))['drill_id']==need('old_drill'),'未修改定义的 stop/start 换了演练身份');return start

def replace_and_fork():
    sid=need('sid');api('POST',session(sid)+'/stop',{})
    old=next(d for d in api('GET','/deployments') if d['deployment_id']==need('old_deployment')['deployment_id']);V['frozen_before']=copy.deepcopy(old['snapshot'])
    api('POST',session(sid)+'/nodes/1/replace-instance',{'instance':need('web'),'instance_version':2})
    now=api('GET',session(sid));check(next(n for n in now['nodes'] if n['id']==1)['instance_version']==2,now)
    history=next(d for d in api('GET','/deployments') if d['deployment_id']==old['deployment_id']);check(history['snapshot']==V['frozen_before'],'替换改写了历史快照')
    api('POST',session(sid)+'/start',{'definition':False})
    actual=api('GET',session(sid));check(actual['drill_id']!=need('old_drill'),actual)
    records=api('GET','/deployments');new=next(d for d in records if d['drill_id']==actual['drill_id']);previous=next(d for d in records if d['deployment_id']==old['deployment_id'])
    check(new['deployment_id']!=old['deployment_id'] and previous['snapshot']==V['frozen_before'] and previous['status']=='superseded',{'new':new,'old':previous})
    V.update(new_deployment=new,new_drill=actual['drill_id']);return {'old':old['deployment_id'],'new':new['deployment_id'],'drill':actual['drill_id']}

def old_snapshot_draft():
    value=api('POST','/deployments/'+need('old_deployment')['deployment_id']+'/draft',{})
    check(next(n for n in value['definition']['nodes'] if n['id']==1)['instance_version']==1,value)
    recreated=deploy(value);V['history_draft']=value;return recreated

def native_draft():
    xml=api('GET',session(need('sid'))+'/xml')['data'];opened=api('POST','/api/xml/open',{'data':xml,'start':False})
    d=api('POST','/deployments/'+opened['deployment_id']+'/draft',{})
    check(d['definition']['format']=='core-xml' and 'session_options' in d['definition']['xml_text'],d)
    recreated=deploy(d);check(recreated['deployment_id']!=opened['deployment_id'],recreated);return {'draft':d['topology_id'],'new':recreated['deployment_id']}

def history_protection():
    refs=api('GET','/versions/template/'+need('web')+'/references?version=1')
    check(any(r['collection']=='deployments' for r in refs['documents']),refs)
    api('DELETE','/instances/'+need('web'),expected=409)
    api('POST','/targets/image/delete',{'ref':C['image']},expected=409);return refs

def activation():
    value=api('GET','/versions/target/'+P+'/2/activation');check(value['state']=='registered' and value['on_disk'],value);V['activation']=value;return value

def sse_journal():
    sid=need('sid');ready=threading.Event();events=[];info={};errors=[]
    def read():
        kind='message'
        try:
            with urllib.request.urlopen(BASE+session(sid)+'/events/stream',timeout=12) as response:
                for raw in response:
                    line=raw.decode().strip()
                    if line.startswith('event:'):kind=line[6:].strip()
                    elif line.startswith('data:'):
                        data=json.loads(line[5:])
                        if kind=='ready':info.update(data);ready.set()
                        elif kind=='message':
                            events.append(data)
                            if sum(e.get('text')=='REMAIN_DUPLICATE_PAYLOAD' for e in events)>=3:break
                        kind='message'
        except Exception as e:errors.append(str(e));ready.set()
    thread=threading.Thread(target=read,daemon=True);thread.start();check(ready.wait(8),'SSE 未就绪')
    for _ in range(3):api('POST',session(sid)+'/alert',{'level':'NOTICE','source':P,'text':'REMAIN_DUPLICATE_PAYLOAD'})
    thread.join(12);items=[e for e in events if e.get('text')=='REMAIN_DUPLICATE_PAYLOAD']
    check(len(items)==3 and len({e['event_id'] for e in items})==3,{'events':events,'errors':errors})
    historical=api('GET','/events?drill_id='+info['drill_id']+'&before_id='+str(info['cursor']))['entries']
    check(not ({e['id'] for e in historical}&{e['event_id'] for e in events}),{'history':historical,'live':events})
    V['event_cursor']=items[-1]['event_id'];return {'ready':info,'ids':[e['event_id'] for e in items]}

def sse_reconnect():
    sid=need('sid');api('POST',session(sid)+'/alert',{'source':P,'level':'NOTICE','text':'REPLAY_AFTER_DISCONNECT'})
    received=[]
    q=urllib.request.Request(BASE+session(sid)+'/events/stream',headers={'Last-Event-ID':str(need('event_cursor'))})
    with urllib.request.urlopen(q,timeout=12) as response:
        for raw in response:
            line=raw.decode().strip()
            if line.startswith('data:'):
                data=json.loads(line[5:])
                if data.get('text')=='REPLAY_AFTER_DISCONNECT':received.append(data);break
    check(received and received[0]['event_id']>need('event_cursor'),received);return received

def pending_identity():
    target=ROOT/'state/tmp';backup=ROOT/'state/tmp-identity-backup'
    root_script('pause-identity',f'from pathlib import Path\np=Path({str(target)!r});b=Path({str(backup)!r})\nif p.exists(): p.rename(b)\np.write_text("identity transport unavailable")\n')
    try:
        value=api('GET',session(need('sid')));check(value['identity_status']=='pending' and value['drill_id'] is None,value)
        api('GET',session(need('sid'))+'/events/stream',expected=503)
    finally:
        root_script('restore-identity',f'from pathlib import Path\np=Path({str(target)!r});b=Path({str(backup)!r})\np.unlink()\nif b.exists(): b.rename(p)\n')
    recovered=api('GET',session(need('sid')));check(recovered['identity_status']=='confirmed' and recovered['drill_id']==need('new_drill'),recovered);return recovered['identity_status']

if __name__=='__main__':
    for name,fn in [('新环境准备脚本与按件结果、标识、回执',setup),('仅缺清单任务逐件提交完成结果',partial),('归档已删除仍复用镜像和服务，只补清单',resume_complete),('清单更换镜像不能复用旧镜像成功标记',mismatched_image),('修改实现只重做对应件并展示同名影响',changed_services),('版本草稿保存不改变当前发布内容',drafts),('Bridge 重启后版本草稿仍存在',persisted_draft),('发布靶标新版本并保存真实状态',publish_target),('并行旧草稿发布冲突不覆盖新版本',stale_publish),('不完整模板草稿可保存、修正后显式发布',template_draft),('模板发布新版保留初版',publish_template_update),('冻结初次执行配置及引用',topology),('定义未变化的停止启动保留演练身份',unchanged_restart),('定义替换后生成新部署且历史快照不变',replace_and_fork),('历史快照转独立草稿仍引用旧版本',old_snapshot_draft),('原生 XML 历史转草稿完整保留扩展配置',native_draft),('删除检查列明历史快照和版本引用',history_protection),('实际 daemon 注册状态与文件落盘状态',activation),('历史和实时事件以持久化编号分界',sse_journal),('Last-Event-ID 重连补回断线窗口',sse_reconnect),('真实临时目录故障显示归属待确认且能恢复',pending_identity)]:case(name,fn)
