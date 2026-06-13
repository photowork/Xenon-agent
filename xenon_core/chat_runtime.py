from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Set

from xenon_core.model_request import build_chat_completion_kwargs
from xenon_core.response_runtime import StreamTransportError
from xenon_core.turn_compactor import sanitize_messages_for_api


def run_chat_cycle(
    *,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    model: str,
    retry_count: int,
    max_trim_retries: int,
    context_manager: Any,
    enable_streaming: bool,
    ensure_message_integrity_fn: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
    auto_trim_context_fn: Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], None],
    emergency_context_clear_fn: Callable[[List[Dict[str, Any]], bool], None],
    append_conversation_message_fn: Callable[[List[Dict[str, Any]], Dict[str, Any]], None],
    save_api_request_fn: Callable[[str, List[Dict[str, Any]], Optional[List[Dict[str, Any]]]], None],
    save_api_usage_fn: Optional[Callable[[int], None]] = None,
    retry_request_fn: Callable[..., Any],
    create_completion_fn: Callable[..., Any],
    process_streaming_response_fn: Callable[[Any, List[Dict[str, Any]], List[Dict[str, Any]]], None],
    process_non_streaming_response_fn: Callable[[Any, List[Dict[str, Any]], List[Dict[str, Any]]], None],
    recursive_chat_fn: Callable[[List[Dict[str, Any]], List[Dict[str, Any]], str, int], None],
    set_in_api_call_fn: Callable[[bool], None],
    is_interrupted_fn: Callable[[], bool],
    interrupted_exception_cls: type[BaseException],
    logger: Any,
    thinking_enabled: Optional[bool] = None,
    reasoning_effort: Optional[str] = None,
    print_fn: Callable[..., Any] = print,
) -> None:
    if is_interrupted_fn():
        raise interrupted_exception_cls("用户中断")

    _apply_message_integrity_fix(
        messages=messages,
        ensure_message_integrity_fn=ensure_message_integrity_fn,
    )

    should_return = _handle_context_limit(
        messages=messages,
        tools=tools,
        model=model,
        retry_count=retry_count,
        max_trim_retries=max_trim_retries,
        context_manager=context_manager,
        auto_trim_context_fn=auto_trim_context_fn,
        emergency_context_clear_fn=emergency_context_clear_fn,
        append_conversation_message_fn=append_conversation_message_fn,
        recursive_chat_fn=recursive_chat_fn,
        logger=logger,
        print_fn=print_fn,
    )
    if should_return:
        return

    # DeepSeek thinking mode requires assistant reasoning_content to be carried
    # forward.  Repair old/trimmed history before sending it to the API.
    if thinking_enabled:
        _repair_thinking_history(messages=messages, logger=logger)

    api_messages = sanitize_messages_for_api(
        messages,
        preserve_current_toolchain=True,
        include_reasoning=bool(thinking_enabled),
    )

    save_api_request_fn(model, api_messages, tools)

    try:
        set_in_api_call_fn(True)
        request_kwargs = build_chat_completion_kwargs(
            model=model,
            messages=api_messages,
            tools=tools,
            stream=enable_streaming,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
        )
        response = retry_request_fn(
            create_completion_fn,
            **request_kwargs,
        )

        if enable_streaming:
            try:
                process_streaming_response_fn(response, messages, tools)
                _capture_response_usage(response, save_api_usage_fn)
            except StreamTransportError as error:
                logger.warning("流式响应中断，正在使用非流式请求重试一次: %s", error)
                print_fn("\n[提示] 流式连接中断，正在用非流式方式重试一次...")
                fallback_kwargs = build_chat_completion_kwargs(
                    model=model,
                    messages=api_messages,
                    tools=tools,
                    stream=False,
                    thinking_enabled=thinking_enabled,
                    reasoning_effort=reasoning_effort,
                )
                fallback_response = retry_request_fn(
                    create_completion_fn,
                    **fallback_kwargs,
                )
                process_non_streaming_response_fn(fallback_response, messages, tools)
                _capture_response_usage(fallback_response, save_api_usage_fn)
        else:
            process_non_streaming_response_fn(response, messages, tools)
            _capture_response_usage(response, save_api_usage_fn)

    except interrupted_exception_cls:
        raise
    except Exception as error:
        logger.error("对话失败: %s", error)
        print_fn(f"\n错误: {error}")
    finally:
        set_in_api_call_fn(False)


def _apply_message_integrity_fix(
    *,
    messages: List[Dict[str, Any]],
    ensure_message_integrity_fn: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]],
) -> None:
    fixed_messages = ensure_message_integrity_fn(messages)
    if fixed_messages is messages:
        return

    messages.clear()
    messages.extend(fixed_messages)


