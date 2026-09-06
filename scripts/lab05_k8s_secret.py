"""仅为明确的本地集群创建Lab05实验Secret；凭据经stdin传递，不打印。"""
import json
import secrets
import subprocess

from lab05_runtime import Settings

context = subprocess.check_output(['kubectl', 'config', 'current-context'], text=True).strip()
if context not in {'orbstack', 'docker-desktop', 'minikube', 'kind-kind'}:
    raise SystemExit('当前不是已识别的本地课堂context；未操作集群。')
settings = Settings()
api_key = settings.travel_core_api_key or settings.api_key
if not api_key:
    raise SystemExit('缺少Travel Core API key。')
subprocess.run(['kubectl', 'apply', '-f', '-'], input=json.dumps({
    'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'travel-agent-lab05'},
}), text=True, check=True, capture_output=True)
# 已有Secret则保留，避免更换密码后现存PVC中的数据库无法登录。
existing = subprocess.run(['kubectl', '-n', 'travel-agent-lab05', 'get', 'secret', 'lab05-secrets', '--ignore-not-found', '-o', 'name'], capture_output=True, text=True, check=True)
if existing.stdout.strip():
    print('保留已有lab05-secrets；连接失败时核对本地API key与原配置。')
else:
    password = secrets.token_urlsafe(24)
    body = {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'lab05-secrets', 'namespace': 'travel-agent-lab05'},
            'stringData': {'postgres-password': password, 'api-key': api_key,
                           'database-url': f'postgresql+psycopg://travel:{password}@postgres:5432/travel'}}
    subprocess.run(['kubectl', 'apply', '-f', '-'], input=json.dumps(body), text=True, check=True, capture_output=True)
    print('已创建Lab05本地Secret；未输出凭据。')
