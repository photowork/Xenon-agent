from __future__ import annotations

import copy
import hashlib
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from xenon_core.phase_policy import normalize_phase_state, router_phase_for


def enqueue_pending_user_input(
    pending_user_inputs: List[str],
    user_input: str,
    *,
    limit: int = 8,
) -> Dict[str, Any]:
    normalized = str(user_input or "").strip()
    if not normalized:
        return {
            "queued": False,
            "reason": "empty_input",
            "pending_count": len(pending_user_inputs),
        }

    queue_limit = max(1, int(limit or 1))
    pending_user_inputs.append(normalized)
    dropped_count = max(0, len(pending_user_inputs) - queue_limit)
    if dropped_count:
        del pending_user_inputs[:dropped_count]

    return {
        "queued": True,
        "pending_count": len(pending_user_inputs),
        "dropped_count": dropped_count,
        "latest_input": normalized,
    }


def get_phase_memory_snapshot(
    *,
    goal: str,
    phase: str,
    intent: str,
    recent_failures: Optional[List[str]],
    memory_manager: Any,
    get_cognitive_network_summary_fn: Callable[..., str],
) -> Dict[str, Any]:
    phase_summary_result = memory_manager.build_phase_summary(
        current_query=goal,
        current_phase=phase,
        current_intent=intent,
        limit=8,
        recent_failures=recent_failures or [],
    )
    activation_result = memory_manager.get_activation_set(
        current_query=goal,
        current_phase=phase,
        current_intent=intent,
        limit=5,
        recent_failures=recent_failures or [],
    )
    summary = (
        phase_summary_result.get("summary")
        if phase_summary_result.get("success")
        else get_cognitive_network_summary_fn(
            current_query=goal,
            current_phase=phase,
            current_intent=intent,
            recent_failures=recent_failures,
        )
    )
    activation_set = activation_result.get("activation_set", []) if activation_result.get("success") else []
    return {
        "summary": summary or "",
        "activation_set": activation_set,
    }


def should_resume_task(
    *,
    current_task: Optional[Dict[str, Any]],
    replan_suggestion: Optional[Dict[str, Any]] = None,
    max_tool_failures: int,
) -> bool:
    if not current_task:
        return False
    if current_task.get("status") != "in_progress":
        return False

    execution_state = current_task.get("execution_state", {}) or {}
    autonomous_state = current_task.get("autonomous_state", {}) or {}
    current_phase = normalize_phase_state(
        execution_state.get("phase"),
        execution_state.get("recovery_mode"),
    )["phase"]
    next_actions = execution_state.get("next_actions") or []
    blockage_reason = execution_state.get("blockage_reason")
    failure_streak = int(autonomous_state.get("tool_failure_streak") or 0)

    if replan_suggestion and replan_suggestion.get("should_replan"):
        return True
    if current_phase == "done":
        return False
    if failure_streak >= max_tool_failures and not next_actions:
        return False
    if (
        blockage_reason
        and blockage_reason
        not in {"tool_failed", "tool_timeout", "syntax_error", "context_overflow", "remote_unreachable"}
        and not next_actions
    ):
        return False
    if next_actions:
        return True
    return bool(current_task.get("objective") or current_task.get("description"))


