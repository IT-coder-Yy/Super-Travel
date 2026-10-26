# M1 验证记录

日期：2026-10-26。环境：Windows x64；其他平台通过 GitHub Actions 原生构建环境验证。

本表区分“脚本已提供”“已构建”“已运行”。没有运行记录的项不得标为通过。

| 项目 | 当前状态 | 证据 |
| --- | --- | --- |
| 独立工程与旧项目隔离 | 已建立 | 新目录 `D:\vscode\project\SuperTravelDesktop`；独立仓库 `IT-coder-Yy/SuperTravelAgent` |
| Python JSON/API 测试 | 33 项通过 | 独立 Python 3.13 虚拟环境，验证鉴权、原子保存、冲突、损坏恢复及进程退出 |
| 前端类型检查与构建 | 通过 | `npm run build` |
| 浏览器界面与保存恢复 | 通过：Edge，3 项开场测试与 2 项工作台测试 | 独立会话、草稿刷新恢复/放弃/应用、保存中编辑锁定、聊天保存恢复、更新未配置状态、1024px 无横向溢出；业务测试用固定照片，真实网络照片另行检查 |
| 损坏会话恢复 | API 与隔离真实界面通过 | 正常历史不受影响；有效备份确认后恢复、原件保留、过期扫描拒绝；恢复其他会话前保存当前草稿，恢复过程中禁用相关操作 |
| 平台脚本测试 | 10 项通过 | `scripts/test-platform.py`；独立测试签名另验证版本不匹配拒绝 |
| Windows 冻结 Python 服务 | 通过 | PyInstaller 6.20.0；清除 Python 环境变量且 `python` 命令不可解析后，独立 EXE 通过鉴权、JSON 保存读取、409 及 EOF 退出 |
| Windows x64 包 | 0.1.0 与 0.1.1 签名包已发布，实际升级通过 | 从旧版设置页发现 0.1.1、保存并更新、安装重启；原生进程版本 0.1.1 |
| Windows 桌面保存与正常退出 | 通过 | 原生 WebView2 中创建会话、正式行程、聊天与草稿；延迟 PUT 时窗口等待写入再关闭，退出后无本应用后台进程 |
| macOS arm64/x64 包 | 0.1.0 签名包已发布；0.1.1 构建与桌面启动通过 | 主分支 66c5cbb 的两个原生 runner 实际启动 .app，验证自身后端健康与父进程结束后的清理 |
| Linux x64 包 | 0.1.0 签名 AppImage 已发布；0.1.1 构建与桌面启动通过 | 主分支 66c5cbb 在 Xvfb 下实际启动 AppImage，验证自身后端健康与父进程结束后的清理 |
| 0.1.0 → 0.1.1 原生更新 | 四平台通过：Windows GUI 全流程；macOS arm64/x64、Linux x64 原生更新器 | Windows 会话/正式行程/草稿/聊天内容深比较一致、中文空格自定义目录配置逐字节一致；其他三平台通过旧版启动、篡改/截断包拒绝、签名安装、新版启动及配置/JSON保留。逐平台报告见 `docs/native-update/` |

证据：[v0.1.0 Release](https://github.com/IT-coder-Yy/SuperTravelAgent/releases/tag/v0.1.0)、[完整发布流水线](https://github.com/IT-coder-Yy/SuperTravelAgent/actions/runs/40215652664)、[0.1.1 四平台构建及原生启动验证](https://github.com/IT-coder-Yy/SuperTravelAgent/actions/runs/40215693390)。公开 `latest.json` 的四个平台键与版本签名字段已核对。

本轮四平台构建与三平台原生更新器证据：[329c63d Actions](https://github.com/IT-coder-Yy/SuperTravelAgent/actions/runs/40287148893)。真实 Wikimedia 照片另行联网检查通过，README 截图来自无图片请求拦截的实际页面。

用户已选择 C2 地图定位图标与 A 点云轨道开场，已随 [0.1.1 发布](https://github.com/IT-coder-Yy/SuperTravelAgent/releases/tag/v0.1.1)。开场时长约 3 秒，按任意键跳过，无跳过按钮；按键不传入底层工作区，支持减少动态效果和 Canvas 不可用时直接进入。[发布流水线](https://github.com/IT-coder-Yy/SuperTravelAgent/actions/runs/40288435265)通过，Windows 实际升级完成后已正常退出测试客户端并恢复测试前的目录偏好，测试数据保留。

## 已知边界

- 本文件记录 0.1.1 的 M1 骨架，地图与行程是界面示例；后续双 Agent 接入另见 [M2 进展](M2.md)。
- 0.1.1 尚无模型连接设置与系统凭据存储，不能将该版本视为 M1 全部完成或第一阶段验收通过。
- 本机没有 Windows C++ 编译工具链，不把 Rust 下载成功等同于原生构建成功。
- macOS 签名公证与 Linux 发行版运行兼容性需分别记录，CI 打包成功不能代表所有用户环境均可运行。
