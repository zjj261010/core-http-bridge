"""Bounded, persistent delivery jobs with explicit piece-wise resume."""
import concurrent.futures
import hashlib
import json
import os
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


def fingerprint(body):
    normalized,_=ds.prepare(body)
    return ds.digest(ds.identities(normalized)[0])


def submit(body,prior=None):
    if _stopping.is_set(): raise HTTPException(503,'服务正在停止')
    if not _slots.acquire(False): raise HTTPException(429,'后台任务已满，请稍后重试')
    try:
        normalized,_=ds.prepare(body)
        keys,image_input=ds.identities(normalized,(prior or {}).get('image_input'))
        job={'job_id':repo.identifier('job'),'status':'queued','input':normalized,'input_keys':keys,'image_input':image_input,'created_at':repo.now(),'updated_at':repo.now(),'result':{'pieces':{}},'resume_from':(prior or {}).get('job_id')}
        if prior:
            import copy
            job['previous_pieces']=copy.deepcopy((prior.get('result') or {}).get('pieces',{}))
            old_image=prior.get('image_input') or {}
            receipt=(job['previous_pieces'].get('image') or {}).get('receipt') or {}
            if old_image.get('archive_sha256')==image_input.get('archive_sha256') and not old_image.get('declared_ref') and image_input.get('declared_ref') in receipt.get('images',{}):
                job['previous_pieces']['image']['input_key']=keys['image']
        repo.put('jobs',job['job_id'],job)
        _executor.submit(run,job)
        return public(job)
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
        job.update(status='running',message='正在加载镜像、注册服务和保存清单；docker load 不提供可靠百分比',updated_at=repo.now())
        repo.put('jobs',job['job_id'],job)
        def checkpoint(kind,result):
            job.update(result=result,stage=kind,updated_at=repo.now())
            repo.put('jobs',job['job_id'],job)
        response=ta._import_delivery(job['input'],checkpoint=checkpoint,reuse=job.get('previous_pieces'),input_keys=job['input_keys'])
        payload=json.loads(response.body)
        if payload.get('data'): job['result']=payload['data']
        else: job['error']=payload.get('error')
        if not payload['ok']: status='failed'
        else:
            pieces=payload['data'].get('pieces',{})
            states=[p.get('ok') for p in pieces.values()]
            status='succeeded' if states and all(x is True for x in states) else 'partial' if any(x is True for x in states) else 'failed'
        job.update(status=status,message='任务已结束，请查看各件结果',updated_at=repo.now())
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
    merged={**old['input'],**{k:v for k,v in body.items() if k in ('py','python_files','templates','manifest','image_path')}}
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
