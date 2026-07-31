from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from xenon_core.tool_payload_runtime import (
    build_compact_tool_arguments_preview,
    build_compact_tool_result_preview,
    sanitize_tool_arguments_for_execution,
)


MUTATING_TOOL_NAME_PARTS = (
    "write",
    "create",
    "append",
    "insert",
    "str_replace",
    "replace",
    "delete",
    "move",
    "copy",
)


def touch_tool_usage(
    tool_name: str,
    *,
    touch_single_tool_fn: Callable[[str], None],
    loaded_modules: Dict[str, Dict[str, Any]],
    now_fn: Callable[[], Any] = datetime.now,
) -> None:
    touch_single_tool_fn(tool_name)
    last_used = now_fn()
    for module_info in loaded_modules.values():
        if tool_name in module_info.get("tool_names", set()):
            module_info["last_used"] = last_used


def execute_tool_call(
    *,
    tool_call_id: str,
    tool_name: str,
    arguments_str: str,
    messages: List[Dict[str, Any]],
    parse_arguments_fn: Callable[[str], Dict[str, Any]],
    execute_tool_fn: Callable[[str, Dict[str, Any]], Any],
    record_tool_outcome_fn: Callable[..., None],
    build_tool_call_snapshot_fn: Callable[..., Dict[str, Any]],
    push_recent_tool_result_fn: Callable[[Dict[str, Any]], None],
    monitor_tool_result_snapshot_fn: Callable[[Dict[str, Any]], None],
    add_tool_message_fn: Callable[[List[Dict[str, Any]], str, str], None],
    handle_tool_error_fn: Callable[[List[Dict[str, Any]], str, Exception, str], None],
    build_recovery_plan_fn: Callable[..., Optional[Dict[str, Any]]],
    touch_single_tool_fn: Callable[[str], None],
    loaded_modules: Dict[str, Dict[str, Any]],
    set_tool_executing_fn: Callable[[bool], None],
    stream_callback: Optional[Callable[[Dict[str, Any]], None]],
    current_phase: Optional[str],
    logger: Any,
    print_fn: Callable[..., Any] = print,
    now_fn: Callable[[], Any] = datetime.now,
) -> None:
    arguments: Optional[Dict[str, Any]] = None
    try:
        arguments = sanitize_tool_arguments_for_execution(
            tool_name,
            parse_arguments_fn(arguments_str),
        )
        display_arguments = build_compact_tool_arguments_preview(tool_name, arguments)
        print_fn(f"\n\033[38;2;86;114;79m调用工具: \033[0m\033[38;2;86;114;79m{tool_name}\033[0m")
        print_fn(f"\033[38;2;86;114;79m参数: \033[0m\033[38;2;86;114;79m{display_arguments}\033[0m")

        touch_tool_usage(
            tool_name,
            touch_single_tool_fn=touch_single_tool_fn,
            loaded_modules=loaded_modules,
            now_fn=now_fn,
        )

        set_tool_executing_fn(True)
        if stream_callback:
            stream_callback(
                {
                    "type": "tool_progress",
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "content": build_tool_execution_progress_message(tool_name, arguments),
                }
            )
        _start_time = time.perf_counter()
        result = execute_tool_fn(tool_name, arguments)
        _elapsed = time.perf_counter() - _start_time
        result_text = str(result)
        result_text += f"\n\n[⏱ 执行耗时: {_elapsed:.2f}s]"
        display_result_text = build_compact_tool_result_preview(tool_name, arguments, result)
        print_fn(f"\033[38;2;86;114;79m结果: \033[0m\033[38;2;86;114;79m{display_result_text}\033[0m\n")

        record_tool_outcome_fn(
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            success=True,
        )
        snapshot = build_tool_call_snapshot_fn(
            tool_name=tool_name,
            arguments=arguments,
            success=True,
            result=result,
        )
        push_recent_tool_result_fn(snapshot)
        monitor_tool_result_snapshot_fn(snapshot)

        if stream_callback:
            stream_callback(
                {
                    "type": "tool_result",
                    "tool_call_id": tool_call_id,
                    "content": display_result_text,
                }
            )

        add_tool_message_fn(messages, tool_call_id, result_text)

    except json.JSONDecodeError as error:
        logger.error("JSON解析失败: %s", error)
        logger.error("原始参数字符串: %r", arguments_str)
        error_msg = f"JSON解析错误: {error}\n原始参数: {arguments_str[:200]}..."
        recovery_plan = build_recovery_plan_fn(
            tool_name=tool_name,
            error=error,
            phase=current_phase,
        )
        failure_arguments = {"raw_arguments": arguments_str[:200]}
        _record_failed_tool_call(
            tool_name=tool_name,
            arguments=failure_arguments,
            result=error_msg,
            error_text=str(error),
            recovery_plan=recovery_plan,
            record_tool_outcome_fn=record_tool_outcome_fn,
            build_tool_call_snapshot_fn=build_tool_call_snapshot_fn,
            push_recent_tool_result_fn=push_recent_tool_result_fn,
            monitor_tool_result_snapshot_fn=monitor_tool_result_snapshot_fn,
        )
        print_fn(f"错误: 参数格式错误 - {error}")
        add_tool_message_fn(messages, tool_call_id, error_msg)

    except Exception as error:
        recovery_plan = build_recovery_plan_fn(
            tool_name=tool_name,
            error=error,
            phase=current_phase,
        )
        failure_arguments = arguments if arguments is not None else {"raw_arguments": arguments_str[:200]}
        _record_failed_tool_call(
            tool_name=tool_name,
            arguments=failure_arguments,
            result=str(error),
            error_text=str(error),
            recovery_plan=recovery_plan,
            record_tool_outcome_fn=record_tool_outcome_fn,
            build_tool_call_snapshot_fn=build_tool_call_snapshot_fn,
            push_recent_tool_result_fn=push_recent_tool_result_fn,
            monitor_tool_result_snapshot_fn=monitor_tool_result_snapshot_fn,
        )
        handle_tool_error_fn(messages, tool_call_id, error, "工具执行")

    finally:
        set_tool_executing_fn(False)


