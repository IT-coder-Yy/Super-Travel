from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import httpx
import pytest


BACKEND = Path(__file__).resolve().parents[1] / "main.py"
spec = importlib.util.spec_from_file_location("desktop_backend", BACKEND)
backend = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = backend
spec.loader.exec_module(backend)
TOKEN = "test-only-token-never-for-production"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    with TestClient(backend.create_app(tmp_path / "用户 数据", TOKEN)) as instance:
        yield instance


def create(client, **overrides):
    payload = {"title": "苏州三日", "data": {"messages": [], "formal": {"city": "苏州"}}}
    payload.update(overrides)
    response = client.post("/api/conversations", headers=AUTH, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_health_auth_and_docs(client):
    assert client.get("/api/health").json() == {"status": "ok", "version": backend.VERSION}
    for path in ("/api/conversations", "/docs", "/openapi.json", "/unknown"):
        assert client.get(path).status_code == 401
    assert client.get("/api/conversations", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/docs", headers=AUTH).status_code == 404


@pytest.mark.parametrize("origin", backend.ALLOWED_ORIGINS)
def test_allowed_cors_preflight(client, origin):
    response = client.options("/api/conversations", headers={"Origin": origin, "Access-Control-Request-Method": "PUT", "Access-Control-Request-Headers": "Authorization,Content-Type"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


def test_foreign_origin_cannot_write(client):
    response = client.post("/api/conversations", headers={**AUTH, "Origin": "https://evil.example"}, json={"title": "x", "data": {}})
    assert response.status_code == 403
    assert client.get("/api/conversations", headers=AUTH).json() == []


def test_create_update_backup_conflict_and_restart(client):
    original = create(client)
    assert UUID(original["id"])
    assert original["revision"] == 1
    summaries = client.get("/api/conversations", headers=AUTH).json()
    assert set(summaries[0]) == {"id", "title", "revision", "updated_at"}
    path = f"/api/conversations/{original['id']}"
    update = {"title": "调整后", "data": {"draft": {"note": "保留草稿"}}, "expected_revision": 1}
    changed = client.put(path, headers=AUTH, json=update)
    assert changed.status_code == 200
    assert changed.json()["revision"] == 2
    store = client.app.state.store
    assert json.loads((store.backups / f"{original['id']}.json").read_bytes()) == original
    conflict = client.put(path, headers=AUTH, json=update)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["current_revision"] == 2
    with TestClient(backend.create_app(store.root, TOKEN)) as restarted:
        assert restarted.get(path, headers=AUTH).json() == changed.json()


def test_concurrent_writes_only_one_expected_revision_wins(client):
    document = create(client)
    store = client.app.state.store
    identifier = UUID(document["id"])

    def update(index):
        try:
            store.update(identifier, backend.ConversationUpdate(title=f"edit {index}", data={"value": index}, expected_revision=1))
            return 200
        except backend.StoreError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(update, range(8)))
    assert outcomes.count(200) == 1
    assert outcomes.count(409) == 7
    assert store.get(identifier).revision == 2


@pytest.mark.parametrize("bad_content", [b"{broken", b"{}", b'{"id":"wrong"}', b"x" * (backend.MAX_BYTES + 1)], ids=["malformed-json", "missing-fields", "bad-id", "oversized-file"])
def test_corrupt_file_is_never_overwritten(client, bad_content):
    document = create(client)
    path = client.app.state.store.conversations / f"{document['id']}.json"
    path.write_bytes(bad_content)
    route = f"/api/conversations/{document['id']}"
    assert client.get(route, headers=AUTH).status_code == 503
    assert client.get("/api/conversations", headers=AUTH).status_code == 503
    assert client.put(route, headers=AUTH, json={"title": "replacement", "data": {}, "expected_revision": 1}).status_code == 503
    assert path.read_bytes() == bad_content


def test_failed_atomic_replace_preserves_original_and_backup(client, monkeypatch):
    document = create(client)
    store = client.app.state.store
    path = store.conversations / f"{document['id']}.json"
    before = path.read_bytes()
    original_replace = backend.os.replace

    def fail_main(source, destination):
        if Path(destination) == path:
            raise OSError("simulated disk failure")
        return original_replace(source, destination)

    monkeypatch.setattr(backend.os, "replace", fail_main)
    result = client.put(f"/api/conversations/{document['id']}", headers=AUTH, json={"title": "changed", "data": {}, "expected_revision": 1})
    assert result.status_code == 503
    assert path.read_bytes() == before
    assert (store.backups / path.name).read_bytes() == before
    assert not list(store.conversations.glob("*.tmp"))


def test_request_and_document_size_limits(client, monkeypatch):
    monkeypatch.setattr(backend, "MAX_BYTES", 400)
    response = client.post("/api/conversations", headers=AUTH, json={"title": "x", "data": {"text": "a" * 500}})
    assert response.status_code == 413
    response = client.post("/api/conversations", headers=AUTH, json={"title": "x", "data": {"text": "a" * 300}})
    assert response.status_code == 413
    assert client.get("/api/conversations", headers=AUTH).json() == []


@pytest.mark.parametrize("payload", [{"title": " ", "data": {}}, {"title": "x", "data": []}, {"title": "x", "data": {}, "id": "supplied"}])
def test_invalid_input_rejected(client, payload):
    assert client.post("/api/conversations", headers=AUTH, json=payload).status_code == 422


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_is_rejected_without_server_error(client, value):
    response = client.post("/api/conversations", headers={**AUTH, "Content-Type": "application/json"}, content='{"title":"x","data":{"amount":' + value + '}}')
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_request"
    assert client.get("/api/conversations", headers=AUTH).json() == []


def test_invalid_uuid_and_missing_document(client):
    assert client.get("/api/conversations/not-a-uuid", headers=AUTH).status_code == 422
    assert client.get(f"/api/conversations/{uuid4()}", headers=AUTH).status_code == 404


def test_invalid_directory_fails_before_server(tmp_path):
    location = tmp_path / "file-not-directory"
    location.write_text("preserve", encoding="utf-8")
    with pytest.raises(OSError):
        backend.create_app(location, TOKEN)
    assert location.read_text(encoding="utf-8") == "preserve"


def test_process_ready_port_zero_auth_version_and_stdin_eof(tmp_path):
    env = {**os.environ, "STA_BACKEND_TOKEN": TOKEN, "STA_APP_VERSION": "0.1.1"}
    process = subprocess.Popen([sys.executable, str(BACKEND), "--host", "127.0.0.1", "--port", "0", "--data-dir", str(tmp_path / "独立 目录")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    try:
        line = lines.get(timeout=20)
        ready = json.loads(line)
        assert ready["event"] == "ready"
        assert ready["version"] == "0.1.1"
        assert ready["port"] > 0
        with httpx.Client(base_url=f"http://127.0.0.1:{ready['port']}", trust_env=False) as network:
            assert network.get("/api/health").json() == {"status": "ok", "version": "0.1.1"}
            assert network.get("/api/conversations").status_code == 401
            assert network.get("/api/conversations", headers=AUTH).json() == []
        duplicate = subprocess.run([sys.executable, str(BACKEND), "--port", "0", "--data-dir", str(tmp_path / "独立 目录")], input="", capture_output=True, text=True, env=env, timeout=10)
        assert duplicate.returncode == 2
        assert not duplicate.stdout
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert TOKEN not in line + process.stdout.read() + process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_process_missing_token_fails_without_ready(tmp_path):
    env = dict(os.environ)
    env.pop("STA_BACKEND_TOKEN", None)
    result = subprocess.run([sys.executable, str(BACKEND), "--data-dir", str(tmp_path)], input="", capture_output=True, text=True, env=env, timeout=10)
    assert result.returncode == 2
    assert not result.stdout
