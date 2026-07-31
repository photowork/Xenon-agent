from __future__ import annotations

import copy
import inspect
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from xenon_core.prompt_runtime import DYNAMIC_RUNTIME_PREFIX

CHECKPOINT_SYSTEM_PREFIX = "【任务状态检查点】"


def run_chat_turn(
    *,
    user_input: str,
    internal_context: Optional[Dict[str, Any]],
    current_context: List[Dict[str, Any]],
    context_manager: Any,
    cognitive_network_summary: str,
    prepare_orchestration_decision_fn: Callable[..., Any],
    get_actual_context_status_fn: Callable[[], Dict[str, Any]],
    get_current_tool_names_fn: Callable[[], List[str]],
    get_runtime_system_messages_fn: Callable[..., Tuple[str, str]],
    get_context_token_info_fn: Callable[..., str],
    inject_cognitive_network_summary_fn: Callable[..., None],
    get_recent_failures_fn: Callable[[], List[str]],
    get_current_tools_fn: Callable[[], List[Dict[str, Any]]],
    ensure_context_size_fn: Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], Any],
    chat_fn: Callable[[List[Dict[str, Any]], List[Dict[str, Any]], str], None],
    cleanup_old_summaries_if_healthy_fn: Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], None],
    save_memory_log_fn: Callable[..., None],
    set_current_context_fn: Callable[[List[Dict[str, Any]]], None],
    interrupted_exception_cls: type[BaseException],
    model_for_chat: str,
    logger: Any,
    print_fn: Callable[..., Any] = print,
    reset_loaded_tools_for_next_turn_fn: Optional[Callable[[], Optional[str]]] = None,
    compact_turn_after_commit_fn: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
    stream_callback_fn: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> None:
    save_memory_log_fn("user", content=user_input)
    decision = prepare_orchestration_decision_fn(user_input, internal_context=internal_context)

    context_copy = copy.deepcopy(current_context)
    static_content, dynamic_content = get_runtime_system_messages_fn(decision=decision)

    _replace_leading_system_messages(context_copy)
    _remove_runtime_dynamic_messages(context_copy)
    context_copy.insert(0, {"role": "system", "content": static_content})
    _insert_before_last_user(
        context_copy, {"role": "system", "content": dynamic_content}
    )
    inject_cognitive_network_summary_fn(
        context_copy,
        current_query=user_input,
        current_phase=decision.phase if decision else "",
        current_intent=decision.intent if decision else "",
        recent_failures=get_recent_failures_fn(),
    )

    # 提问时间戳必须紧贴 user 消息之前插入（在动态块/认知网络块之后）。
    # 若由 chat_entry 提前追加，动态块会插到它后面，把每轮必变的时间戳
    # 留在稳定前缀中间，影响后续轮次的缓存命中。
    _insert_before_last_user(
        context_copy,
        {
            "role": "system",
            "content": f"提问时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        },
    )

    tools = get_current_tools_fn()
    ensure_context_size_fn(context_copy, tools)

    # 当前时间信息和上下文 Token 状态不放入 API 请求（按用户要求删除）。
    # token_info 仅用于本地日志记录，便于排查上下文占用。
    token_info = _get_context_token_info(get_context_token_info_fn, context_copy, tools)
    dynamic_for_log = _get_runtime_dynamic_content(context_copy)
    if token_info:
        dynamic_for_log = dynamic_for_log + "\n\n" + token_info
    save_memory_log_fn("system", content=dynamic_for_log)

    context_committed = False
    try:
        chat_fn(context_copy, tools, model_for_chat)
        _append_answer_timestamp(context_copy, print_fn, stream_callback_fn)
        set_current_context_fn(_context_for_next_turn(context_copy, compact_turn_after_commit_fn))
        context_committed = True
        cleanup_old_summaries_if_healthy_fn(context_copy, tools)
    except interrupted_exception_cls:
        print_fn("\n\033[93m[对话已中断，工具调用结果已保留]\033[0m")
        set_current_context_fn(_context_for_next_turn(context_copy, compact_turn_after_commit_fn))
        context_committed = True
    except Exception as error:
        logger.error("对话过程发生错误: %s", error)
        print_fn(f"\n\033[91m错误: {error}\033[0m")
    finally:
        if reset_loaded_tools_for_next_turn_fn is not None:
            unload_notice = reset_loaded_tools_for_next_turn_fn()
            if unload_notice and context_committed and compact_turn_after_commit_fn is None:
                context_copy.append({"role": "system", "content": unload_notice})
                set_current_context_fn(context_copy)


def _get_context_token_info(
    get_context_token_info_fn: Callable[..., str],
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
) -> str:
    if _accepts_context_token_args(get_context_token_info_fn):
        return get_context_token_info_fn(
            messages=messages,
            tools=tools,
            system_message="",
        )
    return get_context_token_info_fn()


def _accepts_context_token_args(get_context_token_info_fn: Callable[..., str]) -> bool:
    try:
        signature = inspect.signature(get_context_token_info_fn)
    except (TypeError, ValueError):
        return True

    parameters = signature.parameters
    if any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        return True
    return {"messages", "tools", "system_message"}.issubset(parameters)


def _replace_leading_system_messages(messages: List[Dict[str, Any]]) -> None:
    preserved_checkpoints: List[Dict[str, Any]] = []
    while messages and messages[0].get("role") == "system":
        message = messages.pop(0)
        if str(message.get("content", "")).startswith(CHECKPOINT_SYSTEM_PREFIX):
            preserved_checkpoints.append(message)
    if preserved_checkpoints:
        messages[:0] = preserved_checkpoints


def _context_for_next_turn(
    live_messages: List[Dict[str, Any]],
    compact_turn_after_commit_fn: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]],
) -> List[Dict[str, Any]]:
    if compact_turn_after_commit_fn is None:
        return live_messages
    return compact_turn_after_commit_fn(live_messages)


