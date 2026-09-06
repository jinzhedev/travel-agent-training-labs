"""从 Lab 05 输入类型生成两个可交给 Agent 的工具定义。管理接口不导入 Dify。"""
import json
from pathlib import Path

from travel_core.lab05 import PaidQuery

ROOT = Path(__file__).resolve().parents[1]
schema = PaidQuery.model_json_schema()
paths = {}
for kind, description in [('detail', '查询景点详情；每次消耗1个模拟费用额度。'), ('hours', '查询景点开放时间；每次消耗1个模拟费用额度。')]:
    paths[f'/v1/lab05/tools/paid-poi-{kind}'] = {'post': {
        'operationId': 'paid_poi_' + kind,
        'summary': description + ' 返回 blocked 时不再尝试受此任务预算约束的工具。',
        'requestBody': {'required': True, 'content': {'application/json': {'schema': schema}}},
        'responses': {'200': {'description': '模拟查询结果或结构化停止原因'}},
        'security': [{'taskToken': []}],
    }}
result = {'openapi': '3.0.3', 'info': {'title': 'Lab 05 模拟付费查询', 'version': '1.0.0'},
          'servers': [{'url': 'http://host.docker.internal:8000'}], 'paths': paths,
          'components': {'securitySchemes': {'taskToken': {'type': 'apiKey', 'in': 'header', 'name': 'X-Lab-Task-Token'}}}}
output = ROOT / 'dify/tools/lab05-tools.openapi.json'
output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
print(output)
