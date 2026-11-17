"""Only run the three unfinished API cases after explicit retest approval."""
import importlib.util,json,traceback
from pathlib import Path
spec=importlib.util.spec_from_file_location('remaining_cases',Path(__file__).with_name('remaining_plan_acceptance.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
original=m.ROOT;m.V.update(json.loads((original/'remaining-context.json').read_text()))
m.ROOT=original/'focused';m.ROOT.mkdir(exist_ok=False)
results=[]
for case_id,name,fn in [('REMAIN-003','归档已删除仍复用镜像和服务，只补清单',m.resume_complete),('REMAIN-004','清单更换镜像不能复用旧镜像成功标记',m.mismatched_image),('REMAIN-005','修改实现只重做对应件并展示同名影响',m.changed_services)]:
 m.current=case_id;m.sequence=0
 try: detail=fn();status='PASS'
 except m.Blocked as e: detail=str(e);status='BLOCKED'
 except Exception as e: detail={'error':str(e),'trace':traceback.format_exc()[-4000:]};status='FAIL'
 results.append({'id':case_id,'name':name,'status':status,'detail':detail})
 (original/'focused-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,default=str))
 print(case_id,status,name,flush=True)
