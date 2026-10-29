"""Do not create a WebDriver session before the native WebView is ready."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location("native_gui", Path(__file__).resolve().parents[2] / "scripts/native-gui.py")
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)


@pytest.mark.parametrize("becomes_ready", [True, False])
def test_session_requires_explicit_ready_status(monkeypatch, tmp_path, becomes_ready):
    view = gui.WebView(tmp_path / "app", tmp_path)
    requests = []
    statuses = iter([{"ready": False, "message": "waiting for webview initialization"}] * 2 + ([{"ready": True}] if becomes_ready else []))
    clock = iter(range(0, 120, 10))
    monkeypatch.setattr(gui.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(gui.time, "sleep", lambda _: None)
    monkeypatch.setattr(gui.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(poll=lambda: None))

    def request(path, body=None, method=None):
        requests.append(path)
        if path == "/status":
            return next(statuses, {"ready": False})
        assert path == "/session" and view.driver_status == {"ready": True}
        return {"sessionId": "test", "capabilities": {}}

    monkeypatch.setattr(view, "request", request)
    monkeypatch.setattr(view, "command", lambda path, *args: "tauri://localhost" if path == "/url" else None)
    monkeypatch.setattr(view, "js", lambda *args: True)
    monkeypatch.setattr(view, "text", lambda *args: "本地服务已连接")
    monkeypatch.setattr(view, "workspace_visible", lambda: None)
    try:
        if becomes_ready:
            view.start()
            assert requests == ["/status", "/status", "/status", "/session"]
        else:
            with pytest.raises(AssertionError, match="native WebView reports ready=true"):
                view.start()
            assert "/session" not in requests
            assert view.driver_status == {"ready": False}
    finally:
        if view.log:
            view.log.close()
