"""Read-only Markdown/PDF exports of one saved formal-trip snapshot."""
from base64 import b64encode
from datetime import datetime, timezone
from html import escape
import re
from uuid import UUID
from typing import Literal

from fastapi import APIRouter
from pydantic import ValidationError

from supertravel_server.trip_api import TripRevision
from supertravel_server.trip_routes import route_fresh, route_matches
from supertravel_server.trips import ActivityCost, IncomingRoute, TripResult, trip_overview


MODES = {"walking": "步行", "driving": "驾车", "transit": "公共交通"}
PROVIDERS = {"baidu": "百度地图", "amap": "高德地图"}


class TripExport(TripRevision):
    format: Literal["markdown", "pdf"] = "markdown"


def _plain(value, *, multiline=False):
    return "\n".join(line.strip() for line in value.splitlines()) if multiline else " ".join(value.split())


def _text(value, *, multiline=False):
    if not multiline:
        value = " ".join(value.split())
    # Escape every line separately, keeping note spacing without Markdown indentation.
    return "  \n".join(re.sub(r"([\\`*_{}\[\]()#+.!|~:@=\-])", r"\\\1", escape(line.strip(), quote=False))
                       for line in value.splitlines())


def _optional(model, value):
    try:
        return model.model_validate(value).model_dump(exclude_none=True)
    except ValidationError:
        return None


def _money(amount_fen):
    return f"¥{amount_fen // 100}.{amount_fen % 100:02d}"


def _transit(details):
    if details is None:
        return "公交详情未查询。"
    transfers, fare = details.get("transferCount"), details.get("fare")
    lines = [f"换乘 {transfers} 次（按乘车段计算）" if transfers is not None else "换乘次数待确认"]
    lines.append("参考总票价：" + _money(fare["amountFen"]) + "（仅供参考，以实际结果为准）" if fare else "参考总票价未知")
    for segment in details["segments"]:
        if segment["mode"] == "walking":
            lines.append("步行接驳")
        elif segment["mode"] == "other":
            lines.append("此段交通详情待确认")
        else:
            lines.append(f"{_plain(segment.get('lineName') or '线路待确认')}：{_plain(segment.get('departureStop') or '上车站待确认')} 上车 → {_plain(segment.get('arrivalStop') or '下车站待确认')} 下车")
    if not details["segments"]:
        lines.append("公交分段详情待确认")
    lines.append("供应商首方案，仅供出行参考；交通参考价不计入活动费用小计。")
    return "；".join(lines) + "。"


def _route(previous, activity, mode):
    if previous is None:
        return "当日首项，无前序活动接驳。"
    route = _optional(IncomingRoute, activity.get("incomingRoute"))
    if route is None:
        return "未知 / 待确认；无完整路线记录，查询时间：未知；供应商：未知。"
    provider = PROVIDERS.get(route.get("provider"), "未知")
    queried = _plain(route.get("queriedAt") or "未知")
    evidence = f"查询时间：{queried}；供应商：{provider}。"
    if (not route_matches(route, previous["place"], activity["place"], mode)
            or route.get("provider") and route["provider"] != previous["place"]["provider"]
            or route.get("provider") and route["provider"] != activity["place"]["provider"]):
        return f"未知 / 待确认；路线与当前地点或交通方式不匹配。{evidence}"
    if route["status"] != "verified":
        return f"{MODES[route['mode']]}耗时未知 / 待确认。{_plain(route.get('reason') or '尚无可用路线估算。')} {evidence}"
    if not route_fresh(route):
        return f"未知 / 待确认；路线记录已过期或查询时间无效，不能作为当前已核验接驳。{evidence}"
    return (f"已查询估算：{MODES[route['mode']]}约 {route['durationMinutes']} 分钟，"
            f"{route['distanceMeters']} 米。{evidence}"
            + ("\n" + _transit(route.get("transit")) if route["mode"] == "transit" else ""))


