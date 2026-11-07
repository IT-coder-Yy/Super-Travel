"""The overview follows the selected itinerary, including legacy summaries and free days."""
from copy import deepcopy

from fastapi.testclient import TestClient
import pytest

from supertravel_server.trips import validate_result
from .test_trip_api import AUTH, app
from .test_trip_count_constraints import moved_context
from .test_exports import export, file_state, itinerary, saved
from .test_pdf_exports import compact, plain_text, read_pdf


def test_validated_overview_replaces_stale_model_text_without_mutating_input():
    data = moved_context()
    value = {"days": data["days"], "trip": {**data["trip"], "summary": "每天2项；候选雷峰塔；未核实的预算结论"}}
    before = deepcopy(value)
    result = validate_result(value)
    assert result["trip"]["summary"] == "2 天行程；第 1 天 1 项活动；第 2 天 3 项活动。"
    assert result["days"] == before["days"]
    assert value == before


@pytest.mark.parametrize("empty_first", [False, True])
@pytest.mark.parametrize("export_format", ["markdown", "pdf"])
def test_legacy_overview_uses_only_formal_days_without_rewriting_json(app, empty_first, export_format):
    value = moved_context()
    real = {"days": value["days"], "trip": {**value["trip"], "summary": "每天2项；候选雷峰塔；不应进入概览"}}
    real["days"][0].update(title="雷峰塔与断桥旧标题", subtitle="每天2项的旧日程描述")
    if empty_first:
        last = real["days"][0]["activities"].pop()
        last["time"] = "17:00"
        real["days"][1]["activities"].append(last)
    draft = deepcopy(real["days"])
    draft[0]["activities"], draft[1]["activities"] = draft[1]["activities"], draft[0]["activities"]
    expected = "2 天行程；第 1 天自由安排；第 2 天 4 项活动。" if empty_first else "2 天行程；第 1 天 1 项活动；第 2 天 3 项活动。"
    with TestClient(app) as client:
        document = saved(client, real, draft=draft, draftTrip={**real["trip"], "summary": "草稿独有概述"},
                         candidates=[{"id": "candidate", "title": "雷峰塔", "description": "候选独有备注"}])
        before = file_state(app.state.store.root)
        response = export(client, document, format=export_format)
        assert response.status_code == 200, response.text
        result = response.json()
        assert expected in result["markdown"]
        for excluded in ["每天2项", "雷峰塔", "草稿独有概述", "候选独有备注", "生成时概述"]:
            assert excluded not in result["markdown"]
        if export_format == "pdf":
            text = compact(plain_text(read_pdf(result)))
            assert compact(expected) in text and "雷峰塔" not in text
        assert file_state(app.state.store.root) == before


def test_apply_and_restore_refresh_overview_from_each_snapshot(app):
    real = itinerary()
    real["trip"]["summary"] = "旧概述，每天2项；候选雷峰塔"
    draft = deepcopy(real["days"])
    draft[0]["activities"], draft[1]["activities"] = draft[1]["activities"], draft[0]["activities"]
    with TestClient(app) as client:
        document = saved(client, real, draft=draft, draftTrip=real["trip"])
        endpoint = f"/api/conversations/{document['id']}/trip/"
        response = client.post(endpoint + "apply", headers=AUTH, json={"expected_revision": document["revision"]})
        assert response.status_code == 200, response.text
        applied = response.json()
        assert applied["data"]["trip"]["summary"] == "2 天行程；第 1 天自由安排；第 2 天 3 项活动。"
        assert applied["data"]["previousTrip"]["trip"] == real["trip"]
        response = client.post(endpoint + "restore", headers=AUTH, json={"expected_revision": applied["revision"]})
        assert response.status_code == 200, response.text
        assert response.json()["data"]["trip"]["summary"] == "2 天行程；第 1 天 3 项活动；第 2 天自由安排。"
