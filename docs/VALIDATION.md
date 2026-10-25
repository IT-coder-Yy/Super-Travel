# M1 验证记录

日期：2026-10-26。环境：Windows x64；其他平台通过 GitHub Actions 原生构建环境验证。

本表区分“脚本已提供”“已构建”“已运行”。没有运行记录的项不得标为通过。

| 项目 | 当前状态 | 证据 |
| --- | --- | --- |
| 独立工程与旧项目隔离 | 已建立 | 新目录 `D:\vscode\project\SuperTravelDesktop`；独立仓库 `IT-coder-Yy/SuperTravelAgent` |
| Python JSON/API 测试 | 验证中 | 以本次 pytest 最终结果为准 |
| 前端类型检查与构建 | 待验证 | `npm run build` |
| 浏览器界面与保存恢复 | 待验证 | 本地开发服务器与实际页面操作 |
| 平台脚本测试 | 待验证 | `scripts/test-platform.py` |
| Windows x64 包 | 待验证 | GitHub Actions |
| macOS arm64/x64 包 | 待验证 | GitHub Actions |
| Linux x64 包 | 待验证 | GitHub Actions |
| 0.1.0 → 0.1.1 原生更新 | 待验证 | 不能用只生成清单代替实际安装验证 |

## 已知边界

- 地图与行程是界面示例，未接入远程 MCP 和真实模型规划。
- 本机没有 Windows C++ 编译工具链，不把 Rust 下载成功等同于原生构建成功。
- macOS 签名公证与 Linux 发行版运行兼容性需分别记录，CI 打包成功不能代表所有用户环境均可运行。
