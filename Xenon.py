import os
import sys
import json
import logging
import hashlib
import copy
from difflib import SequenceMatcher
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Union, Tuple

__version__ = "0.4.5"
APP_VERSION = __version__

from xenon_core.cognitive_network import CognitiveNetworkState
from Tools.memory_query_handler import SmartMemoryToolManager
from Tools.task_chain_handler import TaskChainToolManager
from xenon_core.agent_bootstrap import bootstrap_agent as core_bootstrap_agent
from xenon_core.agent_orchestrator import AgentOrchestrator, ActionDecision
from xenon_core.autonomy_runtime import (
    build_autonomous_decision as core_build_autonomous_decision,
    build_internal_resume_prompt as core_build_internal_resume_prompt,
    enqueue_pending_user_input as core_enqueue_pending_user_input,
    get_phase_memory_snapshot as core_get_phase_memory_snapshot,
    run_autonomous_cycle as core_run_autonomous_cycle,
    run_autonomous_tick as core_run_autonomous_tick,
    select_active_goal as core_select_active_goal,
    should_resume_task as core_should_resume_task,
    update_autonomous_progress as core_update_autonomous_progress,
)
from xenon_core.context_runtime import (
    ContextManager as CoreContextManager,
    TokenCounter as CoreTokenCounter,
    calculate_actual_tokens as core_calculate_actual_tokens,
    do_context_cleanup as core_do_context_cleanup,
    format_context_token_info as core_format_context_token_info,
    get_actual_context_status as core_get_actual_context_status,
)
from xenon_core.chat_entry import handle_user_chat_entry as core_handle_user_chat_entry
from xenon_core.cli_runtime import run_interactive_agent_session
from xenon_core.chat_runtime import run_chat_cycle as core_run_chat_cycle
from xenon_core.model_request import build_chat_completion_kwargs
from xenon_core.cognitive_signal_runtime import (
    get_cognitive_network_summary as core_get_cognitive_network_summary,
    inject_cognitive_network_summary as core_inject_cognitive_network_summary,
)
from xenon_core.execution_journal import ExecutionJournal
from xenon_core.history_runtime import (
    persist_full_history_snapshot as core_persist_full_history_snapshot,
    save_api_request as core_save_api_request,
    save_memory_log as core_save_memory_log,
    save_turn_debug_trace as core_save_turn_debug_trace,
)
from xenon_core.recovery_manager import RecoveryManager
from xenon_core.runtime_control import (
    handle_interrupt as core_handle_interrupt,
    interruptible_sleep as core_interruptible_sleep,
    restore_signal_handler as core_restore_signal_handler,
    retry_request as core_retry_request,
    setup_signal_handler as core_setup_signal_handler,
)
from xenon_core.recursion_detector import RecursionDetector
from xenon_core.context_tooling import handle_context_manager_tool_call as core_handle_context_manager_tool_call
from xenon_core.context_trim import (
    auto_trim_context as core_auto_trim_context,
    ensure_context_size as core_ensure_context_size,
)
from xenon_core.orchestration_runtime import (
    prepare_orchestration_decision as core_prepare_orchestration_decision,
)
from xenon_core.prompt_runtime import (
    build_available_tools_message as core_build_available_tools_message,
    build_runtime_system_messages as core_build_runtime_system_messages,
    build_system_prompt as core_build_system_prompt,
    load_prompts as core_load_prompts,
)
from xenon_core.message_flow import (
    append_conversation_message as core_append_conversation_message,
    clone_message as core_clone_message,
    ensure_message_integrity as core_ensure_message_integrity,
    extract_tool_call_id as core_extract_tool_call_id,
    find_pending_tool_call_ids as core_find_pending_tool_call_ids,
)
from xenon_core.multi_agent_runtime import build_subagent_prompt
from xenon_core.project_memory import (
    build_project_memory_text as core_build_project_memory_text,
    cleanup_old_summaries_if_healthy as core_cleanup_old_summaries_if_healthy,
    collect_summary_source_messages as core_collect_summary_source_messages,
    emergency_context_clear as core_emergency_context_clear,
    extract_current_checkpoint as core_extract_current_checkpoint,
    generate_smart_summary as core_generate_smart_summary,
    get_project_memory_dir as core_get_project_memory_dir,
    get_project_memory_path as core_get_project_memory_path,
    inject_recent_memory_summary as core_inject_recent_memory_summary,
    write_project_memory_files as core_write_project_memory_files,
)
from xenon_core.response_runtime import (
    cleanup_reasoning_content as core_cleanup_reasoning_content,
    process_non_streaming_response as core_process_non_streaming_response,
    process_streaming_response as core_process_streaming_response,
    validate_and_fix_json as core_validate_and_fix_json,
)
from xenon_core.semantic_router_runtime import (
    build_semantic_router_catalog as core_build_semantic_router_catalog,
    infer_semantic_route as core_infer_semantic_route,
    parse_semantic_route_response as core_parse_semantic_route_response,
)
from xenon_core.tool_payload_runtime import (
    compress_tool_messages_in_place as core_compress_tool_messages_in_place,
    summarize_tool_payload_for_context as core_summarize_tool_payload_for_context,
)
from xenon_core.turn_compactor import (
    TIMESTAMP_SYSTEM_PREFIXES as CORE_TIMESTAMP_SYSTEM_PREFIXES,
    compact_history_for_next_context as core_compact_history_for_next_context,
    compact_turn_for_next_context as core_compact_turn_for_next_context,
    sanitize_messages_for_api as core_sanitize_messages_for_api,
    trim_compact_history as core_trim_compact_history,
)
from xenon_core.tool_catalog import (
    authorize_single_tool as core_authorize_single_tool,
    build_current_tools as core_build_current_tools,
    handle_get_module_tools_call as core_handle_get_module_tools_call,
    handle_get_tool_description_call as core_handle_get_tool_description_call,
    handle_load_module_call as core_handle_load_module_call,
    is_single_tool_loaded as core_is_single_tool_loaded,
    is_tool_loaded as core_is_tool_loaded,
    reset_loaded_tools_for_new_turn as core_reset_loaded_tools_for_new_turn,
    touch_single_tool as core_touch_single_tool,
    unload_module as core_unload_module,
)
from xenon_core.tool_dispatch import handle_tool_call_batch as core_handle_tool_call_batch
from xenon_core.tool_feedback import (
    apply_tool_outcome_state,
    build_tool_outcome_state,
)
from xenon_core.tool_execution import execute_tool_call as core_execute_tool_call
from xenon_core.tool_observability import (
    append_recent_tool_result,
    build_tool_call_snapshot as build_core_tool_call_snapshot,
    collect_recent_failures,
    decay_recent_tool_results,
    get_recent_tool_results,
    safe_stringify_result,
)
from xenon_core.turn_runtime import run_chat_turn as core_run_chat_turn
from xenon_core.tool_runtime import ToolManager as CoreToolManager

try:
    from openai import OpenAI
except ImportError:
    print("\033[91m错误: openai 模块未安装。\033[0m")
    print("\033[91m安装命令: pip install openai\033[0m")
    sys.exit(1)

try:
    import deepseekconfig as deepseek_config
except ImportError:
    print("\033[91m错误: deepseekconfig 模块未找到。请创建 deepseekconfig.py 文件并设置 API_KEY, BASE_URL, MODEL。\033[0m")
    sys.exit(1)

PROJECT_ROOT = Path(__file__).resolve().parent
HISTORY_DIR = PROJECT_ROOT / ".agent_history"
CONTEXT_DIR = PROJECT_ROOT / "Context"
TRACE_LOG_DIR = PROJECT_ROOT / "logs" / "api_traces"

API_KEY = deepseek_config.API_KEY
BASE_URL = deepseek_config.BASE_URL
MODEL = getattr(deepseek_config, "MODEL", "deepseek-v4-flash")
AVAILABLE_MODELS = list(dict.fromkeys(getattr(deepseek_config, "AVAILABLE_MODELS", [MODEL])))
if MODEL not in AVAILABLE_MODELS:
    AVAILABLE_MODELS.insert(0, MODEL)

# 导入tiktoken用于token计数
try:
    import tiktoken
    TIKTOKEN_AVAILABLE = True
except ImportError:
    TIKTOKEN_AVAILABLE = False
    print("\033[93m警告: tiktoken 模块未安装，上下文管理功能将受限。\033[0m")
    print("\033[93m安装命令: pip install tiktoken\033[0m")

MAX_RETRY_ATTEMPTS = 3
NETWORK_RETRY_DELAY = 2
API_TIMEOUT = 120  # API 请求超时时间（秒）
MAX_TOOL_RECURSION_DEPTH = 100  # 单轮内工具递归调用上限，超过后注入提醒但继续执行

ENABLE_STREAMING = True  # 是否启用流式响应
ENABLE_THINKING_MODE = getattr(deepseek_config, "ENABLE_THINKING_MODE", True)  # 使用 DeepSeek thinking 开关
REASONING_EFFORT = getattr(deepseek_config, "REASONING_EFFORT", "high")
SUMMARY_THINKING_ENABLED = getattr(deepseek_config, "SUMMARY_THINKING_ENABLED", False)
ROUTER_THINKING_ENABLED = getattr(deepseek_config, "ROUTER_THINKING_ENABLED", False)
ENABLE_API_REQUEST_LOGGING = True # 开启后会打印所有API请求和响应，包括敏感信息，仅用于调试
ENABLE_MEMORY_LOGGING = False  # 开启后会记录所有交互和模型推理过程，包括敏感信息，仅用于调试

