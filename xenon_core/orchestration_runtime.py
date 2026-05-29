from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from xenon_core.phase_policy import router_phase_for


def prepare_orchestration_decision(
    *,
    user_input: str,
    internal_context: Optional[Dict[str, Any]],
    context_manager: Any,
    last_tool_result: Any,
    get_actual_context_status_fn: Callable[[], Dict[str, Any]],
    get_current_tool_schemas_fn: Callable[[], Any],
    get_cognitive_network_summary_fn: Callable[..., str],
    get_recent_failures_fn: Callable[[], Any],
    agent_orchestrator: Any,
    task_chain_manager: Any,
    execution_journal: Any,
    set_orchestration_decision_fn: Callable[[Any], None],
    logger: Any,
) -> Optional[Any]:
    try:
        current_task_wrapper = task_chain_manager.get_current_task()
        current_task = current_task_wrapper["task"] if current_task_wrapper else None
        context_status = get_actual_context_status_fn() if context_manager else {}
        tool_schemas = get_current_tool_schemas_fn()
        memory_summary = _build_memory_summary(
            user_input=user_input,
            internal_context=internal_context,
            context_manager=context_manager,
            current_task=current_task,
            get_cognitive_network_summary_fn=get_cognitive_network_summary_fn,
            get_recent_failures_fn=get_recent_failures_fn,
        )

        if internal_context and internal_context.get("mode") == "autonomous_continuation":
            decision = agent_orchestrator.prepare_for_continuation(
                continuation_prompt=user_input,
                tool_schemas=tool_schemas,
                current_task=current_task,
                last_result=last_tool_result,
                context_status=context_status,
                memory_summary=memory_summary,
                replan_suggestion=internal_context.get("replan_suggestion"),
                route_hint=internal_context.get("route_hint"),
            )
        else:
            decision = agent_orchestrator.prepare_for_user_input(
                user_input=user_input,
                tool_schemas=tool_schemas,
                current_task=current_task,
                last_result=last_tool_result,
                context_status=context_status,
                memory_summary=memory_summary,
            )

        set_orchestration_decision_fn(decision)

        task_chain_manager.ensure_task(
            description=decision.goal,
            objective=decision.goal,
            metadata={
                "source": "agent_orchestrator_autonomous" if internal_context else "agent_orchestrator",
                "intent": decision.intent,
            },
            steps=agent_orchestrator.default_steps_for_goal(decision.goal),
        )
        task_chain_manager.sync_execution_state(
            phase=decision.phase,
            recovery_mode=getattr(decision, "recovery_mode", "none"),
            blockage_reason=None,
            next_actions=decision.next_actions,
            mode=decision.mode,
            reasoning_summary=decision.reasoning_summary,
            last_tool=decision.selected_tool,
            last_tool_result=None,
            last_replan=internal_context.get("replan_suggestion") if internal_context else None,
        )
        current_task_wrapper = task_chain_manager.get_current_task()
        execution_journal.log_planning(
            decision=decision.to_dict(),
            task_state=current_task_wrapper["task"] if current_task_wrapper else None,
        )
        return decision

    except Exception as error:
        logger.error("Failed to prepare orchestration decision: %s", error)
        set_orchestration_decision_fn(None)
        return None


def _build_memory_summary(
    *,
    user_input: str,
    internal_context: Optional[Dict[str, Any]],
    context_manager: Any,
    current_task: Optional[Dict[str, Any]],
    get_cognitive_network_summary_fn: Callable[..., str],
    get_recent_failures_fn: Callable[[], Any],
) -> str:
    memory_parts = []
    if context_manager and getattr(context_manager, "current_context_summary", None):
        memory_parts.append(context_manager.current_context_summary)

    execution_state = (current_task or {}).get("execution_state", {}) or {}
    cognitive_summary = get_cognitive_network_summary_fn(
        current_query=user_input,
        current_phase=router_phase_for(execution_state.get("phase"), execution_state.get("recovery_mode")),
        current_intent=((current_task or {}).get("metadata", {}) or {}).get("intent"),
        recent_failures=get_recent_failures_fn(),
    )
    if cognitive_summary:
        memory_parts.append(cognitive_summary)

    if internal_context and internal_context.get("memory_summary"):
        memory_parts.append(str(internal_context.get("memory_summary")))

    return "\n\n".join(part for part in memory_parts if part)
