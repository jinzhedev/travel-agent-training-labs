import json
import os
import sys

import httpx
import pytest

from scripts import lab05_runtime


@pytest.mark.parametrize(
    ("dotenv_url", "environment_url", "cli_url", "expected_url"),
    [
        (None, None, None, "http://127.0.0.1:8000"),
        ("https://core-env.test/travel/", None, None, "https://core-env.test/travel"),
        (
            "https://core-env.test",
            "https://core-process.test",
            None,
            "https://core-process.test",
        ),
        (
            "https://core-env.test",
            "https://core-process.test",
            "http://127.0.0.1:18005/",
            "http://127.0.0.1:18005",
        ),
    ],
)
def test_inspect_uses_configured_service(
    tmp_path, monkeypatch, capsys, dotenv_url, environment_url, cli_url, expected_url
):
    for name in list(os.environ):
        if name.lower() in {"travel_core_base_url", "travel_core_api_key", "api_key"}:
            monkeypatch.delenv(name)
    env_file = tmp_path / ".env"
    settings = "TRAVEL_CORE_API_KEY=fixture-key\n"
    if dotenv_url is not None:
        settings += f"TRAVEL_CORE_BASE_URL={dotenv_url}\n"
    env_file.write_text(settings)
    monkeypatch.setitem(lab05_runtime.Settings.model_config, "env_file", env_file)
    if environment_url is not None:
        monkeypatch.setenv("TRAVEL_CORE_BASE_URL", environment_url)

    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({"task_id": "task-1"}))
    argv = ["lab05_runtime.py", "inspect", "--session", str(session_file)]
    if cli_url is not None:
        argv.extend(["--base-url", cli_url])
    monkeypatch.setattr(sys, "argv", argv)

    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"task_id": "task-1", "used_calls": 2})

    client_type = httpx.Client

    def client(**kwargs):
        return client_type(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(lab05_runtime.httpx, "Client", client)
    lab05_runtime.main()

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == f"{expected_url}/v1/lab05/tasks/task-1"
    assert requests[0].headers["X-API-Key"] == "fixture-key"
    output = capsys.readouterr().out
    assert json.loads(output)["body"]["used_calls"] == 2
    assert "fixture-key" not in output


@pytest.mark.parametrize("paths", [{}, {"/v1/lab05/tasks": {"get": {}}}])
def test_eval_rejects_missing_route_before_creating_tasks(tmp_path, monkeypatch, paths):
    report = tmp_path / "report.json"
    report.write_text('{"previous_report": true}')
    monkeypatch.setattr(sys, "argv", ["lab05_runtime.py", "eval", "--output", str(report)])
    monkeypatch.setattr(lab05_runtime, "Settings", lambda: type("Config", (), {
        "travel_core_base_url": "https://core.test",
        "travel_core_api_key": "fixture-key", "api_key": "",
    })())
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"paths": paths})

    client_type = httpx.Client
    monkeypatch.setattr(lab05_runtime.httpx, "Client", lambda **kw: client_type(
        **kw, transport=httpx.MockTransport(handle)
    ))
    with pytest.raises(SystemExit, match="尚未提供 POST /v1/lab05/tasks"):
        lab05_runtime.main()
    assert [(r.method, r.url.path) for r in requests] == [("GET", "/openapi.json")]
    assert json.loads(report.read_text()) == {"previous_report": True}


def test_preflight_accepts_registered_lab05_route():
    with httpx.Client(base_url="https://core.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"paths": {"/v1/lab05/tasks": {"post": {}}}})
    )) as client:
        lab05_runtime.check_lab05_api(client, {})
