# SuperTravel Desktop

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

用户确认 Tauri 2、React、TypeScript、Vite 与本地 Python/FastAPI；Windows、macOS、Linux 首发。

## Users

希望用中文规划国内单目的地、1～7 天行程的个人用户，使用自己的模型 API Key。

## Product Purpose

本地优先旅行助手。规划 Agent 负责沟通、澄清与任务计划，执行 Agent 调用远程工具。会话和行程以 JSON 存在本机用户目录，可自定义。修改已有行程先预览草稿，应用后才替换正式版。

## Capabilities and Constraints

当前只建设 M1：独立桌面骨架、Python 生命周期、JSON 保存与恢复、三平台打包和可信更新。模型规划和真实地图服务尚未接入；示例行程和示意地图必须明确标注，不能伪装为已生成的真实结果。不读取旧项目运行数据，不迁移旧数据，不创建账号。用户已授权创建公开发布仓库 `IT-coder-Yy/SuperTravelAgent`。

## Brand Commitments

按用户指定的 https://github.com/IT-coder-Yy/SuperTravel-Agent README Preview 实现 C1 Glacier White 风格：冰川白、深蓝文字、蓝色操作按钮、西湖地图主画布、窄侧栏、浮动日程与底部对话框。参考是概念图，不是实时地图截图。

## Evidence on Hand

已核对远程 README，Preview 与旧项目 assets/preview/supertravelagent-c1-concept.png 一致。旧项目仅作为只读参考；新代码独立编写。

## Product Principles

- 程序升级和本地用户数据分离。
- 当前阶段未实现的能力明确显示状态。
- 交互与示意地图结构继承用户参考图。
- 密钥不随日志、会话 JSON 或安装包分发。
