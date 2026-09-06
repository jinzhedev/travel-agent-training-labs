# Lab 05：替换 Pod 后读取原业务记录

使用独立 namespace `travel-agent-lab05`，部署一个 Travel Core API Pod 与单实例 PostgreSQL。只替换 API Pod，不删除数据库、PVC 或 namespace。

## 1. 本地部署

先确认 context 是本地集群：

```bash
kubectl config current-context
```

在课程根目录构建镜像。镜像必须对本地集群可见；OrbStack 通常直接共享本机镜像，kind 需额外执行 `kind load docker-image migu-travel-core:lab05-runtime-v1`。

```bash
docker build --target runtime -t migu-travel-core:lab05-runtime-v1 .
uv run python scripts/lab05_k8s_secret.py
kubectl apply --dry-run=client -f labs/05-reliability-deployment/k8s/travel-core.yaml
kubectl apply -f labs/05-reliability-deployment/k8s/travel-core.yaml
kubectl -n travel-agent-lab05 rollout status statefulset/postgres --timeout=180s
kubectl -n travel-agent-lab05 rollout status deployment/travel-core --timeout=180s
```

Secret 脚本只接受已识别的本地 context，生成随机数据库密码，并读取根 `.env` 的 Travel Core API key；凭据不打印。已有 Secret 会保留，避免重跑脚本更换数据库密码。`rollout status` 这里只用于等待启动，不安排版本发布实验。

## 2. 建立独立业务记录

终端一保持：

```bash
kubectl -n travel-agent-lab05 port-forward service/travel-core 18005:8000
```

终端二：

```bash
uv run python scripts/lab05_runtime.py new --base-url http://127.0.0.1:18005 \
  --session reports/local/lab05/k8s.json
uv run python scripts/lab05_runtime.py call --base-url http://127.0.0.1:18005 \
  --session reports/local/lab05/k8s.json --poi xm_nanputuo
```

为了隔离部署变量，使用固定候选文件：

```json
{"title":"文化景点推荐","poi_ids":["xm_nanputuo"],"reason":"Pod替换实验固定候选，景点已通过本任务工具查询。"}
```

保存为 `reports/local/lab05/k8s-candidate.json`，然后：

```bash
uv run python scripts/lab05_runtime.py prepare --base-url http://127.0.0.1:18005 \
  --session reports/local/lab05/k8s.json --candidate reports/local/lab05/k8s-candidate.json
uv run python scripts/lab05_runtime.py commit --base-url http://127.0.0.1:18005 \
  --session reports/local/lab05/k8s.json
uv run python scripts/lab05_runtime.py inspect --base-url http://127.0.0.1:18005 \
  --session reports/local/lab05/k8s.json > reports/local/lab05/k8s-before.json
```

该候选是固定部署测试输入，不标成模型生成结果。

## 3. 替换 API Pod

```bash
kubectl -n travel-agent-lab05 get pods -l app=travel-core \
  -o custom-columns=NAME:.metadata.name,UID:.metadata.uid
kubectl -n travel-agent-lab05 delete pod -l app=travel-core
kubectl -n travel-agent-lab05 rollout status deployment/travel-core --timeout=180s
kubectl -n travel-agent-lab05 get pods -l app=travel-core \
  -o custom-columns=NAME:.metadata.name,UID:.metadata.uid
```

原 port-forward 可能因绑定的 Pod 消失而退出。在终端一重新执行原 port-forward 命令，再读取：

```bash
uv run python scripts/lab05_runtime.py inspect --base-url http://127.0.0.1:18005 \
  --session reports/local/lab05/k8s.json > reports/local/lab05/k8s-after.json
diff -u reports/local/lab05/k8s-before.json reports/local/lab05/k8s-after.json
```

预期 diff 为空，Pod UID 已改变。核对同一个 task_id、operation_id、recommendation_id、候选内容和 used_calls，而不只检查 HTTP 200。

单副本 API 在替换期间允许暂时不可用，本实验不验证零停机。PostgreSQL 未被替换；本实验不验证数据库高可用或 Dify Run 自动恢复。

## 排障与停止条件

- `ImagePullBackOff`：确认本地集群可见镜像；不要随意改为外部未知镜像。
- API 初始重启：先等 PostgreSQL Ready，再看 API 日志；启动时会创建缺少的课程表。
- 401：核对根 `.env` 与 namespace 内原 Secret 的配置，不输出 Secret 值。
- 替换后数据缺失：停止重复提交，核对原 task_id、访问端口、数据库连接和 PVC。
- 本地集群不可用：记录 `not_run`，不能以文件校验代替真实 Pod 替换。
