# Client

React/TypeScript/Vite 工作台与 Tauri 桌面壳。下列命令均在仓库根目录执行。

## 目录与职责

| 路径 | 职责 |
|---|---|
| `src/App.tsx` | 工作台状态协调与页面组合 |
| `src/hooks/useConversations.ts` | 会话加载、保存与恢复 |
| `src/hooks/useAppUpdate.ts` | 应用更新状态与操作 |
| `src/components/` | 工作台组件，包括通用 Dialog 与 Photo |
| `src-tauri/` | Rust 桌面生命周期、凭据、文件保存与可信更新 |
| `tests/e2e/` | Playwright 界面回归 |
| `tests/fixtures/` | 本地应用/模型夹具与带署名的固定照片 |
| `tests/native/` | 原生 GUI 的服务夹具与驱动就绪检查 |
| `scripts/` | 图标、工具/更新配置、原生 GUI 与升级验收 |

## 行程编辑

真实行程的编辑弹窗支持从手柄拖动活动，调整当天顺序，或放到其他日期标签以跨日移动。悬停只高亮日期，不切换或保存；松开后预填目标日期和末尾位置，需要核对位置、时间并保存草稿，再点击“应用修改”。支持移入空日，源日移空后保留；目标满 8 项、重复地点、时间冲突沿用现有校验。拖动中按 Esc、切换日期或在无效落点放下可取消；原有上下移按钮保留，手柄也支持 Enter/空格打开移动表单。不会自动调整其他活动时间。

## 开发、验证与构建

```powershell
npm ci
uv sync --project server --locked
npm run dev
```

根 npm workspace 统一安装依赖，仓库只保留一份 `package-lock.json`。浏览器开发地址为 `http://127.0.0.1:1420`；单独运行 `npm run web` 仅启动前端，不能替代本地后端。桌面开发使用 `npm run desktop:dev`，所需平台依赖见[根说明](../README.md)。模型与工具配置边界见[后端说明](../server/README.md)。

```powershell
npm run build
npm run test:ui
npm run desktop:build
```

Playwright 自行启动隔离后端和 Vite，无须预先启动开发服务器；本机默认使用已安装的 Edge。开场页也使用同一夹具。正式更新、签名和发布流程见[根说明](../README.md)。

后续 Release 的资产文件名如下，`<版本>` 由发布版本号替换；已发布的 v0.1.3 及更早资产保持原名。

| 平台与用途 | 文件名 |
|---|---|
| Windows x64 安装与更新 | `SuperTravel_<版本>_x64-setup.exe` |
| Linux x64 安装与更新 | `SuperTravel_<版本>_amd64.AppImage` |
| macOS Apple Silicon 安装 | `SuperTravel_<版本>_aarch64.dmg` |
| macOS Intel 安装 | `SuperTravel_<版本>_x64.dmg` |
| macOS Apple Silicon 自动更新 | `SuperTravel_<版本>_aarch64-apple-darwin.app.tar.gz` |
| macOS Intel 自动更新 | `SuperTravel_<版本>_x86_64-apple-darwin.app.tar.gz` |

每个更新包保留同名 `.sig` 文件，`latest.json` 的下载 URL 与 `SHA256SUMS.txt` 同步使用新文件名；更新清单中的平台标识保持不变，以兼容已安装客户端。

## 界面测试照片

以下 JPEG 是 2026-10-26 从应用实际使用的 Wikimedia 缩略图响应缓存的原始字节，未再次编辑。只由 Playwright 业务测试返回，不进入生产应用；生产仍请求远程图片。