# 上下文 token 上限配置
MAX_CONTEXT_TOKENS_DEFAULT =1000000  # 默认上下文上限，可根据需要调整
OUTPUT_TOKEN_RESERVE = 8000  # 为模型输出预留的 token 余量，防止输入+输出超限

# 【测试用】上下文 token 上限，设为 None 使用默认值，设为较小值可快速触发裁剪测试
MAX_CONTEXT_TOKENS_TEST = None  # 例如: 10000

# 上下文保护配置（保留给旧接口兼容；当前压缩以 checkpoint 为主，不再依赖最近 N 轮）
PROTECTED_CONVERSATION_ROUNDS = 3

# 智能摘要配置
SUMMARY_MODEL = getattr(deepseek_config, "SUMMARY_MODEL", MODEL)  # 摘要默认关闭 thinking，仍使用新版模型名
SUMMARY_MAX_TOKENS = 800  # 摘要最大token数
SUMMARY_TIMEOUT = 30  # 摘要生成超时时间（秒）

ENABLE_API_REQUEST_LOGGING = getattr(deepseek_config, "ENABLE_API_REQUEST_LOGGING", ENABLE_API_REQUEST_LOGGING)
CONTEXT_COMPACT_AFTER_TURN = getattr(deepseek_config, "CONTEXT_COMPACT_AFTER_TURN", True)
KEEP_RAW_TRACE_LOG = getattr(deepseek_config, "KEEP_RAW_TRACE_LOG", True)
INCLUDE_TOOL_RESULTS_IN_NEXT_TURN = getattr(deepseek_config, "INCLUDE_TOOL_RESULTS_IN_NEXT_TURN", False)
INCLUDE_REASONING_IN_HISTORY = getattr(deepseek_config, "INCLUDE_REASONING_IN_HISTORY", False)
MAX_COMPACT_HISTORY_TURNS = getattr(deepseek_config, "MAX_COMPACT_HISTORY_TURNS", 20)
MAX_DISPLAY_HISTORY_MESSAGES = 200  # display_history 最大保留消息数（显示用，与API提交无关）
SUMMARY_MAX_TOKENS = 1200
SUMMARY_TIMEOUT = 45
ROUTER_MODEL = getattr(deepseek_config, "ROUTER_MODEL", MODEL)
ROUTER_TIMEOUT = 20
ROUTER_MAX_TOKENS = 450
SUMMARY_RECENT_MESSAGE_LIMIT = 60
SUMMARY_MAX_TOOL_SNIPPET = 800
SUMMARY_MAX_MESSAGE_SNIPPET = 1200
SUMMARY_INJECTION_MAX_CHARS = 4000
COMPRESSED_TOOL_RESULT_MAX_CHARS = 240
COMPRESSED_TOOL_RESULT_MAX_LINES = 8
PROJECT_MEMORY_FILENAME = "project_context_latest.txt"
PROJECT_MEMORY_SNAPSHOT_PREFIX = "project_context_snapshot_"

PROMPTS_DIR = PROJECT_ROOT / "prompts"

def load_prompts() -> str:
    """
    加载 prompts 文件夹中的所有文档内容
    
    Returns:
        合并后的提示词内容字符串
    """
    return core_load_prompts(prompts_dir=PROMPTS_DIR, logger=logger)

SYSTEM_PROMPT_BASE = r"""你是 Xenon，一个智能助手。

运行时会另外注入当前时间、文件系统、工具状态、任务编排、上下文状态、自我模型和成长摘要；这些动态信息优先于静态提示词。

【静态底线】
- 保持自然、有帮助、可协作，不机械展示内部流程。
- 复杂任务先理解目标和约束，再选择工具或行动。
- 读取大文件前先检查大小；超过 50KB 时用搜索、分块读取或只读关键区域。
- 涉及无法核实、实时变化或专业高风险内容时，降低确定性并说明验证路径。
"""

def get_system_prompt() -> str:
    """
    获取系统提示词（实时加载 prompts 文件夹内容）
    
    Returns:
        完整的系统提示词
    """
    return core_build_system_prompt(
        system_prompt_base=SYSTEM_PROMPT_BASE,
        prompts_dir=PROMPTS_DIR,
        logger=logger,
    )


class ColoredFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None, style='%'):
        super().__init__(fmt, datefmt, style)
        self.COLOR_CODE = '\033[38;2;86;114;79m'
        self.RESET_CODE = '\033[0m'

    def format(self, record):
        message = super().format(record)
        return f"{self.COLOR_CODE}{message}{self.RESET_CODE}"


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', encoding='utf-8')
logger = logging.getLogger(__name__)

