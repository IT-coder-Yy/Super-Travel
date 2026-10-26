# 外部服务接入

用户只填写模型 URL、模型名、API Key。地图与检索连接、服务凭据、额度由开发者统一维护，不增加用户工具密钥表单。

客户端 Python 直接使用各家现有服务；不需要运行或部署此前的 `tools-service` 网关，也不需要安装各家 Node MCP 包。旧网关源码已移除，历史实现仍可从 Git 查证。

| 服务 | 当前连接 | 开放能力 | 当前验证 |
| --- | --- | --- | --- |
| 高德地图 | 官方 Streamable HTTP MCP | 11 个地点、路线、天气工具 | 远程发现成功；此前天气成功，部分 POI 调用额度不足 |
| 百度地图 | 官方 Streamable HTTP MCP | 11 个地点、路线、天气、坐标工具 | 远程发现成功；单次杭州西湖查询返回 10 条 |
| Tavily | 官方 Streamable HTTP MCP | Search、Extract | 远程发现成功；此前检索返回额度不足，本次未重复查询 |
| Serper | 官方搜索 API | 网页搜索 | 单次查询成功 |
| 小红书公开内容 | Serper 站内公开索引搜索 | 公开笔记链接和搜索摘要 | 单次查询成功；不是官方小红书 MCP，不读取登录后笔记全文 |

小红书旧实现本身是自定义适配器，并非一个已部署的官方公共 MCP 地址。本轮恢复其公开内容搜索能力，不启用旧 `XHS_COOKIE`，不包含点赞、评论或发布操作。

## 开发配置

在启动 `npm run dev` 的进程中显式设置 `STA_DEV_ENV_FILE`，指向开发者自己的环境文件。启动脚本只读取以下字段并传入本机 Python 内存，不复制旧文件，不传给浏览器，也不写入会话 JSON：

- 模型：`SAGE_BASE_URL`、`SAGE_MODEL_NAME`、`SAGE_API_KEY`。
- 外部服务：`AMAP_MAPS_API_KEY`、`BAIDU_MAP_API_KEY`、`TAVILY_API_KEY`、`SERPER_API_KEY`。

工具字段也可通过 Python 进程的同名环境变量配置。缺少某一项只影响对应服务，不阻止模型聊天或其他工具。`STA_MCP_URL` 仅保留作为显式自定义入口兼容选项；通常应不设置它，以使用各家直连。

官方地址在后端固定，防止误配置把凭据发送到无关域名。高德使用 `key` 参数，百度使用 `ak` 参数，Tavily 使用 `Authorization: Bearer`，Serper 使用 `X-API-KEY`。凭据与基础 URL 分开传入传输层；拒绝重定向，日志和返回数据会遮盖密钥及其 URL 编码。

## 状态与额度

设置页初次打开只读取本机状态，不自动查询上游。点击“检查工具服务”才发现远程 MCP 工具；Serper 与小红书的目录由本地适配器提供，目录注册不会冒充真实 API 已验证。

“已配置”“连接情况”“最近调用”分别记录。额度不足保留配置与工具，不自动重试这次失败，也不假定所有供应商都在同一天重置额度。额度恢复后重新发起需求即可，无需改接入代码。某家暂时不可连接时，其他服务仍能返回工具列表。

## 发布边界

当前正式 Release 仍为 0.1.1；这些能力属于 0.1.2 开发分支。本机联调可以读取开发者的 `.env`，公开安装包不能包含开发者的共享密钥。用户只配置模型的产品约定不变；正式分发前还需落实由我们维护的凭据代理或供应商支持的受限授权方式，该部分尚未部署。凭据代理不等于重新实现百度、高德等外部服务。

## 验证与来源

[真实调用记录](external-services-validation.json)：生产 `ExternalTools` 发现 26 个只读工具；百度、Serper、小红书公开索引各最多查询一次，没有模型调用或失败重试。257 条内存日志扫描未发现原始或 URL 编码密钥，原日志及完整搜索结果均未保存。

接入方式依据：[高德官方文档](https://lbs.amap.com/api/mcp-server/gettingstarted)、[百度官方 MCP](https://github.com/baidu-maps/mcp/blob/main/README_zh.md)、[Tavily 官方 MCP 文档](https://docs.tavily.com/documentation/mcp)、[Serper 官方服务](https://serper.dev/)。不把工具发现成功当成所有业务接口均有额度。
