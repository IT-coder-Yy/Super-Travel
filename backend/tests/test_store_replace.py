"""Real-file checks for the narrowly bounded Windows atomic-replace retry."""
import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import main as backend


def windows_permission(code):
    error = PermissionError(errno.EACCES, "injected replace denial")
    error.winerror = code
    return error


def patch_replace(monkeypatch, replace, *, platform="nt", fsync=os.fsync):
    delays = []
    monkeypatch.setattr(backend, "os", SimpleNamespace(
        name=platform, replace=replace, fsync=fsync, unlink=os.unlink,
        open=os.open, close=os.close, O_RDONLY=os.O_RDONLY,
    ))
    monkeypatch.setattr(backend, "time", SimpleNamespace(sleep=delays.append))
    return delays


def existing(tmp_path):
    store = backend.ConversationStore(tmp_path / "真实文件 测试")
    document = store.create(backend.ConversationInput(title="原会话", data={"value": "原内容"}))
    target = store._path(document.id)
    return store, document, target, target.read_bytes()


def update(store, document):
    return store.update(document.id, backend.ConversationUpdate(title="新会话", data={"value": "新内容"}, expected_revision=document.revision))


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_one_windows_denial_reuses_same_temporary_file_then_replaces(monkeypatch, tmp_path, winerror):
    store, document, target, original = existing(tmp_path)
    attempts = []

    def replace(source, destination):
        if Path(destination) == target:
            attempts.append(Path(source))
            assert target.read_bytes() == original
            assert json.loads(Path(source).read_bytes())["data"] == {"value": "新内容"}
            if len(attempts) == 1:
                raise windows_permission(winerror)
        os.replace(source, destination)

    delays = patch_replace(monkeypatch, replace)
    changed = update(store, document)
    assert delays == [0.02]
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert store.get(document.id) == changed and changed.revision == document.revision + 1
    assert (store.backups / target.name).read_bytes() == original
    assert not list(store.root.rglob("*.tmp"))


def test_permanent_windows_denial_is_bounded_and_preserves_original(monkeypatch, tmp_path):
    store, document, target, original = existing(tmp_path)
    attempts = []

    def replace(source, destination):
        if Path(destination) == target:
            attempts.append(Path(source))
            assert target.read_bytes() == original
            raise windows_permission(5)
        os.replace(source, destination)

    delays = patch_replace(monkeypatch, replace)
    with pytest.raises(backend.StoreError) as error:
        update(store, document)
    assert error.value.detail["code"] == "save_failed"
    assert error.value.__cause__.winerror == 5
    assert len(attempts) == 4 and len(set(attempts)) == 1
    assert delays == [0.02, 0.05, 0.1]
    assert target.read_bytes() == original
    assert (store.backups / target.name).read_bytes() == original
    assert not list(store.root.rglob("*.tmp"))


@pytest.mark.parametrize("platform,kind,code", [("posix", "permission", 5), ("nt", "permission", 17), ("nt", "permission", None), ("nt", "disk_full", 112)])
def test_non_windows_or_other_errors_fail_without_retry(monkeypatch, tmp_path, platform, kind, code):
    store, _document, target, original = existing(tmp_path)
    attempts = []

    def replace(source, destination):
        attempts.append(Path(source))
        if kind == "permission":
            raise windows_permission(code)
        error = OSError(errno.ENOSPC, "injected full disk")
        error.winerror = code
        raise error

    delays = patch_replace(monkeypatch, replace, platform=platform)
    with pytest.raises(OSError):
        store._atomic_replace(target, b'{"new":true}')
    assert len(attempts) == 1 and delays == []
    assert target.read_bytes() == original
    assert not list(store.root.rglob("*.tmp"))


def test_windows_denial_during_flush_is_not_retried(monkeypatch, tmp_path):
    store, _document, target, original = existing(tmp_path)
    attempts = []

    def flush(_descriptor):
        raise windows_permission(5)

    delays = patch_replace(monkeypatch, lambda *args: attempts.append(args), fsync=flush)
    with pytest.raises(PermissionError):
        store._atomic_replace(target, b'{"new":true}')
    assert not attempts and not delays
    assert target.read_bytes() == original
    assert not list(store.root.rglob("*.tmp"))
