"""Bounded route queries using provider coordinates, never display coordinates."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import math

from jsonschema import validate
from referencing import Registry

from backend.mcp_client import ToolError


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


def _metrics(result, provider, mode):
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
            return math.ceil(seconds / 60), math.ceil(distance)
    return None


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
            metrics = None if result.get("isError") else _metrics(result, provider, mode)
            if metrics is None:
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
    return {**route, "status": "verified", "provider": provider, "durationMinutes": metrics[0], "distanceMeters": metrics[1], "reason": "查询时的路线估算，不是出行当天的交通保证。"}
