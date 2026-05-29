from __future__ import annotations

import copy
from typing import Any, Callable, Dict, List, Optional


def clone_message(message: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(message)


def append_conversation_message(
    messages: List[Dict[str, Any]],
    message: Dict[str, Any],
    *,
    record_full_history: bool = True,
    record_full_history_fn: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> None:
    messages.append(message)
    if record_full_history and record_full_history_fn is not None:
        record_full_history_fn(message)


def extract_tool_call_id(tool_call: Any) -> Optional[str]:
    if isinstance(tool_call, dict):
        return tool_call.get("id")
    return getattr(tool_call, "id", None)


def find_pending_tool_call_ids(
    messages: List[Dict[str, Any]],
    *,
    exclude_id: Optional[str] = None,
) -> set[str]:
    pending = set()
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            for tool_call in message["tool_calls"]:
                tool_call_id = extract_tool_call_id(tool_call)
                if tool_call_id:
                    pending.add(tool_call_id)
        elif message.get("role") == "tool":
            responded_id = message.get("tool_call_id")
            if responded_id in pending:
                pending.remove(responded_id)

    if exclude_id:
        pending.discard(exclude_id)
    return pending


def ensure_message_integrity(
    messages: List[Dict[str, Any]],
    *,
    logger: Any,
) -> List[Dict[str, Any]]:
    fixed_messages = []
    pending_tool_calls: Dict[str, str] = {}

    for message in messages:
        fixed_messages.append(message)

        if message.get("role") == "assistant" and message.get("tool_calls"):
            for tool_call in message["tool_calls"]:
                if isinstance(tool_call, dict):
                    tool_call_id = tool_call.get("id")
                    tool_name = (tool_call.get("function", {}) or {}).get("name", "unknown")
                else:
                    tool_call_id = getattr(tool_call, "id", None)
                    tool_name = getattr(getattr(tool_call, "function", None), "name", "unknown")
                if tool_call_id:
                    pending_tool_calls[tool_call_id] = tool_name

        if message.get("role") == "tool":
            tool_call_id = message.get("tool_call_id")
            if tool_call_id in pending_tool_calls:
                del pending_tool_calls[tool_call_id]

    if pending_tool_calls:
        logger.warning("检测到 %s 个未响应的 tool_calls，正在修复...", len(pending_tool_calls))
        for tool_call_id, tool_name in pending_tool_calls.items():
            fixed_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": (
                        "[系统提示] 此工具调用因上下文压缩或异常被中断，未实际执行。\n"
                        f"工具名称: {tool_name}\n"
                        "请根据已有信息继续处理，如需该工具的结果请重新调用。"
                    ),
                }
            )

    return fixed_messages
