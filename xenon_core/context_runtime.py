from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import tiktoken
except ImportError:  # pragma: no cover - depends on local runtime
    tiktoken = None

MAX_CONTEXT_TOKENS_DEFAULT = 50000
OUTPUT_TOKEN_RESERVE = 8000
TIKTOKEN_AVAILABLE = tiktoken is not None

# DeepSeek V3 tokenizer 路径（位于 xenon_core 根目录）
_DEFAULT_DS_TOKENIZER_PATH = (
    Path(__file__).resolve().parent
    / "tokenizer.json"
)


class TokenCounter:
    """Token计数器类，用于估计和管理上下文token使用量。

    优先使用 DeepSeek V3 tokenizer（精确），
    不可用时回退到 tiktoken cl100k_base。
    """

    def __init__(
        self,
        model_name: str = "deepseek-v4-flash",
        *,
        logger: Any = None,
        ds_tokenizer_path: Optional[str] = None,
    ):
        self.model_name = model_name
        self._logger = logger or logging.getLogger(__name__)
        self._ds_tokenizer = None  # tokenizers.Tokenizer 实例

        # 解析 DeepSeek tokenizer 路径
        if ds_tokenizer_path is None and _DEFAULT_DS_TOKENIZER_PATH.exists():
            ds_tokenizer_path = str(_DEFAULT_DS_TOKENIZER_PATH)

        # 尝试加载 DeepSeek V3 tokenizer
        if ds_tokenizer_path:
            try:
                from tokenizers import Tokenizer as HfTokenizer

                self._ds_tokenizer = HfTokenizer.from_file(ds_tokenizer_path)
                self._logger.info(
                    "已加载 DeepSeek V3 tokenizer: %s", ds_tokenizer_path
                )
            except Exception as exc:
                self._logger.warning(
                    "加载 DeepSeek tokenizer 失败 (%s)，回退到 tiktoken", exc
                )

        # 回退到 tiktoken
        if self._ds_tokenizer is None:
            self.encoder = None
            if TIKTOKEN_AVAILABLE:
                try:
                    self.encoder = tiktoken.encoding_for_model(model_name)
                except KeyError:
                    self.encoder = tiktoken.get_encoding("cl100k_base")
                except Exception as error:
                    self._logger.error("初始化 token 编码器失败: %s", error)
            if self.encoder is None:
                self._logger.warning(
                    "无可用的 token 计数器（tiktoken 和 DeepSeek tokenizer 均不可用）"
                )

    @property
    def using_deepseek_tokenizer(self) -> bool:
        """是否正在使用 DeepSeek V3 tokenizer"""
        return self._ds_tokenizer is not None

    def count_tokens(self, text: str) -> int:
        """计算文本的 token 数。优先使用 DeepSeek tokenizer。"""
        if not text:
            return 0
        if self._ds_tokenizer is not None:
            return len(self._ds_tokenizer.encode(text).ids)
        if self.encoder is not None:
            return len(self.encoder.encode(text))
        return 0

    def estimate_context_tokens(
        self, system_prompt: str, memories: List[str], current_query: str
    ) -> int:
        total_tokens = 0
        total_tokens += self.count_tokens(system_prompt)
        for memory in memories:
            total_tokens += self.count_tokens(memory)
        total_tokens += self.count_tokens(current_query)
        total_tokens += 4 * (1 + len(memories) + 1)
        return total_tokens

    def estimate_messages_tokens(self, messages: List[Dict[str, str]]) -> int:
        total_tokens = 0

        for message in messages:
            content = message.get("content", "")
            if content:
                total_tokens += self.count_tokens(content)

            if message.get("role") == "assistant":
                reasoning = message.get("reasoning_content", "")
                if reasoning:
                    total_tokens += self.count_tokens(reasoning)

                tool_calls = message.get("tool_calls", [])
                if tool_calls:
                    for tool_call in tool_calls:
                        tool_call_json = (
                            json.dumps(tool_call, ensure_ascii=False)
                            if isinstance(tool_call, dict)
                            else str(tool_call)
                        )
                        total_tokens += self.count_tokens(tool_call_json)

            if message.get("role") == "tool":
                tool_content = message.get("content", "")
                if tool_content:
                    total_tokens += self.count_tokens(tool_content)

            total_tokens += 4

        return total_tokens

    def estimate_total_tokens(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        total = 0
        for message in messages:
            total += self.estimate_messages_tokens([message])
            if message.get("tool_calls"):
                for tool_call in message["tool_calls"]:
                    tool_call_str = json.dumps(tool_call, ensure_ascii=False)
                    total += self.count_tokens(tool_call_str)
        if tools:
            tools_str = json.dumps(tools, ensure_ascii=False)
            total += self.count_tokens(tools_str)
        return total

    def get_token_usage_percentage(self, current_tokens: int, max_tokens: int = None) -> float:
        if max_tokens is None:
            max_tokens = MAX_CONTEXT_TOKENS_DEFAULT
        if max_tokens <= 0:
            return 100.0

        percentage = (current_tokens / max_tokens) * 100
        return round(percentage, 2)

    def get_token_usage_warning_level(self, current_tokens: int, max_tokens: int = None) -> str:
        percentage = self.get_token_usage_percentage(current_tokens, max_tokens)

        if percentage < 50:
            return "low"
        if percentage < 75:
            return "medium"
        if percentage < 90:
            return "high"
        return "critical"

    def get_recommendation(self, current_tokens: int, max_tokens: int = None) -> Optional[str]:
        percentage = self.get_token_usage_percentage(current_tokens, max_tokens)

        if percentage > 90:
            return "上下文token使用率超过90%，必须立即执行上下文清理"
        if percentage > 85:
            return "上下文token使用率超过85%，建议执行上下文清理或加载网络图谱摘要"
        if percentage > 70:
            return "上下文token使用率超过70%，考虑优化记忆加载策略"
        return None


class ContextManager:
    """上下文管理器类，负责上下文清理、加载和token监控"""

    def __init__(
        self,
        memory_dir: str = "Memory/memory_Write",
        max_context_tokens: int = None,
        *,
        output_token_reserve: int = OUTPUT_TOKEN_RESERVE,
        token_counter_cls: Any = TokenCounter,
        tiktoken_available: Optional[bool] = None,
        logger: Any = None,
    ):
        self._logger = logger or logging.getLogger(__name__)
        self.memory_dir = Path(memory_dir)
        self.max_context_tokens = (
            max_context_tokens if max_context_tokens is not None else MAX_CONTEXT_TOKENS_DEFAULT
        )
        self.output_token_reserve = max(0, int(output_token_reserve or 0))
        self.input_budget_after_reserve = max(1, self.max_context_tokens - self.output_token_reserve)
        # 兼容旧调用名：所有上下文使用率和压缩阈值统一以配置上限为基准。
        self.effective_max_tokens = self.max_context_tokens

        self.token_counter = None
        token_counter_enabled = TIKTOKEN_AVAILABLE if tiktoken_available is None else tiktoken_available
        if token_counter_enabled and token_counter_cls is not None:
            try:
                self.token_counter = token_counter_cls(logger=self._logger)
            except Exception as error:
                self._logger.error("Token计数器初始化失败: %s", error)

        self.last_cleanup_time = None
        self.cleanup_block_until = None
        self.cleanup_block_duration_minutes = 2
        self.cleanup_count = 0
        self.current_context_summary = ""
        self.cleanup_thresholds = {
            "warning": 0.70,
            "recommend": 0.85,
            "critical": 0.90,
            "trigger": 0.80,
        }

    def get_effective_max_tokens(self) -> int:
        return self.max_context_tokens

    def get_input_budget_after_reserve(self) -> int:
        return self.input_budget_after_reserve

    def should_cleanup_context(self, current_tokens: int) -> Tuple[bool, str, str]:
        if self.is_cleanup_blocked():
            return False, "blocked", "上下文清理临时锁定中（上次清理完成不久）"
        if current_tokens is None:
            return False, "unknown", "无法确定token使用量"

        ratio = current_tokens / self.max_context_tokens
        if ratio >= self.cleanup_thresholds["critical"]:
            return True, "critical", f"token使用率超过{self.cleanup_thresholds['critical']*100:.0f}% ({ratio:.1%})"
        if ratio >= self.cleanup_thresholds["recommend"]:
            return True, "recommend", f"token使用率超过{self.cleanup_thresholds['recommend']*100:.0f}% ({ratio:.1%})"
        if ratio >= self.cleanup_thresholds["warning"]:
            return False, "warning", f"token使用率超过{self.cleanup_thresholds['warning']*100:.0f}% ({ratio:.1%})"
        return False, "low", f"token使用率正常 ({ratio:.1%})"

    def should_trigger_cleanup(self, current_tokens: int) -> bool:
        if self.is_cleanup_blocked():
            return False
        if current_tokens is None:
            return False
        ratio = current_tokens / self.max_context_tokens
        return ratio >= self.cleanup_thresholds["trigger"]

    def estimate_current_tokens(self, system_prompt: str, memories: List[str], current_query: str) -> Optional[int]:
        if not self.token_counter:
            self._logger.warning("Token计数器不可用，无法估计token使用量")
            return None

        try:
            return self.token_counter.estimate_context_tokens(system_prompt, memories, current_query)
        except Exception as error:
            self._logger.error("估计token使用量失败: %s", error)
            return None

    def load_memory_summaries(self, limit: int = 5) -> List[str]:
        summaries = []
        if not self.memory_dir.exists():
            self._logger.warning("记忆目录不存在: %s", self.memory_dir)
            return summaries

        try:
            memory_files = sorted(
                self.memory_dir.glob("*.txt"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for index, file_path in enumerate(memory_files[:limit]):
                try:
                    content = file_path.read_text(encoding="utf-8")
                    summary = content[:200].replace("\n", " ").strip()
                    if len(content) > 200:
                        summary += "..."
                    summaries.append(f"记忆{index + 1}: {summary}")
                except Exception as error:
                    self._logger.error("读取记忆文件失败 %s: %s", file_path, error)
        except Exception as error:
            self._logger.error("加载记忆摘要失败: %s", error)

        return summaries

    def generate_network_summary(self, topic: Optional[str] = None) -> str:
        recent_memories = self.load_memory_summaries(limit=10)
        summary_parts = [f"当前时间: {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}"]

        if recent_memories:
            summary_parts.append("\n最近记忆摘要:")
            for memory in recent_memories[:5]:
                summary_parts.append(f"  - {memory}")
            if len(recent_memories) > 5:
                summary_parts.append(f"  ... 还有{len(recent_memories)-5}条记忆")
        else:
            summary_parts.append("\n暂无近期记忆")

        if topic:
            summary_parts.append(f"\n当前话题聚焦: {topic}")

        if self.last_cleanup_time:
            last_cleanup_str = self.last_cleanup_time.strftime("%Y-%m-%d %H:%M:%S")
            summary_parts.append(f"\n上下文状态: 上次清理时间 {last_cleanup_str}，已清理 {self.cleanup_count} 次")
        else:
            summary_parts.append(f"\n上下文状态: 从未清理，已清理 {self.cleanup_count} 次")

        return "\n".join(summary_parts)

    def cleanup_and_reload_context(self, current_query: str, topic: Optional[str] = None) -> Tuple[List[Dict[str, str]], str]:
        self._logger.info("开始清理并重新加载上下文")

        network_summary = self.generate_network_summary(topic)
        new_context = [
            {
                "role": "system",
                "content": (
                    f"【上下文网络摘要】\n{network_summary}\n\n"
                    "注意：由于上下文长度限制，已清理详细对话历史，保留了记忆摘要。"
                ),
            },
            {"role": "user", "content": current_query},
        ]

        self.last_cleanup_time = datetime.now()
        self.cleanup_block_until = datetime.now() + timedelta(minutes=self.cleanup_block_duration_minutes)
        self.cleanup_count += 1
        self.current_context_summary = network_summary

        cleanup_report = (
            f"上下文清理完成 ({self.cleanup_count}次)\n"
            f"清理时间: {self.last_cleanup_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"网络摘要长度: {len(network_summary)} 字符\n"
            f"新上下文消息数: {len(new_context)}"
        )
        self._logger.info(cleanup_report)
        return new_context, cleanup_report

    def get_context_status(self) -> Dict[str, Any]:
        return {
            "max_context_tokens": self.max_context_tokens,
            "effective_max_tokens": self.effective_max_tokens,
            "input_budget_after_reserve": self.input_budget_after_reserve,
            "output_token_reserve": self.output_token_reserve,
            "cleanup_count": self.cleanup_count,
            "last_cleanup_time": self.last_cleanup_time.isoformat() if self.last_cleanup_time else None,
            "cleanup_blocked": self.is_cleanup_blocked(),
            "cleanup_block_until": self.cleanup_block_until.isoformat() if self.cleanup_block_until else None,
            "current_summary_length": len(self.current_context_summary) if self.current_context_summary else 0,
            "cleanup_thresholds": self.cleanup_thresholds,
            "token_counter_available": self.token_counter is not None,
        }

    def adjust_cleanup_thresholds(
        self,
        warning: Optional[int] = None,
        recommend: Optional[int] = None,
        critical: Optional[int] = None,
    ) -> None:
        warning = _normalize_threshold(warning)
        recommend = _normalize_threshold(recommend)
        critical = _normalize_threshold(critical)

        if warning is not None:
            self.cleanup_thresholds["warning"] = warning
        if recommend is not None:
            self.cleanup_thresholds["recommend"] = recommend
        if critical is not None:
            self.cleanup_thresholds["critical"] = critical

        self._logger.info("清理阈值已更新: %s", self.cleanup_thresholds)

    def is_cleanup_blocked(self) -> bool:
        if self.cleanup_block_until is None:
            return False
        if datetime.now() >= self.cleanup_block_until:
            self.cleanup_block_until = None
            return False
        return True


def format_context_token_info(
    *,
    messages: List[Dict[str, Any]],
    system_message: str,
    current_tools: List[Dict[str, Any]],
    context_manager: Any,
    logger: Any,
) -> str:
    if not context_manager or not getattr(context_manager, "token_counter", None):
        return ""

    try:
        status = get_actual_context_status(
            messages=messages,
            system_message=system_message,
            current_tools=current_tools,
            context_manager=context_manager,
        )
        if not status.get("success"):
            return ""

        tokens = int(status.get("tokens", 0) or 0)

        if tokens <= 0:
            return ""

        configured_max = int(status.get("configured_max_tokens", 0) or 0)
        output_reserve = int(status.get("output_token_reserve", 0) or 0)
        trigger_ratio = float(status.get("cleanup_trigger_ratio", 0.8) or 0.8)
        trigger_tokens = int(status.get("cleanup_trigger_tokens", 0) or 0)
        percentage = status.get("percentage", 0)
        level = status.get("level", "low")
        color_codes = {
            "low": "\033[38;2;111;208;104m",
            "medium": "\033[38;2;255;193;7m",
            "high": "\033[38;2;255;152;0m",
            "critical": "\033[38;2;244;67;54m",
        }
        color = color_codes.get(level, "\033[0m")
        reset = "\033[0m"
        hint = ""
        if level in {"high", "critical"}:
            hint = "\n- 提示: 如果话题已切换或token使用量过高，可使用 context_manager_tool 模块的工具主动清理上下文"

        cleanup_signal = ""
        if getattr(context_manager, "is_cleanup_blocked", None) and context_manager.is_cleanup_blocked():
            block_until = getattr(context_manager, "cleanup_block_until", None)
            block_minutes = getattr(context_manager, "cleanup_block_duration_minutes", 2)
            if block_until:
                remaining = int((block_until - datetime.now()).total_seconds() / 60)
                cleanup_signal = (
                    f"\n\n【上下文清理标记】\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ 上下文已于最近完成清理，{remaining}分钟内不会再次触发清理\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━"
                )

        return (
            f"\n\n【上下文状态】\n"
            f"- Token使用量: {tokens:,}/{configured_max:,} ({percentage}%)\n"
            f"- 输出预留: {output_reserve:,} | 自动压缩阈值: {trigger_tokens:,} ({trigger_ratio:.0%})\n"
            f"- 消息数: {len(messages)} 条 | 工具数: {len(current_tools)} 个\n"
            f"- 状态: {color}{level}{reset}{hint}"
            f"{cleanup_signal}"
        )
    except Exception as error:
        logger.error("获取token信息失败: %s", error)
        return ""


def _calculate_token_breakdown(
    *,
    messages: List[Dict[str, Any]],
    system_message: str,
    current_tools: List[Dict[str, Any]],
    context_manager: Any,
) -> Dict[str, int]:
    token_counter = getattr(context_manager, "token_counter", None)
    if token_counter is None:
        return {
            "system_tokens": 0,
            "message_tokens": 0,
            "tool_tokens": 0,
            "total_tokens": 0,
        }

    system_tokens = token_counter.count_tokens(system_message) if system_message else 0
    message_tokens = token_counter.estimate_messages_tokens(messages) if messages else 0
    tool_tokens = 0
    if current_tools:
        tools_json = json.dumps(current_tools, ensure_ascii=False)
        tool_tokens = token_counter.count_tokens(tools_json)

    return {
        "system_tokens": system_tokens,
        "message_tokens": message_tokens,
        "tool_tokens": tool_tokens,
        "total_tokens": system_tokens + message_tokens + tool_tokens,
    }


def calculate_actual_tokens(
    *,
    messages: List[Dict[str, Any]],
    system_message: str = "",
    current_tools: List[Dict[str, Any]],
    context_manager: Any,
) -> int:
    if not context_manager or not getattr(context_manager, "token_counter", None):
        return 0

    breakdown = _calculate_token_breakdown(
        messages=messages,
        system_message=system_message,
        current_tools=current_tools,
        context_manager=context_manager,
    )
    return int(breakdown["total_tokens"])


def get_actual_context_status(
    *,
    messages: List[Dict[str, Any]],
    system_message: str = "",
    current_tools: List[Dict[str, Any]],
    context_manager: Any,
) -> Dict[str, Any]:
    if not context_manager or not getattr(context_manager, "token_counter", None):
        return {
            "success": False,
            "error": "Token计数器不可用",
            "token_counter_available": False,
        }

    try:
        breakdown = _calculate_token_breakdown(
            messages=messages,
            system_message=system_message,
            current_tools=current_tools,
            context_manager=context_manager,
        )
        tokens = int(breakdown["total_tokens"])
        configured_max = _get_configured_max_tokens(context_manager)
        output_reserve = int(getattr(context_manager, "output_token_reserve", 0) or 0)
        trigger_ratio = getattr(context_manager, "cleanup_thresholds", {}).get("trigger", 0.8)
        trigger_tokens = int(configured_max * trigger_ratio)
        percentage = context_manager.token_counter.get_token_usage_percentage(tokens, configured_max)
        level = context_manager.token_counter.get_token_usage_warning_level(tokens, configured_max)
        return {
            "success": True,
            "tokens": tokens,
            "max_tokens": configured_max,
            "effective_max_tokens": configured_max,
            "configured_max_tokens": configured_max,
            "output_token_reserve": output_reserve,
            "cleanup_trigger_tokens": trigger_tokens,
            "cleanup_trigger_ratio": trigger_ratio,
            "percentage": percentage,
            "level": level,
            "message_count": len(messages),
            "tool_count": len(current_tools),
            "system_tokens": int(breakdown["system_tokens"]),
            "message_tokens": int(breakdown["message_tokens"]),
            "tool_tokens": int(breakdown["tool_tokens"]),
            "message": (
                f"当前上下文使用 {tokens:,}/{configured_max:,} tokens ({percentage}%)，"
                f"输出预留 {output_reserve:,}，"
                f"自动压缩阈值 {trigger_tokens:,} ({trigger_ratio:.0%})，状态: {level}"
            ),
        }
    except Exception as error:
        return {"success": False, "error": str(error)}


def _get_effective_max_tokens(context_manager: Any) -> int:
    return _get_configured_max_tokens(context_manager)


def _get_configured_max_tokens(context_manager: Any) -> int:
    configured = getattr(context_manager, "max_context_tokens", None)
    if configured:
        return int(configured)
    get_effective = getattr(context_manager, "get_effective_max_tokens", None)
    if callable(get_effective):
        return int(get_effective())
    return int(getattr(context_manager, "effective_max_tokens", None) or 0)


def do_context_cleanup(
    *,
    arguments: Dict[str, Any],
    messages: List[Dict[str, Any]],
    current_context: List[Dict[str, Any]],
    context_manager: Any,
) -> Dict[str, Any]:
    try:
        current_query = arguments.get("current_query", "继续对话")
        topic = arguments.get("topic", None)
        new_context, cleanup_report = context_manager.cleanup_and_reload_context(
            current_query=current_query,
            topic=topic,
        )
        current_context.clear()
        current_context.extend(new_context)
        messages.clear()
        messages.extend(new_context)
        return {
            "success": True,
            "cleanup_report": cleanup_report,
            "message": f"上下文已清理并重新加载。{cleanup_report}",
        }
    except Exception as error:
        return {"success": False, "error": str(error)}


def _normalize_threshold(value: Optional[int]) -> Optional[float]:
    if value is None:
        return None

    numeric = float(value)
    if numeric > 1:
        numeric /= 100.0
    return max(0.0, min(1.0, numeric))
