"""M1 local JSON API. No model or Agent is connected in this milestone."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hmac
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator
import uvicorn


VERSION = os.environ.get("STA_APP_VERSION", "0.1.0")
MAX_BYTES = 2 * 1024 * 1024
ALLOWED_ORIGINS = [
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
]


class ConversationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    data: dict[str, Any]

    @field_validator("data")
    @classmethod
    def json_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError("data must contain finite JSON values") from exc
        return value


class ConversationUpdate(ConversationInput):
    expected_revision: int = Field(ge=1)


class Conversation(ConversationInput):
    id: UUID
    revision: int = Field(ge=1)
    updated_at: AwareDatetime


class ConversationSummary(BaseModel):
    id: UUID
    title: str
    revision: int
    updated_at: AwareDatetime


class StoreError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.detail = {"code": code, "message": message, **extra}


class ConversationStore:
    def __init__(self, data_dir: Path):
        self.root = data_dir.expanduser().resolve()
        self.conversations = self.root / "conversations"
        self.backups = self.root / "backups"
        self._locks: dict[UUID, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        for directory in (self.conversations, self.backups):
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=directory) as probe:
                probe.write(b"writable")
                probe.flush()
                os.fsync(probe.fileno())

    def _lock(self, conversation_id: UUID) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(conversation_id, threading.RLock())

    def _path(self, conversation_id: UUID) -> Path:
        return self.conversations / f"{conversation_id}.json"

    def _read(self, conversation_id: UUID) -> tuple[Conversation, bytes]:
        path = self._path(conversation_id)
        try:
            if path.is_symlink():
                raise StoreError(503, "unsafe_file", "会话文件不能是符号链接。")
            with path.open("rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("oversized document")
            document = Conversation.model_validate_json(raw)
            if document.id != conversation_id:
                raise ValueError("document ID does not match its filename")
            return document, raw
        except FileNotFoundError as exc:
            raise StoreError(404, "not_found", "会话不存在。") from exc
        except (ValidationError, ValueError, RecursionError) as exc:
            raise StoreError(503, "corrupt_conversation", "会话文件损坏或格式不兼容，已保留原件。") from exc
        except OSError as exc:
            raise StoreError(503, "storage_unavailable", "无法读取本地会话，请检查数据目录。") from exc

    @staticmethod
    def _atomic_replace(path: Path, payload: bytes) -> None:
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
                temporary = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
            if os.name != "nt":
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def _save(self, document: Conversation, previous: bytes | None = None) -> Conversation:
        payload = document.model_dump_json().encode("utf-8")
        if len(payload) > MAX_BYTES:
            raise StoreError(413, "document_too_large", "会话文件超过 2 MiB 限制。")
        try:
            if previous is not None:
                self._atomic_replace(self.backups / f"{document.id}.json", previous)
            self._atomic_replace(self._path(document.id), payload)
        except OSError as exc:
            raise StoreError(503, "save_failed", "保存失败，原文件或上一份有效备份已保留。") from exc
        return document

    def create(self, payload: ConversationInput) -> Conversation:
        document = Conversation(**payload.model_dump(), id=uuid4(), revision=1, updated_at=datetime.now(timezone.utc))
        with self._lock(document.id):
            return self._save(document)

    def get(self, conversation_id: UUID) -> Conversation:
        with self._lock(conversation_id):
            return self._read(conversation_id)[0]

    def update(self, conversation_id: UUID, payload: ConversationUpdate) -> Conversation:
        with self._lock(conversation_id):
            current, previous = self._read(conversation_id)
            if payload.expected_revision != current.revision:
                raise StoreError(409, "revision_conflict", "会话已被更新，请重新加载。", current_revision=current.revision)
            document = Conversation(id=current.id, title=payload.title, data=payload.data, revision=current.revision + 1, updated_at=datetime.now(timezone.utc))
            return self._save(document, previous)

    def summaries(self) -> list[ConversationSummary]:
        summaries = []
        try:
            paths = list(self.conversations.glob("*.json"))
        except OSError as exc:
            raise StoreError(503, "storage_unavailable", "无法读取会话目录。") from exc
        for path in paths:
            try:
                conversation_id = UUID(path.stem)
            except ValueError:
                continue
            document = self.get(conversation_id)
            summaries.append(ConversationSummary(**document.model_dump(exclude={"data"})))
        return sorted(summaries, key=lambda item: item.updated_at, reverse=True)


class LocalAccessMiddleware:
    def __init__(self, app: Any, token: str):
        self.app = app
        self.token = token.encode("utf-8")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        origin = headers.get(b"origin")
        if origin is not None and origin.decode("latin-1") not in ALLOWED_ORIGINS:
            await JSONResponse(status_code=403, content={"detail": {"code": "origin_denied", "message": "请求来源不受允许。"}})(scope, receive, send)
            return
        public_health = scope["path"] == "/api/health" and scope["method"] == "GET"
        authorization = headers.get(b"authorization", b"")
        if not public_health and not hmac.compare_digest(authorization, b"Bearer " + self.token):
            await JSONResponse(status_code=401, headers={"WWW-Authenticate": "Bearer"}, content={"detail": {"code": "unauthorized", "message": "需要有效的本地应用令牌。"}})(scope, receive, send)
            return
        if scope["method"] in {"POST", "PUT"}:
            chunks = []
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > MAX_BYTES:
                    await JSONResponse(status_code=413, content={"detail": {"code": "request_too_large", "message": "请求超过 2 MiB 限制。"}})(scope, receive, send)
                    return
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            delivered = False

            async def replay_body() -> dict[str, Any]:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, replay_body, send)
            return
        await self.app(scope, receive, send)


def create_app(data_dir: Path, token: str) -> FastAPI:
    if not token:
        raise ValueError("STA_BACKEND_TOKEN must be supplied by the desktop process")
    store = ConversationStore(data_dir)
    app = FastAPI(title="SuperTravel Desktop", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.add_middleware(LocalAccessMiddleware, token=token)
    app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["GET", "POST", "PUT"], allow_headers=["Authorization", "Content-Type"])

    @app.exception_handler(StoreError)
    async def storage_error(_request: Any, exc: StoreError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Any, exc: RequestValidationError) -> JSONResponse:
        fields = [".".join(map(str, error["loc"])) for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": {"code": "invalid_request", "message": "请求字段格式不正确。", "fields": fields}})

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": VERSION}

    @app.get("/api/conversations", response_model=list[ConversationSummary])
    def list_conversations() -> list[ConversationSummary]:
        return store.summaries()

    @app.post("/api/conversations", response_model=Conversation, status_code=201)
    def create_conversation(payload: ConversationInput) -> Conversation:
        return store.create(payload)

    @app.get("/api/conversations/{conversation_id}", response_model=Conversation)
    def get_conversation(conversation_id: UUID) -> Conversation:
        return store.get(conversation_id)

    @app.put("/api/conversations/{conversation_id}", response_model=Conversation)
    def update_conversation(conversation_id: UUID, payload: ConversationUpdate) -> Conversation:
        return store.update(conversation_id, payload)

    return app


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
