"""Desktop process entry point: bind loopback, lock data, and watch the parent."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import sys
import threading

import uvicorn

from supertravel_server.app import VERSION, create_app


def default_data_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "SuperTravelDesktop"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "SuperTravelDesktop"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "SuperTravelDesktop"


class DesktopServer(uvicorn.Server):
    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started and not self.should_exit and sockets:
            print(json.dumps({"event": "ready", "port": sockets[0].getsockname()[1], "version": VERSION}), flush=True)


@contextmanager
def data_directory_lock(directory: Path):
    with (directory / ".backend.lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def main() -> int:
    parser = argparse.ArgumentParser(description="SuperTravel Desktop local backend")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1"])
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.data_dir is not None and not args.data_dir.is_absolute():
        parser.error("--data-dir must be an absolute path")
    token = os.environ.get("STA_BACKEND_TOKEN", "")
    if not token:
        print("STA_BACKEND_TOKEN is required.", file=sys.stderr)
        return 2
    try:
        app = create_app(args.data_dir or default_data_dir(), token)
        with data_directory_lock(app.state.store.root), socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind((args.host, args.port))
            listener.listen(128)
            server = DesktopServer(uvicorn.Config(app, host=args.host, port=args.port, loop="asyncio", http="h11", ws="none", access_log=False, log_level="warning", timeout_graceful_shutdown=5))

            def watch_parent() -> None:
                try:
                    while sys.stdin.buffer.read(1):
                        pass
                finally:
                    server.should_exit = True

            threading.Thread(target=watch_parent, daemon=True, name="parent-stdin").start()
            server.run(sockets=[listener])
        return 0
    except (OSError, ValueError):
        print("Backend startup failed: verify the data directory and local port.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