def select_active_goal(
    *,
    current_task: Optional[Dict[str, Any]],
    replan_suggestion: Optional[Dict[str, Any]],
    recent_failures: Optional[List[str]] = None,
    pending_user_inputs: Optional[List[str]] = None,
    max_tool_failures: int,
) -> Optional[Dict[str, Any]]:
    if pending_user_inputs:
        pending_input = pending_user_inputs[0]
        return {
            "source": "pending_user_input",
            "goal": pending_input,
            "phase": "plan",
            "recovery_mode": "none",
            "next_actions": ["address_pending_user_input"],
            "pending_index": 0,
        }

    if not current_task:
        return None

    execution_state = current_task.get("execution_state", {}) or {}
    goal = current_task.get("objective") or current_task.get("description") or ""
    current_phase_state = normalize_phase_state(
        execution_state.get("phase", "plan"),
        execution_state.get("recovery_mode", "none"),
    )
    current_phase = current_phase_state["phase"]
    current_recovery_mode = current_phase_state["recovery_mode"]
    next_actions = list(execution_state.get("next_actions") or [])
    blockage_reason = execution_state.get("blockage_reason")

    if replan_suggestion and replan_suggestion.get("should_replan"):
        replan_phase = normalize_phase_state(
            replan_suggestion.get("suggested_phase") or "recover",
            replan_suggestion.get("recovery_mode", "reflect"),
            recovery=True,
        )
        return {
            "source": "replan",
            "goal": goal,
            "phase": replan_phase["phase"],
            "recovery_mode": replan_phase["recovery_mode"],
            "next_actions": list(replan_suggestion.get("next_actions") or next_actions),
            "reasoning_summary": replan_suggestion.get("reasoning_summary", ""),
        }

    if blockage_reason and next_actions:
        return {
            "source": "recovery",
            "goal": goal,
            "phase": "recover",
            "recovery_mode": current_recovery_mode if current_recovery_mode != "none" else "debug",
            "next_actions": next_actions,
            "reasoning_summary": f"recover_from={blockage_reason}",
        }

    if should_resume_task(
        current_task=current_task,
        replan_suggestion=replan_suggestion,
        max_tool_failures=max_tool_failures,
    ):
        return {
            "source": "current_task",
            "goal": goal,
            "phase": current_phase,
            "recovery_mode": current_recovery_mode,
            "next_actions": next_actions,
            "reasoning_summary": f"resume_task failures={len(recent_failures or [])}",
        }
    return None


def build_internal_resume_prompt(
    *,
    goal_payload: Dict[str, Any],
    memory_summary: str,
    activation_set: List[Dict[str, Any]],
    self_model: Dict[str, Any],
    recent_failures: Optional[List[str]] = None,
    replan_suggestion: Optional[Dict[str, Any]] = None,
) -> str:
    lines = [
        "[INTERNAL_CONTINUATION]",
        "This is an internal Xenon continuation prompt, not a new user request.",
        f"active_goal: {goal_payload.get('goal', '')}",
        f"goal_source: {goal_payload.get('source', 'current_task')}",
        f"current_phase: {goal_payload.get('phase', 'plan')}",
    ]
    if goal_payload.get("recovery_mode") and goal_payload.get("recovery_mode") != "none":
        lines.append(f"recovery_mode: {goal_payload.get('recovery_mode')}")

    next_actions = goal_payload.get("next_actions") or []
    if next_actions:
        lines.append("next_actions: " + ", ".join(str(action) for action in next_actions))

    if recent_failures:
        lines.append("recent_failures: " + " | ".join(str(item)[:120] for item in recent_failures[:3]))

    unstable_phases = self_model.get("unstable_phases", [])
    if unstable_phases:
        lines.append("self_model_unstable_phases: " + ", ".join(unstable_phases[:3]))
    if self_model.get("recent_replan_count", 0):
        lines.append(f"recent_replan_count: {self_model.get('recent_replan_count')}")

    if replan_suggestion and replan_suggestion.get("should_replan"):
        lines.append(f"replan_hint: {replan_suggestion.get('reasoning_summary', '')}")

    if memory_summary:
        lines.append("\n[PHASE_MEMORY]")
        lines.append(memory_summary)

    if activation_set:
        lines.append("\n[ACTIVATION_SET]")
        for item in activation_set[:5]:
            lines.append(f"- {item.get('cognitive_type', 'memory')}: {item.get('summary', '')}")

    lines.append(
        "\nAdvance the current goal with the smallest executable next step. "
        "Use plan/act/verify for normal progress. Use recover only for a concrete "
        "failure, missing context, loop, or contradiction, and leave recover with a specific next action."
    )
    return "\n".join(lines).strip()


