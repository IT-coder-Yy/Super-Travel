"""Exercise the real native updater against an isolated published old installation.

The CI-only Rust example uses the locked updater plugin's public API. This is
not old-client GUI automation: Windows covers the actual settings-button flow.
macOS/Linux evidence covers old/new native startup, real verification/install,
corrupt/interrupted download rejection, and byte-for-byte profile preservation.
"""
from __future__ import annotations

import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(file))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


native = load_module("native_smoke", "smoke-native.py")
release = load_module("prepare_release", "prepare-release.py")


def download(url: str, destination: Path, prefix: str) -> None:
    if not url.startswith(prefix + "/"):
        raise ValueError("Published artifact URL does not belong to the expected public Release")
    request = Request(url, headers={"User-Agent": "SuperTravel-native-update-check"})
    with urlopen(request, timeout=90) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path.rglob("*")) if path.is_dir() else [path]
    for entry in files:
        digest.update(str(entry.relative_to(path) if path.is_dir() else entry.name).encode())
        if entry.is_symlink():
            digest.update(os.readlink(entry).encode())
        elif entry.is_file():
            digest.update(bytes.fromhex(release.digest(entry)))
    return digest.hexdigest()


def make_profile(profile: Path) -> dict[Path, bytes]:
    custom = profile / "用户 行程"
    conversations = custom / "conversations"
    conversations.mkdir(parents=True)
    config = (profile / "home/Library/Application Support" if sys.platform == "darwin" else profile / "config") / "com.supertravel.desktop/desktop.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    activity = {"id": "bridge", "title": "更新前的行程", "time": "09:30", "duration": "1 小时", "description": "升级保留的备注", "x": 1010, "y": 235}
    day = {"day": 1, "title": "杭州", "subtitle": "升级保留测试", "activities": [activity]}
    draft = {**day, "activities": [{**activity, "title": "尚未应用的草稿"}]}
    conversation = {"id": "13cd4818-f20c-4f75-8e47-aa5ad295e260", "title": "原生升级保留测试", "revision": 2, "updated_at": "2026-10-26T00:00:00Z", "data": {
        "schema": 1, "selectedDay": 1, "days": [day], "draft": [draft], "favorites": ["bridge"],
        "messages": [{"id": "before-update", "role": "user", "content": "升级后保留的旅行对话"}],
    }}
    files = {config: {"dataDirectory": str(custom)}, conversations / f"{conversation['id']}.json": conversation}
    for path, value in files.items():
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return {path: path.read_bytes() for path in files}


def assert_profile(snapshot: dict[Path, bytes]) -> None:
    for path, expected in snapshot.items():
        if not path.is_file() or path.read_bytes() != expected:
            raise RuntimeError(f"The upgrade changed the preserved profile file: {path.name}")