def _remove_runtime_dynamic_messages(messages: List[Dict[str, Any]]) -> None:
    """移除历史中残留的旧动态运行时 system 消息（按统一前缀识别）。

    常规模式下 current_context 会保留上一轮的动态块，若不清理会逐轮累积、污染历史。
    """
    messages[:] = [
        message
        for message in messages
        if not (
            message.get("role") == "system"
            and str(message.get("content", "")).startswith(DYNAMIC_RUNTIME_PREFIX)
        )
    ]


def _insert_before_last_user(
    messages: List[Dict[str, Any]], message: Dict[str, Any]
) -> None:
    """在最后一条 user 消息之前插入一条消息。

    运行时约定：最新 user 输入由 chat_entry 在调用 run_chat_turn 前追加为末元素。
    把动态信息插在它之前，可使 [静态提示词 + 对话历史] 构成稳定前缀，最大化缓存命中。
    若不存在 user 消息，则追加到末尾。
    """
    insert_pos = len(messages)
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") == "user":
            insert_pos = idx
            break
    messages.insert(insert_pos, message)


def _find_runtime_dynamic_index(messages: List[Dict[str, Any]]) -> int:
    """定位动态运行时 system 消息的下标，找不到返回 -1。"""
    for idx, message in enumerate(messages):
        if (
            message.get("role") == "system"
            and str(message.get("content", "")).startswith(DYNAMIC_RUNTIME_PREFIX)
        ):
            return idx
    return -1


def _append_to_runtime_dynamic(
    messages: List[Dict[str, Any]], text: str
) -> None:
    """把 token 用量等信息追加到动态运行时 system 消息末尾。

    动态块每轮重建，承载 token 等运行时状态；静态块（messages[0]）保持不变以利于缓存。
    """
    idx = _find_runtime_dynamic_index(messages)
    if idx < 0:
        return
    messages[idx]["content"] = str(messages[idx].get("content", "")) + text


def _get_runtime_dynamic_content(messages: List[Dict[str, Any]]) -> str:
    """读取动态运行时 system 消息内容用于日志记录；找不到时回退到首条 system。"""
    idx = _find_runtime_dynamic_index(messages)
    if idx >= 0:
        return str(messages[idx].get("content", ""))
    if messages and messages[0].get("role") == "system":
        return str(messages[0].get("content", ""))
    return ""


def _append_answer_timestamp(
    messages: List[Dict[str, Any]],
    print_fn: Callable[..., Any] = print,
    stream_callback_fn: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> None:
    """在每轮回答完成时，追加一条 system 消息记录完成时间。

    不嵌入 assistant content，保持内容干净；WebUI 默认不显示 system 消息。
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    timestamp_message = {"role": "system", "content": f"回答完成时间：{timestamp}"}
    messages.append(timestamp_message)
    # 终端仍输出，方便调试
    print_fn(f"[回答完成时间: {timestamp}]")