for handler in logging.root.handlers:
    handler.setFormatter(ColoredFormatter(fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

logging.getLogger('httpx').setLevel(logging.WARNING)


class InterruptedException(Exception):
    """自定义中断异常"""
    pass


class TokenCounter(CoreTokenCounter):
    """Backward-compatible import surface for the extracted context runtime."""


class ContextManager(CoreContextManager):
    """Backward-compatible import surface for the extracted context runtime."""
class ToolManager(CoreToolManager):
    """Backward-compatible import surface for the extracted tool runtime."""


class AIAgent:
    def __init__(self):
        core_bootstrap_agent(
            self,
            openai_client_cls=OpenAI,
            api_key=API_KEY,
            base_url=BASE_URL,
            api_timeout=API_TIMEOUT,
            router_timeout=ROUTER_TIMEOUT,
            tool_manager_cls=ToolManager,
            task_chain_manager_cls=TaskChainToolManager,
            memory_manager_cls=SmartMemoryToolManager,
            execution_journal_cls=ExecutionJournal,
            recovery_manager_cls=RecoveryManager,
            agent_orchestrator_cls=AgentOrchestrator,
            cognitive_network_cls=CognitiveNetworkState,
            context_manager_cls=ContextManager,
            tiktoken_available=TIKTOKEN_AVAILABLE,
            max_context_tokens_test=MAX_CONTEXT_TOKENS_TEST,
            max_context_tokens_default=MAX_CONTEXT_TOKENS_DEFAULT,
            get_cognitive_network_summary_fn=self._get_cognitive_network_summary,
            logger=logger,
            history_dir_name=str(HISTORY_DIR),
            context_dir_name=str(CONTEXT_DIR),
            print_fn=print,
        )
        self.model = MODEL
        self._last_request_estimated_tokens = None
        self._last_api_total_tokens = None
        self._last_live_context_status = None
        self._tool_cycle_count = 0  # 单轮内工具递归循环计数器（替代旧指纹检测器）
        self._recursion_detector = RecursionDetector(threshold=3)  # 内容指纹检测器

    def set_model(self, model: str) -> str:
        if model not in AVAILABLE_MODELS:
            raise ValueError(f"Unsupported model: {model}")
        self.model = model
        return self.model

    def get_model(self) -> str:
        return getattr(self, "model", MODEL)

    def _clone_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        return core_clone_message(message)

    def _record_full_history_message(self, message: Dict[str, Any]):
        if CONTEXT_COMPACT_AFTER_TURN:
            return
        self.full_conversation_history.append(self._clone_message(message))
        self._persist_full_history_snapshot()

    def _append_conversation_message(self, messages: List[Dict], message: Dict[str, Any], record_full_history: bool = True):
        # 始终记录 user/assistant/tool 消息到 display_history（专供 WebUI 显示，与 API 提交解耦）
        role = message.get("role", "")
        if role in {"user", "assistant", "tool"}:
            self.display_history.append(self._clone_message(message))
            if len(self.display_history) > MAX_DISPLAY_HISTORY_MESSAGES:
                self.display_history = self.display_history[-MAX_DISPLAY_HISTORY_MESSAGES:]

        core_append_conversation_message(
            messages,
            message,
            record_full_history=record_full_history and not CONTEXT_COMPACT_AFTER_TURN,
            record_full_history_fn=self._record_full_history_message,
        )

    def _summarize_tool_payload_for_context(self, content: str) -> str:
        return core_summarize_tool_payload_for_context(
            content,
            max_chars=COMPRESSED_TOOL_RESULT_MAX_CHARS,
            max_lines=COMPRESSED_TOOL_RESULT_MAX_LINES,
        )

    def _compress_tool_messages_in_place(
        self,
        messages: List[Dict],
        protected_indices: Optional[set] = None,
        allow_protected: bool = False
    ) -> int:
        return core_compress_tool_messages_in_place(
            messages,
            summarize_tool_payload_fn=self._summarize_tool_payload_for_context,
            protected_indices=protected_indices,
            allow_protected=allow_protected,
        )

    def get_full_context(self) -> List[Dict[str, Any]]:
        # 返回 display_history（含工具调用、思考过程的完整历史）供 WebUI 显示
        # API 提交使用的是 current_context（已被压缩），两者已解耦
        if self.display_history:
            return copy.deepcopy(self.display_history)
        return copy.deepcopy(self.full_conversation_history)

    def set_full_context(self, messages: List[Dict[str, Any]]):
        if CONTEXT_COMPACT_AFTER_TURN:
            # 保存完整版本供 WebUI 显示
            self.display_history = copy.deepcopy(messages or [])
            if len(self.display_history) > MAX_DISPLAY_HISTORY_MESSAGES:
                self.display_history = self.display_history[-MAX_DISPLAY_HISTORY_MESSAGES:]

            # 压缩版本用于 API 提交（省 token），恢复完整展示历史时也按轮次折叠，
            # 避免把没有工具证据的“已执行/将执行”文本带回模型上下文。
            sanitized = core_compact_history_for_next_context(messages or [])
            next_context = core_trim_compact_history(
                sanitized,
                MAX_COMPACT_HISTORY_TURNS,
            )
            self.compact_history = self._compact_history_messages_only(next_context)
            self.full_conversation_history = copy.deepcopy(self.compact_history)
            self.current_context = copy.deepcopy(next_context)
        else:
            self.full_conversation_history = copy.deepcopy(messages or [])
            self.display_history = copy.deepcopy(self.full_conversation_history)
        self._persist_full_history_snapshot()

    def _persist_full_history_snapshot(self):
        core_persist_full_history_snapshot(
            full_conversation_history=self.full_conversation_history,
            history_dir=self.history_dir,
            history_session_id=self.history_session_id,
            logger=logger,
        )

    def _get_cognitive_network_summary(
        self,
        current_query: Optional[str] = None,
        current_phase: Optional[str] = None,
        current_intent: Optional[str] = None,
        recent_failures: Optional[List[str]] = None,
    ) -> str:
        """Build a compact cognitive-state summary from the persisted network."""
        return core_get_cognitive_network_summary(
            cognitive_network=self.cognitive_network,
            cached_summary=self.cognitive_network_summary,
            logger=logger,
            set_cached_summary_fn=lambda summary: setattr(self, "cognitive_network_summary", summary),
            current_query=current_query,
            current_phase=current_phase,
            current_intent=current_intent,
            recent_failures=recent_failures,
        )

    def _inject_cognitive_network_summary(
        self,
        messages: List[Dict],
        current_query: Optional[str] = None,
        current_phase: Optional[str] = None,
        current_intent: Optional[str] = None,
        recent_failures: Optional[List[str]] = None,
    ):
        """Inject the cognitive network as a persistent system-level state signal."""
        core_inject_cognitive_network_summary(
            messages=messages,
            get_cognitive_network_summary_fn=self._get_cognitive_network_summary,
            current_query=current_query,
            current_phase=current_phase,
            current_intent=current_intent,
            recent_failures=recent_failures,
        )

    def set_stream_callback(self, callback):
        """
        设置流式回调函数。
        callback 应接受一个参数：event_dict，包含 type 和 content 等字段。
        """
        self._stream_callback = callback

    def _handle_interrupt(self, signum, frame):
        """处理中断信号"""
        core_handle_interrupt(
            self,
            interrupted_exception_cls=InterruptedException,
        )

    def _setup_signal_handler(self):
        """设置信号处理器"""
        core_setup_signal_handler(self, signal_handler=self._handle_interrupt)

    def _restore_signal_handler(self):
        """恢复原始信号处理器"""
        core_restore_signal_handler(self)

    def _interruptible_sleep(self, seconds: float):
        """可中断的睡眠"""
        core_interruptible_sleep(
            is_interrupted=lambda: self.interrupted,
            seconds=seconds,
            interrupted_exception_cls=InterruptedException,
        )

    def _retry_request(self, func, *args, **kwargs):
        return core_retry_request(
            func,
            *args,
            max_attempts=MAX_RETRY_ATTEMPTS,
            retry_delay=NETWORK_RETRY_DELAY,
            interrupted_exception_cls=InterruptedException,
            logger=logger,
            is_interrupted=lambda: self.interrupted,
            **kwargs,
        )

    def _save_api_request(self, model: str, messages: List[Dict], tools: Optional[List[Dict]] = None):
        core_save_api_request(
            enabled=ENABLE_API_REQUEST_LOGGING and KEEP_RAW_TRACE_LOG,
            model=model,
            messages=messages,
            tools=tools,
            context_dir=TRACE_LOG_DIR,
            logger=logger,
            max_files=20,
            use_dated_subdir=True,
        )
        # 缓存上一轮实际请求体的估算 token 数，供 WebUI 调试显示使用
        try:
            if self.context_manager and self.context_manager.token_counter:
                self._last_request_estimated_tokens = self.context_manager.token_counter.estimate_total_tokens(
                    messages, tools
                )
        except Exception:
            pass

    def _save_turn_debug_trace(self, turn_messages: List[Dict[str, Any]]):
        core_save_turn_debug_trace(
            enabled=KEEP_RAW_TRACE_LOG,
            turn_messages=turn_messages,
            trace_dir=TRACE_LOG_DIR,
            logger=logger,
            metadata={"active_user_input": getattr(self, "_active_user_input", "")},
            max_files=20,
        )

    def _compact_turn_for_next_context(self, turn_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        self._save_turn_debug_trace(turn_messages)

        if INCLUDE_TOOL_RESULTS_IN_NEXT_TURN:
            next_context = core_sanitize_messages_for_api(
                turn_messages,
                preserve_current_toolchain=True,
                include_reasoning=INCLUDE_REASONING_IN_HISTORY,
            )
            compact_snapshot = core_trim_compact_history(
                next_context,
                MAX_COMPACT_HISTORY_TURNS,
            )
            self.compact_history = self._compact_history_messages_only(compact_snapshot)
            self.full_conversation_history = copy.deepcopy(self.compact_history)
            self._persist_full_history_snapshot()
            return copy.deepcopy(next_context)
        else:
            state_messages = self._preserved_state_messages(turn_messages)
            previous_history = core_compact_history_for_next_context(self.full_conversation_history)
            current_turn = core_compact_turn_for_next_context(turn_messages)
            next_context = state_messages + previous_history + current_turn

        next_context = core_trim_compact_history(
            next_context,
            MAX_COMPACT_HISTORY_TURNS,
        )
        self.compact_history = self._compact_history_messages_only(next_context)
        self.full_conversation_history = copy.deepcopy(self.compact_history)
        self._persist_full_history_snapshot()
        return copy.deepcopy(next_context)

    @staticmethod
    def _compact_history_messages_only(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # user/assistant 之外，额外保留提问时间/回答完成时间 system 消息，
        # 让时间戳随轮次沉淀在持久历史中（模型据此感知时间流动）。
        return copy.deepcopy(
            [
                message
                for message in messages
                if message.get("role") in {"user", "assistant"}
                or (
                    message.get("role") == "system"
                    and str(message.get("content", "")).startswith(CORE_TIMESTAMP_SYSTEM_PREFIXES)
                )
            ]
        )

    @staticmethod
    def _preserved_state_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        sanitized = core_sanitize_messages_for_api(messages or [])
        return [
            copy.deepcopy(message)
            for message in sanitized
            if message.get("role") == "system"
            and str(message.get("content", "")).startswith("【任务状态检查点】")
        ]

    def _save_memory_log(self, role: str, content: str = "", reasoning_content: str = ""):
        core_save_memory_log(
            enabled=ENABLE_MEMORY_LOGGING,
            role=role,
            content=content,
            reasoning_content=reasoning_content,
            logger=logger,
        )

    def _parse_arguments(self, arguments_str: str) -> Dict:
        if not arguments_str or arguments_str.strip() == "":
            return {}

        fixed_json = self._validate_and_fix_json(arguments_str)
        if fixed_json is None:
            raise json.JSONDecodeError("无法修复不完整的JSON", arguments_str, 0)
        return json.loads(fixed_json)

    def _add_tool_message(self, messages: List[Dict], tool_call_id: str, content: str):
        tool_message = {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        self._append_conversation_message(messages, tool_message)

    def _handle_tool_error(self, messages: List[Dict], tool_call_id: str, error: Exception, error_type: str):
        logger.error(f"{error_type}失败: {error}")
        print(f"错误: {error}")
        self._add_tool_message(messages, tool_call_id, f"{error_type}错误: {error}")

    def _get_context_token_info(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
    ) -> str:
        """获取上下文token信息"""
        resolved_messages = messages if messages is not None else self.current_context.copy()
        resolved_tools = tools if tools is not None else self._get_current_tools()
        if system_message is None:
            system_message = self._get_available_tools_message() if messages is None else ""

        return core_format_context_token_info(
            messages=resolved_messages,
            system_message=system_message,
            current_tools=resolved_tools,
            context_manager=self.context_manager,
            logger=logger,
        )

    def _build_semantic_router_catalog(self, tool_schemas: List[Dict[str, Any]]) -> str:
        return core_build_semantic_router_catalog(
            tool_schemas=tool_schemas,
            module_names=self.tool_manager.get_module_list(),
        )

    @staticmethod
    def _parse_semantic_route_response(content: str) -> Optional[Dict[str, Any]]:
        return core_parse_semantic_route_response(content)

    def _infer_semantic_route(
        self,
        user_input: str,
        tool_schemas: List[Dict[str, Any]],
        current_task: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        return core_infer_semantic_route(
            user_input=user_input,
            tool_schemas=tool_schemas,
            current_task=current_task,
            get_module_list_fn=self.tool_manager.get_module_list,
            routing_client=self.routing_client,
            router_model=ROUTER_MODEL,
            router_max_tokens=ROUTER_MAX_TOKENS,
            router_thinking_enabled=ROUTER_THINKING_ENABLED,
            router_reasoning_effort=REASONING_EFFORT if ROUTER_THINKING_ENABLED else None,
            logger=logger,
        )

    def _prepare_orchestration_decision(
        self,
        user_input: str,
        internal_context: Optional[Dict[str, Any]] = None,
    ) -> Optional[ActionDecision]:
        return core_prepare_orchestration_decision(
            user_input=user_input,
            internal_context=internal_context,
            context_manager=self.context_manager,
            last_tool_result=self.last_tool_result,
            get_actual_context_status_fn=self._get_actual_context_status,
            get_current_tool_schemas_fn=self._get_current_tool_schemas,
            get_cognitive_network_summary_fn=self._get_cognitive_network_summary,
            get_recent_failures_fn=self._get_recent_failures,
            agent_orchestrator=self.agent_orchestrator,
            task_chain_manager=self.task_chain_manager,
            execution_journal=self.execution_journal,
            set_orchestration_decision_fn=lambda decision: setattr(self, "orchestration_decision", decision),
            logger=logger,
            get_semantic_route_hint_fn=lambda user_input, tool_schemas, current_task: (
                self._infer_semantic_route(
                    user_input=user_input,
                    tool_schemas=tool_schemas,
                    current_task=current_task,
                )
            ),
        )

    def _record_tool_outcome(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        result: Any = None,
        success: bool = True,
        recovery_plan: Optional[Dict[str, Any]] = None,
    ):
        phase = self.orchestration_decision.phase if self.orchestration_decision else "analyze"
        goal = self.orchestration_decision.goal if self.orchestration_decision else ""
        outcome_state = build_tool_outcome_state(
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            success=success,
            recovery_plan=recovery_plan,
            phase=phase,
            goal=goal,
            orchestration_mode=self.orchestration_decision.mode if self.orchestration_decision else None,
            orchestration_next_actions=self.orchestration_decision.next_actions if self.orchestration_decision else None,
            orchestration_reasoning_summary=self.orchestration_decision.reasoning_summary if self.orchestration_decision else "",
            stringify_result_fn=self._safe_stringify_result,
            summarize_payload_fn=self._summarize_tool_payload_for_context,
        )

        self.last_tool_result = outcome_state["last_tool_result"]
        self.last_recovery_plan = outcome_state["last_recovery_plan"]
        apply_tool_outcome_state(
            outcome_state,
            execution_journal=self.execution_journal,
            task_chain_manager=self.task_chain_manager,
            memory_manager=self.memory_manager,
            logger=logger,
        )

    def _safe_stringify_result(self, value: Any, max_chars: int = 1500) -> str:
        return safe_stringify_result(value, max_chars=max_chars)

    def _build_tool_call_snapshot(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        success: bool,
        result: Any,
        error: str = "",
        recovery_summary: str = "",
    ) -> Dict[str, Any]:
        """构建轻量工具结果快照。"""
        return build_core_tool_call_snapshot(
            tool_name=tool_name,
            arguments=arguments,
            success=success,
            result=result,
            phase=self.orchestration_decision.phase if self.orchestration_decision else "analyze",
            summarize_payload_fn=self._summarize_tool_payload_for_context,
            error=error,
            recovery_summary=recovery_summary,
        )

    def _push_recent_tool_result(self, snapshot: Dict[str, Any]):
        """写入最近工具结果缓冲区，并保持固定长度。"""
        self._recent_tool_results = append_recent_tool_result(
            self._recent_tool_results,
            snapshot,
            limit=self._recent_tool_result_limit,
        )

    def _get_recent_tool_results(self, limit: int = 5) -> List[Dict[str, Any]]:
        """返回最近若干条工具结果副本。"""
        return get_recent_tool_results(self._recent_tool_results, limit=limit)

    def _reset_loaded_tools_for_new_turn(self):
        """新一轮对话前重置已加载的工具授权。"""
        return core_reset_loaded_tools_for_new_turn(
            loaded_modules=self.loaded_modules,
            loaded_single_tools=self.loaded_single_tools,
            approved_tools=self.approved_tools,
        )

    def _handle_load_module(self, tool_call_id: str, arguments_str: str, messages: list) -> None:
        """包装: handle_load_module_call——加载模块工具"""
        core_handle_load_module_call(
            tool_call_id=tool_call_id,
            arguments_str=arguments_str,
            messages=messages,
            parse_arguments_fn=self._parse_arguments,
            get_tool_list_fn=self.tool_manager.get_tool_list,
            get_tool_schema_by_name_fn=self.tool_manager.get_tool_schema_by_name,
            add_tool_message_fn=self._add_tool_message,
            handle_tool_error_fn=self._handle_tool_error,
            loaded_modules=self.loaded_modules,
            loaded_single_tools=self.loaded_single_tools,
            approved_tools=self.approved_tools,
            max_loaded_modules=self._max_loaded_modules,
            logger=logger,
            print_fn=print,
        )

    def _authorize_single_tool(self, tool_name: str, schema: dict) -> None:
        """包装: authorize_single_tool——授权单个工具"""
        core_authorize_single_tool(
            loaded_single_tools=self.loaded_single_tools,
            approved_tools=self.approved_tools,
            tool_name=tool_name,
            schema=schema,
        )

    def _is_tool_loaded(self, tool_name: str) -> bool:
        """包装: is_tool_loaded——检查模块工具是否已加载"""
        return core_is_tool_loaded(self.loaded_modules, tool_name)

    def _is_single_tool_loaded(self, tool_name: str) -> bool:
        """包装: is_single_tool_loaded——检查单工具授权是否存在"""
        return core_is_single_tool_loaded(self.loaded_single_tools, tool_name)

    def _touch_single_tool(self, tool_name: str) -> None:
        """包装: touch_single_tool——更新工具最后使用时间"""
        core_touch_single_tool(self.loaded_single_tools, tool_name)

    def _get_or_create_manual_context_manager_tool(self):
        """包装: get_or_create_context_manager_tool——返回上下文管理器实例"""
        return self.context_manager

    def _decay_recent_tool_results(self, user_input: str):
        """新一轮用户输入到来时，对历史工具证据做轻量衰减。"""
        if not self._recent_tool_results:
            return

        previous_user_input = ""
        for message in reversed(self.current_context):
            if message.get("role") == "user":
                previous_user_input = str(message.get("content", "")).strip()
                break

        self._recent_tool_results = decay_recent_tool_results(
            self._recent_tool_results,
            previous_user_input=previous_user_input,
            user_input=user_input,
            similarity_fn=self._text_similarity,
            same_topic_keep=self._recent_tool_result_same_topic_keep,
            topic_shift_keep=self._recent_tool_result_topic_shift_keep,
            limit=self._recent_tool_result_limit,
        )

    @staticmethod
    def _text_similarity(left: str, right: str) -> float:
        if not left or not right:
            return 0.0
        return SequenceMatcher(None, left, right).ratio()
    def _get_recent_failures(self, limit: int = 3) -> List[str]:
        current_task_wrapper = self.task_chain_manager.get_current_task()
        current_task = current_task_wrapper["task"] if current_task_wrapper else None
        execution_state = (current_task or {}).get("execution_state", {}) or {}
        blockage_reason = execution_state.get("blockage_reason")
        return collect_recent_failures(
            self._recent_tool_results,
            blockage_reason=blockage_reason,
            limit=limit,
        )

    def _get_phase_memory_snapshot(
        self,
        goal: str,
        phase: str,
        intent: str,
        recent_failures: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return core_get_phase_memory_snapshot(
            goal=goal,
            phase=phase,
            intent=intent,
            recent_failures=recent_failures,
            memory_manager=self.memory_manager,
            get_cognitive_network_summary_fn=self._get_cognitive_network_summary,
        )

    def _should_resume_task(
        self,
        current_task: Optional[Dict[str, Any]],
        replan_suggestion: Optional[Dict[str, Any]] = None,
    ) -> bool:
        return core_should_resume_task(
            current_task=current_task,
            replan_suggestion=replan_suggestion,
            max_tool_failures=self._autonomous_max_tool_failures,
        )

    def _select_active_goal(
        self,
        current_task: Optional[Dict[str, Any]],
        replan_suggestion: Optional[Dict[str, Any]],
        recent_failures: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        return core_select_active_goal(
            current_task=current_task,
            replan_suggestion=replan_suggestion,
            recent_failures=recent_failures,
            pending_user_inputs=self._pending_user_inputs,
            max_tool_failures=self._autonomous_max_tool_failures,
        )

    def _build_internal_resume_prompt(
        self,
        goal_payload: Dict[str, Any],
        memory_summary: str,
        activation_set: List[Dict[str, Any]],
        self_model: Dict[str, Any],
        recent_failures: Optional[List[str]] = None,
        replan_suggestion: Optional[Dict[str, Any]] = None,
    ) -> str:
        return core_build_internal_resume_prompt(
            goal_payload=goal_payload,
            memory_summary=memory_summary,
            activation_set=activation_set,
            self_model=self_model,
            recent_failures=recent_failures,
            replan_suggestion=replan_suggestion,
        )

    def _build_autonomous_decision(
        self,
        current_task: Dict[str, Any],
        goal_payload: Dict[str, Any],
        memory_snapshot: Dict[str, Any],
        self_model: Dict[str, Any],
        replan_suggestion: Optional[Dict[str, Any]],
        recent_failures: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return core_build_autonomous_decision(
            current_task=current_task,
            goal_payload=goal_payload,
            memory_snapshot=memory_snapshot,
            self_model=self_model,
            replan_suggestion=replan_suggestion,
            recent_failures=recent_failures,
        )

    def _update_autonomous_progress(
        self,
        previous_task: Dict[str, Any],
        goal_payload: Dict[str, Any],
        internal_prompt: str,
    ) -> Dict[str, Any]:
        return core_update_autonomous_progress(
            previous_task=previous_task,
            goal_payload=goal_payload,
            internal_prompt=internal_prompt,
            task_chain_manager=self.task_chain_manager,
            recent_tool_results=self._recent_tool_results,
            max_phase_stagnation=self._autonomous_max_phase_stagnation,
            max_repeated_actions=self._autonomous_max_repeated_actions,
            max_tool_failures=self._autonomous_max_tool_failures,
        )

    def autonomous_tick(self) -> Dict[str, Any]:
        return core_run_autonomous_tick(
            task_chain_manager=self.task_chain_manager,
            memory_manager=self.memory_manager,
            current_context=self.current_context,
            pending_user_inputs=self._pending_user_inputs,
            get_cognitive_network_summary_fn=self._get_cognitive_network_summary,
            get_recent_failures_fn=self._get_recent_failures,
            get_recent_tool_results_fn=self._get_recent_tool_results,
            cleanup_reasoning_content_fn=self._cleanup_reasoning_content_for_next_request,
            append_conversation_message_fn=self._append_conversation_message,
            process_chat_with_context_fn=self._process_chat_with_context,
            max_phase_stagnation=self._autonomous_max_phase_stagnation,
            max_repeated_actions=self._autonomous_max_repeated_actions,
            max_tool_failures=self._autonomous_max_tool_failures,
            log_autonomous_tick_fn=self.execution_journal.log_autonomous_tick,
        )

    def queue_pending_user_input(self, user_input: str) -> Dict[str, Any]:
        return core_enqueue_pending_user_input(
            self._pending_user_inputs,
            user_input,
            limit=self._pending_user_input_limit,
        )

    def run_autonomous_cycle(self, max_steps: int = 1) -> Dict[str, Any]:
        self._autonomous_running = True
        try:
            return core_run_autonomous_cycle(
                max_steps=max_steps,
                autonomous_tick_fn=self.autonomous_tick,
            )
        finally:
            self._autonomous_running = False

    def plan_multi_agent_subtasks(self, max_subtasks: int = 2) -> Dict[str, Any]:
        current_task_wrapper = self.task_chain_manager.get_current_task()
        if not current_task_wrapper:
            return {"success": False, "status": "idle", "reason": "no_active_task"}

        run = self.multi_agent_runtime.create_run(
            current_task_wrapper["task"],
            max_subtasks=max_subtasks,
        )
        self.multi_agent_runtime.log_plan(self.execution_journal, run)
        return {"success": True, "status": run.get("status"), "run": run}

    def get_multi_agent_status(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        return self.multi_agent_runtime.get_status(run_id=run_id)

    def run_multi_agent_cycle(
        self,
        max_subtasks: int = 2,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        status = self.multi_agent_runtime.get_status(run_id=run_id)
        active_run = status.get("active_run")
        if not active_run:
            planned = self.plan_multi_agent_subtasks(max_subtasks=max_subtasks)
            if not planned.get("success"):
                return planned
            run_id = planned["run"]["run_id"]
        elif run_id is None:
            run_id = active_run.get("run_id")

        execution = self.multi_agent_runtime.run_pending(
            run_id=run_id,
            max_subtasks=max_subtasks,
            executor_fn=self._execute_multi_agent_subtask,
        )
        integration = None
        if execution.get("status") in {"completed", "completed_with_failures"}:
            integration = self.multi_agent_runtime.integrate_run(
                run_id=execution.get("run_id"),
                task_chain_manager=self.task_chain_manager,
                execution_journal=self.execution_journal,
            )

        return {
            "success": execution.get("success", False),
            "status": integration.get("status") if integration else execution.get("status"),
            "run_id": execution.get("run_id"),
            "execution": execution,
            "integration": integration,
        }

    def _execute_multi_agent_subtask(self, subtask: Dict[str, Any]) -> Dict[str, Any]:
        prompt = build_subagent_prompt(subtask)
        messages = [
            {
                "role": "system",
                "content": (
                    "You are an isolated Xenon sub-agent. Keep the result concise, "
                    "structured, and limited to the assigned subtask."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        tools = self._filter_tools_for_subtask(subtask)
        self._chat(messages, tools, self.get_model())
        assistant_messages = [message for message in messages if message.get("role") == "assistant"]
        summary = assistant_messages[-1].get("content", "") if assistant_messages else ""
        return {
            "success": bool(summary.strip()),
            "summary": summary.strip() or "Sub-agent finished without an assistant summary.",
            "artifacts": [],
            "conflicts": [],
            "metadata": {
                "isolated_messages": len(messages),
                "allowed_tools": subtask.get("allowed_tools") or [],
            },
        }

    def _filter_tools_for_subtask(self, subtask: Dict[str, Any]) -> List[Dict[str, Any]]:
        allowed = [str(item).lower() for item in (subtask.get("allowed_tools") or [])]
        if not allowed:
            return []
        filtered = []
        for tool in self._get_current_tools():
            name = str((tool.get("function") or {}).get("name") or "").lower()
            if any(token in name for token in allowed):
                filtered.append(tool)
        return filtered

    def _get_runtime_system_messages(
        self, decision: Optional[ActionDecision] = None
    ) -> Tuple[str, str]:
        """组装系统消息，返回 (static_content, dynamic_content) 两部分。

        static: 纯静态提示词（base + prompts），跨轮不变，放在 messages 最前以命中前缀缓存。
        dynamic: 运行时动态信息（时间/文件系统/自我模型/工具状态/编排），每轮变化，
                 放在对话历史之后、最新 user 之前。
        """
        tool_list = self.tool_manager.get_tool_list()
        module_names = self.tool_manager.get_module_list()
        current_task_wrapper = self.task_chain_manager.get_current_task()
        current_task = current_task_wrapper["task"] if current_task_wrapper else None
        skill_guidance = self._get_skill_guidance(decision)
        orchestration_guidance = self.agent_orchestrator.build_system_guidance(
            decision=decision,
            current_task=current_task,
            journal_summary=self.execution_journal.summarize_recent(limit=6),
            recovery_plan=self.last_recovery_plan,
            skill_guidance=skill_guidance,
        )
        return core_build_runtime_system_messages(
            system_prompt=get_system_prompt(),
            project_root=Path(__file__).parent.resolve(),
            cwd=Path.cwd(),
            tool_list=tool_list,
            module_names=module_names,
            current_task=current_task,
            loaded_modules=self.loaded_modules,
            loaded_single_tools=self.loaded_single_tools,
            orchestration_guidance=orchestration_guidance,
        )

    def _get_available_tools_message(self, decision: Optional[ActionDecision] = None) -> str:
        """兼容接口：返回 static + dynamic 拼接字符串。

        供 webui 的 token 估算等仍期望接收单个字符串的调用方使用。
        运行时组装（run_chat_turn）请改用 _get_runtime_system_messages 获取拆分后的两部分。
        """
        static_content, dynamic_content = self._get_runtime_system_messages(decision)
        return static_content + "\n\n" + dynamic_content

    def _get_skill_guidance(self, decision: Optional[ActionDecision] = None) -> str:
        return ""
    def _ensure_message_integrity(self, messages: List[Dict]) -> List[Dict]:
        """
        确保消息序列完整性：
        - 如果 assistant 消息有 tool_calls，确保每个 tool_call_id 都有对应的 tool 响应
        - 如果缺少响应，添加一个明确的中断响应，告知模型该调用未执行
        """
        return core_ensure_message_integrity(messages, logger=logger)

    # ---------- 新增：主动上下文管理核心方法 ----------
    def _ensure_context_size(self, messages: List[Dict], tools: List[Dict]) -> bool:
        """
        检查当前上下文 token 使用量，如果超过阈值，则触发自动压缩。
        使用 ContextManager 中定义的统一阈值。
        返回 True 表示执行了压缩，False 表示未压缩。
        """
        self._cache_live_context_status(messages, tools)
        trimmed = core_ensure_context_size(
            messages=messages,
            tools=tools,
            context_manager=self.context_manager,
            auto_trim_context_fn=self._auto_trim_context,
            logger=logger,
        )
        self._cache_live_context_status(messages, tools)
        return trimmed

    def _auto_trim_context(self, messages: List[Dict], tools: List[Dict]):
        """
        简单滑窗裁剪上下文：
        1. 保留 system / checkpoint / task state 类状态消息
        2. 保留最新 compact 对话轮次
        3. 保留当前 live turn 的 tool_calls 和 tool results
        4. 删除更早的 compact 历史，不再生成额外摘要
        """
        core_auto_trim_context(
            messages=messages,
            tools=tools,
            context_manager=self.context_manager,
            protected_conversation_rounds=PROTECTED_CONVERSATION_ROUNDS,
            extract_tool_call_id_fn=self._extract_tool_call_id,
            save_context_summary_fn=self._save_context_summary_to_memory,
            compress_tool_messages_fn=self._compress_tool_messages_in_place,
            inject_recent_memory_summary_fn=self._inject_recent_memory_summary,
            emergency_context_clear_fn=self._emergency_context_clear,
            logger=logger,
        )
        self._cache_live_context_status(messages, tools)

    def _save_context_summary_to_memory(self, messages: List[Dict]):
        """
        生成任务状态检查点并保存到 Memory/auto_summary。
        checkpoint 会在压缩后作为唯一任务状态重新注入上下文。
        """
        try:
            dialog_messages, last_user_msg = core_collect_summary_source_messages(
                messages,
                recent_limit=SUMMARY_RECENT_MESSAGE_LIMIT,
            )
            if not dialog_messages:
                return

            latest_path = self._get_project_memory_path()
            try:
                latest_path.unlink(missing_ok=True)
            except Exception as cleanup_error:
                logger.warning("清理旧任务状态检查点失败: %s", cleanup_error)

            previous_checkpoint = core_extract_current_checkpoint(messages)
            logger.info("正在生成任务状态检查点...")
            smart_summary = self._generate_smart_summary(
                dialog_messages,
                previous_checkpoint=previous_checkpoint,
            )
            if not smart_summary:
                return

            summary_text = core_build_project_memory_text(
                smart_summary=smart_summary,
                last_user_message=last_user_msg,
            )
            latest_path, snapshot_path = core_write_project_memory_files(
                summary_text=summary_text,
                project_memory_filename=PROJECT_MEMORY_FILENAME,
                summary_dir=self._get_project_memory_dir(),
            )
            logger.info("任务状态检查点已保存: %s, %s", latest_path, snapshot_path)
        except Exception as e:
            logger.error("保存任务状态检查点失败: %s", e)

    def _inject_recent_memory_summary(self, messages: List[Dict], include_user_question: bool = True):
        """
        注入最新任务状态检查点，并移除旧版摘要/旧检查点消息。
        
        Args:
            messages: 消息列表
            include_user_question: 保留参数兼容旧调用路径，当前不再使用
        """
        core_inject_recent_memory_summary(
            messages=messages,
            project_memory_filename=PROJECT_MEMORY_FILENAME,
            summary_injection_max_chars=SUMMARY_INJECTION_MAX_CHARS,
            include_user_question=include_user_question,
            summary_dir=self._get_project_memory_dir(),
            logger=logger,
        )

    def _get_project_memory_dir(self) -> Path:
        return core_get_project_memory_dir()

    def _get_project_memory_path(self) -> Path:
        return core_get_project_memory_path(
            project_memory_filename=PROJECT_MEMORY_FILENAME,
            summary_dir=self._get_project_memory_dir(),
        )

    def _cleanup_old_summaries_if_healthy(self, messages: List[Dict], tools: List[Dict]):
        """Keep the latest project memory and trim old snapshots once context is healthy."""
        core_cleanup_old_summaries_if_healthy(
            messages=messages,
            tools=tools,
            context_manager=self.context_manager,
            project_memory_snapshot_prefix=PROJECT_MEMORY_SNAPSHOT_PREFIX,
            summary_dir=self._get_project_memory_dir(),
            logger=logger,
        )

    def _generate_smart_summary(self, messages: List[Dict], previous_checkpoint: str = "") -> str:
        """Refresh the task checkpoint used by automatic context compression."""
        return core_generate_smart_summary(
            messages=messages,
            project_memory_filename=PROJECT_MEMORY_FILENAME,
            summary_dir=self._get_project_memory_dir(),
            recent_message_limit=SUMMARY_RECENT_MESSAGE_LIMIT,
            max_message_snippet=SUMMARY_MAX_MESSAGE_SNIPPET,
            max_tool_snippet=SUMMARY_MAX_TOOL_SNIPPET,
            summarize_tool_payload_fn=self._summarize_tool_payload_for_context,
            openai_client_cls=OpenAI,
            api_key=API_KEY,
            base_url=BASE_URL,
            summary_timeout=SUMMARY_TIMEOUT,
            summary_model=SUMMARY_MODEL,
            summary_max_tokens=SUMMARY_MAX_TOKENS,
            summary_thinking_enabled=SUMMARY_THINKING_ENABLED,
            summary_reasoning_effort=REASONING_EFFORT if SUMMARY_THINKING_ENABLED else None,
            include_previous_summary=False,
            previous_summary_override=previous_checkpoint,
            logger=logger,
        )

    # _save_context_summary_to_memory 已合并到第一个定义（L2770），此处不再重复

    # _inject_recent_memory_summary 已合并到第一个定义（L2841），此处不再重复

    def chat(self, user_input: str):
        if getattr(self, "_autonomous_running", False):
            self.interrupted = True
            return self.queue_pending_user_input(user_input)

        # ★ 正常对话中，如果轮次正在运行（工具调用中），入队而非打断
        if getattr(self, "_turn_running", False):
            result = self.queue_pending_user_input(user_input)
            if self._stream_callback:
                self._stream_callback({
                    "type": "user_queued",
                    "content": user_input,
                    "queue_position": result.get("pending_count", 0),
                })
            return result

        reset_loaded_tools_fn = self._reset_loaded_tools_for_new_turn
        if CONTEXT_COMPACT_AFTER_TURN:
            reset_loaded_tools_fn = self._reset_loaded_tools_for_new_turn_without_notice

        core_handle_user_chat_entry(
            user_input=user_input,
            current_context=self.current_context,
            decay_recent_tool_results_fn=self._decay_recent_tool_results,
            set_interrupted_fn=lambda value: setattr(self, "interrupted", value),
            set_active_user_input_fn=lambda value: setattr(self, "_active_user_input", value),
            reset_loaded_tools_for_new_turn_fn=reset_loaded_tools_fn,
            find_pending_tool_call_ids_fn=self._find_pending_tool_call_ids,
            append_conversation_message_fn=self._append_conversation_message,
            cleanup_reasoning_content_fn=self._cleanup_reasoning_content_for_next_request,
            process_chat_with_context_fn=self._process_chat_with_context,
            logger=logger,
        )

    def _reset_loaded_tools_for_new_turn_without_notice(self) -> Optional[str]:
        self._reset_loaded_tools_for_new_turn()
        return None

    def _process_chat_with_context(self, user_input: str, internal_context: Optional[Dict[str, Any]] = None):
        """处理对话（支持排队消息：本轮完成后自动处理队列中的消息）"""
        self._active_user_input = user_input
        self._turn_running = True
        self._tool_cycle_count = 0  # 每轮开始重置工具递归计数器
        self._recursion_detector.reset()  # 每轮开始重置指纹检测器
        try:
            self._run_single_turn(user_input, internal_context)
        finally:
            self._turn_running = False

        # ★ 处理排队消息：本轮完成后，依次处理队列中的消息
        while self._pending_user_inputs:
            pending = self._pending_user_inputs.pop(0)
            self._turn_running = True
            try:
                # 通知前端正在处理排队消息
                if self._stream_callback:
                    self._stream_callback({
                        "type": "queue_processing",
                        "content": pending,
                        "queue_remaining": len(self._pending_user_inputs),
                    })
                # 排队消息也需要经过完整的 entry 预处理（工具重置、上下文清理、附加用户消息）
                self._decay_recent_tool_results(pending)
                self._active_user_input = pending
                reset_loaded_tools_fn = self._reset_loaded_tools_for_new_turn
                if CONTEXT_COMPACT_AFTER_TURN:
                    reset_loaded_tools_fn = self._reset_loaded_tools_for_new_turn_without_notice

                core_handle_user_chat_entry(
                    user_input=pending,
                    current_context=self.current_context,
                    decay_recent_tool_results_fn=lambda _: None,  # 已在上面调用
                    set_interrupted_fn=lambda value: setattr(self, "interrupted", value),
                    set_active_user_input_fn=lambda value: setattr(self, "_active_user_input", value),
                    reset_loaded_tools_for_new_turn_fn=reset_loaded_tools_fn,
                    find_pending_tool_call_ids_fn=self._find_pending_tool_call_ids,
                    append_conversation_message_fn=self._append_conversation_message,
                    cleanup_reasoning_content_fn=self._cleanup_reasoning_content_for_next_request,
                    process_chat_with_context_fn=self._run_single_turn,
                    logger=logger,
                )
            finally:
                self._turn_running = False

    def _run_single_turn(self, user_input: str, internal_context: Optional[Dict[str, Any]] = None):
        """执行单轮对话（被 _process_chat_with_context 和排队处理器复用）"""
        core_run_chat_turn(
            user_input=user_input,
            internal_context=internal_context,
            current_context=self.current_context,
            context_manager=self.context_manager,
            cognitive_network_summary=self.cognitive_network_summary,
            prepare_orchestration_decision_fn=self._prepare_orchestration_decision,
            get_actual_context_status_fn=self._get_actual_context_status,
            get_current_tool_names_fn=self._get_current_tool_names,
            get_runtime_system_messages_fn=self._get_runtime_system_messages,
            get_context_token_info_fn=self._get_context_token_info,
            inject_cognitive_network_summary_fn=self._inject_cognitive_network_summary,
            get_recent_failures_fn=self._get_recent_failures,
            get_current_tools_fn=self._get_current_tools,
            ensure_context_size_fn=self._ensure_context_size,
            chat_fn=self._chat,
            cleanup_old_summaries_if_healthy_fn=self._cleanup_old_summaries_if_healthy,
            save_memory_log_fn=self._save_memory_log,
            set_current_context_fn=lambda context: setattr(self, "current_context", context),
            interrupted_exception_cls=InterruptedException,
            model_for_chat=self.get_model(),
            logger=logger,
            print_fn=print,
            reset_loaded_tools_for_next_turn_fn=self._reset_loaded_tools_for_new_turn,
            compact_turn_after_commit_fn=(
                self._compact_turn_for_next_context if CONTEXT_COMPACT_AFTER_TURN else None
            ),
            stream_callback_fn=self._stream_callback,
        )

    def _chat(self, messages: List[Dict], tools: List[Dict], model: str, _retry_count: int = 0):
        # ★ 中途注入排队消息：在递归调用间隙检查队列，实现实时纠正
        if _retry_count == 0 and getattr(self, "_turn_running", False) and self._pending_user_inputs:
            pending = self._pending_user_inputs.pop(0)
            self._append_conversation_message(messages, {"role": "user", "content": pending})
            self._active_user_input = pending
            if self._stream_callback:
                self._stream_callback({
                    "type": "queue_processing",
                    "content": pending,
                    "queue_remaining": len(self._pending_user_inputs),
                })

        core_run_chat_cycle(
            messages=messages,
            tools=tools,
            model=model,
            retry_count=_retry_count,
            max_trim_retries=2,
            context_manager=self.context_manager,
            enable_streaming=ENABLE_STREAMING,
            ensure_message_integrity_fn=self._ensure_message_integrity,
            auto_trim_context_fn=self._auto_trim_context,
            emergency_context_clear_fn=self._emergency_context_clear,
            append_conversation_message_fn=self._append_conversation_message,
            save_api_request_fn=self._save_api_request,
            save_api_usage_fn=lambda tokens: setattr(self, '_last_api_total_tokens', tokens),
            retry_request_fn=self._retry_request,
            create_completion_fn=self.client.chat.completions.create,
            process_streaming_response_fn=self._process_streaming_response,
            process_non_streaming_response_fn=self._process_non_streaming_response,
            recursive_chat_fn=lambda next_messages, next_tools, next_model, next_retry_count: self._chat(
                next_messages,
                next_tools,
                next_model,
                next_retry_count,
            ),
            set_in_api_call_fn=lambda value: setattr(self, "_in_api_call", value),
            is_interrupted_fn=lambda: self.interrupted,
            interrupted_exception_cls=InterruptedException,
            logger=logger,
            thinking_enabled=ENABLE_THINKING_MODE,
            reasoning_effort=REASONING_EFFORT,
            print_fn=print,
        )

    @staticmethod
    def _extract_tool_call_id(tc) -> Optional[str]:
        """统一提取 tool_call 的 id（兼容 dict 和 object 两种格式）"""
        return core_extract_tool_call_id(tc)

    def _find_pending_tool_call_ids(self, messages: List[Dict], exclude_id: str = None) -> set:
        """遍历消息列表，找出所有未响应的 tool_call_id。"""
        return core_find_pending_tool_call_ids(messages, exclude_id=exclude_id)

    @staticmethod
    def _cleanup_reasoning_content(messages: List[Dict]):
        """清理消息列表中的 reasoning_content 字段。"""
        core_cleanup_reasoning_content(messages)

    @staticmethod
    def _cleanup_reasoning_content_for_next_request(messages: List[Dict]):
        """DeepSeek V4 thinking mode requires returning prior reasoning_content."""
        if ENABLE_THINKING_MODE:
            return None
        return core_cleanup_reasoning_content(messages)

    def _emergency_context_clear(self, messages: List[Dict], include_user_question: bool = False):
        """紧急清空策略：只保留 system 消息、最后的 toolchain 和用户提问。
        提取为公共方法，供 _auto_trim_context 和 _chat 中的超限重试共用。
        """
        core_emergency_context_clear(
            messages=messages,
            include_user_question=include_user_question,
            save_context_summary_fn=self._save_context_summary_to_memory,
            inject_recent_memory_summary_fn=self._inject_recent_memory_summary,
            extract_tool_call_id_fn=self._extract_tool_call_id,
            logger=logger,
        )

    def _try_inject_queued_messages(self, messages: List[Dict]):
        """★ 中途注入：在工具执行间隙将排队消息注入到消息列表中"""
        if not getattr(self, "_turn_running", False):
            return False
        # 短暂释放 GIL，让 FastAPI 线程有机会把消息放入队列
        import time as _time
        for _ in range(20):
            if self._pending_user_inputs:
                break
            _time.sleep(0.01)
        if not self._pending_user_inputs:
            return False
        pending = self._pending_user_inputs.pop(0)
        self._append_conversation_message(messages, {"role": "user", "content": pending})
        self._active_user_input = pending
        print(f"\n[排队注入] 中途将排队消息注入到对话中: {pending[:80]}...", file=sys.stderr)
        if self._stream_callback:
            self._stream_callback({
                "type": "queue_processing",
                "content": pending,
                "queue_remaining": len(self._pending_user_inputs),
            })
        return True

    def _process_streaming_response(self, response, messages: List[Dict], tools: List[Dict]):
        def continue_with_injection(next_messages, next_tools, next_model):
            self._tool_cycle_count += 1
            if self._tool_cycle_count > MAX_TOOL_RECURSION_DEPTH:
                logger.warning("[loop_guard] 工具递归达到上限 (%s)，注入提醒继续执行", MAX_TOOL_RECURSION_DEPTH)
                self._append_conversation_message(next_messages, {
                    "role": "assistant",
                    "content": f"Xenon{MAX_TOOL_RECURSION_DEPTH}次调用了，工具做完了吗？",
                })
                # 不 return，继续执行工具链
            # ★ 内容指纹检测：连续相同工具调用判定为递归死循环
            if self._recursion_detector.check_and_inject(
                next_messages,
                append_message_fn=next_messages.append,
            ):
                logger.warning("[recursion_detector] 检测到递归死循环，强制中断工具链")
                return
            self._try_inject_queued_messages(next_messages)
            self._chat(next_messages, next_tools, next_model)

        streaming_content_parts: List[str] = []
        streaming_reasoning_parts: List[str] = []
        streaming_chars_since_update = 0
        streaming_last_update = 0.0

        def stream_callback_with_usage(event: Dict[str, Any]):
            nonlocal streaming_chars_since_update, streaming_last_update
            if self._stream_callback:
                self._stream_callback(event)

            event_type = event.get("type")
            chunk = str(event.get("content", "") or "")
            if event_type == "content" and chunk:
                streaming_content_parts.append(chunk)
            elif event_type == "thinking" and chunk:
                streaming_reasoning_parts.append(chunk)
            else:
                return

            import time as _time
            streaming_chars_since_update += len(chunk)
            now = _time.monotonic()
            if streaming_chars_since_update < 256 and now - streaming_last_update < 0.75:
                return

            self._cache_streaming_context_status(
                messages,
                tools,
                "".join(streaming_content_parts),
                "".join(streaming_reasoning_parts),
            )
            streaming_chars_since_update = 0
            streaming_last_update = now

        core_process_streaming_response(
            response=response,
            messages=messages,
            tools=tools,
            interrupted_exception_cls=InterruptedException,
            is_interrupted_fn=lambda: self.interrupted,
            stream_callback=stream_callback_with_usage,
            validate_and_fix_json_fn=self._validate_and_fix_json,
            append_conversation_message_fn=self._append_conversation_message,
            save_memory_log_fn=self._save_memory_log,
            handle_tool_calls_fn=self._handle_tool_calls,
            get_current_tools_fn=self._get_current_tools,
            continue_chat_fn=continue_with_injection,
            model_for_recursive_chat=self.get_model(),
            logger=logger,
            print_fn=print,
        )
        self._cache_live_context_status(messages, tools)

    def _process_non_streaming_response(self, response, messages: List[Dict], tools: List[Dict]):
        def continue_with_injection(next_messages, next_tools, next_model):
            self._tool_cycle_count += 1
            if self._tool_cycle_count > MAX_TOOL_RECURSION_DEPTH:
                logger.warning("[loop_guard] 工具递归达到上限 (%s)，注入提醒继续执行", MAX_TOOL_RECURSION_DEPTH)
                self._append_conversation_message(next_messages, {
                    "role": "assistant",
                    "content": f"Xenon{MAX_TOOL_RECURSION_DEPTH}次调用了，工具做完了吗？",
                })
                # 不 return，继续执行工具链
            # ★ 内容指纹检测：连续相同工具调用判定为递归死循环
            if self._recursion_detector.check_and_inject(
                next_messages,
                append_message_fn=next_messages.append,
            ):
                logger.warning("[recursion_detector] 检测到递归死循环，强制中断工具链")
                return
            self._try_inject_queued_messages(next_messages)
            self._chat(next_messages, next_tools, next_model)

        core_process_non_streaming_response(
            response=response,
            messages=messages,
            tools=tools,
            interrupted_exception_cls=InterruptedException,
            is_interrupted_fn=lambda: self.interrupted,
            append_conversation_message_fn=self._append_conversation_message,
            save_memory_log_fn=self._save_memory_log,
            handle_tool_calls_fn=self._handle_tool_calls,
            get_current_tools_fn=self._get_current_tools,
            continue_chat_fn=continue_with_injection,
            model_for_recursive_chat=self.get_model(),
            logger=logger,
            print_fn=print,
        )
        self._cache_live_context_status(messages, tools)

    def _validate_and_fix_json(self, json_str: str) -> Optional[str]:
        """改进版 JSON 修复：使用栈跟踪括号，智能添加缺失的闭合括号"""
        return core_validate_and_fix_json(json_str, logger=logger)

    def _handle_tool_calls(self, tool_calls, messages: List[Dict], tools: List[Dict] = None):
        core_handle_tool_call_batch(
            tool_calls=tool_calls,
            messages=messages,
            tools=tools,
            interrupted_exception_cls=InterruptedException,
            is_interrupted_fn=lambda: self.interrupted,
            handle_load_module_fn=self._handle_load_module,
            handle_get_tool_description_fn=self._handle_get_tool_description,
            handle_get_module_tools_fn=self._handle_get_module_tools,
            handle_context_manager_tool_fn=self._handle_context_manager_tool,
            handle_execute_tool_fn=self._handle_execute_tool,
            add_tool_message_fn=self._add_tool_message,
            is_tool_loaded_fn=self._is_tool_loaded,
            is_single_tool_loaded_fn=self._is_single_tool_loaded,
            logger=logger,
        )

    def _handle_get_tool_description(self, tool_call_id: str, arguments_str: str, messages: List[Dict]):
        core_handle_get_tool_description_call(
            tool_call_id=tool_call_id,
            arguments_str=arguments_str,
            messages=messages,
            parse_arguments_fn=self._parse_arguments,
            get_tool_schema_by_name_fn=self.tool_manager.get_tool_schema_by_name,
            authorize_single_tool_fn=self._authorize_single_tool,
            add_tool_message_fn=self._add_tool_message,
            handle_tool_error_fn=self._handle_tool_error,
            logger=logger,
        )

    def _handle_get_module_tools(self, tool_call_id: str, arguments_str: str, messages: List[Dict]):
        core_handle_get_module_tools_call(
            tool_call_id=tool_call_id,
            arguments_str=arguments_str,
            messages=messages,
            parse_arguments_fn=self._parse_arguments,
            get_tool_list_fn=self.tool_manager.get_tool_list,
            add_tool_message_fn=self._add_tool_message,
            handle_tool_error_fn=self._handle_tool_error,
            logger=logger,
        )

    def _handle_context_manager_tool(self, tool_call_id: str, tool_name: str, arguments_str: str, messages: List[Dict], tools: List[Dict] = None):
        """处理上下文管理工具调用（兼容旧版，但新系统会自动压缩，此方法可保留但简化）"""
        core_handle_context_manager_tool_call(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments_str=arguments_str,
            messages=messages,
            tools=tools,
            parse_arguments_fn=self._parse_arguments,
            find_pending_tool_call_ids_fn=self._find_pending_tool_call_ids,
            add_tool_message_fn=self._add_tool_message,
            get_actual_context_status_fn=self._get_actual_context_status,
            get_actual_token_estimate_fn=self._get_actual_token_estimate,
            get_current_tools_fn=self._get_current_tools,
            auto_trim_context_fn=self._auto_trim_context,
            extract_tool_call_id_fn=self._extract_tool_call_id,
            append_conversation_message_fn=self._append_conversation_message,
            get_or_create_context_manager_tool_fn=self._get_or_create_manual_context_manager_tool,
            handle_tool_error_fn=self._handle_tool_error,
            logger=logger,
        )

    def _cache_live_context_status(self, messages: List[Dict], tools: List[Dict]) -> Dict[str, Any]:
        status = core_get_actual_context_status(
            messages=messages,
            system_message="",
            current_tools=tools,
            context_manager=self.context_manager,
        )
        self._last_live_context_status = status
        return status

    def _cache_streaming_context_status(
        self,
        messages: List[Dict],
        tools: List[Dict],
        content: str,
        reasoning_content: str = "",
    ) -> Optional[Dict[str, Any]]:
        if not content and not reasoning_content:
            return None

        assistant_message: Dict[str, Any] = {
            "role": "assistant",
            "content": content or "",
        }
        if reasoning_content:
            assistant_message["reasoning_content"] = reasoning_content
        return self._cache_live_context_status(messages + [assistant_message], tools)

    def _get_actual_context_status(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        system_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """获取实际的上下文状态"""
        resolved_messages = messages if messages is not None else self.current_context
        resolved_tools = tools if tools is not None else self._get_current_tools()
        if system_message is None:
            system_message = self._get_available_tools_message() if messages is None else ""

        return core_get_actual_context_status(
            messages=resolved_messages,
            system_message=system_message,
            current_tools=resolved_tools,
            context_manager=self.context_manager,
        )

    def _get_actual_token_estimate(self) -> Dict[str, Any]:
        """获取实际的token估计"""
        return self._get_actual_context_status()

    def _get_current_tools(self) -> List[Dict]:
        """获取当前真实可调用的工具列表：元工具 + 已加载模块工具 + 仍有效的单工具授权"""
        return [
            tool
            for tool in core_build_current_tools(
                load_module_tool=self.load_module_tool,
                tool_description_tool=self.tool_description_tool,
                get_module_tools_tool=self.get_module_tools_tool,
                loaded_modules=self.loaded_modules,
                loaded_single_tools=self.loaded_single_tools,
            )
            if tool is not None
        ]

    def _get_current_tool_names(self) -> List[str]:
        """基于 _get_current_tools() 返回当前真实可调用工具名列表。
        统一供 strategy_planning / orchestrator / token 估计等使用。"""
        names = []
        for tool in self._get_current_tools():
            func = tool.get("function", {})
            name = func.get("name")
            if name:
                names.append(name)
        return names

    def _get_current_tool_schemas(self) -> List[Dict]:
        """直接返回当前真实可调用的完整 schema 列表。内部复用 _get_current_tools()。"""
        return self._get_current_tools()

    def _calculate_actual_tokens(self) -> int:
        """计算当前上下文的实际token数"""
        return core_calculate_actual_tokens(
            messages=self.current_context,
            current_tools=self._get_current_tools(),
            context_manager=self.context_manager,
        )

    def _do_context_cleanup(self, arguments: Dict, messages: List[Dict]) -> Dict[str, Any]:
        """执行上下文清理（旧版方法，保留供兼容）"""
        return core_do_context_cleanup(
            arguments=arguments,
            messages=messages,
            current_context=self.current_context,
            context_manager=self.context_manager,
        )

    def _handle_execute_tool(self, tool_call_id: str, tool_name: str, arguments_str: str, messages: List[Dict]):
        """执行工具调用，记录恢复计划和结果快照。"""
        core_execute_tool_call(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments_str=arguments_str,
            messages=messages,
            parse_arguments_fn=self._parse_arguments,
            execute_tool_fn=self.tool_manager.execute_tool,
            record_tool_outcome_fn=self._record_tool_outcome,
            build_tool_call_snapshot_fn=self._build_tool_call_snapshot,
            push_recent_tool_result_fn=self._push_recent_tool_result,
            monitor_tool_result_snapshot_fn=lambda snapshot: None,
            add_tool_message_fn=self._add_tool_message,
            handle_tool_error_fn=self._handle_tool_error,
            build_recovery_plan_fn=self.recovery_manager.build_recovery_plan,
            touch_single_tool_fn=self._touch_single_tool,
            loaded_modules=self.loaded_modules,
            set_tool_executing_fn=lambda value: setattr(self, "_tool_executing", value),
            stream_callback=self._stream_callback,
            current_phase=self.orchestration_decision.phase if self.orchestration_decision else None,
            logger=logger,
        )

    def run(self):
        run_interactive_agent_session(
            self,
            project_root=Path(__file__).resolve().parent,
            model=self.get_model(),
            thinking_enabled=ENABLE_THINKING_MODE,
            streaming_enabled=ENABLE_STREAMING,
            interrupted_exception_cls=InterruptedException,
            app_version=APP_VERSION,
        )


if __name__ == "__main__":
    # ── 启动系统级心跳（CLI 模式）──
    try:
        from xenon_core.heartbeat import start_heartbeat
        start_heartbeat(mode="cli")
    except Exception:
        pass
    
    agent = AIAgent()
    try:
        agent.run()
    finally:
        # ── 停止心跳 ──
        try:
            from xenon_core.heartbeat import stop_heartbeat
            stop_heartbeat()
        except Exception:
            pass





