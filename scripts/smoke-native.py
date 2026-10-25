"""Smoke a real macOS .app or Linux AppImage, including its own backend.

Linux CI: xvfb-run -a python scripts/smoke-native.py <file.AppImage> --version 0.1.0
macOS CI: python scripts/smoke-native.py <SuperTravel.app> --version 0.1.0

This checks process survival, an owned loopback backend, and parent-exit cleanup.
It does not assert rendered UI, macOS Gatekeeper approval, or an in-app Quit event:
the desktop process receives SIGTERM. A runner without a usable GUI must fail.
Only stdlib, ps, and macOS's lsof are needed. No credentials are read from processes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    group: int
    state: str
    birth: str
    command: str


def parse_processes(output: str) -> dict[int, Process]:
    result = {}
    for line in output.splitlines():
        fields = line.split(None, 9)
        if len(fields) == 10:
            process = Process(int(fields[0]), int(fields[1]), int(fields[2]), fields[3], " ".join(fields[4:9]), fields[9])
            result[process.pid] = process
    return result


def process_table() -> dict[int, Process]:
    result = subprocess.run(["ps", "-ww", "-axo", "pid=,ppid=,pgid=,stat=,lstart=,comm="], capture_output=True, text=True, check=True, timeout=5, env={**os.environ, "LC_ALL": "C"})
    return parse_processes(result.stdout)


def descendants(root: int, table: dict[int, Process]) -> set[int]:
    found = {root}
    while True:
        children = {item.pid for item in table.values() if item.parent in found}
        if children <= found:
            return found - {root}
        found |= children


def executable_name(process: Process) -> str:
    if sys.platform.startswith("linux"):
        try:
            return Path(os.readlink(f"/proc/{process.pid}/exe")).name
        except OSError:
            return ""
    return Path(process.command).name


def linux_ports(pid: int) -> set[int]:
    inodes = set()
    try:
        for entry in Path(f"/proc/{pid}/fd").iterdir():
            try:
                link = os.readlink(entry)
            except OSError:
                continue
            if link.startswith("socket:["):
                inodes.add(link[8:-1])
        lines = Path(f"/proc/{pid}/net/tcp").read_text().splitlines()[1:]
    except (FileNotFoundError, ProcessLookupError):
        return set()
    ports = set()
    for line in lines:
        fields = line.split()
        if len(fields) >= 10 and fields[3] == "0A" and fields[9] in inodes:
            address, port = fields[1].split(":")
            if address == "0100007F":
                ports.add(int(port, 16))
    return ports


def parse_lsof_ports(output: str, pid: int) -> set[int]:
    owner = None
    ports = set()
    for line in output.splitlines():
        if line.startswith("p"):
            owner = int(line[1:])
        elif owner == pid and line.startswith("n127.0.0.1:"):
            ports.add(int(line.rsplit(":", 1)[1]))
    return ports


def listening_ports(pid: int) -> set[int]:
    if sys.platform.startswith("linux"):
        return linux_ports(pid)
    result = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-Fpn"], capture_output=True, text=True, timeout=5)
    if result.returncode not in (0, 1):
        raise RuntimeError("lsof failed while inspecting this application's backend")
    return parse_lsof_ports(result.stdout, pid)


def launch_command(artifact: Path) -> tuple[list[str], str]:
    if sys.platform == "darwin":
        if artifact.suffix != ".app" or not artifact.is_dir():
            raise ValueError("macOS requires an unpacked .app bundle, not a .dmg")
        with (artifact / "Contents" / "Info.plist").open("rb") as handle:
            name = plistlib.load(handle)["CFBundleExecutable"]
        if not isinstance(name, str) or Path(name).name != name:
            raise ValueError("Invalid CFBundleExecutable")
        executable = artifact / "Contents" / "MacOS" / name
        command = [str(executable)]
    elif sys.platform.startswith("linux"):
        if not os.environ.get("DISPLAY"):
            raise ValueError("Linux needs a GUI display; run this script under xvfb-run -a")
        if not artifact.is_file() or artifact.suffix != ".AppImage":
            raise ValueError("Linux requires the built .AppImage")
        executable, name = artifact, "supertravel-desktop"
        command = [str(executable), "--appimage-extract-and-run"]
    else:
        raise ValueError("This smoke script supports native macOS and Linux runners only")
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError(f"Application is missing or not executable: {executable}")
    for tool in (["ps", "lsof"] if sys.platform == "darwin" else ["ps"]):
        if not shutil.which(tool):
            raise ValueError(f"Required operating-system tool is unavailable: {tool}")
    return command, name


def run_smoke(artifact: Path, version: str, timeout: float, hold: float, shutdown_timeout: float) -> None:
    command, main_name = launch_command(artifact)
    opener = build_opener(ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix="supertravel-native-") as temporary:
        directory = Path(temporary)
        env = dict(os.environ)
        for key, child in (("HOME", "home"), ("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache")):
            location = directory / child
            location.mkdir()
            env[key] = str(location)
        # Never forward release signing secrets to the application being exercised.
        for key in ("TAURI_SIGNING_PRIVATE_KEY", "TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "STA_BACKEND_TOKEN", "STA_APP_VERSION", "PYTHONHOME", "PYTHONPATH"):
            env.pop(key, None)
        log_path = directory / "native.log"
        with log_path.open("wb") as log:
            child = subprocess.Popen(command, cwd=directory, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            known: dict[int, str] = {}

            def owned() -> dict[int, Process]:
                child.poll()  # Reap the launcher; do not count our own unreaped zombie.
                table = process_table()
                found = {pid for pid, birth in known.items() if pid in table and table[pid].birth == birth}
                found |= {item.pid for item in table.values() if item.group == child.pid}
                for pid in list(found):
                    found |= descendants(pid, table)
                for pid in found:
                    known[pid] = table[pid].birth
                return {pid: table[pid] for pid in found}

            def send_owned(pid: int, sig: signal.Signals) -> None:
                table = process_table()
                current = table.get(pid)
                if current and known.get(pid) == current.birth and not current.state.startswith("Z"):
                    try:
                        os.kill(pid, sig)
                    except ProcessLookupError:
                        pass

            def health(port: int) -> bool:
                try:
                    with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1) as response:
                        result = json.loads(response.read(4096))
                    return result == {"status": "ok", "version": version}
                except (OSError, URLError, ValueError):
                    return False

            try:
                deadline, healthy_since = time.monotonic() + timeout, None
                main_pid, backend_pid, port = None, None, None
                while time.monotonic() < deadline:
                    table = owned()
                    if not table:
                        raise RuntimeError(f"Desktop exited before readiness (launcher exit={child.poll()})")
                    match = None
                    for process in table.values():
                        if process.state.startswith("Z") or executable_name(process) != main_name:
                            continue
                        for pid in descendants(process.pid, table):
                            if executable_name(table[pid]) != "supertravel-backend":
                                continue
                            for candidate in listening_ports(pid):
                                if health(candidate):
                                    match = process.pid, pid, candidate
                                    break
                    if match:
                        if match != (main_pid, backend_pid, port):
                            main_pid, backend_pid, port = match
                            healthy_since = time.monotonic()
                        if time.monotonic() - healthy_since >= hold:
                            break
                    else:
                        healthy_since = None
                        main_pid, backend_pid, port = None, None, None
                    time.sleep(0.25)
                else:
                    raise RuntimeError("No stable desktop process with its own healthy loopback backend before timeout")
                print(f"Native startup passed: desktop PID {main_pid}, backend PID {backend_pid}, 127.0.0.1:{port}, version {version}", flush=True)
                # End only the main desktop process; killing the whole group here would mask orphan bugs.
                send_owned(main_pid, signal.SIGTERM)
                deadline = time.monotonic() + shutdown_timeout
                while time.monotonic() < deadline:
                    remaining = owned()
                    if not remaining:
                        break
                    time.sleep(0.25)
                else:
                    raise RuntimeError(f"Application left processes after parent exit: {sorted(remaining)}")
                print("Native parent-exit cleanup passed: no owned processes remain.", flush=True)
            except BaseException:
                log.flush()
                with log_path.open("rb") as diagnostic:
                    diagnostic.seek(max(0, log_path.stat().st_size - 6000))
                    print(diagnostic.read().decode("utf-8", errors="replace"), file=sys.stderr)
                raise
            finally:
                # A failed assertion stays failed even when this emergency cleanup succeeds.
                for sig, grace in ((signal.SIGTERM, 3), (signal.SIGKILL, 3)):
                    try:
                        remaining = owned()
                    except (OSError, subprocess.SubprocessError) as error:
                        if child.poll() is None:
                            child.terminate()
                            try:
                                child.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                child.kill()
                                child.wait(timeout=3)
                        raise RuntimeError("Process inspection failed; stopped the launched process but descendant cleanup could not be verified") from error
                    for pid in remaining:
                        send_owned(pid, sig)
                    deadline = time.monotonic() + grace
                    while remaining and time.monotonic() < deadline:
                        time.sleep(0.1)
                        remaining = owned()
                if remaining:
                    raise RuntimeError(f"Could not clean up owned PIDs: {sorted(remaining)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--timeout", type=float, default=75)
    parser.add_argument("--hold-seconds", type=float, default=3)
    parser.add_argument("--shutdown-timeout", type=float, default=15)
    args = parser.parse_args()
    if min(args.timeout, args.hold_seconds, args.shutdown_timeout) <= 0:
        parser.error("timeouts and hold duration must be positive")
    try:
        run_smoke(args.artifact.resolve(), args.version, args.timeout, args.hold_seconds, args.shutdown_timeout)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Native smoke failed: {error}") from error


if __name__ == "__main__":
    main()
