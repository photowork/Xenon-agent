from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional


MANUAL_CLEANUP_DONE_NOTE = (
    "[SYSTEM NOTE] Manual context cleanup for the current user request has completed. "
    "Do not call context_manager_tool cleanup again for this same request. "
    "Continue with the user's post-cleanup objective."
)

SYNTHETIC_CLEANUP_REASONING = (
    "Manual context cleanup completed; this synthetic tool-call pair preserves "
    "API message integrity after compression."
)


def handle_context_manager_tool_call(
    *,
    tool_call_id: str,
    tool_name: str,
    arguments_str: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    parse_arguments_fn: Callable[[str], Dict[str, Any]],
    find_pending_tool_call_ids_fn: Callable[[List[Dict[str, Any]], Optional[str]], set],
    add_tool_message_fn: Callable[[List[Dict[str, Any]], str, str], None],
    get_actual_context_status_fn: Callable[[], Dict[str, Any]],
    get_actual_token_estimate_fn: Callable[[], Dict[str, Any]],
    get_current_tools_fn: Callable[[], List[Dict[str, Any]]],
    auto_trim_context_fn: Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], None],
    extract_tool_call_id_fn: Callable[[Any], Optional[str]],
    append_conversation_message_fn: Callable[[List[Dict[str, Any]], Dict[str, Any]], None],
    get_or_create_context_manager_tool_fn: Callable[[], Any],
    handle_tool_error_fn: Callable[[List[Dict[str, Any]], str, Exception, str], None],
    logger: Any,
    print_fn: Callable[..., Any] = print,
) -> None:
    try:
        arguments = parse_arguments_fn(arguments_str)
        method_name = tool_name.replace("context_manager_tool_Context_", "")

        print_fn(f"\n\033[38;2;86;114;79m上下文管理（手动调用）: \033[0m\033[38;2;86;114;79m{method_name}\033[0m")
        print_fn(f"\033[38;2;86;114;79m参数: \033[0m\033[38;2;86;114;79m{arguments}\033[0m")

        pending_tool_call_ids = find_pending_tool_call_ids_fn(messages, tool_call_id)
        if pending_tool_call_ids and method_name == "cleanup_and_reload_context":
            error_msg = "当前有其他待处理的工具调用，无法执行清理。请等待这些工具调用完成后重试。"
            print_fn(f"\033[91m{error_msg}\033[0m")
            add_tool_message_fn(messages, tool_call_id, error_msg)
            return

        if method_name == "get_context_status":
            result = get_actual_context_status_fn()
        elif method_name == "estimate_current_tokens":
            result = get_actual_token_estimate_fn()
        elif method_name == "cleanup_and_reload_context":
            context_manager_tool = get_or_create_context_manager_tool_fn()
            context_manager = _resolve_context_manager(context_manager_tool)
            if (
                _is_cleanup_blocked(context_manager)
                and not bool(arguments.get("force"))
            ):
                result = {
                    "success": True,
                    "skipped": True,
                    "blocked": True,
                    "message": (
                        "Manual context cleanup was already completed recently; "
                        "skipping duplicate cleanup and continuing with the post-cleanup task."
                    ),
                    "next_action": "continue_with_post_cleanup_task",
                }
                add_tool_message_fn(messages, tool_call_id, str(result))
                return

            current_tools = tools if tools is not None else get_current_tools_fn()
            _remove_matching_assistant_tool_call(
                messages,
                tool_call_id,
                extract_tool_call_id_fn=extract_tool_call_id_fn,
            )
            auto_trim_context_fn(messages, current_tools)
            _append_manual_cleanup_done_note(messages)

            if not _has_matching_assistant_tool_call(
                messages,
                tool_call_id,
                extract_tool_call_id_fn=extract_tool_call_id_fn,
            ):
                append_conversation_message_fn(
                    messages,
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": arguments_str,
                                },
                            }
                        ],
                        "reasoning_content": SYNTHETIC_CLEANUP_REASONING,
                    },
                )

            # 获取清理次数并自增，让后续对话知道已经清理过，避免递归清理
            cleanup_count = _mark_cleanup_completed(context_manager)

            result = {
                "success": True,
                "message": f"上下文已自动压缩（通过手动调用），当前累计清理次数: {cleanup_count}",
                "cleanup_count": cleanup_count,
                "new_context_summary": f"当前消息数: {len(messages)}",
                "next_action": "continue_with_post_cleanup_task",
                "repeat_cleanup_allowed": False,
            }
            add_tool_message_fn(messages, tool_call_id, str(result))
            return
        else:
            context_manager_tool = get_or_create_context_manager_tool_fn()
            method = getattr(context_manager_tool, method_name, None)
            if method is None:
                error_msg = f"错误：上下文管理方法 '{method_name}' 不存在。"
                print_fn(f"\n\033[91m{error_msg}\033[0m\n")
                add_tool_message_fn(messages, tool_call_id, error_msg)
                return
            result = method(**arguments)

        print_fn(f"\033[38;2;86;114;79m结果: \033[0m\033[38;2;86;114;79m{result}\033[0m\n")
        add_tool_message_fn(messages, tool_call_id, str(result))

    except json.JSONDecodeError as error:
        logger.error("JSON解析失败: %s", error)
        error_msg = f"JSON解析错误: {error}\n原始参数: {arguments_str[:200]}..."
        print_fn(f"错误: 参数格式错误 - {error}")
        add_tool_message_fn(messages, tool_call_id, error_msg)
    except Exception as error:
        handle_tool_error_fn(messages, tool_call_id, error, "上下文管理")