def _handle_context_limit(
    *,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    model: str,
    retry_count: int,
    max_trim_retries: int,
    context_manager: Any,
    auto_trim_context_fn: Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], None],
    emergency_context_clear_fn: Callable[[List[Dict[str, Any]], bool], None],
    append_conversation_message_fn: Callable[[List[Dict[str, Any]], Dict[str, Any]], None],
    recursive_chat_fn: Callable[[List[Dict[str, Any]], List[Dict[str, Any]], str, int], None],
    logger: Any,
    print_fn: Callable[..., Any],
) -> bool:
    token_counter = getattr(context_manager, "token_counter", None)
    if not context_manager or not token_counter:
        return False

    total_tokens = token_counter.estimate_total_tokens(messages, tools)
    max_tokens = context_manager.get_effective_max_tokens()
    if total_tokens < max_tokens:
        return False

    if retry_count >= max_trim_retries:
        logger.error("多次裁剪后仍超限，无法继续")
        print_fn("\033[91m错误：上下文过大，即使多次裁剪也无法处理。请使用 /clear 命令清空上下文。\033[0m")
        append_conversation_message_fn(
            messages,
            {
                "role": "assistant",
                "content": "抱歉，当前上下文过长，我无法继续处理。请使用 /clear 命令清空上下文后重试。",
            },
        )
        return True

    logger.warning(
        "Token 超限（%s / %s），正在自动裁剪... (尝试 %s/%s)",
        total_tokens,
        max_tokens,
        retry_count + 1,
        max_trim_retries,
    )
    print_fn(
        f"\n\033[93m[警告] 上下文过长，正在尝试压缩... (尝试 {retry_count + 1}/{max_trim_retries})\033[0m"
    )

    auto_trim_context_fn(messages, tools)

    new_total = token_counter.estimate_total_tokens(messages, tools)
    if new_total >= max_tokens:
        logger.warning("裁剪后仍超限 (%s / %s)，尝试更激进的清理...", new_total, max_tokens)
        emergency_context_clear_fn(messages, False)

    recursive_chat_fn(messages, tools, model, retry_count + 1)
    return True


def _repair_thinking_history(
    *,
    messages: List[Dict[str, Any]],
    logger: Any,
) -> None:
    """Normalize conversation history for DeepSeek thinking mode.

    DeepSeek thinking mode requires that every assistant message with
    ``tool_calls`` also carries ``reasoning_content``.  Context trimming may
    drop ``reasoning_content`` while keeping the assistant+tool pair, which
    triggers a 400 error from the API.  The safest repair for broken toolchains
    is to remove the entire block (assistant + its tool responses).

    Older plain assistant messages may also be missing the field because prior
    versions cleaned it before each request.  They cannot be reconstructed, but
    adding an empty field keeps the request shape compatible without deleting
    useful conversation context.
    """
    # Pass 1: identify tool_call_ids from broken assistant messages.
    broken_tool_call_ids: Set[str] = set()
    broken_assistant_indices: Set[int] = set()

    for idx, message in enumerate(messages):
        if (
            message.get("role") == "assistant"
            and message.get("tool_calls")
            and not message.get("reasoning_content")
        ):
            broken_assistant_indices.add(idx)
            for tc in message["tool_calls"]:
                tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tc_id:
                    broken_tool_call_ids.add(tc_id)

    if not broken_assistant_indices:
        _ensure_plain_assistant_reasoning_content(messages=messages, logger=logger)
        return

    # Pass 2: also mark tool responses that belong to broken tool_call_ids.
    for idx, message in enumerate(messages):
        if (
            message.get("role") == "tool"
            and message.get("tool_call_id") in broken_tool_call_ids
        ):
            broken_assistant_indices.add(idx)

    # Pass 3: rebuild the message list, skipping broken entries.
    repaired = [m for idx, m in enumerate(messages) if idx not in broken_assistant_indices]

    removed = len(messages) - len(repaired)
    if removed:
        logger.warning(
            "思考模式修复: 移除 %d 条消息（缺少 reasoning_content 的工具链）",
            removed,
        )
        messages.clear()
        messages.extend(repaired)

    _ensure_plain_assistant_reasoning_content(messages=messages, logger=logger)


def _ensure_plain_assistant_reasoning_content(
    *,
    messages: List[Dict[str, Any]],
    logger: Any,
) -> None:
    patched = 0
    for message in messages:
        if message.get("role") != "assistant":
            continue
        if message.get("tool_calls"):
            continue
        if "reasoning_content" in message:
            continue
        message["reasoning_content"] = ""
        patched += 1

    if patched:
        logger.warning(
            "思考模式修复: 为 %d 条历史 assistant 消息补齐空 reasoning_content",
            patched,
        )


def _capture_response_usage(
    response: Any,
    save_api_usage_fn: Optional[Callable[[int], None]],
) -> None:
    """从 API 响应中提取真实 token 用量并通过回调保存。"""
    if save_api_usage_fn is None:
        return
    try:
        usage = getattr(response, "usage", None)
        if usage is not None:
            total = getattr(usage, "total_tokens", None)
            if total is not None and total > 0:
                save_api_usage_fn(int(total))
    except Exception:
        pass