| 文件 | 作者与原作品 | 缩略尺寸 |
| --- | --- | --- |
| [tests/fixtures/west-lake-sunset.jpg](tests/fixtures/west-lake-sunset.jpg) | [DXR：桥畔日落](https://commons.wikimedia.org/wiki/File:Sunset_over_the_Zigzag_Bridge,_West_Lake,_Hangzhou_20090909_1.jpg) | 960px |
| [tests/fixtures/west-lake-twilight.jpg](tests/fixtures/west-lake-twilight.jpg) | [Tyler Valentine：西湖暮色](https://commons.wikimedia.org/wiki/File:West_lake_twilight.jpg) | 500px |

照片均按 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) 使用，来源与产品的 README、设置页一致。

默认业务回归产生 `artifacts/workspace-fixture-*.png`，它们不证明远程图片可用。设置 `STA_LIVE_PHOTOS=1` 并仅运行标题含“真实 Wikimedia”的测试，会直接访问远程图片、记录响应/失败原因并生成 `artifacts/preview-network.png`。真实照片检查失败不会被静默替换为 fixture 成功。

## 三平台原生业务界面验收

`client/scripts/native-gui.py --target <当前平台 Rust target>` 在一次性 CI runner 执行，使用真实系统 WebView：Windows WebView2、macOS WKWebView、Linux WebKitGTK。

Linux CI 安装系统中文字体 `fonts-noto-cjk`，截图必须能正常显示中文；测试不向产品前端注入字体或改变其样式。用户在精简 Linux 系统上也需要可用的中文系统字体。

- 点击原生工作台设置模型与工具服务；创建空会话；普通聊天直接回答、信息不足时澄清；生成两天行程；切换日期与点击地图点；聊天生成修改草稿；填写费用、验证时间冲突拦截；重新定位、应用保存；关闭整个原生进程后重启恢复。
- 模型是明确标注的本机 HTTPS OpenAI 格式夹具，工具是本机 HTTP 共享工具夹具。调用生产 Python Agent、HTTP 与 JSON 存储模块。这里不证明真实模型回答质量、高德配额或公网底图始终可用；真实服务验证另行记录。
- Rust `native-gui-tests` feature 只为测试加入 embedded WebDriver；生产默认构建不启用。前端保持正式 `client/dist`，没有浏览器 mock、前端测试分支或 Vite 服务。测试应用使用独立 identifier 和系统凭据服务名。
- HTTPS 夹具 CA 只临时追加至 CI Python 的 certifi，测试 sidecar 单独冻结到 `.local/native-gui/<随机目录>/bin`，随后恢复 certifi。不会安装到操作系统信任库。正式 sidecar 和已签名 Release 安装包均在此前产生，脚本断言它们未被测试构建替换。
- 因多了测试驱动和 CA，这是同生产代码的原生 GUI 测试变体，不是与发布安装包逐字节相同的构建。最终安装包的启动、更新与数据保留仍由原有 native smoke/update 流程验收。
- 驱动在真实 WebView 内派发 DOM 点击和输入事件，验证业务控件与原生渲染兼容性；不冒充真人物理键鼠、输入法或操作系统文件选择器的人工验收。

启动时最多等待 60 秒，只有驱动 `/status` 明确返回 `ready: true` 后才创建会话；HTTP 服务已响应不等于原生 WebView 已初始化。超时仍使验收失败，报告保留最后驱动状态和原生进程退出码。

报告：`artifacts/<target>/native-gui/report.json`。正常有五张实际 WebView 截图及 SHA-256；每张截图前验证工作台已挂载、开场动画消失、底层不再 inert，生成/重启恢复截图还必须等待真实底图瓦片加载且无加载/错误提示，避免把遮罩下的 DOM 断言误当成视觉通过。每张截图记录 `workspaceVisible` 与已加载瓦片数 `mapTilesLoaded`。失败保留截图和 DOM 文本，CI 失败以阻止发布。日志 marker 为 `SUPERTRAVEL_NATIVE_GUI_REPORT:` 后接单行 JSON。独立上传 artifact 为 `native-gui-<target>`。

参考（2026-10-28 查证）：

- [Tauri 官方 WebDriver 指南](https://v2.tauri.app/develop/tests/webdriver/)：推荐 embedded driver，支持 macOS；独立 tauri-driver 仍不支持 WKWebView。
- [WebdriverIO 插件安装与测试 feature](https://webdriver.io/docs/desktop-testing/tauri/plugin-setup)：仅测试启用 `tauri-plugin-wdio-webdriver`。
- [WebdriverIO 官方实现](https://github.com/webdriverio/desktop-mobile/tree/main/packages/tauri-plugin-webdriver)：原生平台执行器、W3C 路由和回环监听地址。本项目用 Python 标准库调用这些公开 W3C 协议，不引入第二套 npm 测试运行器。

公交接驳可展开供应商首方案的线路、上下车站、换乘次数与参考总票价；保留键盘展开方式。供应商返回的有效总票价（含 0）原样展示，并注明“仅供参考，以实际结果为准”；缺失值仍显示未知。公交/驾车接驳还可展开同次响应的出租车估算、时段及计价备注；有效零出租车金额同样原样展示并注明仅供参考，缺失显示暂无估算。公交与出租车是替代选项，不相加；交通参考价不计入活动费用小计。旧行程缺详情可继续读取；地点、方式变化或记录过期时不展示旧票价，展开详情不会额外查询地图。