def exercise(args) -> dict:
    platform = json.loads((args.artifact_dir / "platform.json").read_text())["platform"]
    if (sys.platform == "darwin") != platform.startswith("darwin-") or sys.platform not in ("darwin", "linux"):
        raise ValueError("This test must run on the artifact's native macOS/Linux runner")
    metadata = json.loads((args.artifact_dir / "platform.json").read_text())
    version = metadata["version"]
    if version == args.old_version:
        raise ValueError("Old and new versions must differ")
    public_key = os.environ.get("STA_UPDATER_PUBLIC_KEY", "").strip()
    if not public_key:
        raise ValueError("Public updater key is required; no private key is used by this test")
    new_name = unquote(urlparse(metadata["url"]).path.rsplit("/", 1)[-1])
    if Path(new_name).name != new_name:
        raise ValueError("Unsafe artifact filename")
    new_package = args.artifact_dir / new_name
    if not any(item["name"] == new_name and item["sha256"] == release.digest(new_package) for item in metadata["files"]):
        raise ValueError("Current package does not match collected artifact checksum")
    prefix = f"https://github.com/{args.repository}/releases/download/v{args.old_version}"
    with tempfile.TemporaryDirectory(prefix="supertravel-upgrade-") as temporary:
        root = Path(temporary)
        profile = root / "profile"
        snapshot = make_profile(profile)
        old_manifest = root / "old-manifest.json"
        checksums = root / "SHA256SUMS.txt"
        download(prefix + "/latest.json", old_manifest, prefix)
        download(prefix + "/SHA256SUMS.txt", checksums, prefix)
        previous = json.loads(old_manifest.read_text())
        if previous["version"] != args.old_version:
            raise ValueError("Published old manifest version mismatch")
        old_url = previous["platforms"][platform]["url"]
        old_name = unquote(urlparse(old_url).path.rsplit("/", 1)[-1])
        if Path(old_name).name != old_name:
            raise ValueError("Unsafe old artifact filename")
        old_package = root / old_name
        download(old_url, old_package, prefix)
        if f"{release.digest(old_package)}  {old_name}" not in checksums.read_text().splitlines():
            raise ValueError("Published old package checksum mismatch")
        install_dir = root / "installed"
        install_dir.mkdir()
        if sys.platform == "darwin":
            with tarfile.open(old_package) as archive:
                archive.extractall(install_dir, filter="data")
            matches = list(install_dir.glob("*.app"))
            if len(matches) != 1:
                raise ValueError("Old macOS update package must contain one .app")
            installed = matches[0]
            executable = Path(native.launch_command(installed)[0][0])
        else:
            installed = install_dir / "SuperTravel.AppImage"
            shutil.copy2(old_package, installed)
            installed.chmod(0o755)
            executable = installed
        native.run_smoke(installed, args.old_version, 75, 2, 15, profile)
        if not (profile / "用户 行程/.backend.lock").is_file():
            raise RuntimeError("Old desktop did not use the configured isolated data directory")
        assert_profile(snapshot)
        old_hash = fingerprint(installed)
        state = {"mode": "tampered"}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path == "/latest.json":
                    body = json.dumps({"version": version, "platforms": {platform: {"signature": metadata["signature"], "url": f"http://127.0.0.1:{self.server.server_port}/package"}}}).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                elif self.path == "/package":
                    length = new_package.stat().st_size
                    self.send_response(200); self.send_header("Content-Length", str(length)); self.end_headers()
                    with new_package.open("rb") as stream:
                        first = stream.read(4096)
                        if state["mode"] == "tampered":
                            first = bytes([first[0] ^ 1]) + first[1:]
                        self.wfile.write(first)
                        if state["mode"] == "interrupted":
                            self.wfile.flush(); self.close_connection = True
                            self.connection.shutdown(socket.SHUT_RDWR)
                        else:
                            shutil.copyfileobj(stream, self.wfile)
                else:
                    self.send_error(404)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            env = dict(os.environ)
            for key, child in (("HOME", "home"), ("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache")):
                env[key] = str(profile / child)
            for key in ("TAURI_SIGNING_PRIVATE_KEY", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "STA_BACKEND_TOKEN", "STA_APP_VERSION"):
                env.pop(key, None)
            for mode in ("tampered", "interrupted", "success"):
                state["mode"] = mode
                probe = root / "probe.json"
                probe.write_text(json.dumps({"executable": str(executable), "endpoint": f"http://127.0.0.1:{server.server_port}/latest.json", "public_key": public_key, "platform": platform, "old_version": args.old_version, "new_version": version, "mode": mode}), encoding="utf-8")
                result = subprocess.run([str(args.driver), str(probe)], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=150)
                print(result.stdout, end="", flush=True)
                print(result.stderr, end="", file=sys.stderr, flush=True)
                result.check_returncode()
                if f"SUPERTRAVEL_UPDATE_PROBE_OK:{mode}" not in result.stdout.splitlines():
                    raise RuntimeError(f"Native updater exited without proving the {mode} assertion")
                assert_profile(snapshot)
                if mode != "success" and fingerprint(installed) != old_hash:
                    raise RuntimeError(f"The {mode} attempt changed the installed old version")
            if fingerprint(installed) == old_hash:
                raise RuntimeError("Successful updater did not replace the installation")
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
        native.run_smoke(installed, version, 75, 2, 15, profile)
        assert_profile(snapshot)
        return {"platform": platform, "from": args.old_version, "to": version, "driver": "CI-only native Tauri updater API", "old_client_gui_tested": False, "old_native_startup": "passed", "corrupt_download_rejected_old_install_preserved": "passed", "interrupted_download_rejected_old_install_preserved": "passed", "signed_native_install": "passed", "new_native_startup_and_exit": "passed", "conversation_trip_draft_and_config_bytes_preserved": "passed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--driver", type=Path, required=True)
    parser.add_argument("--old-version", default="0.1.0")
    parser.add_argument("--repository", default="IT-coder-Yy/Super-Travel")
    args = parser.parse_args()
    args.artifact_dir, args.driver = args.artifact_dir.resolve(), args.driver.resolve()
    report = exercise(args)
    (args.artifact_dir / "native-update-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
