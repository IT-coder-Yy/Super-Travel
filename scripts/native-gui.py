"""CI-only business acceptance in the actual Windows/macOS/Linux system WebView.

The embedded W3C driver is compiled only with native-gui-tests. Model/tool replies
are explicit local fixtures; the normal Python HTTP/agent/storage code still runs.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/native/fixture_server.py"
ELEMENT = "element-6066-11e4-a52e-4f735466cecf"


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], cwd=ROOT, check=True, **kwargs)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def wait(check, description, seconds=45):
    end = time.monotonic() + seconds
    last = None
    while time.monotonic() < end:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError, AssertionError, RuntimeError) as error:
            last = str(error)
        time.sleep(0.25)
    raise AssertionError(f"Timed out: {description}; last error: {last}")


def stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


@contextmanager
def credentials(work):
    """Only temporary CI accounts/keychains; never changes a user's credential."""
    if sys.platform == "darwin":
        def security(*args, check=True):
            return subprocess.run(["security", *args], check=check, capture_output=True, text=True, timeout=20)
        original_default = shlex.split(security("default-keychain", "-d", "user", check=False).stdout)
        if original_default == ["<NULL>"]:
            original_default = []
        original_search = shlex.split(security("list-keychains", "-d", "user").stdout)
        keychain = str(work / "gui.keychain-db")
        password = uuid.uuid4().hex
        security("create-keychain", "-p", password, keychain)
        try:
            security("default-keychain", "-d", "user", "-s", keychain)
            security("unlock-keychain", "-p", password, keychain)
            yield
        finally:
            security("default-keychain", "-d", "user", "-s", *original_default)
            security("list-keychains", "-d", "user", "-s", *original_search)
            security("delete-keychain", keychain)
    elif sys.platform == "linux":
        # The workflow supplies a private D-Bus session and virtual display.
        control = work / "keyring"
        control.mkdir()
        with (work / "keyring.log").open("w") as log:
            process = subprocess.Popen(["gnome-keyring-daemon", "--foreground", "--unlock", "--components=secrets", f"--control-directory={control}"], stdin=subprocess.PIPE, stdout=log, stderr=log)
            process.stdin.write(b"supertravel-native-gui-fixture\n")
            process.stdin.close()
            try:
                run("gdbus", "wait", "--session", "--timeout", "20", "org.freedesktop.secrets")
                yield
            finally:
                stop(process)
    else:
        yield


