"""Local preferences: explicit saves, bounded reads and preserved original files."""
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from backend.main import ConversationInput, ConversationStore, StoreError, create_app
from backend.preferences import MAX_PREFERENCE_BYTES, PreferencesStore, TravelPreferences


TOKEN = "preferences-fixture"
AUTH = {"Authorization": "Bearer " + TOKEN}
DEFAULT = {"schema": 1, "enabled": False, "budgetAmountFen": None, "pace": None,
           "dietaryNotes": "", "companionMode": None}
SELECTED = {"schema": 1, "enabled": True, "budgetAmountFen": 300000, "pace": "relaxed",
            "dietaryNotes": "清淡，避免花生\n优先当地家常菜", "companionMode": "family"}
URL = "/api/travel-preferences"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in ("STA_MODEL_API_KEY", "STA_MODEL_BASE_URL", "STA_MODEL_NAME", "STA_MCP_URL", "STA_SHARED_TOOLS_URL",
                 "BAIDU_MAP_API_KEY", "AMAP_MAPS_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def preferences(tmp_path):
    return PreferencesStore(ConversationStore(tmp_path), StoreError)


def file_state(root):
    return {path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


def test_missing_file_returns_empty_defaults_without_writing_and_supports_no_store(preferences):
    before = file_state(preferences.store.root)
    assert preferences.read() == {"preferences": DEFAULT}
    assert preferences.snapshot() == (None, None)
    assert file_state(preferences.store.root) == before
    assert not preferences.path.exists()
    detached = PreferencesStore(None, None)
    assert detached.read() == {"preferences": DEFAULT}
    assert detached.snapshot() == (None, None)
    with pytest.raises(RuntimeError, match="没有本地数据目录"):
        detached.save(TravelPreferences())


def test_explicit_save_preserves_chinese_and_restart_backup_and_data_directory_isolation(preferences, tmp_path):
    first = preferences.save(TravelPreferences(**SELECTED))
    assert first == {"preferences": SELECTED}
    raw = preferences.path.read_bytes()
    assert "清淡" in raw.decode("utf-8")
    restarted = PreferencesStore(ConversationStore(tmp_path), StoreError)
    assert restarted.read() == first
    assert restarted.save(TravelPreferences(**{**SELECTED, "pace": "packed"}))["preferences"]["pace"] == "packed"
    assert preferences.path.with_suffix(".json.previous").read_bytes() == raw
    isolated = PreferencesStore(ConversationStore(tmp_path / "another-data-directory"), StoreError)
    assert isolated.snapshot() == (None, None)
    assert isolated.read() == {"preferences": DEFAULT}
    assert not isolated.path.exists()


@pytest.mark.parametrize("fields, active", [
    ({}, False), ({"enabled": True}, False), ({"enabled": True, "dietaryNotes": " \n\t"}, False),
    ({"enabled": False, "budgetAmountFen": 300000}, False),
    ({"enabled": True, "budgetAmountFen": 0}, True),
    ({"enabled": True, "pace": "balanced"}, True),
    ({"enabled": True, "companionMode": "solo"}, True),
    ({"enabled": True, "dietaryNotes": "少辣"}, True),
])
def test_only_enabled_nonempty_preferences_enter_snapshot(preferences, fields, active):
    selected = TravelPreferences(**fields)
    preferences.save(selected)
    snapshot, error = preferences.snapshot()
    assert error is None
    expected = selected.model_dump(exclude={"schema_version", "enabled"}) if active else None
    assert snapshot == expected
    if active:
        assert set(snapshot) == {"budgetAmountFen", "pace", "dietaryNotes", "companionMode"}


def test_unsaved_changes_do_not_apply_and_snapshot_is_independent(preferences):
    preferences.save(TravelPreferences(**SELECTED))
    old, error = preferences.snapshot()
    assert error is None
    form = preferences.read()["preferences"]
    form.update(dietaryNotes="未保存的饮食要求", budgetAmountFen=1)
    assert preferences.snapshot()[0] == old
    changed = TravelPreferences(**{**SELECTED, "pace": "packed", "budgetAmountFen": 500000})
    preferences.save(changed)
    latest, error = preferences.snapshot()
    assert latest["pace"] == "packed" and latest["budgetAmountFen"] == 500000
    assert old["pace"] == "relaxed" and old["budgetAmountFen"] == 300000
    old["dietaryNotes"] = "不应写入磁盘"
    assert preferences.read()["preferences"]["dietaryNotes"] == SELECTED["dietaryNotes"]
    preferences.save(TravelPreferences(**{**SELECTED, "enabled": False}))
    assert preferences.snapshot() == (None, None)
    assert preferences.read()["preferences"]["dietaryNotes"] == SELECTED["dietaryNotes"]
    preferences.save(TravelPreferences())
    assert preferences.read() == {"preferences": DEFAULT} and preferences.snapshot() == (None, None)


@pytest.mark.parametrize("raw", [b"broken-json SECRET", b"", b"null", b"[]", b'{"schema":2}',
                                 b'{"enabled":true,"budgetAmountFen":true}', b'{"unknown":"SECRET"}'])
def test_corrupt_reads_preserve_original_and_explicit_repairs_keep_unique_backup(preferences, raw):
    preferences.path.write_bytes(raw)
    before = file_state(preferences.store.root)
    result = preferences.read()
    assert result["preferences"] == DEFAULT and result["error"]
    assert "SECRET" not in result["error"]
    assert preferences.snapshot() == (None, result["error"])
    assert file_state(preferences.store.root) == before
    preferences.save(TravelPreferences(**SELECTED))
    archives = list(preferences.store.root.glob("travel-preferences.json.corrupt-*"))
    assert len(archives) == 1 and archives[0].read_bytes() == raw
    preferences.save(TravelPreferences())
    preferences.save(TravelPreferences(**SELECTED))
    assert archives[0].read_bytes() == raw
    assert preferences.read() == {"preferences": SELECTED}
    preferences.path.write_bytes(b"another-corrupt-original")
    preferences.save(TravelPreferences())
    archives = list(preferences.store.root.glob("travel-preferences.json.corrupt-*"))
    assert len(archives) == 2
    assert {path.read_bytes() for path in archives} == {raw, b"another-corrupt-original"}


def test_read_does_not_silently_restore_previous_preferences(preferences):
    preferences.save(TravelPreferences(**SELECTED))
    preferences.save(TravelPreferences())
    previous = preferences.path.with_suffix(".json.previous").read_bytes()
    preferences.path.write_bytes(b"broken")
    assert preferences.read()["preferences"] == DEFAULT
    assert preferences.snapshot()[0] is None
    assert preferences.path.read_bytes() == b"broken"
    assert preferences.path.with_suffix(".json.previous").read_bytes() == previous


@pytest.mark.parametrize("failure_target", ["backup", "current"])
def test_write_failures_keep_old_settings_and_snapshot(preferences, monkeypatch, failure_target):
    preferences.save(TravelPreferences(**SELECTED))
    before = preferences.path.read_bytes()
    atomic_replace = preferences.store._atomic_replace
    def fail(path, content):
        if (path == preferences.path) == (failure_target == "current"):
            raise OSError("SECRET disk details")
        atomic_replace(path, content)
    monkeypatch.setattr(preferences.store, "_atomic_replace", fail)
    with pytest.raises(StoreError) as rejected:
        preferences.save(TravelPreferences())
    assert rejected.value.status == 503 and rejected.value.detail["code"] == "preferences_save_failed"
    assert "SECRET" not in str(rejected.value.detail)
    assert preferences.path.read_bytes() == before
    assert preferences.read() == {"preferences": SELECTED}
    if failure_target == "current":
        assert preferences.path.with_suffix(".json.previous").read_bytes() == before


def test_corrupt_backup_failure_never_overwrites_damaged_original(preferences, monkeypatch):
    preferences.path.write_bytes(b"damaged-original")
    def fail(path, content):
        raise OSError("archive write denied")
    monkeypatch.setattr(preferences.store, "_atomic_replace", fail)
    with pytest.raises(StoreError):
        preferences.save(TravelPreferences())
    assert preferences.path.read_bytes() == b"damaged-original"


def test_oversized_file_is_bounded_and_cannot_be_overwritten(preferences):
    raw = b"x" * (MAX_PREFERENCE_BYTES + 1)
    preferences.path.write_bytes(raw)
    before = file_state(preferences.store.root)
    assert preferences.read()["preferences"] == DEFAULT
    assert "过大文件" in preferences.read()["error"]
    with pytest.raises(StoreError, match="手动处理过大文件"):
        preferences.save(TravelPreferences())
    assert file_state(preferences.store.root) == before


@pytest.mark.parametrize("is_backup", [False, True])
def test_symlink_guards_reject_read_or_save_without_following_target(preferences, monkeypatch, is_backup):
    preferences.save(TravelPreferences(**SELECTED))
    before = file_state(preferences.store.root)
    original = Path.is_symlink
    protected = preferences.path.with_suffix(".json.previous") if is_backup else preferences.path
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == protected or original(path))
    if not is_backup:
        assert preferences.read()["preferences"] == DEFAULT
        assert "符号链接" in preferences.read()["error"]
        assert preferences.snapshot()[0] is None
    with pytest.raises(StoreError, match="符号链接"):
        preferences.save(TravelPreferences())
    assert file_state(preferences.store.root) == before


def test_unreadable_file_does_not_hide_error_or_overwrite_existing_preferences(preferences, monkeypatch):
    preferences.save(TravelPreferences(**SELECTED))
    before = preferences.path.read_bytes()
    def fail():
        raise PermissionError("SECRET OS detail")
    monkeypatch.setattr(preferences, "_read_raw", fail)
    result = preferences.read()
    assert result["preferences"] == DEFAULT and result["error"]
    assert "SECRET" not in result["error"]
    with pytest.raises(StoreError):
        preferences.save(TravelPreferences())
    assert preferences.path.read_bytes() == before


@pytest.mark.parametrize("field, value", [
    ("budgetAmountFen", True), ("budgetAmountFen", -1), ("budgetAmountFen", 100000001),
    ("budgetAmountFen", 0.1), ("budgetAmountFen", "300000"),
    ("enabled", 1), ("enabled", "true"), ("schema", True), ("schema", "1"), ("schema", 2),
    ("pace", "slow"), ("companionMode", "亲子"), ("dietaryNotes", None),
    ("dietaryNotes", "食" * 1001), ("apiKey", "SECRET"),
])
def test_api_strictly_rejects_invalid_fields_without_writing(tmp_path, field, value):
    app = create_app(tmp_path, TOKEN)
    with TestClient(app, headers=AUTH) as client:
        before = file_state(tmp_path)
        response = client.put(URL, json={**SELECTED, field: value})
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "invalid_request"
        assert "SECRET" not in response.text
        assert file_state(tmp_path) == before


def test_api_roundtrip_restart_auth_and_zero_upper_boundary(tmp_path):
    app = create_app(tmp_path, TOKEN)
    with TestClient(app) as client:
        assert client.get(URL).status_code == 401
        assert client.put(URL, json=SELECTED).status_code == 401
        assert client.get(URL, headers=AUTH).json() == {"preferences": DEFAULT}
        for budget in (0, 100000000):
            selected = {**SELECTED, "budgetAmountFen": budget, "dietaryNotes": "食" * 1000}
            response = client.put(URL, headers=AUTH, json=selected)
            assert response.status_code == 200 and response.json() == {"preferences": selected}
    with TestClient(create_app(tmp_path, TOKEN), headers=AUTH) as client:
        assert client.get(URL).json() == {"preferences": selected}
        assert client.put(URL, json=DEFAULT).json() == {"preferences": DEFAULT}
        assert client.get(URL).json() == {"preferences": DEFAULT}


def test_api_corrupt_error_and_save_failure_do_not_report_success(tmp_path, monkeypatch):
    path = tmp_path / "travel-preferences.json"
    path.write_bytes(b"broken-original")
    app = create_app(tmp_path, TOKEN)
    with TestClient(app, headers=AUTH) as client:
        result = client.get(URL).json()
        assert result["preferences"] == DEFAULT and result["error"]
        def fail(path, content):
            raise OSError("SECRET disk details")
        monkeypatch.setattr(app.state.store, "_atomic_replace", fail)
        response = client.put(URL, json=SELECTED)
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "preferences_save_failed"
        assert "SECRET" not in response.text and "preferences" not in response.json()
        assert path.read_bytes() == b"broken-original"


def test_saving_preferences_does_not_modify_conversations_or_configurations(tmp_path):
    app = create_app(tmp_path, TOKEN)
    document = app.state.store.create(ConversationInput(title="已有会话", data={"messages": [], "days": [], "draft": None}))
    (tmp_path / "unrelated-settings.json").write_bytes(b'{"model":"unchanged"}')
    with TestClient(app, headers=AUTH) as client:
        before = file_state(tmp_path)
        assert client.put(URL, json=SELECTED).status_code == 200
        assert client.put(URL, json=DEFAULT).status_code == 200
        after = file_state(tmp_path)
        assert all(after[path] == state for path, state in before.items())
        assert app.state.store.get(document.id).model_dump() == document.model_dump()
