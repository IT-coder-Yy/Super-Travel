# Server

Python 后端负责本地 API、会话存储、双 Agent 和只读工具接入。桌面应用管理自己的本地 sidecar；工具凭据服务由开发者单独启动。下列命令均在仓库根目录执行。

## 目录与职责

| 路径 | 职责 |
|---|---|
| `src/` | `.py` 文件与生成的 `assets/` 直接置于此处；逻辑包 `supertravel_server` 由 `pyproject.toml` 映射到该目录 |
| `src/main.py` | 命令行、回环端口、数据目录锁和父进程退出 |
| `src/app.py` | FastAPI 应用与路由组合 |
| `src/access.py` | 来源、访问令牌与请求大小校验 |
| `src/conversations.py` | 会话模型、原子保存、修订与备份恢复 |
| `tests/` | 后端业务、存储和工具回归 |
| `scripts/` | 资源准备、PyInstaller 冻结、工具服务启动与验证 |
| `pyproject.toml`、`uv.lock`、`.python-version` | Python 3.13、运行/dev 依赖及锁定版本 |
| `.env.example` | 本机工具凭据模板，不填写真实值 |

## 依赖与启动

```powershell
uv sync --project server --locked
npm ci
npm run dev
```

根开发命令同时启动 Python 与 Vite，开发数据使用 `.local/dev-data`。只开发桌面时使用 `npm run desktop:dev`；桌面壳自行管理 Python，不与浏览器开发服务同时启动。可通过 `STA_PYTHON` 指定解释器，默认优先使用 `server/.venv`。

添加运行依赖使用 `uv add --project server 包名`，开发依赖使用 `uv add --project server --dev 包名`，一起提交 `pyproject.toml` 和 `uv.lock`。不再维护 requirements 文件。

## 本机工具服务

工具服务复用已有官方 MCP/API 连接，只接收工具名称及查询参数；模型请求和本地会话不会经过它。四项工具密钥留在服务进程环境中，客户端只配置地址。

桌面默认连接 `http://127.0.0.1:8080`，工具服务需要开发者另行启动，不随安装包自动运行。安装项目依赖后，在仓库根目录准备自己的环境文件（字段见 [.env.example](.env.example)），再执行：

```powershell
$env:STA_DEV_ENV_FILE = ".local/tools.env"
npm run tools:dev
```

macOS/Linux 可使用 `STA_DEV_ENV_FILE=.local/tools.env npm run tools:dev`。启动脚本只从该文件加载 `AMAP_MAPS_API_KEY`、`BAIDU_MAP_API_KEY`、`TAVILY_API_KEY`、`SERPER_API_KEY` 四项工具凭据，不加载模型 Key；真实环境文件不要提交到 Git。服务默认只监听 `127.0.0.1:8080`，端口可用 `STA_TOOLS_PORT` 修改。客户端设置中可填写对应服务地址，仅允许 HTTPS 或回环 HTTP，不能包含用户名、密码、查询参数或片段。

当前没有已上线的公共服务，本机开发和发布不会自动部署服务器。本机模式的正式 tag 构建无需运行或访问这项工具服务；显式使用 HTTPS 服务时，CI 仍检查目录中的凭据状态和地点工具。`GET /healthz` 只用于进程健康检查；验证工具可用性仍需目录和真实只读查询。

环境模板只列四项凭据；`STA_TOOLS_PORT`、`STA_TOOLS_PER_IP_MINUTE`、`STA_TOOLS_DAILY_LIMIT`、`STA_TOOLS_MAX_CONCURRENT` 如需调整，应作为启动进程的环境变量设置，启动脚本不会从凭据文件加载这些选项。

## 工具协议与限额

| 端点 | 行为 |
| --- | --- |
| `GET /healthz` | `{ "ok": true }`，不访问上游 |
| `GET /v1/status` | `{ "providers": [...] }`，读取内存状态 |
| `GET /v1/tools` | `{ "tools": [...], "providers": [...] }`；目录最多每 5 分钟刷新 |
| `POST /v1/call` | 接受 `{ "name": "...", "arguments": {} }`，返回 `{ "result": ..., "providers": [...] }` |

