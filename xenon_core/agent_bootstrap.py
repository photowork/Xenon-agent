from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from xenon_core.execution_context import (
    SandboxContext,
    ToolHealthChecker,
    create_default_context,
)
from xenon_core.delivery_closure import GitStatusProbe, DeliveryReport
from xenon_core.multi_agent_runtime import MultiAgentRuntime


def build_core_management_tools() -> Dict[str, Dict[str, Any]]:
    return {
        "load_module_tool": {
            "type": "function",
            "function": {
                "name": "load_module",
                "description": "加载指定模块的所有工具描述，加载后可直接使用该模块的全部工具。一次可加载多个模块。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "module_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "要加载的模块名称列表，如 ['code_editor_handler', 'file_manager']",
                        }
                    },
                    "required": ["module_names"],
                },
            },
        },
        "tool_description_tool": {
            "type": "function",
            "function": {
                "name": "get_tool_description",
                "description": "获取指定工具的详细描述和参数信息。当某个工具不在已加载模块中时，可用此工具单独获取。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool_name": {
                            "type": "string",
                            "description": "工具名称，例如 terminal_handler_Terminal_get_system_info",
                        }
                    },
                    "required": ["tool_name"],
                },
            },
        },
        "get_module_tools_tool": {
            "type": "function",
            "function": {
                "name": "get_module_tools",
                "description": "获取指定模块下的所有工具名称列表，不含详细参数。加载工具请使用 load_module。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "module_name": {
                            "type": "string",
                            "description": "模块名称，例如 terminal_handler",
                        }
                    },
                    "required": ["module_name"],
                },
            },
        },
    }


def initialize_context_manager(
    *,
    context_manager_cls: Any,
    tiktoken_available: bool,
    max_context_tokens_test: Optional[int],
    max_context_tokens_default: int,
    logger: Any,
    print_fn: Callable[..., Any] = print,
) -> Any:
    context_manager = None
    if tiktoken_available:
        try:
            max_tokens = (
                max_context_tokens_test
                if max_context_tokens_test is not None
                else max_context_tokens_default
            )
            context_manager = context_manager_cls(max_context_tokens=max_tokens)
            print_fn(
                f"\033[38;2;111;208;104m[OK] Context manager initialized (limit: {max_tokens} tokens)\033[0m"
            )
        except Exception as error:
            logger.error("上下文管理器初始化失败: %s", error)
            context_manager = None
    else:
        print_fn("\033[93m[WARN] Context manager unavailable because tiktoken is not installed.\033[0m")
    return context_manager


