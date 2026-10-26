# 独立 MCP 工具服务（本机开发）

这里把高德和 Tavily 的官方 stdio 工具接到一个 MCP Streamable HTTP 入口。桌面 Python 后端只保存入口地址，地图和检索 Key 留在这个独立服务中。它不随客户端打包、不会随客户端自动启动，也没有修改旧项目。

当前固定监听 `127.0.0.1:8766`，客户端地址是 `http://127.0.0.1:8766/mcp`。这一步用于本机开发联调；面向真实用户的远程 HTTPS 部署、服务鉴权和额度控制尚未实现。

## 安装与启动

需要 Python 3.13 和 Node.js 22.14 或以上版本。在项目根目录执行：

```powershell
python -m venv tools-service/.venv
tools-service/.venv/Scripts/python -m pip install -r tools-service/requirements-dev.txt
npm ci --prefix tools-service --ignore-scripts
tools-service/.venv/Scripts/python tools-service/start.py --env-file .local/tools.env
```

macOS/Linux 使用 `tools-service/.venv/bin/python` 替换上面的 Python 路径。`--port` 可指定端口，监听地址保持回环。

开发者自行在不提交 Git 的环境文件中配置 `AMAP_MAPS_API_KEY` 和 `TAVILY_API_KEY`，也可以不传 `--env-file`，从服务进程的同名环境变量读取。环境文件只解析这两个字段；不会复制文件，也不会把模型密钥或其他配置传给工具。

每个 Node 子进程只收到自己的 Key 与 SDK 必需的系统环境变量，并使用一个空临时工作目录，避免 Tavily 的 `dotenv.config()` 意外加载额外 `.env`。子进程 stderr 不回传客户端。工具 schema、结果中的 Key 与 URL 编码后的 Key 会替换为 `[REDACTED]`；协议异常使用固定安全消息。

## 开放的工具

- 高德：地点搜索、周边搜索、POI 详情、地理/逆地理编码、天气、距离以及步行/骑行/驾车/公交路线，共 11 个。名字统一为 `amap_maps_*`。
- Tavily：`tavily_search` 和 `tavily_extract`。
- 不开放 IP 定位、crawl/map/research、文件系统、小红书或任意命令工具。

网关保留供应商的 `inputSchema`，调用前校验参数；业务错误以 MCP `isError: true` 返回，供规划 Agent 修改计划。取消会沿异步调用传播；客户端不接受迟到的结果。默认单个上游调用限时 25 秒，桌面 MCP 请求限时 30 秒。

后端接口位于 `backend/mcp_client.py`：

```python
client = McpClient("http://127.0.0.1:8766/mcp")
tools = await client.list_tools()  # name / description / inputSchema
result = await client.call_tool("amap_maps_text_search", {"keywords": "西湖", "city": "杭州"})
```

客户端只使用官方 MCP SDK，不读取服务密钥。远程地址须为 HTTPS，URL 不允许携带 userinfo、查询参数或 fragment。`ToolError` 提供 `code`、`retryable`、`message`；`asyncio.CancelledError` 原样传播。`_meta` 不进入模型工具结果。

## 依赖与验证

官方来源：[Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)、[高德接入文档](https://developer.amap.com/api/mcp-server/gettingstarted)、[Tavily MCP](https://github.com/tavily-ai/tavily-mcp)。

Python 固定 `mcp==2.3.0`。高德最新包仍为 `0.0.8`，Tavily 使用 `0.2.22`；这两个包分别硬钉了旧 TypeScript SDK `1.0.1` 和 `1.26.0`。仅升级上层包无法解决安全审计，因此本目录使用 npm `overrides` 固定官方 SDK `1.32.2` 与 Zod `4.6.7`，并提交 lockfile。2026-10-26 安装后 `npm audit` 为 **0 vulnerabilities**；升级后真实 stdio 握手、13 个工具发现和两家 API 调用均已验证。

```powershell
tools-service/.venv/Scripts/python -m pytest tools-service/tests -q
npm audit --prefix tools-service
```

测试覆盖：只读取白名单凭据、子进程凭据隔离、工具白名单/schema 保留、参数拒绝、工具错误与 Key 脱敏、官方 SDK 结果转换、连接错误分类、取消与连接关闭。测试使用假凭据，不访问上游服务。

真实联网验证的边界：2026-10-26，高德西湖 POI 查询返回 `USER_DAILY_QUERY_OVER_LIMIT`，Tavily 西湖/UNESCO 检索返回账户 `plan usage limit`，不能视为已拿到地点/检索成功数据。同日单次杭州天气查询成功，返回 4 天天气预报；这只证明天气能力可用，不代表 POI 配额或路线功能恢复。地点和检索接口需恢复可用额度后再验收，不以模拟资料代替真实结果。
