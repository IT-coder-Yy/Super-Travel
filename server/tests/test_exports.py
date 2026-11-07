"""Formal-trip exports never query upstream services or modify stored state."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from supertravel_server.app import create_app
from .test_trip_api import AUTH, app, create, trip_result


def itinerary():
    result = trip_result("正式备注")
    stops = result["days"][0]["activities"]
    first = stops[0]
    first["cost"] = {"amountFen": 0, "currency": "CNY", "source": "user"}
    for number, amount in [(2, 1234), (3, None)]:
        stop = deepcopy(first)
        stop.update(id=f"stop-{number}", title=f"正式地点{number}", time=f"{number + 8}:30")
        stop["place"].update(id=f"baidu:place{number}", name=stop["title"])
        stop.pop("cost")
        if amount is not None:
            stop["cost"] = {"amountFen": amount, "currency": "CNY", "source": "user"}
        stops.append(stop)
    stops[1]["incomingRoute"] = {"fromPlaceId": first["place"]["id"], "toPlaceId": stops[1]["place"]["id"],
        "mode": "walking", "status": "verified", "durationMinutes": 13, "distanceMeters": 842,
        "provider": "baidu", "queriedAt": datetime.now(timezone.utc).isoformat()}
    result["trip"]["transportMode"] = "walking"
    result["days"].append({"day": 2, "title": "自由日", "subtitle": "保留空日", "activities": []})
    return result


def saved(client, value=None, **extra):
    document = create(client, real=False)
    return client.app.state.store.mutate(UUID(document["id"]),
        lambda data: data.update(**(itinerary() if value is None else value), **extra)).model_dump(mode="json")


def export(client, document, **payload):
    return client.post(f"/api/conversations/{document['id']}/trip/export", headers=AUTH,
                       json={"expected_revision": document["revision"], **payload})


def file_state(root):
    return {path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


def test_formal_snapshot_costs_routes_empty_day_and_read_only(app, monkeypatch):
    with TestClient(app) as client:
        document = saved(client)
        before = file_state(app.state.store.root)
        def forbidden(*args, **kwargs):
            pytest.fail("export must not query upstream or write storage")
        monkeypatch.setattr(app.state.store, "mutate", forbidden)
        monkeypatch.setattr(app.state.store, "_save", forbidden)
        monkeypatch.setattr("supertravel_server.trip_routes.query_route", forbidden)
        monkeypatch.setattr("supertravel_server.trip_api.refresh_routes", forbidden)
        response = export(client, document)
        assert response.status_code == 200, response.text
        result = response.json()
        assert set(result) == {"revision", "filename", "markdown"}
        assert result["revision"] == document["revision"]
        assert result["filename"] == "杭州-行程.md"
        text = result["markdown"]
        for expected in ("正式行程概览", "第 1 天 3 项活动；第 2 天自由安排", "快照会话版本：2", "行程生成时间：", "导出生成时间：",
                         "正式备注", "固定测试地址", "约 60 分钟", "免费，用户填写", "¥12.34", "费用未知项：1 项",
                         "已查询估算：步行约 13 分钟，842 米", "供应商：百度地图", "查询时间：", "自由安排", "第 2 天"):
            assert expected in text
        assert "正式修订" not in text
        assert file_state(app.state.store.root) == before


@pytest.mark.parametrize("export_format", ["markdown", "pdf"])
def test_draft_candidates_chat_settings_and_internal_fields_are_excluded(app, export_format):
    with TestClient(app) as client:
        real = itinerary()
        draft = deepcopy(real["days"])
        draft[0]["activities"][0]["description"] = "DRAFT_ONLY_SECRET"
        document = saved(client, real, draft=draft, draftTrip={**real["trip"], "summary": "DRAFT_SUMMARY_SECRET"},
            candidates=[{"id": "candidate", "description": "CANDIDATE_SECRET"}],
            draftCandidates=[{"id": "candidate", "description": "DRAFT_CANDIDATE_SECRET"}],
            previousTrip={"days": [], "trip": {"summary": "PREVIOUS_SECRET"}},
            messages=[{"id": "message", "role": "user", "content": "CHAT_SECRET"}],
            modelSettings={"apiKey": "API_KEY_SECRET"}, photo="https://IMAGE_SECRET", internal="INTERNAL_SECRET")
        path = app.state.store._path(UUID(document["id"]))
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["title"] = "PRIVATE_CHAT_TITLE"
        path.write_text(json.dumps(raw), encoding="utf-8")
        response = export(client, document, format=export_format)
        assert response.status_code == 200, response.text
        result = response.json()
        assert "正式备注" in result["markdown"]
        for excluded in ("SECRET", "PRIVATE_CHAT_TITLE", "latitude", "longitude", "sourceLocation", "coordinateSystem",
                         "baidu:fixturepoi", "30.2", "120.1", "fixture-stop"):
            assert excluded not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("export_format", ["markdown", "pdf"])
def test_old_schema_defaults_do_not_rewrite_json_and_survive_restart(app, export_format):
    with TestClient(app) as client:
        real = itinerary()
        for day in real["days"]:
            for activity in day["activities"]:
                activity.pop("cost", None)
                activity.pop("incomingRoute", None)
        real["trip"].pop("transportMode")
        document = saved(client, real)
        path = app.state.store._path(UUID(document["id"]))
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["data"].pop("candidates")
        raw["data"].pop("draftCandidates")
        path.write_text(json.dumps(raw), encoding="utf-8")
    with TestClient(create_app(app.state.store.root, "trip-api-fixture")) as client:
        before = file_state(app.state.store.root)
        response = export(client, document, format=export_format)
        assert response.status_code == 200, response.text
        text = response.json()["markdown"]
        assert "费用未知项：3 项" in text
        assert "已知费用小计（人民币，用户填写）：¥0.00" in text
        assert "免费，用户填写" not in text
        assert "无完整路线记录" in text
        assert file_state(app.state.store.root) == before


@pytest.mark.parametrize("change, expected", [
    ({"queriedAt": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}, "已过期或查询时间无效"),
    ({"queriedAt": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()}, "已过期或查询时间无效"),
    ({"fromPlaceId": "baidu:wrong"}, "与当前地点或交通方式不匹配"),
    ({"mode": "driving"}, "与当前地点或交通方式不匹配"),
    ({"provider": "amap"}, "与当前地点或交通方式不匹配"),
])
@pytest.mark.parametrize("export_format", ["markdown", "pdf"])
def test_stale_mismatched_or_future_routes_are_not_verified(app, change, expected, export_format):
    with TestClient(app) as client:
        real = itinerary()
        real["days"][0]["activities"][1]["incomingRoute"].update(change)
        response = export(client, saved(client, real), format=export_format)
        assert response.status_code == 200, response.text
        text = response.json()["markdown"]
        assert expected in text and "查询时间：" in text
        assert "已查询估算" not in text and "13 分钟" not in text


@pytest.mark.parametrize("cost, route", [
    ({}, {"status": "verified", "durationMinutes": 13}),
    ({"amountFen": True}, {"status": "verified", "queriedAt": "invalid"}),
    ({"amountFen": -1}, None),
    ({"amountFen": 10, "source": "provider"}, None),
])
@pytest.mark.parametrize("export_format", ["markdown", "pdf"])
def test_incomplete_or_invalid_optional_evidence_is_unknown(app, cost, route, export_format):
    with TestClient(app) as client:
        real = trip_result()
        real["days"][0]["activities"][0]["cost"] = cost
        second = deepcopy(real["days"][0]["activities"][0])
        second.update(id="second", title="另一地点", time="11:00", incomingRoute=route)
        second["place"].update(id="baidu:second", name="另一地点")
        real["days"][0]["activities"].append(second)
        response = export(client, saved(client, real), format=export_format)
        assert response.status_code == 200, response.text
        text = response.json()["markdown"]
        assert "费用未知项：2 项" in text and "无完整路线记录" in text
        assert "已查询估算" not in text and "免费，用户填写" not in text


@pytest.mark.parametrize("export_format", ["markdown", "pdf"])
def test_unknown_route_preserves_safe_reason_query_time_and_provider(app, export_format):
    with TestClient(app) as client:
        real = itinerary()
        route = real["days"][0]["activities"][1]["incomingRoute"]
        route.update(status="unknown", reason="服务额度不足，请出发前确认。")
        route.pop("durationMinutes")
        route.pop("distanceMeters")
        text = export(client, saved(client, real), format=export_format).json()["markdown"]
        assert "步行耗时未知 / 待确认" in text
        assert "服务额度不足，请出发前确认。" in text and "供应商：百度地图" in text


@pytest.mark.parametrize("kind", ["empty", "draft_only", "sample", "all_empty", "missing_place"])
@pytest.mark.parametrize("export_format", ["markdown", "pdf"])
def test_export_requires_nonempty_real_formal_trip(app, kind, export_format):
    with TestClient(app) as client:
        real = trip_result()
        if kind == "empty":
            value = {"days": []}
        elif kind == "draft_only":
            value = {"days": [], "draft": real["days"], "draftTrip": real["trip"]}
        elif kind == "sample":
            value = {"days": real["days"]}
        elif kind == "all_empty":
            real["days"][0]["activities"] = []
            value = real
        else:
            real["days"][0]["activities"][0].pop("place")
            value = real
        document = saved(client, value)
        before = file_state(app.state.store.root)
        response = export(client, document, format=export_format)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "trip_export_unavailable"
        assert file_state(app.state.store.root) == before


@pytest.mark.parametrize("export_format", ["markdown", "pdf"])
def test_revision_conflict_requires_refresh_without_export_or_mutation(app, export_format):
    with TestClient(app) as client:
        document = saved(client)
        before = file_state(app.state.store.root)
        response = export(client, document, format=export_format, expected_revision=document["revision"] - 1)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "revision_conflict"
        assert response.json()["detail"]["current_revision"] == document["revision"]
        assert "markdown" not in response.json()
        assert file_state(app.state.store.root) == before


@pytest.mark.parametrize("export_format", ["markdown", "pdf"])
def test_one_store_read_keeps_snapshot_even_if_later_revision_changes(app, monkeypatch, export_format):
    with TestClient(app) as client:
        document = saved(client)
        original = app.state.store.get
        calls = []
        def one_read(identifier):
            calls.append(identifier)
            snapshot = original(identifier)
            app.state.store.mutate(identifier, lambda data: data["days"][0]["activities"][0].update(description="NEWER_REVISION"))
            return snapshot
        monkeypatch.setattr(app.state.store, "get", one_read)
        response = export(client, document, format=export_format)
        assert response.status_code == 200, response.text
        assert len(calls) == 1
        assert response.json()["revision"] == document["revision"]
        assert "NEWER_REVISION" not in response.json()["markdown"]
        assert original(UUID(document["id"])).revision == document["revision"] + 1


def test_chinese_special_text_is_escaped_without_losing_note_lines(app):
    with TestClient(app) as client:
        real = trip_result()
        unsafe = "中文第一行\n# 伪标题\n\n<script>alert(1)</script> [链接](https://evil.test) ![图片](x) `代码` | *强调* &lt;"
        real["trip"]["summary"] = unsafe
        stop = real["days"][0]["activities"][0]
        stop["description"] = unsafe
        stop["title"] = stop["place"]["name"] = "中文 [地点] <b>"
        stop["place"]["address"] = "地址\n# 假标题"
        response = export(client, saved(client, real))
        assert response.status_code == 200, response.text
        text = response.json()["markdown"]
        assert "- 备注：中文第一行  \n  \\# 伪标题" in text
        assert "&lt;script&gt;" in text and "&amp;lt;" in text
        assert "\\[链接\\]\\(https\\://evil\\.test\\)" in text
        assert "<script>" not in text and "\n# 伪标题" not in text and "![图片]" not in text
        assert "中文 \\[地点\\] &lt;b&gt;" in text


@pytest.mark.parametrize("destination", ["../杭州:西湖*?\\<景区>|", "CON", "😀", "杭" * 60])
def test_filename_is_short_safe_and_not_chat_title(app, destination):
    with TestClient(app) as client:
        real = trip_result()
        real["trip"]["destination"] = destination
        response = export(client, saved(client, real))
        assert response.status_code == 200, response.text
        filename = response.json()["filename"]
        assert filename.endswith("-行程.md") and len(filename) <= 38
        assert not any(char in filename for char in '/\\:*?"<>|')
        assert "固定测试会话" not in filename and ".." not in filename


@pytest.mark.parametrize("payload", [{}, {"expected_revision": 0}, {"expected_revision": True},
                                      {"expected_revision": "2"}, {"expected_revision": 2, "days": []}])
def test_request_is_strict_and_does_not_accept_client_snapshot(app, payload):
    with TestClient(app) as client:
        document = saved(client)
        response = client.post(f"/api/conversations/{document['id']}/trip/export", headers=AUTH, json=payload)
        assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_request"


def test_export_requires_auth_and_existing_conversation(app):
    with TestClient(app) as client:
        url = f"/api/conversations/{uuid4()}/trip/export"
        assert client.post(url, json={"expected_revision": 1}).status_code == 401
        assert client.post(url, headers=AUTH, json={"expected_revision": 1}).status_code == 404


@pytest.mark.parametrize("export_format", ["markdown", "pdf"])
def test_legacy_meta_defaults_to_walking_and_rejects_transit_evidence(app, export_format):
    with TestClient(app) as client:
        real = itinerary()
        real["trip"].pop("transportMode")
        real["days"][0]["activities"][1]["incomingRoute"]["mode"] = "transit"
        response = export(client, saved(client, real), format=export_format)
        assert response.status_code == 200, response.text
        text = response.json()["markdown"]
        assert "与当前地点或交通方式不匹配" in text
        assert "已查询估算" not in text


def test_multiline_notes_keep_internal_spacing_and_old_summary_is_excluded(app):
    with TestClient(app) as client:
        real = trip_result()
        real["trip"]["summary"] = "概述  保留空格\n第二行"
        real["days"][0]["activities"][0]["description"] = "备注  保留空格\n下一行  仍有空格"
        response = export(client, saved(client, real))
        assert response.status_code == 200, response.text
        text = response.json()["markdown"]
        assert "概述  保留空格" not in text
        assert "备注  保留空格  \n  下一行  仍有空格" in text
