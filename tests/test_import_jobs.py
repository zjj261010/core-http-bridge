from types import SimpleNamespace

from fastapi.responses import JSONResponse

import delivery_state
import jobs
import target_admin


def test_enqueue_leaves_hashing_to_worker_and_unchanged_resume_reuses_digest(client, monkeypatch, tmp_path):
    path = tmp_path / 'image.tar'
    path.write_bytes(b'archive')
    pending, hashed, completed = [], [], []
    monkeypatch.setattr(jobs, '_executor', SimpleNamespace(submit=lambda function, job: pending.append((function, job))))
    digest = delivery_state.file_digest

    def record_hash(path, **kwargs):
        hashed.append(path)
        return digest(path, **kwargs)

    def import_delivery(body, *, checkpoint, reuse, input_keys):
        completed.append((reuse, input_keys))
        return JSONResponse({'ok':True,'data':{'pieces':{
            'image':{'ok':True,'state':'done','input_key':input_keys['image']},
            'services':{'ok':False,'state':'failed'},
        }}})

    monkeypatch.setattr(delivery_state, 'file_digest', record_hash)
    monkeypatch.setattr(target_admin, '_import_delivery', import_delivery)
    response = client.post('/jobs/import', json={'image_path':str(path)})
    assert response.status_code == 202 and response.json()['data']['status'] == 'queued'
    assert not hashed
    job = response.json()['data']['job_id']
    function, data = pending.pop()
    function(data)
    assert hashed == [str(path)]
    assert client.get('/jobs/' + job).json()['data']['status'] == 'partial'

    resumed = client.post('/jobs/' + job + '/resume', json={})
    assert resumed.status_code == 202
    function, data = pending.pop()
    function(data)
    assert hashed == [str(path)]
    assert completed[1][0]['image']['input_key'] == completed[1][1]['image']

    path.write_bytes(b'new archive')
    client.post('/jobs/' + job + '/resume', json={}).raise_for_status()
    function, data = pending.pop()
    function(data)
    assert hashed == [str(path), str(path)]
    assert completed[2][1]['image'] != completed[0][1]['image']