def export_blocks(days, trip, revision):
    blocks = [("title", f"{_plain(trip['destination'])}行程"), ("body", "已保存正式版"),
              ("item", f"快照会话版本：{revision}"),
              ("item", f"行程生成时间：{_plain(trip['generatedAt'])}"),
              ("item", f"导出生成时间：{datetime.now(timezone.utc).isoformat()}"),
              ("heading", "正式行程概览"), ("body", trip_overview(days))]
    total, unknown = 0, 0
    for day in days:
        blocks += [("heading", f"第 {day['day']} 天")]
        if not day["activities"]:
            blocks.append(("body", "自由安排"))
        previous = None
        for index, activity in enumerate(day["activities"], 1):
            cost = _optional(ActivityCost, activity.get("cost"))
            if cost is None:
                cost_text = "未知 / 待确认"
                unknown += 1
            else:
                total += cost["amountFen"]
                cost_text = _money(cost["amountFen"]) + ("（免费，用户填写）" if cost["amountFen"] == 0 else "（用户填写）")
            blocks += [("activity", f"{index}. {_plain(activity['time'])} · {_plain(activity['title'])}"),
                       ("item", f"地址：{_plain(activity['place']['address']) or '未知 / 待确认'}"),
                       ("item", f"停留时长：{_plain(activity['duration'])}"),
                       ("item", f"备注：{_plain(activity['description'], multiline=True)}"),
                       ("item", f"费用（人民币）：{cost_text}"),
                       ("item", f"接驳：{_route(previous, activity, trip.get('transportMode') or 'walking')}")]
            previous = activity
    blocks += [("heading", "费用汇总"), ("item", f"已知费用小计（人民币，用户填写）：{_money(total)}"),
               ("item", f"费用未知项：{unknown} 项"),
               ("body", "已知小计仅包含已填写的活动费用，未知项不按免费计算；不代表完整旅行总价或已核验票价。"),
               ("body", "接驳只反映保存时的路线记录；超过 24 小时的估算标为待确认，导出不会重新查询，出行当天交通仍需确认。")]
    return blocks


def _markdown(blocks):
    prefixes = {"title": "# ", "heading": "## ", "activity": "### ", "body": "", "item": "- "}
    lines = []
    for index, (kind, text) in enumerate(blocks):
        # Decimal points between digits need no Markdown escaping.
        text = re.sub(r"(?<=\d)\\\.(?=\d)", ".", _text(text, multiline=True))
        lines.append(prefixes[kind] + (text.replace("\n", "\n  ") if kind == "item" else text))
        if kind != "item" or index + 1 == len(blocks) or blocks[index + 1][0] != "item":
            lines.append("")
    return "\n".join(lines)


def render_markdown(days, trip, revision):
    return _markdown(export_blocks(days, trip, revision))


def export_router(store, error_type):
    router = APIRouter(prefix="/api/conversations")

    @router.post("/{conversation_id}/trip/export")
    def export_trip(conversation_id: UUID, payload: TripExport):
        document = store.get(conversation_id)
        if document.revision != payload.expected_revision:
            raise error_type(409, "revision_conflict", "会话已被更新，请重新加载后确认导出。", current_revision=document.revision)
        days, trip = document.data.get("days"), document.data.get("trip")
        try:
            # Legacy optional evidence must not prevent exporting a valid formal itinerary.
            core_days = [{**day, "activities": [{key: value for key, value in activity.items()
                          if key not in ("cost", "incomingRoute")} for activity in day["activities"]]} for day in days]
            TripResult.model_validate({"days": core_days, "trip": trip})
        except (ValidationError, TypeError, KeyError, AttributeError):
            raise error_type(422, "trip_export_unavailable", "请先生成并保存真实正式行程；示例、仅有草稿或不完整行程不能导出。") from None
        destination = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff _-]", "", trip["destination"]).strip(" _-")[:32].strip(" _-") or "旅行"
        blocks = export_blocks(days, trip, document.revision)
        result = {"revision": document.revision, "filename": f"{destination}-行程.md", "markdown": _markdown(blocks)}
        if payload.format == "pdf":
            from supertravel_server.pdf_export import PdfExportError, render_pdf
            try:
                result["pdfBase64"] = b64encode(render_pdf(result["markdown"])).decode("ascii")
            except PdfExportError as error:
                raise error_type(422, "trip_pdf_unavailable", str(error)) from None
            result["filename"] = f"{destination}-行程.pdf"
            result["previewText"] = "\n\n".join(text for _, text in blocks)
        return result

    return router