def build_tool_execution_progress_message(tool_name: str, arguments: Dict[str, Any]) -> str:
    if not _looks_like_mutating_tool(tool_name):
        return f"开始执行工具: {tool_name}"

    target_path = _extract_target_path(arguments)
    payload = _extract_text_payload(arguments)
    details = []
    if target_path:
        details.append(_truncate_text(target_path, 120))
    if payload:
        details.append(f"{len(payload):,} 字符 / {_count_lines(payload):,} 行")

    suffix = f" ({', '.join(details)})" if details else ""
    return f"开始执行文件操作: {tool_name}{suffix}"


def _looks_like_mutating_tool(tool_name: str) -> bool:
    lowered = (tool_name or "").lower()
    return any(part in lowered for part in MUTATING_TOOL_NAME_PARTS)


def _extract_target_path(arguments: Dict[str, Any]) -> str:
    for key in ("file_path", "path", "destination_path", "source_path"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _extract_text_payload(arguments: Dict[str, Any]) -> str:
    for key in ("content", "new_str"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + 1


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _record_failed_tool_call(
    *,
    tool_name: str,
    arguments: Dict[str, Any],
    result: Any,
    error_text: str,
    recovery_plan: Optional[Dict[str, Any]],
    record_tool_outcome_fn: Callable[..., None],
    build_tool_call_snapshot_fn: Callable[..., Dict[str, Any]],
    push_recent_tool_result_fn: Callable[[Dict[str, Any]], None],
    monitor_tool_result_snapshot_fn: Callable[[Dict[str, Any]], None],
) -> None:
    record_tool_outcome_fn(
        tool_name=tool_name,
        arguments=arguments,
        result=result,
        success=False,
        recovery_plan=recovery_plan,
    )
    snapshot = build_tool_call_snapshot_fn(
        tool_name=tool_name,
        arguments=arguments,
        success=False,
        result=result,
        error=error_text,
        recovery_summary=(recovery_plan or {}).get("summary", ""),
    )
    push_recent_tool_result_fn(snapshot)
    monitor_tool_result_snapshot_fn(snapshot)
