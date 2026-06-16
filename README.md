# Xenon Agent

> 基于递归对话与动态工具编排的 AI 智能体系统。集成代码编辑、网络搜索、语音交互、视觉识别、文档处理、3D 建模、因果推理、计算引擎等 20+ 工具模块，配备完整的 WebUI、多智能体协作与自主运行框架。
>
> An AI agent system based on recursive dialogue and dynamic tool orchestration, featuring 20+ tool modules including code editor, web search, speech, vision, document processing, 3D modeling, causal reasoning, and a computational engine, with a complete WebUI, multi-agent collaboration, and autonomous runtime framework.

![Xenon 运行界面](assets/screenshots/xenon.jpg)

## 功能特性

- **🧠 递归对话架构** — 对话轮次管理、上下文裁剪与压缩、记忆持久化、认知网络
- **🛠️ 动态工具编排** — 按需加载工具模块，每轮对话自动注册/卸载，含工具路由、调度、执行监控
- **🌐 完整 WebUI** — 浏览器端交互界面，支持流式 SSE 响应、会话管理、工具调用可视化
- **📝 代码编辑** — 文件导航、精准定位、安全写入与回退、代码搜索
- **🔍 网络搜索** — 实时网页搜索与内容抓取
- **👁️ 视觉识别** — 图像分析与 OCR 文字提取（多 API 后端）
- **🗣️ 语音交互** — 语音识别 (ASR) 与语音合成 (TTS)
- **📄 文档处理** — PDF、Word、Excel、WPS 读写
- **🔌 SSH** — 远程服务器连接与命令执行
- **📊 数据可视化** — 图表生成与网页视频渲染
- **📦 程序打包** — 一键打包为可执行程序
- **🧮 计算引擎** — 符号计算、数值计算、统计分析
- **🔗 因果推理** — 因果图建模、反事实分析、路径分析
- **🏗️ FreeCAD 集成** — 3D 参数化建模与自动化
- **🤖 多智能体协作** — 子智能体管理与任务分派
- **🔄 任务编排** — 可编程任务链，支持步骤依赖与条件分支
- **📚 技能系统** — 可沉淀经验的技能文档管理与自动加载
- **🚀 自主运行** — 意图识别、自主规划、执行与修复循环

## 快速开始

### 环境要求

- Python 3.10 – 3.13（推荐 3.10 / 3.11）
- Windows / Linux / macOS

### 安装

```bash
# 克隆仓库
git clone https://github.com/photowork/Xenon-agent.git
cd Xenon-agent

# 创建虚拟环境（推荐）
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 配置环境变量

创建 `.env` 文件（可参考下方环境变量表）或直接设置系统环境变量：

| 变量名 | 说明 | 必需 |
|--------|------|------|
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | ✅ |
| `XENON_VISION_API_KEY` | 视觉识别 API 密钥（支持 ZHIPUAI/BIGMODEL/GLM） | ❌ |
| `DASHSCOPE_API_KEY` | 阿里云通义 API 密钥（ASR/TTS） | ❌ |
| `XENON_WEB_APP_KEY` | WebUI 应用密钥 | ❌ |
| `GITHUB_TOKEN` / `GH_TOKEN` | GitHub 个人访问令牌 | ❌ |

### 启动方式

**方式一：WebUI 启动器（推荐）**

```bash
python launcher.py
```

图形化界面启动，支持一键打开浏览器、切换模型、配置环境变量。

**方式二：命令行直接启动**

```bash
python Xenon.py
```

**方式三：一键脚本**

```bash
# Windows
start_Xenon.bat     # 命令行模式
start_webui.bat     # WebUI 模式

