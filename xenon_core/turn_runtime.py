from __future__ import annotations

import copy
import inspect
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

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
    get_available_tools_message_fn: Callable[..., str],
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
) -> None:
    save_memory_log_fn("user", content=user_input)
    decision = prepare_orchestration_decision_fn(user_input, internal_context=internal_context)

    context_copy = copy.deepcopy(current_context)
    system_message = get_available_tools_message_fn(decision=decision)

    _replace_leading_system_messages(context_copy)
    context_copy.insert(0, {"role": "system", "content": system_message})
    inject_cognitive_network_summary_fn(
        context_copy,
        current_query=user_input,
        current_phase=decision.phase if decision else "",
        current_intent=decision.intent if decision else "",
        recent_failures=get_recent_failures_fn(),
    )

    tools = get_current_tools_fn()
    ensure_context_size_fn(context_copy, tools)

    token_info = _get_context_token_info(get_context_token_info_fn, context_copy, tools)
    if token_info:
        context_copy[0]["content"] = str(context_copy[0].get("content", "")) + token_info
    save_memory_log_fn("system", content=context_copy[0].get("content", ""))

    context_committed = False
    try:
        chat_fn(context_copy, tools, model_for_chat)
        context_copy.append(_build_round_timestamp())
        set_current_context_fn(_context_for_next_turn(context_copy, compact_turn_after_commit_fn))
        context_committed = True
        cleanup_old_summaries_if_healthy_fn(context_copy, tools)
    except interrupted_exception_cls:
        print_fn("\n\033[93m[对话已中断，工具调用结果已保留]\033[0m")
        context_copy.append(_build_round_timestamp())
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


def _build_round_timestamp() -> Dict[str, Any]:
    """构建本轮完成时间戳消息，注入到对话上下文中。

    智能体通过对比此时间戳与系统提示词中的【当前时间】，
    可以感知每轮对话之间的时间间隔（几分钟前、几小时前）。
    """
    return {
        "role": "system",
        "content": f"[本轮完成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]",
    }


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

