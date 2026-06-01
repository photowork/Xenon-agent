# Xenon Web UI

基于 `Xenon.py` 的网页对话界面，支持流式响应、会话管理、工具调用展示和中断控制。

## 功能

- 响应式页面，适配桌面端和移动端
- SSE 流式输出，实时展示思考、工具调用和最终回答
- 会话创建、切换、删除
- SQLite 持久化存储会话与消息
- 工具调用和工具结果的可视化展示
- 可随时停止生成

## 启动前提

- 建议始终使用项目根目录下的虚拟环境启动：
  - `D:\Xenon\agent_Xenon\venv\Scripts\python.exe`
- 确保项目根目录存在：
  - `deepseekconfig.py`
  - `Xenon.py`
- 首次运行前建议先安装依赖：

```bash
venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 启动方式

### Windows

在项目根目录执行：

```bash
start_webui.bat
```

或者在 `webui` 目录执行：

```bash
start.bat
```

### Linux / Termux

在项目根目录执行：

```bash
sh start_webui.sh
```

也可以在 `webui` 目录执行：

```bash
sh start.sh
```

低性能设备建议保持默认的 `XENON_WEBUI_PREWARM=auto`：Termux 会跳过启动预热，普通 Linux 会把预热放到后台，避免浏览器打开时提示服务未就绪。

### 手动启动

在项目根目录执行：

```bash
venv\Scripts\python.exe webui\main.py
```

服务默认启动在 [http://localhost:8000](http://localhost:8000)。

## 使用说明

1. 打开 [http://localhost:8000](http://localhost:8000)
2. 点击“+ 新对话”创建会话
3. 输入消息并发送
4. 观察流式输出中的思考、工具调用和回答
5. 如需中断，点击“停止”

## API

- `GET /`：前端页面
- `GET /sessions`：获取会话列表
- `POST /sessions`：创建会话
- `DELETE /sessions/{session_id}`：删除会话
- `GET /sessions/{session_id}/messages`：获取会话消息
- `POST /chat/{session_id}`：发送消息并接收流式响应
- `POST /chat/{session_id}/stop`：停止生成

## 目录结构

```text
webui/
├── main.py
├── database.py
├── stream_adapter.py
├── index.html
├── start.bat
└── README.md
```

## 常见问题

### 启动后提示依赖缺失

请确认你使用的是项目 `venv` 中的 Python，而不是系统 Python：

```bash
venv\Scripts\python.exe -c "import sys; print(sys.executable)"
```

### 页面打不开

确认 `webui\main.py` 已成功启动，并检查 `8000` 端口是否被占用。

### 工具能力与 CLI 表现不一致

Web UI 和 CLI 都依赖同一个主程序入口 `Xenon.py`。如果两者表现不同，优先检查启动时输出的运行时健康检查和工具加载报告。
