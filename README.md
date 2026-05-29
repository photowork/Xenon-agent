# Xenon Agent

> 基于递归对话与动态工具编排的 AI 智能体系统。包含代码编辑器、浏览器、语音、视频等工具模块，以及完整的 WebUI 和任务编排引擎。
>
> An AI agent system based on recursive dialogue and dynamic tool orchestration, featuring code editor, browser, speech, and video modules, along with a complete WebUI and task orchestration engine.

![Xenon 运行界面](assets/screenshots/xenon.jpg)

## 功能特性

- **🧠 递归对话架构** — 对话轮次管理、上下文裁剪与压缩、记忆持久化
- **🛠️ 动态工具编排** — 按需加载工具模块，每轮对话自动注册/卸载
- **🌐 完整 WebUI** — 浏览器端交互界面，支持流式响应
- **📝 代码编辑** — 文件导航、精准定位、安全写入与回退
- **🔍 网络搜索** — 实时网页搜索与内容抓取
- **👁️ 视觉识别** — 图像分析与 OCR 文字提取
- **🗣️ 语音交互** — 语音识别 (ASR) 与语音合成 (TTS)
- **📄 文档处理** — PDF、Word、Excel 读写
- **🔌 SSH** — 远程服务器连接
- **📊 数据可视化** — 图表生成与视频渲染
- **📦 程序打包** — 一键打包为可执行程序

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

创建 `.env` 文件或直接设置系统环境变量：

| 变量名 | 说明 | 必需 |
|--------|------|------|
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | ✅ |
| `XENON_VISION_API_KEY` | 视觉识别 API 密钥 | ❌ |
| `DASHSCOPE_API_KEY` | 阿里云通义 API 密钥（ASR/TTS） | ❌ |
| `XENON_WEB_APP_KEY` | WebUI 应用密钥 | ❌ |

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

## 项目结构

```
Xenon-agent/
├── Xenon.py                        # 主程序入口
├── launcher.py                 # WebUI 启动器
├── requirements.txt            # Python 依赖
├── .gitignore                  # Git 忽略规则
│
├── xenon_core/                 # 核心引擎
│   ├── agent_bootstrap.py      # 智能体启动引导
│   ├── chat_entry.py           # 对话入口
│   ├── chat_runtime.py         # 对话运行时
│   ├── context_tooling.py      # 上下文工具管理
│   ├── context_trim.py         # 上下文裁剪
│   ├── history_runtime.py      # 历史记录
│   ├── project_memory.py       # 项目记忆管理
│   ├── tool_catalog.py         # 工具目录
│   ├── tool_router.py          # 工具路由分配
│   ├── turn_runtime.py         # 轮次运行时
│   └── turn_compactor.py       # 轮次压缩
│
├── Tools/                      # 工具模块
│   ├── code_editor/            # 代码编辑器
│   ├── soul/                   # 自我模型模块
│   ├── ssh_handler/            # SSH 远程连接
│   ├── asr_handler/            # 语音识别
│   ├── tts_handler/            # 语音合成
│   ├── chart_handler.py        # 图表生成
│   ├── context_manager_tool.py # 上下文管理
│   ├── debug_handler.py        # 调试工具
│   ├── excel_handler.py        # Excel 处理
│   ├── file_manager.py         # 文件管理
│   ├── github_manager.py       # GitHub 管理
│   ├── memory_query_handler.py # 记忆查询
│   ├── pdf_handler.py          # PDF 处理
│   ├── program_packager.py     # 程序打包
│   ├── sub_agent_handler.py    # 子智能体管理
│   ├── task_chain_handler.py   # 任务链编排
│   ├── terminal_handler.py     # 终端执行
│   ├── video_handler.py        # 视频处理
│   ├── vision_tool.py          # 视觉识别
│   ├── web_search_handler.py   # 网络搜索
│   ├── web_video_renderer.py   # 网页视频渲染
│   └── word_handler.py         # Word 处理
│
├── webui/                      # Web 界面
│   ├── index.html              # 前端页面
│   ├── main.py                 # 后端服务
│   └── stream_adapter.py       # 流式适配
│
├── prompts/                    # 提示词
│   └── prompt.md               # 系统提示词
│
├── Skills/                     # 技能文档
├── configs/                    # 配置文件
└── README.md                   # 项目介绍
```

## 核心概念

### 递归对话

Xenon 的核心是递归对话架构：每一轮对话开始时会重新加载工具，结束时卸载并持久化记忆。这种设计确保了会话的隔离性和状态的可控性。

### 动态工具编排

所有工具模块在对话启动时按需加载，执行完毕后自动卸载。工具通过统一的接口注册到工具目录（`tool_catalog.py`），由路由（`tool_router.py`）分配执行。

### 记忆与自我模型

系统维护一个持续演进的记忆网络，记录交互历史、偏好、错误模式等。自我模型模块（`soul_handler.py`）管理智能体的身份认知和状态感知。

## 工具模块一览

| 模块 | 功能 | 激活方式 |
|------|------|---------|
| 代码编辑器 | 文件导航、编辑、搜索 | `load_module(['code_editor_handler'])` |
| 文件管理 | 文件读写、目录操作 | `load_module(['file_manager'])` |
| 网络搜索 | 搜索引擎查询、内容抓取 | `load_module(['web_search_handler'])` |
| 视觉识别 | 图像分析、文字识别 | `load_module(['vision_tool'])` |
| 语音识别 | 语音转文字 | `load_module(['asr_handler'])` |
| 语音合成 | 文字转语音 | `load_module(['tts_handler'])` |
| PDF 处理 | PDF 读取、信息提取 | `load_module(['pdf_handler'])` |
| Word/Excel | 文档表格读写 | `load_module(['word_handler', 'excel_handler'])` |
| SSH | 远程服务器连接 | `load_module(['ssh_handler'])` |
| GitHub | 仓库文件管理 | `load_module(['github_manager'])` |
| 视频处理 | 视频生成与处理 | `load_module(['video_handler'])` |
| 程序打包 | 打包为可执行文件 | `load_module(['program_packager'])` |
| 图表 | 数据可视化图表 | `load_module(['chart_handler'])` |

## 开源协议

本项目采用 MIT 协议开源。