def build_autonomous_decision(
    *,
    current_task: Dict[str, Any],
    goal_payload: Dict[str, Any],
    memory_snapshot: Dict[str, Any],
    self_model: Dict[str, Any],
    replan_suggestion: Optional[Dict[str, Any]],
    recent_failures: Optional[List[str]] = None,
) -> Dict[str, Any]:
    metadata = current_task.get("metadata", {}) or {}
    execution_state = current_task.get("execution_state", {}) or {}
    phase_state = normalize_phase_state(
        goal_payload.get("phase") or execution_state.get("phase") or "plan",
        goal_payload.get("recovery_mode") or execution_state.get("recovery_mode"),
    )
    phase = phase_state["phase"]
    recovery_mode = phase_state["recovery_mode"]
    intent = metadata.get("intent", "general_execution")
    prompt = build_internal_resume_prompt(
        goal_payload=goal_payload,
        memory_summary=memory_snapshot.get("summary", ""),
        activation_set=memory_snapshot.get("activation_set", []),
        self_model=self_model,
        recent_failures=recent_failures,
        replan_suggestion=replan_suggestion,
    )
    route_hint = {
        "phase": router_phase_for(
            (replan_suggestion or {}).get("suggested_phase") or phase,
            (replan_suggestion or {}).get("recovery_mode") or recovery_mode,
        ),
        "intent": intent,
    }
    return {
        "mode": "autonomous_continuation",
        "prompt": prompt,
        "goal_source": goal_payload.get("source", "current_task"),
        "memory_summary": memory_snapshot.get("summary", ""),
        "activation_set": memory_snapshot.get("activation_set", []),
        "replan_suggestion": replan_suggestion,
        "route_hint": route_hint,
    }


def update_autonomous_progress(
    *,
    previous_task: Dict[str, Any],
    goal_payload: Dict[str, Any],
    internal_prompt: str,
    task_chain_manager: Any,
    recent_tool_results: List[Dict[str, Any]],
    max_phase_stagnation: int,
    max_repeated_actions: int,
    max_tool_failures: int,
    forced_status: Optional[str] = None,
    forced_stop_reason: Optional[str] = None,
    now_fn: Callable[[], datetime] = datetime.now,
) -> Dict[str, Any]:
    current_task_wrapper = task_chain_manager.get_current_task()
    current_task = current_task_wrapper["task"] if current_task_wrapper else previous_task
    previous_execution = previous_task.get("execution_state", {}) or {}
    current_execution = current_task.get("execution_state", {}) or {}
    previous_autonomous = previous_task.get("autonomous_state", {}) or {}
    current_phase_state = normalize_phase_state(
        current_execution.get("phase", "plan"),
        current_execution.get("recovery_mode", "none"),
    )
    previous_phase_state = normalize_phase_state(
        previous_execution.get("phase", "plan"),
        previous_execution.get("recovery_mode", "none"),
    )
    current_phase = current_phase_state["phase"]
    current_recovery_mode = current_phase_state["recovery_mode"]
    current_actions = current_execution.get("next_actions") or []
    previous_phase = previous_phase_state["phase"]
    previous_actions = previous_execution.get("next_actions") or []

    phase_stagnation_count = (
        int(previous_autonomous.get("phase_stagnation_count") or 0) + 1
        if current_phase == previous_phase and current_actions == previous_actions
        else 0
    )
    repeated_action_count = (
        int(previous_autonomous.get("repeated_action_count") or 0) + 1
        if current_actions and current_actions == previous_actions
        else 0
    )
    latest_tool_failure = bool(recent_tool_results and recent_tool_results[-1].get("success") is False)
    tool_failure_streak = (
        int(previous_autonomous.get("tool_failure_streak") or 0) + 1
        if latest_tool_failure
        else 0
    )

    status = "continued"
    stop_reason = None
    if phase_stagnation_count >= max_phase_stagnation:
        status = "stopped"
        stop_reason = "phase_stagnation"
    elif repeated_action_count >= max_repeated_actions:
        status = "stopped"
        stop_reason = "repeated_next_actions"
    elif tool_failure_streak >= max_tool_failures:
        status = "blocked"
        stop_reason = "tool_failure_streak"

    prompt_signature = hashlib.md5(internal_prompt.encode("utf-8")).hexdigest()[:12]
    if forced_status:
        status = forced_status
        stop_reason = forced_stop_reason
    elif forced_stop_reason:
        stop_reason = forced_stop_reason

    task_chain_manager.sync_autonomous_state(
        status=status,
        phase_stagnation_count=phase_stagnation_count,
        repeated_action_count=repeated_action_count,
        tool_failure_streak=tool_failure_streak,
        last_phase=current_phase,
        last_next_actions=current_actions,
        last_goal_source=goal_payload.get("source", "current_task"),
        last_prompt_signature=prompt_signature,
        last_tick_at=now_fn().isoformat(),
        last_result={
            "phase": current_phase,
            "recovery_mode": current_recovery_mode,
            "next_actions": current_actions,
            "latest_tool_success": not latest_tool_failure,
        },
        stop_reason=stop_reason,
    )
    return {
        "status": status,
        "stop_reason": stop_reason,
        "phase": current_phase,
        "recovery_mode": current_recovery_mode,
        "next_actions": current_actions,
        "goal_source": goal_payload.get("source", "current_task"),
        "latest_tool_success": not latest_tool_failure,
        "prompt_signature": prompt_signature,
    }