失败返回非 2xx 和 `{ "error": { "code": "...", "retryable": false, "message": "固定安全提示" } }`。公共状态不会包含其他客户端的 `lastCall`。服务不接受任意转发地址、请求头或工具凭据，参数必须通过已发现工具的 JSON Schema 校验；每个请求最多 16 KiB，读取正文最多 10 秒。

默认每 IP 每分钟 20 次调用请求、全局每天 1000 次、最多 4 个同时调用；超限返回 429，不调用上游。日额度按 UTC 日重置。通过准入的无效请求和失败请求也计数，避免通过无效参数绕过入口保护。

这些限额保存在单进程内存中，重启会重置；本地工具启动脚本固定使用 1 worker。

默认关闭请求访问日志，不记录查询内容。固定异常提示与已有凭据脱敏同时使用，不能依靠客户端隐藏共享密钥。

## PDF 资源

字体和表情图片由脚本下载，不保存在 Git 中。`npm run dev`、`npm run desktop:dev` 会在启动前检查；桌面正式构建和 CI 也会自动准备。首次需联网下载固定版本并校验摘要，后续复用本地完整资源；下载中断或文件损坏可重试，不会把未校验的半文件当作可用资源。

手动准备（所有平台使用同一条命令）：

```powershell
npm run assets:prepare
```

生成文件在 `server/src/assets/`，源下载缓存在 `.local/pdf-assets/`，均被 Git 忽略。首次无网且资源缺失时，开发界面仍可启动，但 PDF 导出会提示资源不可用；联网运行上述命令并重启开发服务后重试。正式打包遇到不完整资源会停止，成功生成的安装包内含全部资源，安装后离线导出。

资源使用 [Noto Sans SC](https://github.com/google/fonts/tree/6c1dbe27dcf7d817d4b62cec033ba77fa8e2a5ec/ofl/notosanssc)（SIL OFL 1.1，固定 400 字重）、[Jigmo 2025-09-12](https://kamichikoichi.github.io/jigmo/)（CC0 1.0，标准 CJK 回退子集）和 [Twemoji 17.0.3](https://github.com/jdecked/twemoji/tree/b6b55fef1e8636b540a6d016a4729ca8cdf2e60b)（Twitter and contributors，图形 CC BY 4.0）。脚本同步获取许可证并生成来源说明，随安装包分发；含 emoji 的 PDF 保留署名。支持范围取决于固定字库及完整 emoji 序列，未支持字符会明确报错。

## 验证与打包

```powershell
npm run test:backend
npm run test:assets
node --test server/scripts/tools-dev.test.mjs
```

后端测试入口会先准备 PDF 资源，再运行 `server/tests/` 和 `client/tests/native/`。PyInstaller 在临时构建目录中按逻辑包名组织源码，将资源收进 `supertravel_server/assets/`；正式构建和原生验收共用冻结函数，源码仍保持扁平目录。桌面构建入口为 `npm run desktop:build`，详见[客户端说明](../client/README.md)。

## 公交方案记录

`incomingRoute.transit` 是可选字段，包含首方案 `segments`（`mode`、可选 `lineName` / `departureStop` / `arrivalStop`）、可选 `transferCount` 和 `fare`（整数 `amountFen`、`currency: CNY`）。百度读取 `routes[0].price`，高德读取 `transits[0].cost`；只取同一方案的分段，每组可替代线路仅取第一条，不累加分段票价。有效总票价（含 0）在界面和导出原样展示，并注明“仅供参考，以实际结果为准”；缺失值仍显示未知，有效耗时仍保留。

字段随现有路线记录保存、草稿应用、上一版恢复及 Markdown/PDF 正式导出；旧 JSON 无需迁移。接驳凭据校验仍比较完整服务端记录，客户端和模型不能自行附加票价；交通参考价不进入用户填写的活动费用小计。