class WebView:
    def __init__(self, binary, output):
        self.binary, self.output = binary, output
        self.process = None
        self.session = ""
        self.log = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, body=None, method=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=30) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(error.read().decode()) from None
        value = result.get("value")
        if isinstance(value, dict) and "error" in value:
            raise RuntimeError(str(value))
        return value

    def command(self, path, body=None, method=None):
        return self.request(f"/session/{self.session}" + path, body, method)

    def start(self):
        with socket.socket() as port:
            port.bind(("127.0.0.1", 0))
            number = port.getsockname()[1]
        self.base = f"http://127.0.0.1:{number}"
        self.log = (self.output / "native-app.log").open("a", encoding="utf-8")
        env = {**os.environ, "TAURI_WEBDRIVER_PORT": str(number)}
        self.process = subprocess.Popen([str(self.binary)], cwd=self.binary.parent, env=env, stdout=self.log, stderr=self.log)
        wait(lambda: self.request("/status"), "native embedded driver starts", 60)
        result = self.request("/session", {"capabilities": {"alwaysMatch": {"wdio:tauriServiceOptions": {"windowLabel": "main"}}}})
        self.session = result["sessionId"]
        self.capabilities = result["capabilities"]
        self.command("/window/rect", {"width": 1440, "height": 960})
        wait(lambda: self.js("return !!window.__TAURI_INTERNALS__ && document.querySelectorAll('.startup-intro').length === 0"), "native frontend and opening animation", 45)
        wait(lambda: any(value in self.text(".storage-indicator") for value in ("本地服务已连接", "已保存到本机")), "production sidecar connects", 60)
        assert self.command("/url").startswith(("tauri://", "http://tauri.localhost", "https://tauri.localhost")), "Must use packaged assets, never Vite/browser preview"

    def close(self, graceful=False):
        if graceful and self.process and self.process.poll() is None:
            try:
                self.command("/window", method="DELETE")
            except (OSError, ValueError):
                # The last window may exit before the HTTP response is flushed.
                pass
            self.process.wait(timeout=20)
        stop(self.process)
        if self.log:
            self.log.close()
        self.session = ""

    def js(self, script, *args):
        return self.command("/execute/sync", {"script": script, "args": list(args)})

    def element(self, selector, index=0, xpath=False):
        values = self.command("/elements", {"using": "xpath" if xpath else "css selector", "value": selector})
        if len(values) <= index:
            raise AssertionError(f"Missing UI element: {selector} #{index}")
        return values[index][ELEMENT]

    def click(self, selector, index=0, xpath=False):
        element = wait(lambda: self.element(selector, index, xpath), f"element {selector}")
        wait(lambda: self.command(f"/element/{element}/enabled"), f"enabled {selector}")
        self.command(f"/element/{element}/click", {})

    def button(self, text):
        self.click(f"//button[@aria-label={json.dumps(text, ensure_ascii=False)} or normalize-space(.)={json.dumps(text, ensure_ascii=False)}]", xpath=True)

    def fill(self, selector, value, index=0, xpath=False):
        element = self.element(selector, index, xpath)
        self.command(f"/element/{element}/clear", {})
        self.command(f"/element/{element}/value", {"text": value})
        def matches():
            actual = self.command(f"/element/{element}/property/value")
            return actual == value or (value.replace('.', '', 1).isdigit() and actual == str(float(value)))
        wait(matches, f"input value {selector}")

    def label(self, name, value, index=0):
        self.fill(f"//label[text()={json.dumps(name, ensure_ascii=False)}]/input", value, index, xpath=True)

    def text(self, selector):
        return self.js("return document.querySelector(arguments[0])?.textContent ?? ''", selector)

    def contains(self, selector, text):
        wait(lambda: text in self.text(selector), f"{selector} contains {text}")

    def absent(self, selector):
        wait(lambda: self.js("return document.querySelectorAll(arguments[0]).length === 0", selector), f"{selector} closes")

    def screenshot(self, name):
        data = base64.b64decode(self.command("/screenshot"), validate=True)
        assert data[:8] == b"\x89PNG\r\n\x1a\n", "Native screenshot must be PNG"
        width, height = struct.unpack(">II", data[16:24])
        assert width >= 900 and height >= 600, (width, height)
        path = self.output / name
        path.write_bytes(data)
        return {"file": name, "sha256": sha(path), "width": width, "height": height}


def build(target, work):
    extension = ".exe" if os.name == "nt" else ""
    original_sidecar = ROOT / "src-tauri/binaries" / f"supertravel-backend-{target}{extension}"
    hashes = {"productionSidecar": sha(original_sidecar)}
    hashes["frontendAssets"] = {str(path.relative_to(ROOT / "dist")): sha(path) for path in sorted((ROOT / "dist").rglob("*")) if path.is_file()}
    run(sys.executable, FIXTURE, "--prepare", work)
    # Do not touch the production PyInstaller dist/spec or src-tauri/binaries.
    run(sys.executable, FIXTURE, "--trust", work)
    try:
        run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--name", "supertravel-backend", "--distpath", work / "bin", "--workpath", work / "pyinstaller", "--specpath", work, "--paths", ROOT, "--collect-submodules", "uvicorn", ROOT / "backend/main.py")
    finally:
        run(sys.executable, FIXTURE, "--restore", work)
    assert sha(original_sidecar) == hashes["productionSidecar"], "Production sidecar must not be replaced by the fixture variant"
    identifier = "com.supertravel.desktop.nativegui.run" + uuid.uuid4().hex
    overrides = {"identifier": identifier, "app": {"security": {"capabilities": ["main-window", {"identifier": "native-gui-driver", "windows": ["main"], "permissions": ["wdio-webdriver:default"]}]}}, "bundle": {"active": False}}
    env = {**os.environ, "TAURI_CONFIG": json.dumps(overrides)}
    run("cargo", "build", "--manifest-path", "src-tauri/Cargo.toml", "--release", "--locked", "--target", target, "--features", "native-gui-tests,tauri/custom-protocol", env=env)
    binary = work / "bin" / f"supertravel-desktop{extension}"
    shutil.copy2(ROOT / "src-tauri/target" / target / "release" / binary.name, binary)
    hashes.update({"testApp": sha(binary), "testSidecar": sha(work / "bin" / f"supertravel-backend{extension}"), "testIdentifier": identifier})
    return binary, hashes


