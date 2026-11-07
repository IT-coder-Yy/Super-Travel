"""Conversation validation, atomic JSON storage, and backup recovery."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator

from supertravel_server.agent_service import AgentService
from supertravel_server.candidates import merge_candidate_fields, normalize_candidate_fields
from supertravel_server.trip_api import guard_trip_write

MAX_BYTES = 2 * 1024 * 1024


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
    @field_validator("data")
    @classmethod
    def candidate_defaults(cls, value):
        return normalize_candidate_fields(value)

    id: UUID
    revision: int = Field(ge=1)
    updated_at: AwareDatetime


class ConversationSummary(BaseModel):
    id: UUID
    title: str
    revision: int
    updated_at: AwareDatetime


class RecoveryOption(BaseModel):
    id: UUID
    title: str
    message: str
    backup_updated_at: AwareDatetime | None = None
    token: str | None = None


class RecoveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    token: str = Field(pattern=r"^[a-f0-9]{64}$")


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

    @staticmethod
    def _read_bytes(path: Path) -> bytes:
        try:
            if path.is_symlink():
                raise StoreError(503, "unsafe_file", "会话文件不能是符号链接。")
            with path.open("rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise StoreError(503, "corrupt_conversation", "会话文件超过 2 MiB，已保留原件，请手动检查。")
            return raw
        except FileNotFoundError as exc:
            raise StoreError(404, "not_found", "会话不存在。") from exc
        except OSError as exc:
            raise StoreError(503, "storage_unavailable", "无法读取本地会话，请检查数据目录。") from exc

    @staticmethod
    def _validate(conversation_id: UUID, raw: bytes) -> Conversation:
        try:
            document = Conversation.model_validate_json(raw)
            if document.id != conversation_id:
                raise ValueError("document ID does not match its filename")
            return document
        except (ValidationError, ValueError, RecursionError) as exc:
            raise StoreError(503, "corrupt_conversation", "会话文件损坏或格式不兼容，已保留原件。") from exc

    def _read(self, conversation_id: UUID, path: Path | None = None) -> tuple[Conversation, bytes]:
        raw = self._read_bytes(path or self._path(conversation_id))
        return self._validate(conversation_id, raw), raw

    @staticmethod
    def _atomic_replace(path: Path, payload: bytes) -> None:
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
                temporary = handle.name
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # Windows can briefly deny replacement; retry only the already-written file.
            for delay in (0.02, 0.05, 0.1, None):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError as exc:
                    if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33) or delay is None:
                        raise
                    time.sleep(delay)
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
        guard_trip_write({}, payload.data, StoreError)
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
            data = merge_candidate_fields(current.data, payload.data)
            guard_trip_write(current.data, data, StoreError)
            if AgentService._active(current.data) and any(data.get(key) != current.data.get(key) for key in ("days", "draft", "draftTrip", "candidates", "draftCandidates")):
                raise StoreError(409, "trip_busy", "请等待当前任务结束或取消后，再修改行程。")
            if "agentPending" in current.data:
                data = {**data, **{key: current.data[key] for key in ("messages", "agent", "agentPending") if key in current.data}}
            document = Conversation(id=current.id, title=payload.title, data=data, revision=current.revision + 1, updated_at=datetime.now(timezone.utc))
            return self._save(document, previous)

    def mutate(self, conversation_id: UUID, change, *, expected_revision: int | None = None) -> Conversation:
        with self._lock(conversation_id):
            current, previous = self._read(conversation_id)
            if expected_revision is not None and current.revision != expected_revision:
                raise StoreError(409, "revision_conflict", "会话已被更新，请重新加载。", current_revision=current.revision)
            data = current.data
            change(data)
            document = Conversation(id=current.id, title=current.title, data=data, revision=current.revision + 1, updated_at=datetime.now(timezone.utc))
            return self._save(document, previous)

    def _ids(self) -> list[UUID]:
        try:
            paths = list(self.conversations.glob("*.json"))
        except OSError as exc:
            raise StoreError(503, "storage_unavailable", "无法读取会话目录。") from exc
        identifiers = []
        for path in paths:
            try:
                identifiers.append(UUID(path.stem))
            except ValueError:
                continue
        return identifiers

    def summaries(self) -> list[ConversationSummary]:
        summaries = []
        for conversation_id in self._ids():
            try:
                document = self.get(conversation_id)
            except StoreError:
                continue
            summaries.append(ConversationSummary(**document.model_dump(exclude={"data"})))
        return sorted(summaries, key=lambda item: item.updated_at, reverse=True)

    def _recovery_state(self, conversation_id: UUID) -> tuple[RecoveryOption, bytes, Conversation | None] | None:
        option = RecoveryOption(id=conversation_id, title=f"{conversation_id}.json", message="")
        try:
            raw = self._read_bytes(self._path(conversation_id))
        except StoreError as exc:
            option.message = exc.detail["message"] + " 无法自动恢复。"
            return option, b"", None
        try:
            self._validate(conversation_id, raw)
            return None
        except StoreError:
            pass
        try:
            backup, backup_raw = self._read(conversation_id, self.backups / f"{conversation_id}.json")
        except StoreError:
            option.message = "会话已损坏，未找到有效备份，无法自动恢复。原文件保持不变。"
            return option, raw, None
        option.title = backup.title
        option.message = "会话已损坏，可从上一份有效备份恢复。"
        option.backup_updated_at = backup.updated_at
        option.token = hashlib.sha256(raw + b"\0" + backup_raw).hexdigest()
        return option, raw, backup

    def recovery_options(self) -> list[RecoveryOption]:
        options = []
        for conversation_id in self._ids():
            with self._lock(conversation_id):
                state = self._recovery_state(conversation_id)
                if state is not None:
                    options.append(state[0])
        return options

    def recover(self, conversation_id: UUID, token: str) -> Conversation:
        with self._lock(conversation_id):
            state = self._recovery_state(conversation_id)
            if state is None or state[0].token is None or not hmac.compare_digest(state[0].token, token):
                raise StoreError(409, "recovery_changed", "会话或备份已变化，请重新扫描后再恢复。")
            _, original, backup = state
            assert backup is not None
            now = datetime.now(timezone.utc)
            # 损坏原件的修订号不可读，新的基线避免旧窗口继续写入原修订号。
            document = backup.model_copy(update={"revision": max(backup.revision + 1, int(now.timestamp() * 1000)), "updated_at": now})
            if AgentService._active(document.data):
                previous_run = document.data.get("agent")
                document.data = {**document.data, "agentPending": False, "agent": {**(previous_run if isinstance(previous_run, dict) else {}), "status": "interrupted", "content": "备份中的任务已中断，可重新发送需求继续。"}}
            try:
                archive = self.root / "recovered-originals"
                if archive.is_symlink():
                    raise OSError("unsafe archive directory")
                archive.mkdir(exist_ok=True)
                self._atomic_replace(archive / f"{conversation_id}-{uuid4()}.json", original)
            except OSError as exc:
                raise StoreError(503, "save_failed", "无法保留损坏原件，恢复未执行。") from exc
            latest = self._recovery_state(conversation_id)
            if latest is None or latest[0].token != token:
                raise StoreError(409, "recovery_changed", "会话或备份已变化，请重新扫描后再恢复。")
            return self._save(document)
