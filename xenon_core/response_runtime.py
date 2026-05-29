from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional


TOOLLESS_ACTION_GUARD_PREFIX = "[TOOLLESS_ACTION_GUARD]"
_PATH_OR_FILE_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\s'\"<>|]+|/[^\s'\"<>|]+|`[^`]+\.(?:py|md|json|txt|html|bat|sh|csv|xlsx|docx|pdf)`)",
    re.IGNORECASE,
)
_FIRST_PERSON_ACTION_MARKERS = (
    "让我",
    "我先",
    "我来",
    "我现在",
    "这就",
    "马上",
    "来修",
    "let me",
    "i'll",
    "i will",
    "i am going to",
    "i'm going to",
)
_ACTION_VERBS = (
    "查看",
    "看看",
    "读取",
    "检查",
    "搜索",
    "查找",
    "定位",
    "打开",
    "修改",
    "修正",
    "修复",
    "写入",
    "替换",
    "同步",
    "运行",
    "执行",
    "验证",
    "加载",
    "调用",
    "确认",
    "核对",
    "对比",
    "创建",
    "删除",
    "保存",
    "inspect",
    "read",
    "check",
    "search",
    "locate",
    "open",
    "modify",
    "edit",
    "fix",
    "write",
    "replace",
    "sync",
    "run",
    "execute",
    "verify",
    "load",
    "call",
)
_ACTION_COMPLETION_CLAIMS = (
    "找到了",
    "看到了",
    "检查完",
    "读取完",
    "修正完",
    "修改完",
    "修复完",
    "已修正",
    "已修改",
    "已写入",
    "已同步",
    "已完成",
    "完成了",
    "搞定",
    "验证通过",
    "fixed",
    "done",
    "updated",
    "verified",
)