def _safe_log_autonomous_tick(
    log_autonomous_tick_fn: Optional[Callable[..., Any]],
    *,
    goal_payload: Dict[str, Any],
    result: Dict[str, Any],
    internal_prompt: str = "",
    memory_snapshot: Optional[Dict[str, Any]] = None,
    replan_suggestion: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    if not callable(log_autonomous_tick_fn):
        return None

    replan_summary = ""
    if replan_suggestion:
        replan_summary = replan_suggestion.get("reasoning_summary") or str(replan_suggestion)

    try:
        return log_autonomous_tick_fn(
            goal=goal_payload.get("goal", ""),
            phase=result.get("phase") or goal_payload.get("phase", "analyze"),
            status=result.get("status", "unknown"),
            goal_source=goal_payload.get("source", "current_task"),
            next_actions=result.get("next_actions") or goal_payload.get("next_actions") or [],
            stop_reason=result.get("stop_reason") or result.get("reason"),
            prompt_signature=result.get("prompt_signature")
            or (hashlib.md5(internal_prompt.encode("utf-8")).hexdigest()[:12] if internal_prompt else None),
            result=result,
            replan_summary=replan_summary,
            memory_summary=(memory_snapshot or {}).get("summary", ""),
        )
    except Exception as error:
        result["journal_error"] = str(error)
        return None


def _mark_autonomous_error(
    *,
    task_chain_manager: Any,
    goal_payload: Dict[str, Any],
    internal_prompt: str,
    error: Exception,
) -> Dict[str, Any]:
    prompt_signature = hashlib.md5(internal_prompt.encode("utf-8")).hexdigest()[:12] if internal_prompt else None
    phase_state = normalize_phase_state(
        goal_payload.get("phase", "recover"),
        goal_payload.get("recovery_mode", "debug"),
        recovery=True,
    )
    result = {
        "status": "error",
        "reason": str(error),
        "stop_reason": "exception",
        "phase": phase_state["phase"],
        "recovery_mode": phase_state["recovery_mode"],
        "next_actions": goal_payload.get("next_actions") or [],
        "goal_source": goal_payload.get("source", "current_task"),
        "latest_tool_success": False,
        "prompt_signature": prompt_signature,
    }
    try:
        task_chain_manager.sync_autonomous_state(
            status="error",
            last_goal_source=goal_payload.get("source", "current_task"),
            last_prompt_signature=prompt_signature,
            last_tick_at=datetime.now().isoformat(),
            last_result={
                "error": str(error),
                "phase": phase_state["phase"],
                "recovery_mode": phase_state["recovery_mode"],
            },
            stop_reason="exception",
        )
    except Exception as sync_error:
        result["sync_error"] = str(sync_error)
    return result


def run_autonomous_tick(
    *,
    task_chain_manager: Any,
    memory_manager: Any,
    current_context: List[Dict[str, Any]],
    pending_user_inputs: List[str],
    get_cognitive_network_summary_fn: Callable[..., str],
    get_recent_failures_fn: Callable[[], List[str]],
    get_recent_tool_results_fn: Callable[..., List[Dict[str, Any]]],
    cleanup_reasoning_content_fn: Callable[[List[Dict[str, Any]]], None],
    append_conversation_message_fn: Callable[[List[Dict[str, Any]], Dict[str, Any]], None],
    process_chat_with_context_fn: Callable[..., None],
    max_phase_stagnation: int,
    max_repeated_actions: int,
    max_tool_failures: int,
    log_autonomous_tick_fn: Optional[Callable[..., Any]] = None,
    deepcopy_fn: Callable[[Any], Any] = copy.deepcopy,
) -> Dict[str, Any]:
    while pending_user_inputs and not str(pending_user_inputs[0] or "").strip():
        pending_user_inputs.pop(0)

    if pending_user_inputs:
        pending_input = str(pending_user_inputs[0]).strip()
        current_task_wrapper = task_chain_manager.get_current_task()
        current_task = current_task_wrapper["task"] if current_task_wrapper else {}
        previous_task = deepcopy_fn(current_task)
        goal_payload = {
            "source": "pending_user_input",
            "goal": pending_input,
            "phase": "plan",
            "recovery_mode": "none",
            "next_actions": ["address_pending_user_input"],
            "pending_index": 0,
        }

        cleanup_reasoning_content_fn(current_context)
        append_conversation_message_fn(
            current_context,
            {"role": "user", "content": pending_input},
        )
        try:
            process_chat_with_context_fn(pending_input, internal_context=None)
        except Exception as error:
            result = _mark_autonomous_error(
                task_chain_manager=task_chain_manager,
                goal_payload=goal_payload,
                internal_prompt=pending_input,
                error=error,
            )
            result["prompt"] = pending_input
            result["pending_count"] = len(pending_user_inputs)
            _safe_log_autonomous_tick(
                log_autonomous_tick_fn,
                goal_payload=goal_payload,
                result=result,
                internal_prompt=pending_input,
            )
            return result
        pending_user_inputs.pop(0)

        result = update_autonomous_progress(
            previous_task=previous_task,
            goal_payload=goal_payload,
            internal_prompt=pending_input,
            task_chain_manager=task_chain_manager,
            recent_tool_results=get_recent_tool_results_fn(),
            max_phase_stagnation=max_phase_stagnation,
            max_repeated_actions=max_repeated_actions,
            max_tool_failures=max_tool_failures,
            forced_status="paused",
            forced_stop_reason="pending_user_input",
        )
        result["prompt"] = pending_input
        result["consumed_pending_user_input"] = True
        result["pending_count"] = len(pending_user_inputs)
        _safe_log_autonomous_tick(
            log_autonomous_tick_fn,
            goal_payload=goal_payload,
            result=result,
            internal_prompt=pending_input,
        )
        return result

    current_task_wrapper = task_chain_manager.get_current_task()
    if not current_task_wrapper:
        result = {"status": "idle", "reason": "no_active_task"}
        _safe_log_autonomous_tick(
            log_autonomous_tick_fn,
            goal_payload={"source": "idle", "goal": "", "phase": "idle", "next_actions": []},
            result=result,
        )
        return result

    current_task = current_task_wrapper["task"]
    execution_state = current_task.get("execution_state", {}) or {}
    current_phase_state = normalize_phase_state(
        execution_state.get("phase", "plan"),
        execution_state.get("recovery_mode", "none"),
    )
    current_phase = current_phase_state["phase"]
    current_recovery_mode = current_phase_state["recovery_mode"]
    memory_phase = router_phase_for(current_phase, current_recovery_mode)
    current_intent = str((current_task.get("metadata", {}) or {}).get("intent") or "general_execution")
    goal = current_task.get("objective") or current_task.get("description") or ""
    recent_failures = get_recent_failures_fn()
    memory_snapshot = get_phase_memory_snapshot(
        goal=goal,
        phase=memory_phase,
        intent=current_intent,
        recent_failures=recent_failures,
        memory_manager=memory_manager,
        get_cognitive_network_summary_fn=get_cognitive_network_summary_fn,
    )
    replan_suggestion = None
    self_model: Dict[str, Any] = {}
    goal_payload = select_active_goal(
        current_task=current_task,
        replan_suggestion=replan_suggestion,
        recent_failures=recent_failures,
        pending_user_inputs=pending_user_inputs,
        max_tool_failures=max_tool_failures,
    )
    if not goal_payload:
        task_chain_manager.sync_autonomous_state(
            status="idle",
            last_tick_at=datetime.now().isoformat(),
            stop_reason="no_resumable_goal",
        )
        result = {"status": "idle", "reason": "no_resumable_goal"}
        _safe_log_autonomous_tick(
            log_autonomous_tick_fn,
            goal_payload={"source": "idle", "goal": goal, "phase": current_phase, "next_actions": []},
            result=result,
            memory_snapshot=memory_snapshot,
            replan_suggestion=replan_suggestion,
        )
        return result

    decision_context = build_autonomous_decision(
        current_task=current_task,
        goal_payload=goal_payload,
        memory_snapshot=memory_snapshot,
        self_model=self_model,
        replan_suggestion=replan_suggestion,
        recent_failures=recent_failures,
    )
    internal_prompt = decision_context["prompt"]

    cleanup_reasoning_content_fn(current_context)
    previous_task = deepcopy_fn(current_task)
    append_conversation_message_fn(
        current_context,
        {"role": "user", "content": internal_prompt},
    )
    try:
        process_chat_with_context_fn(internal_prompt, internal_context=decision_context)
    except Exception as error:
        result = _mark_autonomous_error(
            task_chain_manager=task_chain_manager,
            goal_payload=goal_payload,
            internal_prompt=internal_prompt,
            error=error,
        )
        result["prompt"] = internal_prompt
        _safe_log_autonomous_tick(
            log_autonomous_tick_fn,
            goal_payload=goal_payload,
            result=result,
            internal_prompt=internal_prompt,
            memory_snapshot=memory_snapshot,
            replan_suggestion=replan_suggestion,
        )
        return result
    result = update_autonomous_progress(
        previous_task=previous_task,
        goal_payload=goal_payload,
        internal_prompt=internal_prompt,
        task_chain_manager=task_chain_manager,
        recent_tool_results=get_recent_tool_results_fn(),
        max_phase_stagnation=max_phase_stagnation,
        max_repeated_actions=max_repeated_actions,
        max_tool_failures=max_tool_failures,
    )
    result["prompt"] = internal_prompt
    _safe_log_autonomous_tick(
        log_autonomous_tick_fn,
        goal_payload=goal_payload,
        result=result,
        internal_prompt=internal_prompt,
        memory_snapshot=memory_snapshot,
        replan_suggestion=replan_suggestion,
    )
    return result


def run_autonomous_cycle(
    *,
    max_steps: int = 1,
    autonomous_tick_fn: Callable[[], Dict[str, Any]],
) -> Dict[str, Any]:
    try:
        requested_steps = int(max_steps or 1)
    except (TypeError, ValueError):
        requested_steps = 1
    steps = max(1, min(requested_steps, 10))
    results: List[Dict[str, Any]] = []
    for _ in range(steps):
        try:
            tick_result = autonomous_tick_fn()
        except Exception as error:
            tick_result = {"status": "error", "reason": str(error)}
        results.append(tick_result)
        if tick_result.get("status") in {"idle", "stopped", "blocked", "error", "paused"}:
            break

    final_status = results[-1].get("status", "idle") if results else "idle"
    return {
        "success": final_status != "error",
        "requested_steps": steps,
        "executed_steps": len(results),
        "final_status": final_status,
        "results": results,
    }
