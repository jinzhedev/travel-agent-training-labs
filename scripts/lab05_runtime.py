"""Lab 05 的本地操作入口。凭据只写入指定 session 文件，不打印。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from training_eval.agent_runtime import load_cases, run_case

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    travel_core_api_key: str = ''
    api_key: str = ''
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')


def write_json(path: Path, value: dict, *, private=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    if private:
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['new', 'inspect', 'call', 'prepare', 'commit', 'cancel', 'eval'])
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--session', type=Path, default=ROOT/'reports/local/lab05/session.json')
    parser.add_argument('--query', default='查询南普陀寺和鼓浪屿，推荐一处文化景点。')
    parser.add_argument('--profile', choices=['baseline', 'limited'], default='limited')
    parser.add_argument('--fail-first-call', action='store_true')
    parser.add_argument('--lose-commit-response', action='store_true')
    parser.add_argument('--tool', choices=['detail', 'hours'], default='detail')
    parser.add_argument('--poi', default='xm_nanputuo')
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--run-id', default='lab05-manual')
    parser.add_argument('--output', type=Path, default=ROOT/'reports/local/lab05/runtime-report.json')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if args.dry_run:
        print(json.dumps({'cases': len(load_cases()), 'command': args.command, 'network': False}))
        return
    settings = Settings()
    headers = {'X-API-Key': settings.travel_core_api_key or settings.api_key,
               'X-User-ID': 'lab05-learner', 'X-Tenant-ID': 'training', 'X-Correlation-ID': args.run_id}
    with httpx.Client(base_url=args.base_url.rstrip('/'), timeout=30, trust_env=False) as client:
        if args.command == 'eval':
            results = [run_case(client, headers, case) for case in load_cases()]
            report = {'transport': 'live_http', 'model_execution': 'scripted_actions',
                      'tool_provider': 'simulated', 'results': results,
                      'passed': sum(r['status'] == 'pass' for r in results),
                      'not_run': sum(r['status'] == 'not_run' for r in results)}
            write_json(args.output, report)
            print(json.dumps({'passed': report['passed'], 'not_run': report['not_run'], 'report': str(args.output)}))
            if any(r['status'] == 'fail' for r in results):
                raise SystemExit(1)
            return
        if args.command == 'new':
            if args.session.exists():
                raise SystemExit('session 文件已存在；为新任务使用新的 --session 路径，避免丢失原恢复入口。')
            response = client.post('/v1/lab05/tasks', headers=headers, json={
                'query': args.query, 'profile': args.profile, 'fail_first_call': args.fail_first_call,
                'lose_commit_response': args.lose_commit_response})
            response.raise_for_status()
            value = response.json()
            write_json(args.session, value, private=True)
            print(json.dumps({'task_id': value['task_id'], 'call_limit': value['call_limit'], 'private_session': str(args.session)}))
            return
        session = json.loads(args.session.read_text())
        path = '/v1/lab05/tasks/' + session['task_id']
        if args.command == 'inspect':
            response = client.get(path, headers=headers)
        elif args.command == 'call':
            response = client.post('/v1/lab05/tools/paid-poi-' + args.tool,
                                   headers={'X-Lab-Task-Token': session['task_token']}, json={'poi_id': args.poi})
        elif args.command == 'prepare':
            if not args.candidate:
                raise SystemExit('prepare 需要 --candidate <推荐JSON文件>')
            response = client.post(path + '/prepare', headers=headers, json=json.loads(args.candidate.read_text()))
        else:
            response = client.post(path + '/' + args.command, headers=headers)
        try:
            body = response.json()
        except ValueError:
            raise SystemExit(f'HTTP {response.status_code} 未返回JSON；请检查服务地址与port-forward是否仍连接当前Pod。') from None
        print(json.dumps({'http_status': response.status_code, 'body': body}, ensure_ascii=False, indent=2))
        if response.status_code >= 400:
            raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except httpx.RequestError:
        raise SystemExit('请求未完成；请检查服务地址、服务状态与 port-forward，再 inspect 原任务，不要直接新建任务重做。') from None
