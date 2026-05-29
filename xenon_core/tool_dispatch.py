from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Type


def normalize_tool_call(tool_call: Any) -> Dict[str, str]:
    if isinstance(tool_call, dict):
        function_payload = tool_call.get("function", {}) or {}
        return {
            "tool_name": function_payload.get("name", "unknown"),
            "tool_call_id": tool_call.get("id", ""),
            "arguments_str": function_payload.get("arguments", ""),
        }

    function_payload = getattr(tool_call, "function", None)
    return {
        "tool_name": getattr(function_payload, "name", "unknown"),
        "tool_call_id": getattr(tool_call, "id", ""),
        "arguments_str": getattr(function_payload, "arguments", ""),
    }


def handle_tool_call_batch(
    *,
    tool_calls: List[Any],
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    interrupted_exception_cls: Type[BaseException],
    is_interrupted_fn: Callable[[], bool],
    handle_load_module_fn: Callable[[str, str, List[Dict[str, Any]]], None],
    handle_get_tool_description_fn: Callable[[str, str, List[Dict[str, Any]]], None],
    handle_get_module_tools_fn: Callable[[str, str, List[Dict[str, Any]]], None],
    handle_context_manager_tool_fn: Callable[[str, str, str, List[Dict[str, Any]], Optional[List[Dict[str, Any]]]], None],
    handle_execute_tool_fn: Callable[[str, str, str, List[Dict[str, Any]]], None],
    add_tool_message_fn: Callable[[List[Dict[str, Any]], str, str], None],
    is_tool_loaded_fn: Callable[[str], bool],
    is_single_tool_loaded_fn: Callable[[str], bool],
    logger: Any,
    print_fn: Callable[..., Any] = print,
) -> None:
    processed_tool_call_ids = set()
    try:
        for tool_call in tool_calls:
            if is_interrupted_fn():
                raise interrupted_exception_cls("用户中断")

            item = normalize_tool_call(tool_call)
            tool_name = item["tool_name"]
            tool_call_id = item["tool_call_id"]
            arguments_str = item["arguments_str"]
            processed_tool_call_ids.add(tool_call_id)

            if tool_name == "load_module":
                handle_load_module_fn(tool_call_id, arguments_str, messages)
            elif tool_name == "get_tool_description":
                handle_get_tool_description_fn(tool_call_id, arguments_str, messages)
            elif tool_name == "get_module_tools":
                handle_get_module_tools_fn(tool_call_id, arguments_str, messages)
            elif tool_name.startswith("context_manager_tool_"):
                handle_context_manager_tool_fn(tool_call_id, tool_name, arguments_str, messages, tools)
            elif is_tool_loaded_fn(tool_name) or is_single_tool_loaded_fn(tool_name):
                handle_execute_tool_fn(tool_call_id, tool_name, arguments_str, messages)
            else:
                error_msg = (
                    f"工具 '{tool_name}' 尚未加载。请先使用 load_module 加载该工具所属的模块，"
                    f"或使用 get_tool_description 获取单个工具的描述。"
                )
                print_fn(f"\n\033[91m{error_msg}\033[0m\n")
                add_tool_message_fn(messages, tool_call_id, error_msg)
    except interrupted_exception_cls:
        for tool_call in tool_calls:
            item = normalize_tool_call(tool_call)
            tool_call_id = item["tool_call_id"]
            if tool_call_id not in processed_tool_call_ids:
                add_tool_message_fn(messages, tool_call_id, "[用户中断] 工具调用已被用户中断")
        raise
