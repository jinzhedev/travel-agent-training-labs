"""Lab 05：通过命令行操作业务任务，验证调用预算、取消和推荐保存的恢复行为。

这个文件用于配合 labs/05-reliability-deployment/LAB.md 完成后端实操。
脚本通过 HTTP 请求 Travel Core；预算计数、权限检查、任务状态和推荐记录
由 Travel Core 在服务端保存，本地 session 文件只提供任务编号和访问凭据。

有两种使用方式：
  手动实操：创建任务，把任务 token 配置到 Dify 工具认证中，由 Agent 查询并
            生成推荐，再用本脚本检查预算、冻结候选、取消或保存原动作。
  固定回放：执行 eval，由脚本按照金标案例发送请求，验证后端是否正确处理
            超额调用、失败重试、并发争用、权限、取消和重复提交。

各子命令的工作：
  new      创建业务任务，将 task_id、task_token 等写入 --session 指定的文件；
           baseline 配置为 30 次额度，limited 为 5 次，额度由服务端决定。
  inspect  读取原任务的最新状态、已用额度、工具证据、待保存动作和最终结果。
  call     手动调用一次模拟付费查询工具；服务端准入后消耗该任务的额度。
  prepare  将 --candidate 中的推荐冻结为待保存动作，由服务端生成 operation_id。
  commit   提交已冻结的推荐；重复提交已完成的原动作时取回原结果。
  cancel   取消尚未完成的任务，阻止后续动作；已完成的保存不会因此撤销。
  eval     先检查目标服务的任务创建接口，再按 12 条金标独立创建并回放任务。
           数据 revision 变化案例保留为 not_run，由隔离测试验证。

配置与文件：
  读取脚本所在仓库根目录的 .env，不会自动读取相邻仓库的配置。
  地址优先级：--base-url > 进程 TRAVEL_CORE_BASE_URL > .env > 本机 8000 端口。
  管理接口使用 TRAVEL_CORE_API_KEY，未设置时回退到 API_KEY。
  本脚本不调用 Dify API，不读取 DIFY_BASE_URL，也不创建或发布 Chatflow。
  --session 指定手动操作的任务文件；恢复时继续使用原文件，new 不覆盖已有文件。
  --output 指定 eval 报告，默认 reports/local/lab05/runtime-report.json。
  session 含任务凭据，仅文件所有者可读写，不作为实验报告提交。

故障模拟参数在 new 时设置：
  --fail-first-call       首次工具调用模拟失败，用于检查失败尝试与重试的额度消耗。
  --lose-commit-response  保存成功后模拟返回 HTTP 504，用于练习查询和恢复原结果。

在仓库根目录运行：
  uv run python scripts/lab05_runtime.py eval --dry-run
  uv run python scripts/lab05_runtime.py eval
  uv run python scripts/lab05_runtime.py new --session reports/local/lab05/a1.json
  uv run python scripts/lab05_runtime.py inspect --session reports/local/lab05/a1.json

--dry-run 只加载并校验本地案例，不连接服务、不创建任务。
eval 使用真实 HTTP 请求，但动作由固定脚本产生，付费工具使用模拟额度；
报告通过只能证明这些后端规则生效，不能证明 Dify Agent 的推荐质量或停止行为。
真实模型实操需要按 LAB.md 在 Dify 中运行，并核对对应的工具结果和 trace。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from training_eval.agent_runtime import load_cases, run_case

# 以脚本位置定位仓库，避免从不同工作目录运行时读错配置或写错报告位置。
ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    # 同名进程环境变量优先于 .env；extra='ignore' 允许 .env 包含其他实验的配置。
    travel_core_base_url: str = 'http://127.0.0.1:8000'
    travel_core_api_key: str = ''
    api_key: str = ''
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')


def write_json(path: Path, value: dict, *, private=False):
    # session 含任务凭据，创建时即限制为仅文件所有者可读写；普通报告使用 0644。
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
    if private:
        # os.open 的 mode 不会修改已有文件权限，因此覆盖私有文件时也要收紧权限。
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write('\n')


def check_lab05_api(client, headers):
    # 评测前核对任务创建路由，避免连到旧版本服务后才逐条失败或覆盖旧报告。
    response = client.get('/openapi.json', headers=headers)
    if response.status_code == 404:
        raise SystemExit('目标服务没有 /openapi.json；请核对 TRAVEL_CORE_BASE_URL 是否指向 Travel Core。')
    response.raise_for_status()
    try:
        schema = response.json()
        paths = schema['paths']
        available = isinstance(paths, dict) and 'post' in paths.get('/v1/lab05/tasks', {})
    except (ValueError, KeyError, TypeError):
        raise SystemExit('目标服务未返回有效 OpenAPI；请核对 Travel Core 地址与代理配置。') from None
    if not available:
        raise SystemExit(
            '目标 Travel Core 尚未提供 POST /v1/lab05/tasks。'
            '请在目标服务器更新 Lab 05 源码、重建镜像并重建容器；仅重启旧容器无效。'
            '本次未创建评测任务，也未更新评测报告。'
        )


def main():
    # new 创建任务；其余手动命令通过同一 session 操作原任务；eval 独立运行金标。
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('command', choices=['new', 'inspect', 'call', 'prepare', 'commit', 'cancel', 'eval'])
    parser.add_argument('--base-url', help='覆盖 TRAVEL_CORE_BASE_URL；默认读取本仓库 .env')
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
    parser.add_argument('--cases', nargs='+', help='eval 只运行指定案例，例如 L05-005 L05-012')
    args = parser.parse_args()
    cases = load_cases() if args.command == 'eval' or args.dry_run else []
    if args.cases:
        if args.command != 'eval':
            parser.error('--cases 仅用于 eval')
        unknown = set(args.cases) - {c['case_id'] for c in cases}
        if unknown:
            parser.error('未知案例：' + ', '.join(sorted(unknown)))
        cases = [c for c in cases if c['case_id'] in args.cases]
    if args.dry_run:
        # 只加载并校验本地案例，不读取 session，也不创建 HTTP 客户端。
        print(json.dumps({'cases': len(cases), 'command': args.command, 'network': False}))
        return
    settings = Settings()
    # 命令行地址优先于配置；专用 Travel Core 密钥为空时回退到通用 API_KEY。
    base_url = args.base_url or settings.travel_core_base_url
    headers = {'X-API-Key': settings.travel_core_api_key or settings.api_key,
               'X-User-ID': 'lab05-learner', 'X-Tenant-ID': 'training', 'X-Correlation-ID': args.run_id}
    # run_id 用于关联请求；任务身份由服务端 task_id 确定。
    # trust_env=False 禁用环境代理等 HTTPX 环境配置，直接连接指定服务。
    with httpx.Client(base_url=base_url.rstrip('/'), timeout=30, trust_env=False) as client:
        if args.command == 'eval':
            check_lab05_api(client, headers)
            # 用固定动作通过真实 HTTP 回放金标；模型动作和工具提供方均为模拟。
            results = [run_case(client, headers, case) for case in cases]
            report = {'transport': 'live_http', 'model_execution': 'scripted_actions',
                      'tool_provider': 'simulated', 'results': results,
                      'passed': sum(r['status'] == 'pass' for r in results),
                      'not_run': sum(r['status'] == 'not_run' for r in results)}
            write_json(args.output, report)
            print(json.dumps({'passed': report['passed'], 'not_run': report['not_run'], 'report': str(args.output)}))
            # 先保存失败证据再返回非零退出码；not_run 单独计数，不计为通过或失败。
            if any(r['status'] == 'fail' for r in results):
                raise SystemExit(1)
            return
        if args.command == 'new':
            # session 是后续查询与恢复的入口，禁止新任务覆盖已有任务凭据。
            if args.session.exists():
                raise SystemExit('session 文件已存在；为新任务使用新的 --session 路径，避免丢失原恢复入口。')
            response = client.post('/v1/lab05/tasks', headers=headers, json={
                'query': args.query, 'profile': args.profile, 'fail_first_call': args.fail_first_call,
                'lose_commit_response': args.lose_commit_response})
            response.raise_for_status()
            value = response.json()
            # 保存完整响应，并输出 task_token，方便复制到 Dify 工具认证中。
            write_json(args.session, value, private=True)
            print(json.dumps({'task_id': value['task_id'], 'task_token': value['task_token'],
                              'call_limit': value['call_limit'], 'private_session': str(args.session)}))
            return
        # 本地 session 提供任务编号和凭据；最新任务状态始终向服务端查询。
        session = json.loads(args.session.read_text())
        path = '/v1/lab05/tasks/' + session['task_id']
        if args.command == 'inspect':
            response = client.get(path, headers=headers)
        elif args.command == 'call':
            # 付费工具使用任务 token 鉴权，由服务端归属到原任务并执行调用额度检查。
            response = client.post('/v1/lab05/tools/paid-poi-' + args.tool,
                                   headers={'X-Lab-Task-Token': session['task_token']}, json={'poi_id': args.poi})
        elif args.command == 'prepare':
            # 候选推荐从 JSON 文件读取，交给服务端校验并保存为待提交动作。
            if not args.candidate:
                raise SystemExit('prepare 需要 --candidate <推荐JSON文件>')
            response = client.post(path + '/prepare', headers=headers, json=json.loads(args.candidate.read_text()))
        else:
            # 此处只剩 commit/cancel；提交和取消的状态约束、重复提交处理由服务端负责。
            response = client.post(path + '/' + args.command, headers=headers)
        try:
            body = response.json()
        except ValueError:
            raise SystemExit(f'HTTP {response.status_code} 未返回JSON；请检查服务地址与port-forward是否仍连接当前Pod。') from None
        # 手动操作保留错误响应正文，方便查看额度拦截、状态冲突或结果未知等证据。
        print(json.dumps({'http_status': response.status_code, 'body': body}, ensure_ascii=False, indent=2))
        if response.status_code >= 400:
            raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except httpx.RequestError:
        # 网络异常不代表服务端未执行；先 inspect 原任务，避免重复创建或执行动作。
        raise SystemExit('请求未完成；请检查服务地址、服务状态与 port-forward，再 inspect 原任务，不要直接新建任务重做。') from None
