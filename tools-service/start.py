"""Run the independent tool service; it is never started by the desktop client."""

import argparse
from pathlib import Path

import uvicorn

from gateway import create_server, load_keys


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SuperTravel 本机开发 MCP 网关")
    parser.add_argument("--env-file", type=Path, help="只读取 AMAP_MAPS_API_KEY 与 TAVILY_API_KEY")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    try:
        server = create_server(load_keys(args.env_file))
    except (OSError, ValueError):
        parser.exit(1, "无法读取工具服务凭据，请检查环境文件和必需变量。\n")
    uvicorn.run(server.streamable_http_app(json_response=True), host="127.0.0.1", port=args.port, access_log=False)