# Linux/macOS
sh start_webui.sh
```

## 项目结构

```
Xenon-agent/
├── Xenon.py                        # 主程序入口
├── launcher.py                     # WebUI 启动器
├── deepseekconfig.py               # API 配置
├── requirements.txt                # Python 依赖
├── .gitignore                      # Git 忽略规则
├── LICENSE                         # MIT 开源协议
├── xenon_logo.ico                  # 应用图标
├── start_Xenon.bat                 # 命令行启动脚本 (Windows)
├── start_webui.bat                 # WebUI 启动脚本 (Windows)
├── start_Terminal.bat              # 终端启动脚本 (Windows)
├── start_webui.sh                  # WebUI 启动脚本 (Linux/macOS)
│
├── xenon_core/                     # 核心引擎（41 个模块）
│   ├── agent_bootstrap.py          # 智能体启动引导
│   ├── agent_orchestrator.py       # 智能体编排器
│   ├── autonomy_runtime.py         # 自主运行运行时
│   ├── boot_report.py              # 启动报告
│   ├── chat_entry.py               # 对话入口
│   ├── chat_runtime.py             # 对话运行时
│   ├── cli_runtime.py              # 命令行运行时
│   ├── cognitive_network.py        # 认知网络（记忆图谱）
│   ├── cognitive_signal_runtime.py # 认知信号处理
│   ├── context_runtime.py          # 上下文管理运行时
│   ├── context_tooling.py          # 上下文工具管理
│   ├── context_trim.py             # 上下文裁剪
│   ├── delivery_closure.py         # 交付闭环
│   ├── eval_runtime.py             # 自评估运行时
│   ├── execution_context.py        # 执行上下文
│   ├── execution_journal.py        # 执行日志
│   ├── history_runtime.py          # 历史记录
│   ├── message_flow.py             # 消息流控制
│   ├── model_request.py            # 模型请求封装
│   ├── multi_agent_runtime.py      # 多智能体运行时
│   ├── orchestration_runtime.py    # 编排运行时
│   ├── phase_policy.py             # 阶段策略
│   ├── project_memory.py           # 项目记忆管理
│   ├── prompt_runtime.py           # 提示词运行时
│   ├── recovery_manager.py         # 故障恢复
│   ├── response_runtime.py         # 响应生成运行时
│   ├── runtime_control.py          # 运行时控制
│   ├── runtime_health.py           # 运行时健康检查
│   ├── self_model.py               # 自我模型管理
│   ├── semantic_router_runtime.py  # 语义路由
│   ├── tool_catalog.py             # 工具目录
│   ├── tool_dispatch.py            # 工具调度
│   ├── tool_execution.py           # 工具执行
│   ├── tool_feedback.py            # 工具反馈
│   ├── tool_observability.py       # 工具可观测性
│   ├── tool_payload_runtime.py     # 工具载荷运行时
│   ├── tool_router.py              # 工具路由分配
│   ├── tool_runtime.py             # 工具运行时
│   ├── turn_compactor.py           # 轮次压缩
│   └── turn_runtime.py             # 轮次运行时
│
├── Tools/                          # 工具模块
│   ├── code_editor/                # 代码编辑器（含导航器）
│   ├── soul/                       # 自我模型与灵魂引擎
│   ├── ssh_handler/                # SSH 远程连接
│   ├── asr_handler/                # 语音识别
│   ├── tts_handler/                # 语音合成
│   ├── freecad_handler/            # FreeCAD 3D 建模集成
│   ├── causal_reasoner.py          # 因果推理引擎
│   ├── chart_handler.py            # 图表生成
│   ├── code_navigator.py           # 代码智能导航
│   ├── computational_engine.py     # 计算引擎
│   ├── context_manager_tool.py     # 上下文管理
│   ├── debug_handler.py            # 调试工具
│   ├── ds_balance.py               # DeepSeek 余额查询
│   ├── excel_handler.py            # Excel 处理
│   ├── file_manager.py             # 文件管理
│   ├── github_manager.py           # GitHub 管理
│   ├── memory_query_handler.py     # 记忆查询
│   ├── pdf_handler.py              # PDF 处理
│   ├── program_packager.py         # 程序打包
│   ├── skill_handler.py            # 技能文档管理
│   ├── sub_agent_handler.py        # 子智能体管理
│   ├── task_chain_handler.py       # 任务链编排
│   ├── terminal_handler.py         # 终端执行
│   ├── video_handler.py            # 视频处理
│   ├── vision_tool.py              # 视觉识别
│   ├── web_search_handler.py       # 网络搜索
│   ├── web_video_renderer.py       # 网页视频渲染
│   ├── word_handler.py             # Word 处理
│   └── wps_handler.py              # WPS Office 处理
│
├── webui/                          # Web 界面
│   ├── index.html                  # 前端页面
│   ├── main.py                     # 后端服务
│   ├── database.py                 # SQLite 会话存储
│   ├── stream_adapter.py           # SSE 流式适配
│   ├── start.bat                   # Windows 启动脚本
│   ├── start.sh                    # Linux 启动脚本
│   └── README.md                   # WebUI 说明文档
│
├── prompts/                        # 提示词
│   └── prompt.md                   # 系统提示词
│
├── Skills/                         # 技能文档（可沉淀经验）
├── assets/                         # 静态资源
│   └── screenshots/                # 截图
└── README.md                       # 项目介绍
```

## 核心概念

### 递归对话

Xenon 的核心是递归对话架构：每一轮对话开始时会重新加载工具，结束时卸载并持久化记忆。这种设计确保了会话的隔离性和状态的可控性。

### 动态工具编排

所有工具模块在对话启动时按需加载，执行完毕后自动卸载。工具通过统一的接口注册到工具目录（`tool_catalog.py`），由路由（`tool_router.py`）分配执行，支持调度、执行监控与结果反馈的全链路追踪。

### 记忆与自我模型

系统维护一个持续演进的认知网络（`cognitive_network.py`），记录交互历史、偏好、错误模式、决策依据等。自我模型模块（`self_model.py` / `soul/`）管理智能体的身份认知、状态感知和递归内省。

### 编排与自主运行

编排运行时（`orchestration_runtime.py`）管理多阶段任务执行，支持从意图识别到计划、执行、验证的完整闭环。自主运行时（`autonomy_runtime.py`）赋予智能体在没有用户指令时主动探索、学习和自我改进的能力。

### 多智能体协作

多智能体运行时（`multi_agent_runtime.py`）支持创建和管理子智能体，每个子智能体可独立执行子任务，并通过消息总线与主智能体通信协作。

## 工具模块一览

| 模块 | 功能 | 激活方式 |
|------|------|---------|
| 代码编辑 & 导航 | 文件导航、编辑、搜索、智能跳转 | `load_module(['code_editor_handler'])` |
| 文件管理 | 文件读写、目录操作 | `load_module(['file_manager'])` |
| 网络搜索 | 搜索引擎查询、内容抓取 | `load_module(['web_search_handler'])` |
| 视觉识别 | 图像分析、文字识别 | `load_module(['vision_tool'])` |
| 语音识别 | 语音转文字 | `load_module(['asr_handler'])` |
| 语音合成 | 文字转语音 | `load_module(['tts_handler'])` |
| PDF 处理 | PDF 读取、信息提取 | `load_module(['pdf_handler'])` |
| Word/Excel/WPS | 文档表格读写 | `load_module(['word_handler', 'excel_handler', 'wps_handler'])` |
| SSH | 远程服务器连接与命令执行 | `load_module(['ssh_handler'])` |
| GitHub | 仓库文件管理 | `load_module(['github_manager'])` |
| 视频处理 | 视频生成与处理 | `load_module(['video_handler'])` |
| 程序打包 | 打包为可执行文件 | `load_module(['program_packager'])` |
| 图表 | 数据可视化图表 | `load_module(['chart_handler'])` |
| 因果推理 | 因果图建模、反事实分析 | `load_module(['causal_reasoner'])` |
| 计算引擎 | 符号/数值/统计分析 | `load_module(['computational_engine'])` |
| 任务编排 | 多步骤任务链 | `load_module(['task_chain_handler'])` |
| 子智能体 | 子智能体创建与管理 | `load_module(['sub_agent_handler'])` |
| 技能管理 | 技能文档的读写与版本管理 | `load_module(['skill_handler'])` |
| 3D 建模 | FreeCAD 参数化建模 | `load_module(['freecad_handler'])` |
| 调试 | 日志分析、错误诊断 | `load_module(['debug_handler'])` |
| DeepSeek 余额 | 查询 DeepSeek 账户余额 | `load_module(['ds_balance'])` |
| 记忆查询 | 认知网络检索与关联分析 | `load_module(['memory_query_handler'])` |
| 网页视频渲染 | 网页内容渲染为视频 | `load_module(['web_video_renderer'])` |
| 终端 | 系统命令执行 | `load_module(['terminal_handler'])` |
| 上下文管理 | 上下文状态检查与压缩 | `load_module(['context_manager_tool'])` |

## 开源协议

本项目采用 MIT 协议开源。
