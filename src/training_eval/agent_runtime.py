"""Lab 05 固定动作回放；模拟模型提出的调用，不冒充真实模型轨迹。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CASES = Path(__file__).resolve().parents[2] / 'datasets/eval/agent-runtime-v1/cases.jsonl'
POIS = ['xm_nanputuo', 'xm_gulangyu', 'xm_shapowei']
CANDIDATE = {'title': '文化景点推荐', 'poi_ids': ['xm_nanputuo'], 'reason': '依据本任务查询结果。'}


def load_cases():
    cases = [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]
    if [c['case_id'] for c in cases] != [f'L05-{i:03}' for i in range(1, 13)]:
        raise ValueError('Lab 05 金标须包含连续且不重复的12条案例')
    return cases


def run_case(client, headers: dict, case: dict) -> dict:
    scenario = case['scenario']
    if scenario == 'context_revision_changed':
        return {'case_id': case['case_id'], 'status': 'not_run',
                'reason': '数据revision变化由隔离测试验证，不修改共享课堂数据。'}
    response = client.post('/v1/lab05/tasks', headers=headers, json={
        'query': case['description'], 'fail_first_call': scenario == 'charged_failure',
        'lose_commit_response': scenario == 'resume_unknown_result',
    })
    response.raise_for_status()
    created = response.json()
    task_id = created['task_id']
    tool_headers = {'X-Lab-Task-Token': created['task_token']}
    path = f'/v1/lab05/tasks/{task_id}'
    results = []

    def call(i=0, extra=None):
        tool = 'paid-poi-detail' if i % 2 == 0 else 'paid-poi-hours'
        result = client.post('/v1/lab05/tools/' + tool, headers=tool_headers,
                             json={'poi_id': POIS[i % 3], **(extra or {})})
        return result

    extra = {}
    if scenario in ('within_budget', 'charged_failure'):
        results = [call(0).json(), call(0).json()]
    elif scenario in ('single_round', 'multiple_rounds', 'switch_tool'):
        rounds = {'single_round': [100], 'multiple_rounds': [3, 3, 3], 'switch_tool': [5, 2]}[scenario]
        for size in rounds:
            results.extend(call(i).json() for i in range(size))
    elif scenario == 'concurrent_budget':
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda i: call(i).json(), range(20)))
    elif scenario == 'forged_task':
        extra['http_status'] = call(extra={'task_id': 'replacement', 'call_limit': 100}).status_code
    elif scenario == 'owner_isolation':
        extra['http_status'] = client.get(path, headers={**headers, 'X-User-ID': 'another-user'}).status_code
    else:
        results = [call(0).json()]
        prepared = client.post(path + '/prepare', headers=headers, json=CANDIDATE)
        prepared.raise_for_status()
        original = prepared.json()['pending_action']
        if scenario == 'cancel_late_action':
            client.post(path + '/cancel', headers=headers).raise_for_status()
            late = call().json()
            denied = client.post(path + '/commit', headers=headers)
            extra['late_blocked'] = late.get('stop_reason') == 'task_cancelled' and denied.status_code == 409
        elif scenario == 'changed_action':
            changed = client.post(path + '/prepare', headers=headers, json={**CANDIDATE, 'title': '重新规划的不同推荐'})
            extra['http_status'] = changed.status_code
            extra['original_action_preserved'] = client.get(path, headers=headers).json()['pending_action'] == original
        elif scenario == 'resume_unknown_result':
            first = client.post(path + '/commit', headers=headers)
            first_result = client.get(path, headers=headers).json()['result']
            second = client.post(path + '/commit', headers=headers)
            second.raise_for_status()
            extra.update(same_result_id=first.status_code == 504 and first_result['recommendation_id'] == second.json()['result']['recommendation_id'], replayed=second.json()['replayed'])
    state = client.get(path, headers=headers)
    state.raise_for_status()
    state = state.json()
    actual = {
        'used_calls': state['used_calls'], 'executed_calls': sum(e['kind'] == 'executed' for e in state['events']),
        'blocked_calls': state['blocked_calls'], 'status': state['status'], 'result': state['result'],
        'upstream_errors': sum(r.get('status') == 'upstream_error' for r in results),
        'commit_events': sum(e['kind'] == 'committed' for e in state['events']), **extra,
    }
    passed = all(actual.get(key) == value for key, value in case['expected'].items())
    if scenario == 'cancel_late_action':
        passed = passed and extra['late_blocked']
    return {'case_id': case['case_id'], 'status': 'pass' if passed else 'fail',
            'task_id': task_id, 'actual': actual, 'evidence': state,
            'model_execution': 'scripted_actions', 'tool_provider': 'simulated'}
