"""Opt-in live acceptance: real models/tools, isolated JSON, no credentials in reports."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from backend.main import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow actual model/tool requests and their usage costs")
    parser.add_argument("--days", type=int, nargs="+", choices=(1, 3, 7), default=[1, 3, 7])
    args = parser.parse_args()
    required = ("STA_MODEL_BASE_URL", "STA_MODEL_NAME", "STA_MODEL_API_KEY", "STA_SHARED_TOOLS_URL")
    if not args.live or any(not os.environ.get(name) for name in required):
        parser.error("Pass --live and configure STA_MODEL_BASE_URL, STA_MODEL_NAME, STA_MODEL_API_KEY, STA_SHARED_TOOLS_URL")
    # Force the production relay path; no provider secrets or direct MCP override enter the client.
    for name in ("STA_MCP_URL", "AMAP_MAPS_API_KEY", "BAIDU_MAP_API_KEY", "TAVILY_API_KEY", "SERPER_API_KEY"):
        os.environ.pop(name, None)
    (ROOT / ".local").mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="live-trip-scenarios-", dir=ROOT / ".local"))
    token = secrets.token_hex(24)
    headers = {"Authorization": "Bearer " + token}
    report = {"checkedAt": datetime.now(timezone.utc).isoformat(), "simulated": False,
              "scope": "Real model and remote tools through production HTTP handlers; isolated JSON. Not native GUI acceptance.", "cases": []}
    saved_documents = []
    app = create_app(directory, token)
    model_calls = {"planner": 0, "executor": 0, "summary": 0}
    real_chat = app.state.agents.model.chat

    async def measured_chat(messages, tools=None, **kwargs):
        role = "summary" if not tools else "planner" if tools[0]["function"]["name"] == "route_request" else "executor"
        model_calls[role] += 1
        return await real_chat(messages, tools, **kwargs)

    app.state.agents.model.chat = measured_chat
    with TestClient(app, headers=headers) as client:
        def request(method, path, body=None):
            response = client.request(method, "/api" + path, json=body)
            if response.is_error:
                raise RuntimeError("api_" + str(response.status_code))
            return response.json()

        def completed(identifier):
            deadline = time.monotonic() + 360
            while time.monotonic() < deadline:
                document = request("GET", "/conversations/" + identifier)
                state = document["data"].get("agent", {})
                if not document["data"].get("agentPending"):
                    if state.get("status") == "completed":
                        return document
                    if state.get("status") in ("failed", "needs_input", "cancelled"):
                        raise RuntimeError("agent_" + state["status"])
                    if not state:
                        raise RuntimeError("planner_did_not_execute")
                time.sleep(1)
            request("POST", "/conversations/" + identifier + "/cancel")
            raise RuntimeError("acceptance_timeout")

        for days in args.days:
            city = {1: "杭州", 3: "成都", 7: "北京"}[days]
            case = {"days": days, "destination": city, "passed": False}
            before_calls = dict(model_calls)
            report["cases"].append(case)
            identifier = None
            try:
                document = request("POST", "/conversations", {"title": f"真实验收：{city}{days}天", "data": {
                    "schema": 1, "days": [], "draft": None, "selectedDay": 1, "messages": [], "favorites": []}})
                identifier = document["id"]
                base = "/conversations/" + identifier
                session = app.state.agents.session(UUID(identifier))
                original_on_state = session.on_state
                transitions = []

                async def observe(state):
                    if not transitions or transitions[-1]["status"] != state["status"]:
                        transitions.append({"status": state["status"], "toolCalls": state["tool_calls"],
                                            "error": (state.get("error") or "")[:500]})
                    await original_on_state(state)

                session.on_state = observe
                case["transitions"] = transitions
                text = (f"请直接生成{city}{days}天的国内单目的地旅行行程，每天恰好2个不同景点，"
                        "各天尽量不重复，09:00到18:00慢慢游览，每个景点停留60分钟。"
                        "只用百度地图查询真实地点和相邻景点步行路线，安排足够接驳时间。"
                        "不查询天气、酒店、餐饮或票价，费用未知留空；查询失败明确标注未知，不编造。")
                request("POST", base + "/messages", {"message_id": str(uuid4()), "content": text})
                document = completed(identifier)
                data = document["data"]
                activities = [a for day in data["days"] for a in day["activities"]]
                routes = [day["activities"][1].get("incomingRoute") for day in data["days"]]
                assert len(data["days"]) == days and all(len(day["activities"]) == 2 for day in data["days"])
                assert all(a.get("place", {}).get("provider") == "baidu" for a in activities)
                assert all(route and route["status"] in ("verified", "unknown") for route in routes)
                case.update(toolCalls=data["agent"]["tool_calls"], replans=data["agent"]["replans"],
                            verifiedPlaces=len(activities), verifiedRoutes=sum(r["status"] == "verified" for r in routes),
                            unknownRoutes=sum(r["status"] == "unknown" for r in routes),
                            plannerSummary=bool(data["agent"].get("content")))
                assert case["verifiedRoutes"] > 0, "no_live_route_evidence"
                # User-entered cost exercises the same guarded draft/apply endpoints as the editor.
                proposal = deepcopy(data)
                proposal["draft"], proposal["draftTrip"] = deepcopy(data["days"]), deepcopy(data["trip"])
                proposal["draft"][0]["activities"][0]["cost"] = {"amountFen": 1250, "currency": "CNY", "source": "user"}
                proposal["draft"][0]["activities"][0]["description"] += " 本机验收备注。"
                pending = request("PUT", base, {"title": document["title"], "data": proposal, "expected_revision": document["revision"]})
                assert pending["data"]["days"] == data["days"]
                applied = request("POST", base + "/trip/apply", {"expected_revision": pending["revision"]})
                assert applied["data"]["days"][0]["activities"][0]["cost"]["amountFen"] == 1250
                assert applied["data"]["previousTrip"]["days"] == data["days"]
                stored = json.loads((directory / "conversations" / (identifier + ".json")).read_text(encoding="utf-8"))
                assert stored == applied
                saved_documents.append(applied)
                case.update(draftIsolation=True, editApply=True, jsonSave=True, previousVersion=True, passed=True)
            except Exception as error:
                # Exception texts may contain external responses; only fixed codes/types reach the report.
                case["error"] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                if identifier:
                    request("POST", "/conversations/" + identifier + "/cancel")
            case["modelCalls"] = {role: count - before_calls[role] for role, count in model_calls.items()}
            print(json.dumps(case, ensure_ascii=True), flush=True)
            (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with TestClient(create_app(directory, token), headers=headers) as restarted:
        for document in saved_documents:
            restored = restarted.get("/api/conversations/" + document["id"]).json()
            assert restored == document
    report["restartRecoveredCases"] = len(saved_documents)
    report["restartRecovery"] = bool(saved_documents)
    report["passed"] = len(saved_documents) == len(args.days) and all(case["passed"] for case in report["cases"])
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Report: " + str(directory / "report.json"))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
