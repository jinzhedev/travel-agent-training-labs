"""Lab 05 后端规则测试，使用同目录 conftest.py 提供的本地测试数据库和客户端。"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from training_eval.agent_runtime import CANDIDATE, load_cases, run_case
from travel_core import lab05
from travel_core.database import SessionLocal


@pytest.mark.parametrize('case', load_cases(), ids=lambda c: c['case_id'])
def test_runtime_gold(client, headers, case, monkeypatch):
    if case['scenario'] != 'context_revision_changed':
        assert run_case(client, headers, case)['status'] == 'pass'
        return
    created = client.post('/v1/lab05/tasks', headers=headers, json={'query': '推荐文化景点'}).json()
    path = '/v1/lab05/tasks/' + created['task_id']
    client.post('/v1/lab05/tools/paid-poi-detail', headers={'X-Lab-Task-Token': created['task_token']}, json={'poi_id': 'xm_nanputuo'})
    assert client.post(path + '/prepare', headers=headers, json=CANDIDATE).status_code == 200
    changed = {**lab05.corpus(), 'revision': 'test-new-revision'}
    monkeypatch.setattr(lab05, 'corpus', lambda: changed)
    assert client.post(path + '/commit', headers=headers).status_code == 409
    assert client.get(path, headers=headers).json()['result'] is None


def test_budget_survives_new_sessions_and_cancel_is_final(client, headers):
    created = client.post('/v1/lab05/tasks', headers=headers, json={'query': '跨进程额度'}).json()
    def call(_):
        with SessionLocal() as db:
            return lab05.paid_query(lab05.PaidQuery(poi_id='xm_nanputuo'), 'paid_poi_detail', created['task_token'], db)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(call, range(30)))
    assert sum(r['status'] == 'ok' for r in results) == 5
    path = '/v1/lab05/tasks/' + created['task_id']
    assert client.post(path + '/cancel', headers=headers).json()['status'] == 'cancelled'
    assert call(0)['stop_reason'] == 'task_cancelled'
    assert client.post(path + '/prepare', headers=headers, json=CANDIDATE).status_code == 409


def test_model_cannot_create_task_or_read_other_token(client, headers):
    created = client.post('/v1/lab05/tasks', headers=headers, json={'query': '预算'}).json()
    token = {'X-Lab-Task-Token': created['task_token']}
    assert client.post('/v1/lab05/tasks', headers=token, json={'query': '重置预算'}).status_code == 401
    assert client.post('/v1/lab05/tools/paid-poi-detail', json={'poi_id': 'xm_nanputuo'}).status_code == 401
    path = '/v1/lab05/tasks/' + created['task_id']
    assert 'task_token' not in client.get(path, headers=headers).json()
    assert client.get(path, headers={**headers, 'X-Tenant-ID': 'other'}).status_code == 404


def test_unobserved_candidate_and_post_commit_cancel(client, headers):
    created = client.post('/v1/lab05/tasks', headers=headers, json={'query': '推荐'}).json()
    path = '/v1/lab05/tasks/' + created['task_id']
    assert client.post(path + '/prepare', headers=headers, json=CANDIDATE).status_code == 409
    client.post('/v1/lab05/tools/paid-poi-detail', headers={'X-Lab-Task-Token': created['task_token']}, json={'poi_id': 'xm_nanputuo'})
    client.post(path + '/prepare', headers=headers, json=CANDIDATE)
    saved = client.post(path + '/commit', headers=headers).json()
    cancelled = client.post(path + '/cancel', headers=headers).json()
    assert cancelled['status'] == 'completed'
    assert cancelled['result'] == saved['result']


def test_crash_after_admission_does_not_refund_and_late_result_cannot_uncancel(client, headers, monkeypatch):
    created = client.post('/v1/lab05/tasks', headers=headers, json={'query': '中断边界'}).json()
    path = '/v1/lab05/tasks/' + created['task_id']
    original = lab05.corpus
    def crash():
        raise RuntimeError('模拟准入后进程故障')
    monkeypatch.setattr(lab05, 'corpus', crash)
    with SessionLocal() as db, pytest.raises(RuntimeError):
        lab05.paid_query(lab05.PaidQuery(poi_id='xm_nanputuo'), 'paid_poi_detail', created['task_token'], db)
    assert client.get(path, headers=headers).json()['used_calls'] == 1
    def cancel_during_call():
        client.post(path + '/cancel', headers=headers).raise_for_status()
        return original()
    monkeypatch.setattr(lab05, 'corpus', cancel_during_call)
    response = client.post('/v1/lab05/tools/paid-poi-detail', headers={'X-Lab-Task-Token': created['task_token']}, json={'poi_id': 'xm_nanputuo'})
    assert response.json()['status'] == 'ok'
    state = client.get(path, headers=headers).json()
    assert state['status'] == 'cancelled'
    assert state['used_calls'] == 2
    assert state['result'] is None
