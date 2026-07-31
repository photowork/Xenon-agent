from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional


TOOL_PROGRESS_ARGUMENT_STEP_CHARS = 256
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
REASONING_ONLY_RETRY_MARKER = "[REASONING_ONLY_FINAL_RESPONSE_REQUIRED]"
REASONING_ONLY_RETRY_PROMPT = (
    f"{REASONING_ONLY_RETRY_MARKER}\n"
    "Your previous response produced reasoning_content only, with no user-visible "
    "assistant content and no tool call. The user interface cannot treat that as a "
    "completed answer. Reply now with a concise, user-visible final answer based on "
    "the existing conversation and tool results. Do not call another tool unless it "
    "is absolutely necessary."
)
REASONING_ONLY_FALLBACK_CONTENT = (
    "我刚才只生成了思考内容，没有形成可见回复；工具结果已经保留。"
    "请再发一句“继续”，我会接着完成。"
)


class StreamTransportError(RuntimeError):
    """Raised when the model stream ends because the HTTP transport broke."""


STREAM_TRANSPORT_ERROR_MARKERS = (
    "incomplete chunked read",
    "peer closed connection without sending complete message body",
    "remote protocol error",
    "connection reset",
    "read timeout",
    "network error",
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
    tool_progress_state: Dict[str, Dict[str, Any]] = {}

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
                changed_tool_calls = _collect_streamed_tool_calls(
                    delta_tool_calls=delta_tool_calls,
                    tool_calls_buffer=tool_calls_buffer,
                    tool_calls_by_index=tool_calls_by_index,
                    tool_calls_by_id=tool_calls_by_id,
                )
                _emit_streamed_tool_progress_events(
                    changed_tool_calls=changed_tool_calls,
                    stream_callback=stream_callback,
                    progress_state=tool_progress_state,
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

        if reasoning_content and not final_content:
            if _continue_after_reasoning_only_response(
                messages=messages,
                reasoning_content=reasoning_content,
                append_conversation_message_fn=append_conversation_message_fn,
                save_memory_log_fn=save_memory_log_fn,
                get_current_tools_fn=get_current_tools_fn,
                continue_chat_fn=continue_chat_fn,
                model_for_recursive_chat=model_for_recursive_chat,
                logger=logger,
                stream_callback=stream_callback,
            ):
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

    except interrupted_exception_cls:
        raise
    except Exception as error:
        if _is_stream_transport_error(error):
            _close_response_safely(response)
            logger.warning("流式响应传输中断，将交由上层重试: %s", error)
            if stream_callback:
                stream_callback(
                    {
                        "type": "tool_progress",
                        "content": "流式连接中断，正在切换到非流式方式重试...",
                    }
                )
            raise StreamTransportError(str(error)) from error
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
            "reasoning_content": reasoning_content,
        }

        if original_tool_calls:
            assistant_message["tool_calls"] = [
                _serialize_tool_call(tool_call) for tool_call in original_tool_calls
            ]
            append_conversation_message_fn(messages, assistant_message)
            if reasoning_content or response_content:
                save_memory_log_fn(
                    "assistant",
                    content=response_content,
                    reasoning_content=reasoning_content,
                )
            handle_tool_calls_fn(original_tool_calls, messages, tools)
            _raise_if_interrupted(
                is_interrupted_fn=is_interrupted_fn,
                interrupted_exception_cls=interrupted_exception_cls,
            )
            updated_tools = get_current_tools_fn()
            continue_chat_fn(messages, updated_tools, model_for_recursive_chat)
        elif reasoning_content and not response_content:
            _continue_after_reasoning_only_response(
                messages=messages,
                reasoning_content=reasoning_content,
                append_conversation_message_fn=append_conversation_message_fn,
                save_memory_log_fn=save_memory_log_fn,
                get_current_tools_fn=get_current_tools_fn,
                continue_chat_fn=continue_chat_fn,
                model_for_recursive_chat=model_for_recursive_chat,
                logger=logger,
                stream_callback=None,
            )
        elif reasoning_content or response_content:
            assistant_message["content"] = response_content
            append_conversation_message_fn(messages, assistant_message)
            save_memory_log_fn(
                "assistant",
                content=response_content,
                reasoning_content=reasoning_content,
            )

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


def _is_stream_transport_error(error: Exception) -> bool:
    text = f"{type(error).__module__}.{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in STREAM_TRANSPORT_ERROR_MARKERS)


def _continue_after_reasoning_only_response(
    *,
    messages: List[Dict[str, Any]],
    reasoning_content: str,
    append_conversation_message_fn: Callable[[List[Dict[str, Any]], Dict[str, Any]], None],
    save_memory_log_fn: Callable[..., None],
    get_current_tools_fn: Callable[[], List[Dict[str, Any]]],
    continue_chat_fn: Callable[[List[Dict[str, Any]], List[Dict[str, Any]], str], None],
    model_for_recursive_chat: str,
    logger: Any,
    stream_callback: Optional[Callable[[Dict[str, Any]], None]],
) -> bool:
    if _has_recent_reasoning_only_retry_guard(messages):
        logger.warning("Model returned reasoning_content only after the retry guard; emitting fallback content")
        if stream_callback:
            stream_callback({"type": "content", "content": REASONING_ONLY_FALLBACK_CONTENT})
        assistant_message = {
            "role": "assistant",
            "content": REASONING_ONLY_FALLBACK_CONTENT,
            "reasoning_content": reasoning_content,
        }
        append_conversation_message_fn(messages, assistant_message)
        save_memory_log_fn(
            "assistant",
            content=REASONING_ONLY_FALLBACK_CONTENT,
            reasoning_content=reasoning_content,
        )
        return True

    logger.warning("Model returned reasoning_content only; requesting a user-visible final response")
    append_conversation_message_fn(
        messages,
        {"role": "system", "content": REASONING_ONLY_RETRY_PROMPT},
    )
    continue_chat_fn(messages, get_current_tools_fn(), model_for_recursive_chat)
    return True


def _has_recent_reasoning_only_retry_guard(messages: List[Dict[str, Any]]) -> bool:
    for message in reversed(messages[-6:]):
        if message.get("role") != "system":
            continue
        if REASONING_ONLY_RETRY_MARKER in str(message.get("content", "")):
            return True
    return False


def _collect_streamed_tool_calls(
    *,
    delta_tool_calls: Any,
    tool_calls_buffer: List[Dict[str, Any]],
    tool_calls_by_index: Dict[Any, Dict[str, Any]],
    tool_calls_by_id: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    changed_tool_calls: List[Dict[str, Any]] = []

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
            if function_name or function_arguments:
                changed_tool_calls.append(current_tool_call)

    return changed_tool_calls


def _emit_streamed_tool_progress_events(
    *,
    changed_tool_calls: List[Dict[str, Any]],
    stream_callback: Optional[Callable[[Dict[str, Any]], None]],
    progress_state: Dict[str, Dict[str, Any]],
) -> None:
    if not stream_callback:
        return

    emitted_ids = set()
    for tool_call in changed_tool_calls:
        tool_call_id = tool_call.get("id") or f"tool_call_{len(progress_state)}"
        if tool_call_id in emitted_ids:
            continue
        emitted_ids.add(tool_call_id)

        function_payload = tool_call.get("function", {}) or {}
        tool_name = function_payload.get("name") or ""
        arguments = function_payload.get("arguments") or ""
        argument_chars = len(arguments)

        if not tool_name and argument_chars <= 0:
            continue

        previous = progress_state.get(tool_call_id, {})
        last_chars = int(previous.get("argument_chars", -TOOL_PROGRESS_ARGUMENT_STEP_CHARS))
        last_tool_name = previous.get("tool_name", "")

        should_emit = (
            not previous
            or tool_name != last_tool_name
            or argument_chars - last_chars >= TOOL_PROGRESS_ARGUMENT_STEP_CHARS
        )
        if not should_emit:
            continue

        progress_state[tool_call_id] = {
            "argument_chars": argument_chars,
            "tool_name": tool_name,
        }
        stream_callback(
            {
                "type": "tool_progress",
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "content": _build_tool_argument_progress_message(tool_name, argument_chars),
            }
        )


def _build_tool_argument_progress_message(tool_name: str, argument_chars: int) -> str:
    label = "正在准备文件操作" if _looks_like_mutating_tool(tool_name) else "正在准备工具调用"
    tool_label = tool_name or "tool"
    return f"{label}: {tool_label} ({argument_chars:,} 字符)"


def _looks_like_mutating_tool(tool_name: str) -> bool:
    lowered = (tool_name or "").lower()
    return any(part in lowered for part in MUTATING_TOOL_NAME_PARTS)


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
