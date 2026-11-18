"""Bounded, persistent delivery jobs with explicit piece-wise resume."""
import concurrent.futures
import copy
import json
import threading
from fastapi import APIRouter, HTTPException
import repository as repo
import delivery_state as ds

router=APIRouter()
_executor=concurrent.futures.ThreadPoolExecutor(max_workers=2,thread_name_prefix='delivery')
_slots=threading.BoundedSemaphore(10)
_stopping=threading.Event()


def recover():
    for job in repo.read('jobs').values():
        if job['status'] in ('queued','running'):
            job.update(status='interrupted',message='服务重启导致任务中断；请检查各件结果后续办',updated_at=repo.now())
            repo.put('jobs',job['job_id'],job)


def submit(body,prior=None):
    if _stopping.is_set(): raise HTTPException(503,'服务正在停止')
    if any(key in body and not isinstance(body[key], str) for key in ('dir', 'image_path', 'bundle_upload_id')):
        raise HTTPException(400,'导入路径或上传编号格式无效')
    if not _slots.acquire(False): raise HTTPException(429,'后台任务已满，请稍后重试')
    try:
        job={'job_id':repo.identifier('job'),'status':'queued','stage':'queued','input':body,
             'created_at':repo.now(),'updated_at':repo.now(),'result':{'pieces':{}},
             'resume_from':(prior or {}).get('job_id')}
        if prior:
            previous = {**(prior.get('previous_pieces') or {}), **(prior.get('result') or {}).get('pieces', {})}
            job['previous_pieces']=copy.deepcopy(previous)
            job['image_input']=prior.get('image_input') or {}
        result = public(job)
        repo.put('jobs',job['job_id'],job)
        _executor.submit(run,job)
        return result
    except Exception:
        _slots.release();raise


def public(job,editable=False):
    result={k:v for k,v in job.items() if k not in ('input','previous_pieces')}
    if editable:
        result['editable']={k:job['input'].get(k) for k in ('package','py','python_files','templates','manifest')}
    return result


def run(job):
    import target_admin as ta
    try:
        def persist(**changes):
            job.update(updated_at=repo.now(), **changes)
            repo.put('jobs',job['job_id'],job)

        body = job['input']
        if 'bundle_upload_id' in body and not body.get('source_dir'):
            persist(status='running',stage='unpacking',message='正在解包交付文件')
            directory = ta.unpack_uploaded_delivery(body['bundle_upload_id'], _stopping.is_set)
            body = {**body, 'dir': str(directory)}
        persist(status='running',stage='validating',message='正在校验归档及交付内容')
        normalized,_=ds.prepare(body)
        if body.get('image_path'):
            normalized['image_path'] = body['image_path']
        old_image=job.get('image_input') or {}
        keys,image_input=ds.identities(normalized,old_image,cancelled=_stopping.is_set)
        previous = job.get('previous_pieces') or {}
        receipt=(previous.get('image') or {}).get('receipt') or {}
        if (old_image.get('archive_sha256')==image_input.get('archive_sha256') and
                not old_image.get('declared_ref') and image_input.get('declared_ref') in receipt.get('images',{})):
            previous['image']['input_key']=keys['image']
        persist(input=normalized,input_keys=keys,image_input=image_input,stage='loading',message='正在处理交付内容')
        def checkpoint(kind,result):
            state = result['pieces'][kind]['state']
            message = ('镜像已接收，等待在镜像详情确认' if state == 'pending-confirmation'
                       else result['pieces'][kind]['detail'])
            persist(result=result,stage=kind,message=message)
        response=ta._import_delivery(normalized,checkpoint=checkpoint,reuse=previous,input_keys=keys)
        payload=json.loads(response.body)
        if payload.get('data'): job['result']=payload['data']
        else: job['error']=payload.get('error')
        if not payload['ok']: status='failed'
        else:
            pieces=payload['data'].get('pieces',{})
            states=[p.get('ok') for p in pieces.values()]
            status='succeeded' if states and all(x is True for x in states) else 'partial' if any(x is True for x in states) else 'failed'
        message = (payload['data']['hint'] if status == 'succeeded' and normalized.get('defer_services')
                   else '任务已结束，请查看各件结果')
        job.update(status=status,message=message,updated_at=repo.now())
    except Exception as e:
        job.update(status='failed',message=str(e),updated_at=repo.now())
    finally:
        if _stopping.is_set() and job['status'] not in ('succeeded','partial'):
            job.update(status='interrupted',message='Bridge 停止导致任务中断，可续办')
        repo.put('jobs',job['job_id'],job)
        _slots.release()


@router.post('/jobs/import',status_code=202)
def create(body:dict):
    try: return {'ok':True,'data':submit(body)}
    except (ValueError,SyntaxError,TypeError) as e: raise HTTPException(400,str(e))


@router.get('/jobs')
def listing():
    return {'ok':True,'data':[public(job) for job in repo.read('jobs').values()]}


@router.get('/jobs/{job_id}')
def get(job_id:str):
    d=repo.read('jobs',job_id)
    if not d: raise HTTPException(404,'任务不存在')
    return {'ok':True,'data':public(d,editable=True)}


@router.post('/jobs/{job_id}/resume',status_code=202)
def resume(job_id:str,body:dict):
    import target_admin as ta
    old=repo.read('jobs',job_id)
    if not old: raise HTTPException(404,'任务不存在')
    if old['status'] not in ('partial','failed','interrupted'): raise HTTPException(409,'该任务无需续办')
    merged={**old['input'],**{k:v for k,v in body.items() if k in ('py','python_files','templates','manifest','image_path','defer_services')}}
    try: return {'ok':True,'data':submit(merged,prior=old)}
    except (ValueError,SyntaxError,TypeError) as e: raise HTTPException(400,str(e))


def shutdown():
    _stopping.set()
    import target_admin
    target_admin.shutdown_docker()
    _executor.shutdown(wait=False,cancel_futures=True)
    for job in repo.read('jobs').values():
        if job['status'] in ('queued','running'):
            job.update(status='interrupted',message='Bridge 停止导致任务中断，可续办')
            repo.put('jobs',job['job_id'],job)
