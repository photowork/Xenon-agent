from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional


def build_tool_outcome_state(
    *,
    tool_name: str,
    arguments: Dict[str, Any],
    result: Any,
    success: bool,
    recovery_plan: Optional[Dict[str, Any]],
    phase: str,
    goal: str,
    orchestration_mode: Optional[str],
    orchestration_next_actions: Optional[List[str]],
    orchestration_reasoning_summary: str,
    stringify_result_fn: Callable[[Any], str],
    summarize_payload_fn: Callable[[str], str],
) -> Dict[str, Any]:
    serialized_result = stringify_result_fn(result if success else recovery_plan or result)
    blockage_reason = None if success else (recovery_plan or {}).get("blockage_reason")
    next_actions = (
        list(orchestration_next_actions or [])
        if success
        else list((recovery_plan or {}).get("next_actions", []) or [])
    )
    reasoning_summary = (
        orchestration_reasoning_summary
        if success
        else str((recovery_plan or {}).get("summary", "") or "")
    )
    memory_summary = reasoning_summary or summarize_payload_fn(serialized_result)

    return {
        "last_tool_result": {
            "tool_name": tool_name,
            "arguments": copy.deepcopy(arguments),
            "result": serialized_result,
            "success": success,
            "timestamp": datetime.now().isoformat(),
        },
        "last_recovery_plan": copy.deepcopy(recovery_plan) if recovery_plan is not None else None,
        "journal_payload": {
            "goal": goal,
            "phase": phase,
            "tool_name": tool_name,
            "arguments": copy.deepcopy(arguments),
            "result": serialized_result,
            "success": success,
            "recovery": copy.deepcopy(recovery_plan) if recovery_plan is not None else None,
        },
        "execution_state_payload": {
            "phase": phase,
            "blockage_reason": blockage_reason,
            "next_actions": next_actions,
            "mode": orchestration_mode,
            "reasoning_summary": reasoning_summary,
            "last_tool": tool_name,
            "last_tool_result": serialized_result,
        },
        "memory_payload": {
            "goal": goal,
            "phase": phase,
            "tool_name": tool_name,
            "success": success,
            "blockage_reason": blockage_reason,
            "lesson": str((recovery_plan or {}).get("summary", "") or "") if not success else "",
            "summary": memory_summary,
            "next_actions": next_actions,
        },
    }


def apply_tool_outcome_state(
    outcome_state: Dict[str, Any],
    *,
    execution_journal: Any,
    task_chain_manager: Any,
    memory_manager: Any,
    logger: Any,
) -> None:
    execution_journal.log_tool_execution(**outcome_state["journal_payload"])
    task_chain_manager.sync_execution_state(**outcome_state["execution_state_payload"])

    if memory_manager is None:
        return

    try:
        memory_manager.write_execution_memory(**outcome_state["memory_payload"])
    except Exception as error:
        logger.debug("Failed to persist execution memory: %s", error)
