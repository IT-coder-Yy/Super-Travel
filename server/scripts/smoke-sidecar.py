"""Exercise the frozen Python service, JSON persistence, auth and clean EOF shutdown."""
import argparse
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import tempfile
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--version", default="0.1.0")
    args = parser.parse_args()
    token = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="supertravel-smoke-") as directory:
        process = subprocess.Popen([str(args.binary.resolve()), "--host", "127.0.0.1", "--port", "0", "--data-dir", directory], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env={**os.environ,"STA_BACKEND_TOKEN":token,"STA_APP_VERSION":args.version})
        messages = queue.Queue()
        def read():
            for line in process.stdout:
                try: data = json.loads(line)
                except ValueError: continue
                if data.get("event") == "ready": messages.put(data)
        threading.Thread(target=read,daemon=True).start()
        try:
            ready=messages.get(timeout=60)
            assert ready["version"] == args.version and ready["port"] > 0
            base=f"http://127.0.0.1:{ready['port']}"
            def request(path, method="GET", body=None, authenticated=True):
                data=None if body is None else json.dumps(body).encode()
                headers={"Content-Type":"application/json"}
                if authenticated: headers["Authorization"]=f"Bearer {token}"
                with urlopen(Request(base+path,data=data,headers=headers,method=method),timeout=15) as response: return json.load(response)
            assert request("/api/health")["version"] == args.version
            try: request("/api/conversations", authenticated=False)
            except HTTPError as error: assert error.code == 401
            else: raise AssertionError("Unauthenticated write API was accessible")
            created=request("/api/conversations","POST",{"title":"更新保留测试","data":{"message":"杭州三天"}})
            updated=request(f"/api/conversations/{created['id']}","PUT",{"title":"更新保留测试","data":{"message":"保存完成"},"expected_revision":created["revision"]})
            assert updated["revision"] > created["revision"]
            try: request(f"/api/conversations/{created['id']}","PUT",{"title":"过期请求","data":{},"expected_revision":created["revision"]})
            except HTTPError as error: assert error.code == 409
            else: raise AssertionError("Stale update was not rejected")
            assert request(f"/api/conversations/{created['id']}")["data"]["message"] == "保存完成"
            process.stdin.close()
            assert process.wait(timeout=15) == 0
            assert list(Path(directory).rglob("*.json")), "No JSON file persisted"
            print("Frozen sidecar: auth, JSON save/read, stale revision rejection and EOF shutdown passed.")
        finally:
            if process.poll() is None:
                process.stdin.close()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name == 'nt': subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
                    else: process.kill()
                    process.wait(timeout=10)
            if process.returncode:
                print(process.stderr.read()[-4000:])

if __name__ == "__main__": main()