def _safe_print(print_fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    try:
        print_fn(*args, **kwargs)
    except UnicodeEncodeError:
        safe_args = tuple(_replace_gbk_unsafe_chars(arg) for arg in args)
        try:
            print_fn(*safe_args, **kwargs)
        except Exception:
            return


def _replace_gbk_unsafe_chars(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return value.encode("gbk", errors="replace").decode("gbk")


def cleanup_reasoning_content(messages: List[Dict[str, Any]]) -> None:
    for message in messages:
        message.pop("reasoning_content", None)


def validate_and_fix_json(json_str: str, *, logger: Any) -> Optional[str]:
    if not json_str or json_str.strip() == "":
        return None

    json_str = json_str.strip()

    try:
        json.loads(json_str)
        return json_str
    except json.JSONDecodeError:
        pass

    stack = []
    fixed_chars: List[str] = []
    in_string = False
    escape = False

    for index, char in enumerate(json_str):
        fixed_chars.append(char)

        if not in_string:
            if char == '"' and not escape:
                in_string = True
            elif char in "{[":
                stack.append((char, index))
            elif char in "}]":
                if stack and (
                    (stack[-1][0] == "{" and char == "}")
                    or (stack[-1][0] == "[" and char == "]")
                ):
                    stack.pop()
        else:
            if char == '"' and not escape:
                in_string = False
            elif char == "\\" and not escape:
                escape = True
            else:
                escape = False

    for bracket_type, _ in reversed(stack):
        fixed_chars.append("}" if bracket_type == "{" else "]")

    fixed_str = "".join(fixed_chars)

    try:
        json.loads(fixed_str)
        logger.info("JSON 修复成功（基于栈）")
        return fixed_str
    except json.JSONDecodeError as error:
        logger.warning("栈修复后仍失败，尝试提取有效对象: %s", error)
        start = fixed_str.find("{")
        end = fixed_str.rfind("}")
        if start != -1 and end != -1 and start < end:
            candidate = fixed_str[start : end + 1]
            try:
                json.loads(candidate)
                logger.info("成功提取有效 JSON 对象")
                return candidate
            except json.JSONDecodeError:
                pass
        return None


def process_streaming_response(
    *,
    response: Any,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    interrupted_exception_cls: type[BaseException],
    is_interrupted_fn: Callable[[], bool],
    stream_callback: Optional[Callable[[Dict[str, Any]], None]],
    validate_and_fix_json_fn: Callable[[str], Optional[str]],
    append_conversation_message_fn: Callable[[List[Dict[str, Any]], Dict[str, Any]], None],
    save_memory_log_fn: Callable[..., None],
    handle_tool_calls_fn: Callable[[Any, List[Dict[str, Any]], List[Dict[str, Any]]], None],
    get_current_tools_fn: Callable[[], List[Dict[str, Any]]],
    continue_chat_fn: Callable[[List[Dict[str, Any]], List[Dict[str, Any]], str], None],
    model_for_recursive_chat: str,
    logger: Any,
    print_fn: Callable[..., Any] = print,
) -> None:
    reasoning_content = ""
    final_content = ""
    tool_calls_buffer: List[Dict[str, Any]] = []
    tool_calls_by_index: Dict[Any, Dict[str, Any]] = {}
    tool_calls_by_id: Dict[str, Dict[str, Any]] = {}

    try:
        for chunk in response:
            _raise_if_interrupted(
                is_interrupted_fn=is_interrupted_fn,
                interrupted_exception_cls=interrupted_exception_cls,
                response=response,
            )

            if not getattr(chunk, "choices", None):
                continue

            delta = chunk.choices[0].delta

            delta_reasoning = getattr(delta, "reasoning_content", None)
            if delta_reasoning:
                reasoning_content += delta_reasoning
                _safe_print(print_fn, f"\033[95m{delta_reasoning}\033[0m", end="", flush=True)
                if stream_callback:
                    stream_callback({"type": "thinking", "content": delta_reasoning})

            delta_content = getattr(delta, "content", None)
            if delta_content:
                final_content += delta_content
                _safe_print(print_fn, f"\033[96m{delta_content}\033[0m", end="", flush=True)
                if stream_callback:
                    stream_callback({"type": "content", "content": delta_content})

            delta_tool_calls = getattr(delta, "tool_calls", None)
            if delta_tool_calls:
                _collect_streamed_tool_calls(
                    delta_tool_calls=delta_tool_calls,
                    tool_calls_buffer=tool_calls_buffer,
                    tool_calls_by_index=tool_calls_by_index,
                    tool_calls_by_id=tool_calls_by_id,
                )

        _safe_print(print_fn)

        if tool_calls_buffer:
            _finalize_streamed_tool_calls(
                tool_calls_buffer=tool_calls_buffer,
                validate_and_fix_json_fn=validate_and_fix_json_fn,
            )
            _emit_streamed_tool_call_events(
                tool_calls_buffer=tool_calls_buffer,
                stream_callback=stream_callback,
            )

            assistant_message: Dict[str, Any] = {
                "role": "assistant",
                "content": final_content if final_content else "",
                "tool_calls": tool_calls_buffer,
            }
            if reasoning_content:
                assistant_message["reasoning_content"] = reasoning_content
            append_conversation_message_fn(messages, assistant_message)

            save_memory_log_fn(
                "assistant",
                content=final_content,
                reasoning_content=reasoning_content,
            )

            handle_tool_calls_fn(tool_calls_buffer, messages, tools)
            _raise_if_interrupted(
                is_interrupted_fn=is_interrupted_fn,
                interrupted_exception_cls=interrupted_exception_cls,
            )
            updated_tools = get_current_tools_fn()
            continue_chat_fn(messages, updated_tools, model_for_recursive_chat)
            return

        if final_content or reasoning_content:
            assistant_message = {"role": "assistant", "content": final_content}
            if reasoning_content:
                assistant_message["reasoning_content"] = reasoning_content
            append_conversation_message_fn(messages, assistant_message)
            save_memory_log_fn(
                "assistant",
                content=final_content,
                reasoning_content=reasoning_content,
            )
            if _continue_after_toolless_action_claim(
                messages=messages,
                tools=tools,
                content=final_content,
                reasoning_content=reasoning_content,
                append_conversation_message_fn=append_conversation_message_fn,
                continue_chat_fn=continue_chat_fn,
                model_for_recursive_chat=model_for_recursive_chat,
                logger=logger,
            ):
                return

    except interrupted_exception_cls:
        raise
    except Exception as error:
        logger.error("处理流式响应失败: %s", error)
        _safe_print(print_fn, f"\n错误: {error}")


def process_non_streaming_response(
    *,
    response: Any,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    interrupted_exception_cls: type[BaseException],
    is_interrupted_fn: Callable[[], bool],
    append_conversation_message_fn: Callable[[List[Dict[str, Any]], Dict[str, Any]], None],
    save_memory_log_fn: Callable[..., None],
    handle_tool_calls_fn: Callable[[Any, List[Dict[str, Any]], List[Dict[str, Any]]], None],
    get_current_tools_fn: Callable[[], List[Dict[str, Any]]],
    continue_chat_fn: Callable[[List[Dict[str, Any]], List[Dict[str, Any]], str], None],
    model_for_recursive_chat: str,
    logger: Any,
    print_fn: Callable[..., Any] = print,
) -> None:
    try:
        _raise_if_interrupted(
            is_interrupted_fn=is_interrupted_fn,
            interrupted_exception_cls=interrupted_exception_cls,
        )

        message = response.choices[0].message
        reasoning_content = getattr(message, "reasoning_content", None) or ""
        if reasoning_content:
            _safe_print(print_fn, f"\033[95m{reasoning_content}\033[0m")

        message_content = getattr(message, "content", None) or ""
        if message_content:
            _safe_print(print_fn, f"\033[96m{message_content}\033[0m")

        original_tool_calls = list(getattr(message, "tool_calls", None) or [])
        response_content = message_content

        assistant_message: Dict[str, Any] = {
            "role": "assistant",
            "content": response_content,
        }
        if original_tool_calls:
            assistant_message["tool_calls"] = [
                _serialize_tool_call(tool_call) for tool_call in original_tool_calls
            ]
        if reasoning_content:
            assistant_message["reasoning_content"] = reasoning_content
        append_conversation_message_fn(messages, assistant_message)

        if reasoning_content or response_content:
            save_memory_log_fn(
                "assistant",
                content=response_content,
                reasoning_content=reasoning_content,
            )

        if original_tool_calls:
            handle_tool_calls_fn(original_tool_calls, messages, tools)
            _raise_if_interrupted(
                is_interrupted_fn=is_interrupted_fn,
                interrupted_exception_cls=interrupted_exception_cls,
            )
            updated_tools = get_current_tools_fn()
            continue_chat_fn(messages, updated_tools, model_for_recursive_chat)
        elif _continue_after_toolless_action_claim(
            messages=messages,
            tools=tools,
            content=response_content,
            reasoning_content=reasoning_content,
            append_conversation_message_fn=append_conversation_message_fn,
            continue_chat_fn=continue_chat_fn,
            model_for_recursive_chat=model_for_recursive_chat,
            logger=logger,
        ):
            return

    except interrupted_exception_cls:
        raise
    except Exception as error:
        logger.error("处理响应失败: %s", error)
        _safe_print(print_fn, f"\n错误: {error}")


def _raise_if_interrupted(
    *,
    is_interrupted_fn: Callable[[], bool],
    interrupted_exception_cls: type[BaseException],
    response: Any = None,
) -> None:
    if not is_interrupted_fn():
        return

    if response is not None:
        _close_response_safely(response)
    raise interrupted_exception_cls("用户中断")


def _close_response_safely(response: Any) -> None:
    try:
        response.close()
    except Exception:
        pass


def _collect_streamed_tool_calls(
    *,
    delta_tool_calls: Any,
    tool_calls_buffer: List[Dict[str, Any]],
    tool_calls_by_index: Dict[Any, Dict[str, Any]],
    tool_calls_by_id: Dict[str, Dict[str, Any]],
) -> None:
    for tool_call in delta_tool_calls:
        tool_index = getattr(tool_call, "index", None)
        tool_call_id = getattr(tool_call, "id", None)
        current_tool_call = None

        if tool_index is not None:
            current_tool_call = tool_calls_by_index.get(tool_index)
        if current_tool_call is None and tool_call_id:
            current_tool_call = tool_calls_by_id.get(tool_call_id)

        if current_tool_call is None:
            current_tool_call = {
                "id": tool_call_id or f"tool_call_{len(tool_calls_buffer)}",
                "type": getattr(tool_call, "type", None) or "function",
                "function": {"name": "", "arguments": ""},
            }
            tool_calls_buffer.append(current_tool_call)

        if tool_index is not None:
            tool_calls_by_index[tool_index] = current_tool_call

        if tool_call_id:
            if current_tool_call["id"] != tool_call_id:
                old_id = current_tool_call["id"]
                current_tool_call["id"] = tool_call_id
                tool_calls_by_id.pop(old_id, None)
            tool_calls_by_id[tool_call_id] = current_tool_call
        else:
            tool_calls_by_id[current_tool_call["id"]] = current_tool_call

        tool_type = getattr(tool_call, "type", None)
        if tool_type:
            current_tool_call["type"] = tool_type

        function_payload = getattr(tool_call, "function", None)
        if function_payload:
            function_name = getattr(function_payload, "name", None)
            function_arguments = getattr(function_payload, "arguments", None)
            if function_name:
                current_tool_call["function"]["name"] = function_name
            if function_arguments:
                current_tool_call["function"]["arguments"] += function_arguments


def _finalize_streamed_tool_calls(
    *,
    tool_calls_buffer: List[Dict[str, Any]],
    validate_and_fix_json_fn: Callable[[str], Optional[str]],
) -> None:
    for tool_call in tool_calls_buffer:
        function_payload = tool_call.get("function", {})
        arguments = function_payload.get("arguments")
        if not arguments:
            continue
        fixed_arguments = validate_and_fix_json_fn(arguments)
        if fixed_arguments:
            function_payload["arguments"] = fixed_arguments


def _emit_streamed_tool_call_events(
    *,
    tool_calls_buffer: List[Dict[str, Any]],
    stream_callback: Optional[Callable[[Dict[str, Any]], None]],
) -> None:
    if not stream_callback:
        return

    for tool_call in tool_calls_buffer:
        stream_callback(
            {
                "type": "tool_call",
                "tool_name": tool_call["function"]["name"],
                "arguments": tool_call["function"]["arguments"],
                "tool_call_id": tool_call["id"],
            }
        )


def _serialize_tool_call(tool_call: Any) -> Dict[str, Any]:
    function_payload = getattr(tool_call, "function", None)
    return {
        "id": getattr(tool_call, "id", None),
        "type": getattr(tool_call, "type", None),
        "function": {
            "name": getattr(function_payload, "name", None),
            "arguments": getattr(function_payload, "arguments", None),
        },
    }


def _continue_after_toolless_action_claim(
    *,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    content: str,
    reasoning_content: str,
    append_conversation_message_fn: Callable[[List[Dict[str, Any]], Dict[str, Any]], None],
    continue_chat_fn: Callable[[List[Dict[str, Any]], List[Dict[str, Any]], str], None],
    model_for_recursive_chat: str,
    logger: Any,
) -> bool:
    if not _should_retry_toolless_action_claim(
        messages=messages,
        tools=tools,
        content=content,
        reasoning_content=reasoning_content,
    ):
        return False

    logger.warning(
        "Detected assistant action claim without tool_calls; requesting a tool-call follow-up."
    )
    append_conversation_message_fn(
        messages,
        {
            "role": "system",
            "content": _build_toolless_action_guard_message(tools),
        },
    )
    continue_chat_fn(messages, tools, model_for_recursive_chat)
    return True


def _should_retry_toolless_action_claim(
    *,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    content: str,
    reasoning_content: str,
) -> bool:
    if not _has_callable_tools(tools):
        return False
    if _current_user_turn_already_has_tool_or_guard(messages):
        return False
    return _looks_like_toolless_action_claim(content) or _looks_like_toolless_action_claim(
        reasoning_content
    )


def _has_callable_tools(tools: List[Dict[str, Any]]) -> bool:
    return any((tool.get("function", {}) or {}).get("name") for tool in tools or [])


def _current_user_turn_already_has_tool_or_guard(messages: List[Dict[str, Any]]) -> bool:
    last_user_index = -1
    for index in range(len(messages or []) - 1, -1, -1):
        if messages[index].get("role") == "user":
            last_user_index = index
            break

    scoped = messages[last_user_index + 1 :] if last_user_index >= 0 else messages or []
    for message in scoped:
        if message.get("tool_calls"):
            return True
        if (
            message.get("role") == "system"
            and str(message.get("content", "")).startswith(TOOLLESS_ACTION_GUARD_PREFIX)
        ):
            return True
    return False


def _looks_like_toolless_action_claim(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False

    lowered = text.lower()
    first_person_action = any(marker in lowered for marker in _FIRST_PERSON_ACTION_MARKERS)
    has_action_verb = any(verb in lowered for verb in _ACTION_VERBS)
    completion_claim = any(claim in lowered for claim in _ACTION_COMPLETION_CLAIMS)
    has_path_or_file = bool(_PATH_OR_FILE_RE.search(text))

    if first_person_action and has_action_verb:
        return True
    if completion_claim and (has_action_verb or has_path_or_file):
        return True
    if has_path_or_file and first_person_action:
        return True
    return False


def _build_toolless_action_guard_message(tools: List[Dict[str, Any]]) -> str:
    tool_names = [
        (tool.get("function", {}) or {}).get("name", "")
        for tool in tools or []
    ]
    tool_names = [name for name in tool_names if name]
    available = ", ".join(tool_names[:8]) or "none"
    return (
        f"{TOOLLESS_ACTION_GUARD_PREFIX} Previous assistant text described reading, editing, "
        "checking, or verifying, but this user turn has no tool_call yet. Do not continue "
        "describing external actions in prose. Immediately emit a real tool_call. If the needed "
        "business module is not loaded, call load_module first with the relevant module_names. "
        f"Currently callable tools: {available}."
    )
