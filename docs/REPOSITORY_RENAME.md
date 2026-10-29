# 仓库改名与更新地址核验

2026-10-29，GitHub 仓库由 `IT-coder-Yy/SuperTravelAgent` 更名为 [`IT-coder-Yy/Super-Travel`](https://github.com/IT-coder-Yy/Super-Travel)。仓库 ID 仍为 `1433408128`。

- 本地工程 `D:\vscode\project\SuperTravelDesktop` 的 origin（fetch/push）已改为 `https://github.com/IT-coder-Yy/Super-Travel.git`，已通过 `git ls-remote` 验证；0.1.3 标签保持原提交。
- Actions 变量 `STA_UPDATE_ENDPOINT` 已改为 `https://github.com/IT-coder-Yy/Super-Travel/releases/latest/download/latest.json`。更新公钥配置和签名 Secret 保留。
- 两个原生升级验证脚本的默认仓库、Tauri User-Agent 链接及 Markdown 仓库链接已更新。发布 workflow 和 `ci-build.mjs` 使用动态 `GITHUB_REPOSITORY`，后续构建自动生成新仓库的下载地址。
- 现有 4 个 Release 的 ID、全部资产 ID、大小与 GitHub 摘要均与改名前一致；没有替换已发布安装包或清单。

## 实际验证

| 检查 | 结果 |
| --- | --- |
| 新、旧官方 `releases/latest/download/latest.json` | 均 HTTP 200，内容一致，版本 0.1.3 |
| 清单 SHA-256 | `e1e5b25af1d69921afc10e7d8b794b757bc6b0f762436fe69defbfa8f3ed9a3f`，与发布时记录相同 |
| Windows、macOS 双架构、Linux 的新旧包地址 | 8 次 HEAD 均 HTTP 200，Content-Length 与发布资产大小一致 |
| `gh-proxy.com` 的新旧更新清单入口 | 均 HTTP 200，内容与官方清单完全一致 |
| 发布与更新配置回归 | 23 项通过；新地址生成官方及镜像入口，仍要求版本签名并禁止降级 |

## 已发布版本的兼容方式

0.1.3 及更早安装包内嵌的更新入口、已发布 `latest.json` 的包地址仍使用旧仓库名，本次保留其原始字节。旧版通过已验证的 GitHub 跳转继续读取更新；之后发布的新安装包使用新 Actions 地址。历史验证 JSON 保留当时实际请求的地址，不改写历史证据。

本次验证地址、清单内容、下载响应和配置回归，没有重新安装或执行一次版本升级。公开镜像可用性是检查时的结果。

不要在同一账号重新创建名为 `SuperTravelAgent` 的仓库，否则旧客户端依赖的跳转会失效，见 [GitHub 仓库改名说明](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository)。旧项目仓库 `SuperTravel-Agent` 与本次改名对象不同。