def business(view, fixture, report):
    checks, screenshots = report["checks"], report["screenshots"]
    view.start()
    report["capabilities"] = view.capabilities
    report["url"] = view.command("/url")
    view.button("设置与更新")
    data_dir = Path(view.text(".path-field span"))
    view.contains("body", "密钥保存在操作系统凭据库")
    view.label("模型服务地址", fixture["modelBaseUrl"])
    view.label("模型名称", fixture["model"])
    view.label("模型 API Key", fixture["apiKey"])
    view.button("保存模型设置")
    view.contains("body", "模型配置已保存。")
    assert view.js("return document.querySelector('input[type=password]').value") == ""
    view.button("测试模型连接")
    view.contains("body", "模型连接成功。")
    view.label("工具服务地址", fixture["toolsBaseUrl"])
    view.button("保存工具服务地址")
    view.contains("body", "工具服务地址已保存")
    view.button("检查工具服务")
    view.contains("body", "工具服务已连接")
    view.button("关闭窗口")
    checks.append("native_settings_model_keyring_and_shared_tools")

    view.button("旅程")
    view.label("会话名称", "原生 WebView 固定业务验收")
    view.button("创建本地会话")
    view.contains(".trip-title", "原生 WebView 固定业务验收")
    assert view.js("return document.querySelectorAll('.activity').length") == 0
    screenshots.append(view.screenshot("01-empty.png"))
    checks.append("create_empty_conversation_via_ui")

    def send(text):
        view.fill('[aria-label="旅行对话输入"]', text)
        view.button("发送消息")

    def saved():
        paths = list((data_dir / "conversations").glob("*.json"))
        assert len(paths) == 1, "Isolated native app must have exactly one test conversation"
        return json.loads(paths[0].read_text(encoding="utf-8"))

    send("你好")
    view.contains("body", "固定测试回复：你好，旅行者。")
    assert view.js("return document.querySelectorAll('.activity').length") == 0
    send("我想旅行")
    view.contains('[aria-label="Agent 任务进度"]', "需要你补充信息")
    view.contains("body", "你准备去哪个城市、旅行几天？")
    assert view.js("return document.querySelectorAll('.activity').length") == 0
    checks.append("planner_chat_and_clarification_before_generation")

    send("生成固定行程，杭州两天")
    view.contains(".activity-heading", "固定测试公园")
    view.contains('[aria-label="Agent 任务进度"]', "任务已完成")
    wait(lambda: view.js("return document.querySelectorAll('.real-map-marker').length") == 2, "two native map markers")
    view.click('[role="tab"]', 2)
    view.click('[role="tab"]', 1)
    view.click('[aria-label="查看固定测试博物馆"]')
    wait(lambda: view.js("return document.querySelector('.real-map-marker.is-selected')?.getAttribute('aria-label')") == "查看固定测试博物馆", "map marker selects place")
    formal = saved()
    assert len(formal["data"]["days"]) == 2 and formal["data"]["draft"] is None
    screenshots.append(view.screenshot("02-generated.png"))
    checks.append("planner_executor_generate_two_days_map_markers_and_selection")

    send("调整固定行程，放慢节奏")
    view.contains(".draft-label", "修改草稿")
    view.contains('[aria-label="Agent 任务进度"]', "任务已完成")
    pending = saved()
    assert pending["data"]["days"] == formal["data"]["days"], "Draft must not replace formal data"
    view.button("继续编辑草稿")
    view.label("活动费用（元）", "0")
    view.label("活动费用（元）", "12.50", 1)
    view.contains('[aria-label="行程费用"]', "¥12.50")
    view.label("时间", "09:30", 1)
    view.contains('dialog [role="alert"]', "时间")
    assert view.js("return [...document.querySelectorAll('dialog button')].find(e => e.textContent.trim() === '应用修改').disabled")
    assert saved()["data"]["days"] == formal["data"]["days"]
    screenshots.append(view.screenshot("03-draft-conflict.png"))
    view.label("时间", "11:30", 1)
    view.button("应用修改")
    view.absent("dialog")
    assert saved()["data"]["days"][0]["activities"][1]["cost"]["amountFen"] == 1250
    checks.append("chat_draft_does_not_overwrite_formal_costs_and_conflict_blocks_apply")

    view.button("编辑当天行程")
    view.label("地点", "固定测试花园")
    view.button("重新定位")
    view.contains(".editor-location", "固定测试花园地址")
    view.fill("dialog textarea", "原生 WebView 修改并保存的备注")
    view.button("应用修改")
    view.absent("dialog")
    view.button("保存行程")
    view.contains(".storage-indicator", "已保存到本机")
    before_restart = saved()
    activity = before_restart["data"]["days"][0]["activities"][0]
    assert activity["place"]["id"] == "baidu:fixture-garden"
    assert activity["description"] == "原生 WebView 修改并保存的备注"
    assert fixture["apiKey"] not in json.dumps(before_restart)
    screenshots.append(view.screenshot("04-saved.png"))
    checks.append("resolve_changed_place_apply_save_json_no_model_key")

    view.close(graceful=True)
    view.start()
    view.contains(".trip-title", "原生 WebView 固定业务验收")
    view.contains(".activity-heading", "固定测试花园")
    view.contains('[aria-label="行程费用"]', "¥12.50")
    assert saved()["data"] == before_restart["data"]
    wait(lambda: view.js("return document.querySelectorAll('.real-map-marker').length") == 2, "map restored after native restart")
    screenshots.append(view.screenshot("05-restored.png"))
    checks.append("full_native_process_restart_restores_conversation_trip_map_and_costs")
    view.button("设置与更新")
    view.button("删除 Key")
    view.contains("body", "API Key 已删除。")
    view.button("关闭窗口")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    if os.environ.get("CI") != "true":
        raise SystemExit("Native GUI acceptance is restricted to disposable CI runners; it creates test certificates and keychain entries.")
    work = ROOT / ".local/native-gui" / uuid.uuid4().hex
    work.mkdir(parents=True)
    output = ROOT / "artifacts" / args.target / "native-gui"
    output.mkdir(parents=True, exist_ok=True)
    artifacts = {path: sha(path) for path in output.parent.iterdir() if path.is_file()}
    report = {"schema": 1, "target": args.target, "platform": platform.system(), "status": "failed", "driver": "tauri-plugin-wdio-webdriver 1.5.0 / W3C", "webview": {"Windows": "WebView2", "Darwin": "WKWebView", "Linux": "WebKitGTK"}[platform.system()], "fixtures": "Local HTTPS OpenAI-format model and HTTP shared tools; not live provider acceptance", "buildScope": "Production Rust builder, frontend dist, Python modules; test-only driver and fixture CA; not the byte-identical published installer", "checks": [], "screenshots": []}
    view = None
    fixture_process = None
    try:
        binary, report["build"] = build(args.target, work)
        with (work / "fixture.log").open("w", encoding="utf-8") as log:
            fixture_process = subprocess.Popen([sys.executable, str(FIXTURE), "--run", str(work)], cwd=ROOT, stdout=log, stderr=log)
            ready = work / "ready.json"
            wait(ready.exists, "local fixture servers", 20)
            fixture = json.loads(ready.read_text(encoding="utf-8"))
            with credentials(work):
                view = WebView(binary, output)
                business(view, fixture, report)
                view.close(graceful=True)
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(fixture["toolsBaseUrl"] + "/test/status", timeout=5) as response:
                    report["fixtureCalls"] = json.load(response)
                assert all(report["fixtureCalls"][key] > 0 for key in ("modelCalls", "searchCalls", "routeCalls"))
        assert all(sha(path) == expected for path, expected in artifacts.items()), "Published native packages must remain byte-identical"
        report["checks"].append("production_release_assets_unchanged")
        report["status"] = "passed"
    except Exception as error:
        report["error"] = str(error)
        if view and view.session:
            try:
                report["screenshots"].append(view.screenshot("failure.png"))
                (output / "failure-dom.txt").write_text(view.text("body"), encoding="utf-8")
            except Exception:
                pass
        raise
    finally:
        if view:
            view.close()
        stop(fixture_process)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("SUPERTRAVEL_NATIVE_GUI_REPORT:" + json.dumps(report, ensure_ascii=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
