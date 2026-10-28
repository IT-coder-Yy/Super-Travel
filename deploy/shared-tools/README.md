# 共享工具凭据服务

此服务只代理已有只读工具，复用 `ExternalTools` 中的官方 MCP/API 连接。四项共享密钥仅从服务端环境变量读取；客户端只配置服务 HTTPS 地址，不接收密钥。模型请求和本地会话不会经过本服务，只发送工具名称及查询参数。

## 部署

在仓库根目录执行：

```sh
docker build -f deploy/shared-tools/Dockerfile -t supertravel-tools:0.1.2 .
docker run -d --name supertravel-tools --restart unless-stopped \
  --env-file /secure/supertravel-tools.env \
  -p 127.0.0.1:8080:8080 --read-only --tmpfs /tmp \
  supertravel-tools:0.1.2
```

环境文件字段见 `.env.example`。真实文件由运营方保管，不进入镜像、Git、安装包或日志。专用 `Dockerfile.dockerignore` 在放行父目录后重新排除其全部内容，再逐文件放行四个适配器/服务源码、`requirements.txt` 和两个 Docker 构建文件，避免父目录规则连带包含 `.venv`、`.env` 或用户数据；`.local` 与 Git 也被排除。规则遵循 [Docker 的顺序匹配语义](https://docs.docker.com/build/concepts/context/#dockerignore-files)。容器只复制工具适配器及服务模块，不包含模型配置和会话模块；以非 root 用户运行。

公网必须通过稳定的 HTTPS 反向代理访问。默认关闭代理请求头信任，此时限额按连接到服务的实际 IP 统计；放在反向代理后时，需要在部署命令显式使用 `--proxy-headers --forwarded-allow-ips <可信代理IP>` 替代 `--no-proxy-headers`，同时由代理删除客户端提供的转发头并重新填写。不能使用任意来源 `*`。若全部请求被统计为代理 IP，会共同受到单 IP 限额。

HTTP 健康检查使用 `GET /healthz`。它不查询上游，不能作为工具凭据可用性的证明。上线时还需检查 `GET /v1/tools`，并完成至少一次真实只读查询。项目不自动把本机 `.env` 上传到任何平台。

## 协议与限额

| 端点 | 行为 |
| --- | --- |
| `GET /healthz` | `{ "ok": true }`，不访问上游 |
| `GET /v1/status` | `{ "providers": [...] }`，读取内存状态 |
| `GET /v1/tools` | `{ "tools": [...], "providers": [...] }`；目录最多每 5 分钟刷新 |
| `POST /v1/call` | 接受 `{ "name": "...", "arguments": {} }`，返回 `{ "result": ..., "providers": [...] }` |

失败返回非 2xx 和 `{ "error": { "code": "...", "retryable": false, "message": "固定安全提示" } }`。公共状态不会包含其他客户端的 `lastCall`。服务不接受任意转发地址、请求头或工具凭据，参数必须通过已发现工具的 JSON Schema 校验；每个请求最多 16 KiB，读取正文最多 10 秒。

默认每 IP 每分钟 20 次调用请求、全局每天 1000 次、最多 4 个同时调用；超限返回 429，不调用上游。日额度按 UTC 日重置。通过准入的无效请求和失败请求也计数，避免通过无效参数绕过入口保护。

这些限额保存在单进程内存中，重启会重置；必须使用 1 worker、1 副本。公开免账号接口不能保证完全防止滥用；正式运营还应设置供应商账户额度上限。多副本部署前应改为共享限额存储，本轮不引入数据库、账号或客户端共享秘密。

默认关闭请求访问日志，不记录查询内容。固定异常提示与已有凭据脱敏同时使用，不能依靠客户端隐藏共享密钥。
