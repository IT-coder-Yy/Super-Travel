"""Bounded route queries using provider coordinates, never display coordinates."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math

from jsonschema import validate
from referencing import Registry

from supertravel_server.mcp_client import ToolError


ROUTE_MAX_AGE_SECONDS = 24 * 60 * 60
MODES = {"walking", "driving", "transit"}


def route_fresh(route):
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(route["queriedAt"])).total_seconds()
        return 0 <= age <= ROUTE_MAX_AGE_SECONDS
    except (KeyError, TypeError, ValueError):
        return False


def unknown_route(origin, destination, mode="walking", reason="尚未查询交通，请预留接驳时间。"):
    return {"fromPlaceId": origin["id"], "toPlaceId": destination["id"], "mode": mode,
            "status": "unknown", "reason": reason}


def route_matches(route, origin, destination, mode=None):
    return isinstance(route, dict) and route.get("fromPlaceId") == origin.get("id") and route.get("toPlaceId") == destination.get("id") and (mode is None or route.get("mode") == mode)


def _payloads(result):
    yield result.get("structuredContent")
    for block in result.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                yield json.loads(block["text"])
            except (ValueError, TypeError, KeyError):
                continue


def _first_route(result, provider, mode):
    for payload in _payloads(result):
        if not isinstance(payload, dict):
            continue
        if (provider == "baidu" and str(payload.get("status", "0")) != "0") or (provider == "amap" and str(payload.get("status", "1")) == "0"):
            continue
        if provider == "baidu":
            root = payload.get("result", {})
            paths = root.get("routes", []) if isinstance(root, dict) else []
        else:
            root = payload.get("route", payload)
            paths = root.get("transits" if mode == "transit" else "paths", []) if isinstance(root, dict) else []
        if not isinstance(paths, list) or not paths or not isinstance(paths[0], dict):
            continue
        path = paths[0]
        seconds = path.get("duration")
        if seconds is None and isinstance(path.get("cost"), dict):
            seconds = path["cost"].get("duration")
        distance = path.get("distance")
        if isinstance(seconds, bool) or isinstance(distance, bool):
            continue
        try:
            seconds, distance = float(seconds), float(distance)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and math.isfinite(distance) and 0 < seconds <= 172800 and 0 <= distance <= 3000000:
            return path, (math.ceil(seconds / 60), math.ceil(distance)), root
    return None


def _metrics(result, provider, mode):
    selected = _first_route(result, provider, mode)
    return selected[1] if selected else None


def _fare(value):
    # Keep provider quotes including zero; never sum segment prices.
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or not 0 <= amount <= 1000000:
            return None
        amount *= 100
        if amount == amount.to_integral_value():
            return {"amountFen": int(amount), "currency": "CNY"}
    except InvalidOperation:
        pass
    return None


def _taxi_estimates(root, provider):
    taxi = root.get("taxi") if provider == "baidu" else None
    detail = taxi.get("detail") if isinstance(taxi, dict) else None
    rows = detail if provider == "baidu" else [{"total_price": root.get("taxi_cost")}]
    if not isinstance(rows, list) or len(rows) > 8:
        return []
    estimates = []
    for row in rows:
        fare = _fare(row.get("total_price")) if isinstance(row, dict) else None
        if fare is None:
            continue
        estimate = {**fare, "kind": "estimate"}
        for key, value, limit in (("label", row.get("desc"), 100),
                                  ("note", taxi.get("remark") if isinstance(taxi, dict) else None, 500)):
            if isinstance(value, str) and 0 < len(value.strip()) <= limit:
                estimate[key] = value.strip()
        estimates.append(estimate)
    return estimates


def _ride(mode, name, departure, arrival):
    segment = {"mode": mode}
    for key, value in (("lineName", name), ("departureStop", departure), ("arrivalStop", arrival)):
        if isinstance(value, str) and 0 < len(value.strip()) <= 200:
            segment[key] = value.strip()
    return segment


def _transit_details(path, provider):
    segments = []
    steps = path.get("steps" if provider == "baidu" else "segments")
    if isinstance(steps, list) and 0 < len(steps) <= 32:
        for group in steps:
            # Baidu groups and Amap buslines contain alternatives, not extra transfers.
            step = group[0] if provider == "baidu" and isinstance(group, list) and group else group
            if not isinstance(step, dict):
                segments.append({"mode": "other"})
                continue
            if provider == "baidu":
                vehicle = step.get("vehicle")
                if str(step.get("type")) == "5":
                    segments.append({"mode": "walking"})
                elif str(step.get("type")) == "3" and isinstance(vehicle, dict):
                    segments.append(_ride("subway" if str(vehicle.get("type")) == "1" else "bus",
                                          vehicle.get("name"), vehicle.get("start_name"), vehicle.get("end_name")))
                else:
                    segments.append({"mode": "other"})
            else:
                before = len(segments)
                walking, bus = step.get("walking"), step.get("bus")
                if isinstance(walking, dict) and (walking.get("steps") or walking.get("distance") not in (None, [], "", "0", 0)):
                    segments.append({"mode": "walking"})
                lines = bus.get("buslines") if isinstance(bus, dict) else None
                if isinstance(lines, list) and lines and isinstance(lines[0], dict):
                    line = lines[0]
                    departure, arrival = line.get("departure_stop"), line.get("arrival_stop")
                    kind = str(line.get("type", ""))
                    segments.append(_ride("subway" if "地铁" in kind or "轻轨" in kind else "bus", line.get("name"),
                                          departure.get("name") if isinstance(departure, dict) else None,
                                          arrival.get("name") if isinstance(arrival, dict) else None))
                elif lines not in (None, []):
                    segments.append({"mode": "other"})
                # Empty railway placeholders occur on ordinary bus routes. Actual rail/taxi
                # segments are outside this batch and must not imply a known transfer count.
                unsupported = any(isinstance(step.get(key), dict) and any(step[key].values()) for key in ("railway", "taxi"))
                if unsupported or len(segments) == before:
                    segments.append({"mode": "other"})
    if len(segments) > 64:
        segments = []
    details = {"segments": segments}
    if segments and all(item["mode"] != "other" for item in segments):
        details["transferCount"] = max(0, sum(item["mode"] in ("bus", "subway") for item in segments) - 1)
    fare = _fare(path.get("price" if provider == "baidu" else "cost"))
    if fare is not None:
        details["fare"] = fare
    return details


async def query_route(tools, origin, destination, mode, city):
    if mode not in MODES:
        raise ToolError("tool_arguments", False, "交通方式需要为步行、驾车或公交。")
    route = unknown_route(origin, destination, mode)
    route["queriedAt"] = datetime.now(timezone.utc).isoformat()
    provider = origin["provider"]
    source, target = origin.get("sourceLocation"), destination.get("sourceLocation")
    expected = "bd09ll" if provider == "baidu" else "gcj02"
    if (provider != destination["provider"] or not source or not target
            or source["coordinateSystem"] != expected or target["coordinateSystem"] != expected):
        route["reason"] = "缺少同一地图服务的原始坐标，请重新查询地点；交通耗时未知。"
        return route
    try:
        async with asyncio.timeout(12):
            names = {row["name"]: row for row in await tools.list_tools()}
            name = "baidu_map_directions" if provider == "baidu" else "amap_maps_direction_" + ("transit_integrated" if mode == "transit" else mode)
            definition = names.get(name)
            if not definition:
                route["reason"] = "该地图的路线服务暂不可用，交通耗时未知。"
                return route
            def coordinate(location):
                lng, lat = location["longitude"], location["latitude"]
                return f"{lat:.6f},{lng:.6f}" if provider == "baidu" else f"{lng:.6f},{lat:.6f}"
            arguments = {"origin": coordinate(source), "destination": coordinate(target)}
            if provider == "baidu":
                arguments["model"] = mode
            elif mode == "transit":
                arguments.update(city=city, cityd=city)
            validate(arguments, definition["inputSchema"], registry=Registry())
            result = await tools.call_tool(name, arguments)
            selected = None if result.get("isError") else _first_route(result, provider, mode)
            if selected is None:
                route["reason"] = "路线服务没有返回有效耗时，不能确认接驳是否可行。"
                return route
    except TimeoutError:
        route["reason"] = "路线查询超时，交通耗时未知。"
        return route
    except ToolError as error:
        route["reason"] = "路线服务额度或权限不可用，交通耗时未知。" if error.code in ("tool_quota", "tool_auth", "tool_rate_limit") else "路线查询失败，交通耗时未知，请稍后重试。"
        return route
    except Exception:
        route["reason"] = "路线服务参数或响应不兼容，交通耗时未知。"
        return route
    path, metrics, root = selected
    if mode in ("transit", "driving"):
        route["taxiEstimates"] = _taxi_estimates(root, provider)
    if mode == "transit":
        route["transit"] = _transit_details(path, provider)
    return {**route, "status": "verified", "provider": provider, "durationMinutes": metrics[0], "distanceMeters": metrics[1], "reason": "查询时的路线估算，不是出行当天的交通保证。"}
