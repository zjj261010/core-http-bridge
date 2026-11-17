"""First-run crash check: terminate the real Bridge after image checkpoint commits."""
import importlib.util,json,sys,time
from pathlib import Path
spec=importlib.util.spec_from_file_location('remaining',Path(__file__).with_name('remaining_plan_acceptance.py'));t=importlib.util.module_from_spec(spec);spec.loader.exec_module(t)
t.current='CHECKPOINT-FAULT';root=t.ROOT;result={'id':'CHECKPOINT-FAULT'}
package=t.P+'-crash-piece'
try:
    sources={f'part_{i}.py':f"from core.services.base import CoreService\nclass Part{i}(CoreService):\n name='svc-piece-{i}'\n group='Acceptance'\n startup=[]\n" for i in range(1500)}
    job=t.api('POST','/jobs/import',{'package':package,'image_path':str(Path(t.C['bundle'])/'image.tar'),'python_files':sources})
    deadline=time.monotonic()+60;captured=None
    while time.monotonic()<deadline:
        status=t.api('GET','/jobs/'+job['job_id'])
        if status.get('stage')=='services' and status['status']=='running' and status['result']['pieces']['image'].get('ok'):
            captured=status;break
        if status['status'] not in ('running','queued'):break
        time.sleep(.01)
    if not captured:raise t.Blocked('真实服务件处理在捕获窗口前完成，未伪造中断')
    supervisor=json.loads((root/'processes.json').read_text())['supervisor']
    t.root_script('kill-isolated-bridge',f"import os,signal\nfrom pathlib import Path\nfor child in Path('/proc/{supervisor}/task/{supervisor}/children').read_text().split():\n try: command=Path('/proc/'+child+'/cmdline').read_bytes()\n except FileNotFoundError: continue\n if {str(root/'server.py')!r}.encode() in command: os.kill(int(child),signal.SIGKILL)\n")
    t.control('/restart-bridge')
    interrupted=t.api('GET','/jobs/'+job['job_id']);t.check(interrupted['status']=='interrupted' and interrupted['result']['pieces']['image']['ok'],interrupted)
    resumed=t.wait_job(t.api('POST','/jobs/'+job['job_id']+'/resume',{}));t.check(resumed['result']['pieces']['image'].get('reused') and resumed['result']['pieces']['services']['ok'],resumed)
    result.update(status='PASS',job_id=job['job_id'],interrupted_status=interrupted['status'],image_checkpoint_reused=True,resume_status=resumed['status'])
except t.Blocked as e:result.update(status='BLOCKED',reason=str(e))
except Exception as e:result.update(status='FAIL',error=str(e))
finally:
    # This package has no published target/template references; remove only the dedicated fault fixture.
    t.root_script('cleanup-crash-piece',f"import shutil\nfrom pathlib import Path\nroot=Path({str(root/'state')!r})\nshutil.rmtree(root/'custom_services'/{package!r},ignore_errors=True)\nfor stage in root.glob('.delivery-stage-*'): shutil.rmtree(stage,ignore_errors=True)\n")
    t.control('/restart-bridge')
    (root/'checkpoint-fault.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