def bootstrap_agent(
    agent: Any,
    *,
    openai_client_cls: Any,
    api_key: str,
    base_url: str,
    api_timeout: int,
    router_timeout: int,
    tool_manager_cls: Any,
    task_chain_manager_cls: Any,
    memory_manager_cls: Any,
    execution_journal_cls: Any,
    recovery_manager_cls: Any,
    agent_orchestrator_cls: Any,
    cognitive_network_cls: Any,
    context_manager_cls: Any,
    tiktoken_available: bool,
    max_context_tokens_test: Optional[int],
    max_context_tokens_default: int,
    get_cognitive_network_summary_fn: Callable[[], str],
    logger: Any,
    history_dir_name: str = ".agent_history",
    context_dir_name: str = "Context",
    memory_dir: str = "Memory/memory_Write",
    print_fn: Callable[..., Any] = print,
) -> None:
    agent.client = openai_client_cls(api_key=api_key, base_url=base_url, timeout=api_timeout)
    agent.routing_client = openai_client_cls(api_key=api_key, base_url=base_url, timeout=router_timeout)

    agent.current_context = []
    agent.full_conversation_history = []
    agent.compact_history = []
    agent.display_history = []  # 完整对话历史（含工具调用、思考过程），专用于 WebUI 显示，与API提交解耦

    agent.history_dir = Path(history_dir_name)
    agent.history_dir.mkdir(parents=True, exist_ok=True)
    agent.history_session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    agent.tool_manager = tool_manager_cls()
    agent.task_chain_manager = task_chain_manager_cls()
    agent.memory_manager = memory_manager_cls(memory_dir=memory_dir, enable_network=True)
    agent.execution_journal = execution_journal_cls()
    agent.recovery_manager = recovery_manager_cls()
    agent.agent_orchestrator = agent_orchestrator_cls()

    agent.interrupted = False
    agent.approved_tools = set()
    agent.loaded_modules = {}
    agent._max_loaded_modules = 5
    agent.loaded_single_tools = {}

    agent.context_dir = Path(context_dir_name)
    agent.context_dir.mkdir(exist_ok=True)
    agent._original_sigint_handler = None
    agent._in_api_call = False
    agent._tool_executing = False
    agent._pending_user_inputs = []
    agent._pending_user_input_limit = 8
    agent._autonomous_running = False
    agent.orchestration_decision = None
    agent.last_tool_result = None
    agent.last_recovery_plan = None

    agent.cognitive_network = cognitive_network_cls()
    agent.cognitive_network_summary = ""
    agent._active_user_input = ""
    agent._recent_tool_results = []
    agent._recent_tool_result_limit = 8
    agent._recent_tool_result_same_topic_keep = 6
    agent._recent_tool_result_topic_shift_keep = 3
    agent._autonomous_max_phase_stagnation = 3
    agent._autonomous_max_repeated_actions = 3
    agent._autonomous_max_tool_failures = 3
    agent.multi_agent_runtime = MultiAgentRuntime()
    agent._multi_agent_default_subtasks = 2
    inject_host_agent = getattr(agent.tool_manager, "inject_host_agent", None)
    if callable(inject_host_agent):
        inject_host_agent(agent)

    agent.context_manager = initialize_context_manager(
        context_manager_cls=context_manager_cls,
        tiktoken_available=tiktoken_available,
        max_context_tokens_test=max_context_tokens_test,
        max_context_tokens_default=max_context_tokens_default,
        logger=logger,
        print_fn=print_fn,
    )

    agent.cognitive_network_summary = get_cognitive_network_summary_fn() or ""
    if agent.cognitive_network_summary:
        print_fn("\033[38;2;111;208;104m[OK] Cognitive network initialized from Memory/memory_network.json\033[0m")

    # Phase 4: 统一执行上下文 & 沙箱隔离
    agent.sandbox_context = create_default_context(workspace_root=str(Path.cwd()))
    print_fn("\033[38;2;111;208;104m[OK] Sandbox context initialized (isolation: off)\033[0m")

    # Phase 4: 工具健康检查
    agent.health_checker = ToolHealthChecker()
    health_report = agent.health_checker.check_all()
    print_fn(agent.health_checker.format_report_console())

    # Phase 4: 将沙箱上下文注入到 ToolManager 的各个 handler
    inject_sandbox_context = getattr(agent.tool_manager, "inject_sandbox_context", None)
    if callable(inject_sandbox_context):
        injected = inject_sandbox_context(agent.sandbox_context)
        print_fn(f"\033[38;2;111;208;104m[OK] Sandbox context injected to {injected} tool(s)\033[0m")
    else:
        print_fn("\033[93m[WARN] Tool manager does not support sandbox context injection.\033[0m")

    # Phase 5: Git 状态感知
    agent.git_probe = GitStatusProbe()
    git_state = agent.git_probe.probe()
    print_fn(agent.git_probe.format_status_console())
    agent.delivery_report = DeliveryReport(agent.git_probe)

    tools = build_core_management_tools()
    agent.load_module_tool = tools["load_module_tool"]
    agent.tool_description_tool = tools["tool_description_tool"]
    agent.get_module_tools_tool = tools["get_module_tools_tool"]
    agent._stream_callback = None

