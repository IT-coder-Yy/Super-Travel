# 更新演练

当前实现：Tauri 官方 updater 校验包签名和签名绑定的版本；默认尝试 GH-Proxy 下载公开 GitHub Release 资产，失败回官方地址。更新清单先从构建时配置的固定 GitHub `releases/latest/download/latest.json` 地址读取，网络失败或 404/500 时尝试同一文件的 HTTPS 镜像。镜像不接收本地令牌、模型 Key、账号凭据或带查询参数的地址。未配置地址／公钥的构建显示“未配置更新”，不会宣称已是最新版。

`STA_UPDATE_MIRROR` 可在构建时替换免费镜像前缀，默认 `https://gh-proxy.com/`，设为空字符串可停用。无论清单来自哪个源，安装包均用客户端内置的同一公钥验证签名及绑定版本；清单本身没有独立签名。正式构建的检查与下载请求只允许 HTTPS（包括重定向）。镜像仍可能不可用或缓存旧清单，因此官方源优先，且不把镜像清单当作已验证的软件。原生 updater 在 204 或有效但无新版本的清单时正常结束；HTTP 200 却不是 JSON 时会直接报错，不把错误当作“最新”。这些行为已按锁定的 [tauri-plugin-updater 2.14.0 源码](https://docs.rs/tauri-plugin-updater/2.14.0/src/tauri_plugin_updater/updater.rs.html) 核对。

## 正式 GitHub Release 路径

1. GitHub 仓库配置 secret `TAURI_SIGNING_PRIVATE_KEY`，可选 secret `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`；配置 vars `STA_UPDATER_PUBLIC_KEY` 和 `STA_UPDATE_ENDPOINT=https://github.com/<owner>/<repo>/releases/latest/download/latest.json`。私钥只进入受信构建任务，不提交仓库、日志或安装包。
2. `node scripts/set-version.mjs 0.1.0`，提交全部版本文件，再推送 `v0.1.0` tag。CI 在四个原生 runner 打包并测试 Python sidecar。
3. 全部平台成功后才合并清单、创建草稿 Release、上传全部资产，再发布。普通 main/PR 构建只保留 Actions Artifacts；无签名配置时生成不带更新能力的测试包。
4. 安装 0.1.0，创建并保存会话；按相同步骤发布 0.1.1。应用检查到更新后由用户启动下载与安装，验证重启后版本、会话内容和数据目录。
5. 分别记录 Windows、macOS Apple Silicon/Intel、Linux 的安装与实际更新结果。CI 构建成功或 sidecar 测试成功不等于原生更新已实测。macOS ad-hoc 签名不等于 Apple 公证，首次启动和升级后启动需要在目标机器核验。

## 本机离线故障演练

只允许固定 `localhost`／`127.0.0.1` 地址，并显式设置 `STA_ALLOW_LOCAL_UPDATES=1`。正式发布绝不设置此变量。

1. 用同一测试签名密钥构建 0.1.0 和 0.1.1，均设置 `STA_UPDATE_ENDPOINT=http://127.0.0.1:17891/latest.json`、`STA_UPDATER_PUBLIC_KEY` 与 `TAURI_SIGNING_PRIVATE_KEY`；先保存并安装 0.1.0。测试私钥放 `.local/`，不使用或打印生产私钥。
2. 用 `prepare-release.py --target <target-triple> --bundle-dir src-tauri/target/<target-triple>/release/bundle --base-url http://127.0.0.1:17891 --output .local/signed-next` 收集 0.1.1，输入必须包含原生更新包与对应 `.sig`。
3. `python scripts/make-update-fixture.py .local/signed-next/platform.json --output .local/update-fixture`，再运行 `python scripts/serve-updates.py .local/update-fixture`。在已安装旧版中检查并执行更新。
4. 重新安装旧版，在步骤 3 增加 `--tamper`，复制包会被修改一个字节。更新应报验签失败，不能关闭当前正常版本或改动会话文件。
5. 增加 `--announced-version 9.9.9` 而保持原包及签名，测试版本绑定；应拒绝该包。断开服务器、取消下载、保存失败时也不得开始安装。

本机演练只修改 `.local/` 中的副本；原始发行资产保持不变。切换回正式构建时清除 `STA_ALLOW_LOCAL_UPDATES` 和 localhost endpoint，使用正式签名配置重新构建。

## 已实现的退出与存储边界

签名构建后，CI 在两个 macOS 架构和 Linux 上运行 `smoke-native-update.py`：下载公开 0.1.0 的校验和与更新包，在隔离目录启动旧版，调用真实 Tauri 更新插件拒绝篡改包和截断下载，再安装当前签名包并启动新版。会话、正式行程、未应用草稿和自定义目录配置必须逐字节保留，结果写入构建产物的 `native-update-report.json`。该步骤使用只在 CI 编译的 `updater-probe`，不进入客户端；它验证原生更新器，不代表点击了旧客户端的设置按钮。Windows 的完整界面更新流程另行实测。

- 前端在原生关闭和更新之前等待 JSON 保存；保存失败保持窗口打开。
- Rust 关闭 Python stdin，请求服务正常结束；Windows JobObject 同时管理 PyInstaller 子进程，主程序异常结束时清理后台进程。
- 更新只替换程序，本地会话目录独立。未实现通用数据库迁移、任意断电自动回滚或双 Agent 业务执行，不能用骨架演练声称这些能力完成。