def _has_matching_assistant_tool_call(
    messages: List[Dict[str, Any]],
    tool_call_id: str,
    *,
    extract_tool_call_id_fn: Callable[[Any], Optional[str]],
) -> bool:
    for message in messages:
        if message.get("role") == "assistant" and message.get("tool_calls"):
            for tool_call in message["tool_calls"]:
                if extract_tool_call_id_fn(tool_call) == tool_call_id:
                    return True
    return False


def _resolve_context_manager(context_manager_tool: Any) -> Any:
    return getattr(context_manager_tool, "context_manager", None) or context_manager_tool


def _is_cleanup_blocked(context_manager: Any) -> bool:
    is_blocked = getattr(context_manager, "is_cleanup_blocked", None)
    if callable(is_blocked):
        return bool(is_blocked())
    return False


def _mark_cleanup_completed(context_manager: Any) -> int:
    if context_manager is None:
        return 0

    now = datetime.now()
    if hasattr(context_manager, "last_cleanup_time"):
        context_manager.last_cleanup_time = now

    if hasattr(context_manager, "cleanup_block_until"):
        duration = getattr(context_manager, "cleanup_block_duration_minutes", 0) or 0
        try:
            context_manager.cleanup_block_until = now + timedelta(minutes=float(duration))
        except Exception:
            context_manager.cleanup_block_until = None

    if hasattr(context_manager, "cleanup_count"):
        context_manager.cleanup_count += 1
        return int(context_manager.cleanup_count)

    return 0


def _append_manual_cleanup_done_note(messages: List[Dict[str, Any]]) -> None:
    if any(
        message.get("role") == "system"
        and message.get("content") == MANUAL_CLEANUP_DONE_NOTE
        for message in messages
    ):
        return
    messages.append({"role": "system", "content": MANUAL_CLEANUP_DONE_NOTE})


def _remove_matching_assistant_tool_call(
    messages: List[Dict[str, Any]],
    tool_call_id: str,
    *,
    extract_tool_call_id_fn: Callable[[Any], Optional[str]],
) -> bool:
    changed = False
    filtered_messages: List[Dict[str, Any]] = []

    for message in messages:
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            filtered_messages.append(message)
            continue

        tool_calls = list(message.get("tool_calls") or [])
        remaining_tool_calls = [
            tool_call
            for tool_call in tool_calls
            if extract_tool_call_id_fn(tool_call) != tool_call_id
        ]
        if len(remaining_tool_calls) == len(tool_calls):
            filtered_messages.append(message)
            continue

        changed = True
        if remaining_tool_calls:
            message = dict(message)
            message["tool_calls"] = remaining_tool_calls
            filtered_messages.append(message)
            continue

        # The cleanup call is the action being consumed, not task state that
        # should survive into the checkpoint. Drop the whole assistant turn so
        # its reasoning does not teach the next model turn to clean again.
        continue

    if changed:
        messages[:] = filtered_messages

    return changed
