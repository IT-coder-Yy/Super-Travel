"""Explicitly saved, local travel defaults; never rewrites conversation data."""
import threading
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, ValidationError


MAX_PREFERENCE_BYTES = 16 * 1024
PREFERENCE_FIELDS = ("budgetAmountFen", "pace", "dietaryNotes", "companionMode")
CORRUPT_MESSAGE = "常用旅行偏好文件损坏或格式不兼容，原件已保留，本次不使用默认偏好；可在设置中重新保存或清空。"
READ_MESSAGE = "无法安全读取常用旅行偏好，原件已保留，本次不使用默认偏好；请检查数据目录，并手动处理过大文件或符号链接。"


class TravelPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, serialize_by_alias=True)
    schema_version: int = Field(default=1, alias="schema", ge=1, le=1)
    enabled: bool = False
    budgetAmountFen: int | None = Field(default=None, ge=0, le=100000000)
    pace: Literal["relaxed", "balanced", "packed"] | None = None
    dietaryNotes: str = Field(default="", max_length=1000)
    companionMode: Literal["solo", "couple", "family", "friends"] | None = None


class PreferencesStore:
    def __init__(self, store, error_type):
        self.store, self.error_type = store, error_type
        self.path = store.root / "travel-preferences.json" if store is not None else None
        self._lock = threading.RLock()

    def _read_raw(self):
        if self.path is None:
            return None
        if self.path.is_symlink():
            raise OSError("unsafe preference path")
        try:
            with self.path.open("rb") as file:
                raw = file.read(MAX_PREFERENCE_BYTES + 1)
        except FileNotFoundError:
            return None
        if len(raw) > MAX_PREFERENCE_BYTES:
            raise OSError("preference file too large")
        return raw

    def read(self):
        with self._lock:
            try:
                raw = self._read_raw()
                preferences = TravelPreferences.model_validate_json(raw) if raw is not None else TravelPreferences()
                return {"preferences": preferences.model_dump()}
            except ValidationError:
                return {"preferences": TravelPreferences().model_dump(), "error": CORRUPT_MESSAGE}
            except OSError:
                return {"preferences": TravelPreferences().model_dump(), "error": READ_MESSAGE}

    def snapshot(self):
        result = self.read()
        preferences = result["preferences"]
        if result.get("error"):
            return None, result["error"]
        if not preferences["enabled"] or not any(preferences[key].strip() if isinstance(preferences[key], str)
                                                else preferences[key] is not None for key in PREFERENCE_FIELDS):
            return None, None
        return {key: preferences[key] for key in PREFERENCE_FIELDS}, None

    def save(self, preferences: TravelPreferences):
        if self.store is None:
            raise RuntimeError("当前运行没有本地数据目录，无法保存常用旅行偏好。")
        encoded = preferences.model_dump_json().encode("utf-8")
        with self._lock:
            try:
                raw = self._read_raw()
                if raw is not None:
                    try:
                        TravelPreferences.model_validate_json(raw)
                        backup = self.path.with_suffix(".json.previous")
                    except ValidationError:
                        backup = self.path.with_name(f"travel-preferences.json.corrupt-{uuid4().hex}")
                    if backup.is_symlink():
                        raise OSError("unsafe preference backup")
                    self.store._atomic_replace(backup, raw)
                self.store._atomic_replace(self.path, encoded)
            except OSError:
                raise self.error_type(503, "preferences_save_failed", "常用旅行偏好未保存，旧文件已保留；请检查目录权限，并手动处理过大文件或符号链接。") from None
        return {"preferences": preferences.model_dump()}


def preference_router(preferences):
    router = APIRouter(prefix="/api/travel-preferences")

    @router.get("")
    def read_preferences():
        return preferences.read()

    @router.put("")
    def save_preferences(payload: TravelPreferences):
        return preferences.save(payload)

    return router
