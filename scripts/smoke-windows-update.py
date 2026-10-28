"""Pre-release Windows updater/NSIS rehearsal on a disposable GitHub-hosted runner.

Uses the actual signed candidate package. Does not claim an old-client GUI click
or desktop restart: those are checked separately after the public Release exists.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import unquote, urlparse

spec = importlib.util.spec_from_file_location("native_update", Path(__file__).with_name("smoke-native-update.py"))
native_update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native_update)
ROOT = Path(__file__).resolve().parents[1]
PLATFORM = "windows-x86_64-nsis"


def require_hosted_windows() -> Path:
    if sys.platform != "win32" or os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise ValueError("This installer rehearsal requires a disposable GitHub-hosted Windows runner")
    return Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)


def product_version(executable: Path) -> str:
    quoted = str(executable).replace("'", "''")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                             "$ErrorActionPreference='Stop'; (Get-Item -LiteralPath '" + quoted + "').VersionInfo.ProductVersion"],
                            check=True, capture_output=True, text=True, timeout=15)
    return result.stdout.strip()


def require_probe_result(result, mode: str) -> None:
    result.check_returncode()
    marker = "SUPERTRAVEL_UPDATE_PROBE_VERIFIED:success" if mode == "success" else f"SUPERTRAVEL_UPDATE_PROBE_OK:{mode}"
    if marker not in result.stdout.splitlines():
        raise RuntimeError(f"The native updater did not prove its {mode} result")


def exercise(args) -> dict:
    runner_temp = require_hosted_windows()
    metadata = json.loads((args.artifact_dir / "platform.json").read_text(encoding="utf-8"))
    version = metadata["version"]
    if metadata["platform"] != PLATFORM or version == args.old_version:
        raise ValueError("Expected a newer Windows NSIS candidate")
    public_key = os.environ.get("STA_UPDATER_PUBLIC_KEY", "").strip()
    if not public_key:
        raise ValueError("The public verification key is required; no private key is used")
    name = unquote(urlparse(metadata["url"]).path.rsplit("/", 1)[-1])
    if not name or Path(name).name != name:
        raise ValueError("Unsafe candidate filename")
    package = args.artifact_dir / name
    if not any(item["name"] == name and item["sha256"] == native_update.release.digest(package) for item in metadata["files"]):
        raise ValueError("Candidate checksum mismatch")
    expected = {
        "supertravel-desktop.exe": ROOT / "src-tauri/target/x86_64-pc-windows-msvc/release/supertravel-desktop.exe",
        "supertravel-backend.exe": ROOT / "src-tauri/binaries/supertravel-backend-x86_64-pc-windows-msvc.exe",
    }
    expected_hashes = {name: native_update.release.digest(path) for name, path in expected.items()}
    prefix = f"https://github.com/{args.repository}/releases/download/v{args.old_version}"
    with tempfile.TemporaryDirectory(prefix="supertravel-win-update-", dir=runner_temp) as temporary:
        root = Path(temporary)
        previous_manifest, sums = root / "old.json", root / "SHA256SUMS.txt"
        native_update.download(prefix + "/latest.json", previous_manifest, prefix)
        native_update.download(prefix + "/SHA256SUMS.txt", sums, prefix)
        previous = json.loads(previous_manifest.read_text(encoding="utf-8"))
        if previous["version"] != args.old_version:
            raise ValueError("Published old version mismatch")
        old_url = previous["platforms"][PLATFORM]["url"]
        old_name = unquote(urlparse(old_url).path.rsplit("/", 1)[-1])
        if not old_name or Path(old_name).name != old_name:
            raise ValueError("Unsafe old package filename")
        old_package = root / old_name
        native_update.download(old_url, old_package, prefix)
        if f"{native_update.release.digest(old_package)}  {old_name}" not in sums.read_text().splitlines():
            raise ValueError("Published old package checksum mismatch")
        install = root / "installed"
        subprocess.run([str(old_package), "/S", "/NS", f"/D={install}"], check=True, timeout=120)
        executable = install / "supertravel-desktop.exe"
        if product_version(executable) != args.old_version:
            raise RuntimeError("The old installer did not install the expected version")
        snapshot = native_update.make_profile(root / "profile")
        # Windows does not honour an APPDATA override for Tauri's known-folder API.
        generated_config = next(path for path in snapshot if path.name == "desktop.json")
        config = Path(os.environ["APPDATA"]) / "com.supertravel.desktop/desktop.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        with config.open("xb") as handle:
            handle.write(snapshot.pop(generated_config))
        snapshot[config] = config.read_bytes()
        old_hash = native_update.fingerprint(install)
        state = {"mode": "tampered"}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path == "/latest.json":
                    body = json.dumps({"version": version, "platforms": {PLATFORM: {"signature": metadata["signature"], "url": f"http://127.0.0.1:{self.server.server_port}/package"}}}).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                elif self.path == "/package":
                    self.send_response(200); self.send_header("Content-Length", str(package.stat().st_size)); self.end_headers()
                    with package.open("rb") as stream:
                        first = stream.read(4096)
                        self.wfile.write(bytes([first[0] ^ 1]) + first[1:] if state["mode"] == "tampered" else first)
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
            for key in ("TAURI_SIGNING_PRIVATE_KEY", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "STA_BACKEND_TOKEN", "STA_APP_VERSION"):
                env.pop(key, None)
            for mode in ("tampered", "interrupted", "success"):
                state["mode"] = mode
                probe = root / "probe.json"
                probe.write_text(json.dumps({"executable": str(executable), "endpoint": f"http://127.0.0.1:{server.server_port}/latest.json", "public_key": public_key,
                                             "platform": PLATFORM, "old_version": args.old_version, "new_version": version, "mode": mode}), encoding="utf-8")
                result = subprocess.run([str(args.driver), str(probe)], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=150)
                print(result.stdout, end="", flush=True)
                print(result.stderr, end="", file=sys.stderr, flush=True)
                if result.returncode:
                    print(f"Windows probe exited with 0x{result.returncode & 0xffffffff:08X}; collecting read-only loader evidence", flush=True)
                    diagnostics = args.artifact_dir / "windows-update-diagnostics"
                    diagnostics.mkdir(exist_ok=True)
                    copies = []
                    for label, source in (("updater-probe.exe", args.driver), ("candidate-desktop.exe", expected["supertravel-desktop.exe"]),
                                          ("published-old-desktop.exe", executable)):
                        copies.append(shutil.copy2(source, diagnostics / label))
                    try:
                        diagnostic = subprocess.run([sys.executable, str(ROOT / "scripts/diagnose-windows-loader.py"),
                                                     str(diagnostics / "windows-loader-report.json"), *map(str, copies)], env=env, timeout=120)
                        print(f"Loader diagnostic exit code: {diagnostic.returncode}", flush=True)
                    except (OSError, subprocess.SubprocessError) as error:
                        print(f"Loader diagnostic could not finish: {type(error).__name__}", flush=True)
                require_probe_result(result, mode)
                native_update.assert_profile(snapshot)
                if mode != "success" and native_update.fingerprint(install) != old_hash:
                    raise RuntimeError("Rejected update changed the old installation")
            # The Windows plugin exits when NSIS starts, before installation finishes.
            deadline = time.monotonic() + 120
            while True:
                try:
                    installed = all(native_update.release.digest(install / name) == digest for name, digest in expected_hashes.items())
                except OSError:
                    installed = False
                if installed and product_version(executable) == version:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("NSIS did not install the exact candidate binaries")
                time.sleep(1)
            subprocess.run([sys.executable, str(ROOT / "scripts/smoke-sidecar.py"), str(install / "supertravel-backend.exe"), "--version", version], check=True, env=env, timeout=120)
            native_update.assert_profile(snapshot)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            # Only remove our exact synthetic preference; never overwrite another writer.
            if config.read_bytes() != snapshot[config]:
                raise RuntimeError("Synthetic preference changed; retained for inspection")
            config.unlink()
        return {"platform": PLATFORM, "from": args.old_version, "to": version, "driver": "CI-only native Tauri updater API and NSIS",
                "old_client_gui_tested": False, "old_installed_executable_version": "passed", "corrupt_download_rejected_old_install_preserved": "passed",
                "interrupted_download_rejected_old_install_preserved": "passed", "signed_native_install_exact_candidate_binaries": "passed",
                "installed_sidecar_startup_and_persistence": "passed", "conversation_trip_draft_and_config_bytes_preserved": "passed",
                "desktop_gui_startup_and_restart": "requires separate manual GUI validation"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--driver", type=Path, required=True)
    parser.add_argument("--old-version", default="0.1.1")
    parser.add_argument("--repository", default="IT-coder-Yy/SuperTravelAgent")
    args = parser.parse_args()
    args.artifact_dir, args.driver = args.artifact_dir.resolve(), args.driver.resolve()
    report = exercise(args)
    (args.artifact_dir / "windows-update-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
