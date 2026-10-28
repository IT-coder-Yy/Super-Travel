"""Loopback-only model/tool fixtures for native UI acceptance, never real travel data."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
from socketserver import TCPServer
import threading
import time


TOOLS = [
    {"name": "baidu_map_search_places", "description": "固定验收地点，不是真实资料", "inputSchema": {
        "type": "object", "properties": {"query": {"type": "string"}, "region": {"type": "string"}},
        "required": ["query", "region"], "additionalProperties": False}},
    {"name": "baidu_map_directions", "description": "固定验收路线，不是真实资料", "inputSchema": {
        "type": "object", "properties": {"origin": {"type": "string"}, "destination": {"type": "string"}, "model": {"type": "string"}},
        "required": ["origin", "destination"], "additionalProperties": False}},
]
PROVIDERS = [{"id": "baidu", "configured": True, "connection": "connected", "toolCount": 2, "message": "固定验收服务，不是真实百度查询"}]
PLACES = [
    {"uid": "fixture-park", "name": "固定测试公园", "address": "固定测试公园地址", "city": "杭州市", "location": {"lat": 30.25, "lng": 120.15}},
    {"uid": "fixture-museum", "name": "固定测试博物馆", "address": "固定测试博物馆地址", "city": "杭州市", "location": {"lat": 30.27, "lng": 120.16}},
    {"uid": "fixture-garden", "name": "固定测试花园", "address": "固定测试花园地址", "city": "杭州市", "location": {"lat": 30.28, "lng": 120.17}},
]


def reply(content=None, name=None, arguments=None):
    message = {"role": "assistant", "content": content}
    if name:
        message["tool_calls"] = [{"id": "native-fixture-call", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if name else "stop"}]}


def model_reply(payload):
    messages, tools = payload["messages"], payload.get("tools", [])
    names = {tool["function"]["name"] for tool in tools}
    if "route_request" in names:
        context = json.loads(messages[-1]["content"])
        text = context["request"]
        kind, content = "plan", "固定验收计划：查询地点、路线并提交杭州两天行程。"
        if "你好" in text:
            kind, content = "answer", "固定测试回复：你好，旅行者。"
        elif "进度" in text:
            kind, content = "progress", "固定验收：查看已有任务进度。"
        elif "缺少" in text or text.strip("。！？ ") == "我想旅行":
            kind, content = "clarification", "固定验收：你准备去哪个城市、旅行几天？"
        decision = {"kind": kind, "content": content, "steps": [], "replace_active": False}
        if kind == "plan":
            decision.update(steps=["查询固定地点与路线，提交两天结构化行程"],
                            trip={"destination": "杭州", "days": 2, "map_provider": "baidu", "transport_mode": "walking"})
        return reply(name="route_request", arguments=decision)
    if "submit_trip" in names:
        context = json.loads(messages[1]["content"])
        calls = {call["function"]["name"] for message in messages for call in message.get("tool_calls", [])}
        if not context.get("available_places") and "search_trip_places" not in calls:
            return reply(name="search_trip_places", arguments={"query": "固定测试公园", "provider": "baidu"})
        if not context.get("available_routes") and "query_trip_route" not in calls:
            return reply(name="query_trip_route", arguments={"from_place_id": "baidu:fixture-park", "to_place_id": "baidu:fixture-museum", "mode": "walking"})
        current = context.get("current_trip")
        if current and any(word in context["request"] for word in ("调整", "修改")):
            days = [{"day": day["day"], "title": day["title"], "subtitle": day["subtitle"], "activities": [
                {"place_id": activity["place"]["id"], "time": activity["time"],
                 "duration_minutes": int(re.search(r"\d+", activity["duration"])[0]),
                 "description": "固定测试调整：改为慢游，保留已有时间。"} for activity in day["activities"]]} for day in current["days"]]
        else:
            days = [{"day": day, "title": f"固定测试第{day}天", "subtitle": "固定验收资料", "activities": [
                {"place_id": "baidu:fixture-park", "time": "09:00", "duration_minutes": 60, "description": "固定测试安排，并非实时旅行建议。"},
                {"place_id": "baidu:fixture-museum", "time": "11:00", "duration_minutes": 60, "description": "固定测试参观"},
            ]} for day in (1, 2)]
        return reply(name="submit_trip", arguments={"summary": "固定离线测试行程", "days": days})
    return reply("固定离线测试行程已生成；时间与费用仍需确认。")


class LoopbackServer(ThreadingHTTPServer):
    def server_bind(self):
        # Fixed loopback fixtures do not need HTTPServer's potentially slow reverse DNS.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass  # Request content and credentials must never enter acceptance logs.

    def send_json(self, value, status=200):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # A cancelled UI task intentionally closes its connection.

    def do_GET(self):
        if self.path == "/v1/tools":
            self.send_json({"tools": TOOLS, "providers": PROVIDERS})
        elif self.path == "/v1/status":
            self.send_json({"providers": PROVIDERS})
        elif self.path == "/test/status":
            with self.server.state_lock:
                self.send_json(deepcopy(self.server.state))
        else:
            self.send_json({"error": "unknown fixture endpoint"}, 404)

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 2 * 1024 * 1024:
                raise ValueError
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError
            if self.path == "/test/mode":
                if payload.get("mode") not in ("ok", "quota", "delay"):
                    raise ValueError
                with self.server.state_lock:
                    self.server.state["mode"] = payload["mode"]
                return self.send_json({"fixture": True, "mode": payload["mode"]})
            if self.path == "/v1/chat/completions":
                if self.headers.get("Authorization") != "Bearer native-fixture-not-a-secret":
                    return self.send_json({"error": "fixture token required"}, 401)
                with self.server.state_lock:
                    self.server.state["modelCalls"] += 1
                return self.send_json(model_reply(payload))
            if self.path == "/v1/call":
                name = payload["name"]
                if name not in {tool["name"] for tool in TOOLS} or not isinstance(payload["arguments"], dict):
                    raise ValueError
                with self.server.state_lock:
                    self.server.state["searchCalls" if name == "baidu_map_search_places" else "routeCalls"] += 1
                    mode = self.server.state["mode"]
                if mode == "quota":
                    return self.send_json({"error": {"code": "tool_quota", "message": "固定验收：模拟额度不足"}}, 429)
                if mode == "delay":
                    time.sleep(4)
                places = [place for place in PLACES if place["uid"] == "fixture-garden"] if payload["arguments"].get("query") == "固定测试花园" else PLACES
                data = {"status": 0, "results": places} if name == "baidu_map_search_places" else {
                    "status": 0, "result": {"routes": [{"duration": 1200, "distance": 1000}]}}
                return self.send_json({"result": {"isError": False, "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}, "providers": PROVIDERS})
            self.send_json({"error": "unknown fixture endpoint"}, 404)
        except (ValueError, TypeError, KeyError, IndexError):
            self.send_json({"error": "invalid fixture request"}, 400)


def prepare(root):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
    root.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SuperTravel local acceptance fixture")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=2))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.IPAddress(ipaddress.ip_address("::1"))]), critical=False)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False).sign(key, hashes.SHA256()))
    (root / "ca.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private = root / "fixture-key.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    private.chmod(0o600)


def change_trust(root, restore=False):
    if os.environ.get("CI", "").lower() != "true":
        raise RuntimeError("Fixture CA trust/restore is allowed only with CI=true")
    import certifi
    bundle = Path(certifi.where()).resolve()
    backup = root / "certifi-backup.pem"
    marker = root / "certifi-path.txt"
    if restore:
        if not backup.exists():
            return
        if marker.read_text(encoding="utf-8") != str(bundle):
            raise RuntimeError("Fixture certifi path changed; refusing to restore another file")
        bundle.write_bytes(backup.read_bytes())
        backup.unlink()
        marker.unlink()
    else:
        if backup.exists():
            raise RuntimeError("Fixture trust backup already exists; restore it before trusting again")
        original = bundle.read_bytes()
        backup.write_bytes(original)
        marker.write_text(str(bundle), encoding="utf-8")
        bundle.write_bytes(original + b"\n" + (root / "ca.pem").read_bytes())


def run(root):
    state = {"fixture": True, "mode": "ok", "modelCalls": 0, "searchCalls": 0, "routeCalls": 0}
    lock = threading.Lock()
    model = LoopbackServer(("127.0.0.1", 0), FixtureHandler)
    tools = LoopbackServer(("127.0.0.1", 0), FixtureHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / "ca.pem", root / "fixture-key.pem")
    model.socket = context.wrap_socket(model.socket, server_side=True)
    for server in (model, tools):
        server.state, server.state_lock = state, lock
        threading.Thread(target=server.serve_forever, daemon=True).start()
    ready = {"modelBaseUrl": f"https://127.0.0.1:{model.server_port}/v1", "model": "native-fixture",
             "apiKey": "native-fixture-not-a-secret", "toolsBaseUrl": f"http://127.0.0.1:{tools.server_port}"}
    temporary = root / "ready.json.tmp"
    temporary.write_text(json.dumps(ready), encoding="utf-8")
    temporary.replace(root / "ready.json")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        for server in (model, tools):
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    for name in ("prepare", "run", "trust", "restore"):
        action.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    if args.prepare:
        prepare(args.prepare.resolve())
    elif args.run:
        run(args.run.resolve())
    else:
        change_trust((args.trust or args.restore).resolve(), restore=bool(args.restore))
