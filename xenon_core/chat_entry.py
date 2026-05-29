from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


INTERRUPTED_TOOL_CALL_MESSAGE = "[系统自动补全] 用户发起新输入，之前的工具调用已被中断"


def handle_user_chat_entry(
    *,
    user_input: str,
    current_context: List[Dict[str, Any]],
    decay_recent_tool_results_fn: Callable[[str], None],
    set_interrupted_fn: Callable[[bool], None],
    set_active_user_input_fn: Callable[[str], None],
    reset_loaded_tools_for_new_turn_fn: Callable[[], Optional[str]],
    find_pending_tool_call_ids_fn: Callable[[List[Dict[str, Any]]], Any],
    append_conversation_message_fn: Callable[[List[Dict[str, Any]], Dict[str, Any]], None],
    cleanup_reasoning_content_fn: Callable[[List[Dict[str, Any]]], None],
    process_chat_with_context_fn: Callable[[str], None],
    logger: Any,
) -> None:
    set_interrupted_fn(False)
    decay_recent_tool_results_fn(user_input)
    set_active_user_input_fn(user_input)

    unload_notice = reset_loaded_tools_for_new_turn_fn()
    if unload_notice:
        append_conversation_message_fn(
            current_context,
            {"role": "system", "content": unload_notice},
        )

    pending_tool_calls = find_pending_tool_call_ids_fn(current_context)
    if pending_tool_calls:
        logger.warning(
            "检测到 %s 个未响应的 tool_calls，在新用户输入前补全响应",
            len(pending_tool_calls),
        )
        for tool_call_id in pending_tool_calls:
            append_conversation_message_fn(
                current_context,
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": INTERRUPTED_TOOL_CALL_MESSAGE,
                },
            )

    cleanup_reasoning_content_fn(current_context)
    append_conversation_message_fn(
        current_context,
        {"role": "user", "content": user_input},
    )
    process_chat_with_context_fn(user_input)
