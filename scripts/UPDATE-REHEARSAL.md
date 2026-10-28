# 更新演练

当前实现：Tauri 官方 updater 校验包签名和签名绑定的版本；默认尝试 GH-Proxy 下载公开 GitHub Release 资产，失败回官方地址。更新清单先从构建时配置的固定 GitHub `releases/latest/download/latest.json` 地址读取，网络失败或 404/500 时尝试同一文件的 HTTPS 镜像。镜像不接收本地令牌、模型 Key、账号凭据或带查询参数的地址。未配置地址／公钥的构建显示“未配置更新”，不会宣称已是最新版。

`STA_UPDATE_MIRROR` 可在构建时替换免费镜像前缀，默认 `https://gh-proxy.com/`，设为空字符串可停用。无论清单来自哪个源，安装包均用客户端内置的同一公钥验证签名及绑定版本；清单本身没有独立签名。正式构建的检查与下载请求只允许 HTTPS（包括重定向）。镜像仍可能不可用或缓存旧清单，因此官方源优先，且不把镜像清单当作已验证的软件。原生 updater 在 204 或有效但无新版本的清单时正常结束；HTTP 200 却不是 JSON 时会直接报错，不把错误当作“最新”。这些行为已按锁定的 [tauri-plugin-updater 2.14.0 源码](https://docs.rs/tauri-plugin-updater/2.14.0/src/tauri_plugin_updater/updater.rs.html) 核对。

## 正式 GitHub Release 路径

1. GitHub 仓库配置 secret `TAURI_SIGNING_PRIVATE_KEY`，可选 secret `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`；配置 vars `STA_UPDATER_PUBLIC_KEY`、`STA_UPDATE_ENDPOINT=https://github.com/<owner>/<repo>/releases/latest/download/latest.json`。工具地址 `STA_SHARED_TOOLS_URL` 可选，0.1.2 默认 `http://127.0.0.1:8080`，由开发者自行运行工具服务；本机模式不要求 CI 访问服务，显式 HTTPS 模式仍校验工具目录。此设置不放宽下文更新清单的 HTTPS 规则。签名私钥只进入受信构建任务，工具凭据只配置在服务端，均不进入公开源码或安装包。
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
- 更新只替换程序，本地会话目录独立。双 Agent 与真实行程的业务验证见 [M3](../docs/M3.md)；升级演练不能替代业务验收。本轮不提供通用数据库迁移或任意断电自动回滚。

## Windows 0.1.1 → 0.1.2：发布前与发布后分开验收

正式 0.1.1 的更新地址已编译到程序中，指向 GitHub `latest.json`。0.1.2 未公开发布时，不能用该旧程序的“检查更新”按钮发现候选包；不要为演练修改旧 Release、生产清单或 TLS。已有 `docs/native-update/windows-x86_64.json` 只证明 0.1.0 → 0.1.1，不能覆盖这次升级。

发布前，Windows 签名构建完成后，可在一次性的 GitHub-hosted Windows runner 执行：

```powershell
python scripts/test-windows-update.py
cargo build --manifest-path src-tauri/Cargo.toml --example updater-probe --release --locked --target x86_64-pc-windows-msvc
python scripts/smoke-windows-update.py --artifact-dir artifacts/x86_64-pc-windows-msvc --driver src-tauri/target/x86_64-pc-windows-msvc/release/examples/updater-probe.exe --old-version 0.1.1
```

验证步骤只需要 `STA_UPDATER_PUBLIC_KEY` 公钥，不传签名私钥。脚本从公开 0.1.1 Release 下载并核对 SHA256，在 `RUNNER_TEMP` 安装旧版，创建中文、空格路径下的会话、行程、草稿、收藏和非秘密目录偏好，再调用锁定的真实 Tauri updater：先拒绝坏签名包及截断下载，确认旧安装和数据未变，最后验签并通过 NSIS 安装当前候选包。Windows 插件启动安装器后会直接退出，所以脚本继续核对新程序版本、主程序及 Python 服务与本次构建的 SHA256 一致、已安装服务可运行，并检查数据和目录配置字节保留。成功后才写 `windows-update-report.json`。

该探针不编入客户端。报告明确 `old_client_gui_tested: false`，不把预发布原生安装检查写成旧客户端按钮更新或桌面自动重启检查。脚本拒绝本地和 self-hosted runner：即使指定独立 `/D` 目录，NSIS 仍会更新当前用户安装注册信息，不能仅靠临时文件夹保护真实用户的安装登记。行为已按 [Tauri CLI 2.12.4 的 NSIS 模板](https://github.com/tauri-apps/tauri/blob/tauri-cli-v2.12.4/crates/tauri-bundler/src/bundle/windows/nsis/installer.nsi) 和锁定 updater 2.14.0 源码核对。

发布后，使用正式 0.1.1 的独立测试安装执行真实 GUI 升级：

1. 先确认没有用户正在运行的 SuperTravel，保存测试安装的版本、文件哈希；备份现有 `desktop.json` 的原始字节或“原本不存在”状态，以及受影响的安装登记和快捷方式。只操作这一非秘密偏好文件，不读取模型凭据或共享工具凭据。
2. 使用独立 WebView 数据目录和中文、空格测试数据目录，准备已保存会话、正式行程、未应用草稿、收藏和消息。Windows Tauri 使用系统 Known Folder，不能把修改 `APPDATA` 环境变量当作可靠隔离；临时目录偏好结束后必须恢复。
3. 通过受支持的 Windows UI API 点击“检查更新”，核对发现 0.1.2，再点击“保存并更新”。记录下载、安装与重新启动的实际结果；若需手动重新打开，必须如实记录。
4. 核对界面及可执行文件版本 0.1.2、后端使用的测试数据目录、会话业务数据 deep-equal 和目录偏好 byte-identical。关闭本次测试进程，恢复原偏好、登记与快捷方式，再确认原安装及用户数据未变。
5. 单独保存本轮 0.1.1 → 0.1.2 记录，保留 0.1.0 → 0.1.1 历史证据。正式共享配置若改变安装包，前一候选包的演练不能替代最终包验收。
