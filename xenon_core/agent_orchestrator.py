from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from xenon_core.phase_policy import normalize_phase_state, router_phase_for
from xenon_core.tool_router import ToolRouter


@dataclass
class ActionDecision:
    mode: str
    goal: str
    phase: str
    recovery_mode: str
    intent: str
    reasoning_summary: str
    candidate_modules: List[str]
    candidate_tools: List[str]
    selected_tool: Optional[str]
    tool_args: Dict[str, Any]
    should_update_task_chain: bool
    should_trim_context: bool
    next_expected_state: str
    next_actions: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class AgentOrchestrator:
    def __init__(self, router: Optional[ToolRouter] = None):
        self.router = router or ToolRouter()

    def prepare_for_user_input(
        self,
        user_input: str,
        tool_schemas: List[dict],
        current_task: Optional[Dict[str, Any]] = None,
        last_result: Optional[Dict[str, Any]] = None,
        context_status: Optional[Dict[str, Any]] = None,
        memory_summary: Optional[str] = None,
        route_hint: Optional[Dict[str, Any]] = None,
        recent_lessons: Optional[List[Dict[str, Any]]] = None,
    ) -> ActionDecision:
        route = self.router.route(
            user_input=user_input,
            tool_schemas=tool_schemas,
            current_task=current_task,
            route_hint=route_hint,
            recent_lessons=recent_lessons,
        )
        mode = self._infer_mode(user_input=user_input, current_task=current_task, last_result=last_result)
        should_trim_context = bool((context_status or {}).get("percentage", 0) >= 75)
        phase_state = normalize_phase_state(route.phase)
        phase = phase_state["phase"]
        recovery_mode = phase_state["recovery_mode"]
        candidate_tools = self._with_management_tool_fallback(
            route.candidate_tools,
            route.candidate_modules,
            tool_schemas,
        )
        next_actions = self._build_next_actions(phase, candidate_tools, recovery_mode=recovery_mode)
        next_expected_state = self._build_expected_state(phase, candidate_tools, recovery_mode=recovery_mode)

        reasoning_summary = route.reasoning_summary
        if route.phase != phase:
            reasoning_summary += f", router_phase={route.phase}, main_phase={phase}"
        if recovery_mode != "none":
            reasoning_summary += f", recovery_mode={recovery_mode}"
        if memory_summary:
            reasoning_summary += ", memory_summary=available"
        if last_result and last_result.get("success") is False:
            reasoning_summary += ", last_step=failed"

        return ActionDecision(
            mode=mode,
            goal=user_input.strip(),
            phase=phase,
            recovery_mode=recovery_mode,
            intent=route.intent,
            reasoning_summary=reasoning_summary,
            candidate_modules=route.candidate_modules,
            candidate_tools=candidate_tools,
            selected_tool=candidate_tools[0] if candidate_tools else None,
            tool_args={},
            should_update_task_chain=True,
            should_trim_context=should_trim_context,
            next_expected_state=next_expected_state,
            next_actions=next_actions,
        )

    def prepare_for_continuation(
        self,
        continuation_prompt: str,
        tool_schemas: List[dict],
        current_task: Optional[Dict[str, Any]] = None,
        last_result: Optional[Dict[str, Any]] = None,
        context_status: Optional[Dict[str, Any]] = None,
        memory_summary: Optional[str] = None,
        replan_suggestion: Optional[Dict[str, Any]] = None,
        route_hint: Optional[Dict[str, Any]] = None,
        recent_lessons: Optional[List[Dict[str, Any]]] = None,
    ) -> ActionDecision:
        execution_state = (current_task or {}).get("execution_state", {}) or {}
        raw_replan_phase = (replan_suggestion or {}).get("suggested_phase")
        raw_recovery_mode = (replan_suggestion or {}).get("recovery_mode")
        if raw_replan_phase:
            hint_state = normalize_phase_state(raw_replan_phase, raw_recovery_mode, recovery=True)
            hinted_router_phase = router_phase_for(hint_state["phase"], hint_state["recovery_mode"])
        else:
            hinted_router_phase = router_phase_for(
                execution_state.get("phase"),
                execution_state.get("recovery_mode"),
            )
        route = self.router.route(
            user_input=continuation_prompt,
            tool_schemas=tool_schemas,
            current_task=current_task,
            route_hint=route_hint
            or {"phase": hinted_router_phase},
            recent_lessons=recent_lessons,
        )

        goal = (
            (current_task or {}).get("objective")
            or (current_task or {}).get("description")
            or continuation_prompt.strip()
        )
        phase_source = (
            raw_replan_phase
            or execution_state.get("phase")
            or route.phase
            or "plan"
        )
        phase_state = normalize_phase_state(
            phase_source,
            raw_recovery_mode or execution_state.get("recovery_mode"),
            recovery=bool(replan_suggestion and replan_suggestion.get("should_replan")),
        )
        phase = phase_state["phase"]
        recovery_mode = phase_state["recovery_mode"]
        intent = route.intent or str(((current_task or {}).get("metadata") or {}).get("intent") or "general_execution")
        candidate_tools = self._with_management_tool_fallback(
            route.candidate_tools,
            route.candidate_modules,
            tool_schemas,
        )
        next_actions = (
            list((replan_suggestion or {}).get("next_actions") or [])
            or list(execution_state.get("next_actions") or [])
            or self._build_next_actions(phase, candidate_tools, recovery_mode=recovery_mode)
        )

        reasoning_summary = route.reasoning_summary
        if route.phase != phase:
            reasoning_summary += f", router_phase={route.phase}, main_phase={phase}"
        if recovery_mode != "none":
            reasoning_summary += f", recovery_mode={recovery_mode}"
        if memory_summary:
            reasoning_summary += ", memory_summary=available"
        if last_result and last_result.get("success") is False:
            reasoning_summary += ", last_step=failed"
        if replan_suggestion and replan_suggestion.get("should_replan"):
            reasoning_summary += f", replan={replan_suggestion.get('chosen_candidate', 'enabled')}"

        return ActionDecision(
            mode="replan" if replan_suggestion and replan_suggestion.get("should_replan") else "continue_task",
            goal=goal,
            phase=phase,
            recovery_mode=recovery_mode,
            intent=intent,
            reasoning_summary=reasoning_summary,
            candidate_modules=route.candidate_modules,
            candidate_tools=candidate_tools,
            selected_tool=candidate_tools[0] if candidate_tools else None,
            tool_args={},
            should_update_task_chain=True,
            should_trim_context=bool((context_status or {}).get("percentage", 0) >= 75),
            next_expected_state=self._build_expected_state(phase, candidate_tools, recovery_mode=recovery_mode),
            next_actions=next_actions,
        )

    def build_system_guidance(
        self,
        decision: Optional[ActionDecision],
        current_task: Optional[Dict[str, Any]] = None,
        journal_summary: str = "",
        recovery_plan: Optional[Dict[str, Any]] = None,
        replan_suggestion: Optional[Dict[str, Any]] = None,
        skill_guidance: str = "",
    ) -> str:
        if not decision:
            return ""

        lines = [
            "[ORCHESTRATOR]",
            f"- mode: {decision.mode}",
            f"- phase: {decision.phase}",
            f"- recovery_mode: {decision.recovery_mode}",
            f"- intent: {decision.intent}",
            f"- goal: {decision.goal}",
            f"- reasoning: {decision.reasoning_summary}",
            f"- candidate modules: {', '.join(decision.candidate_modules) or 'none'}",
            f"- candidate tools: {', '.join(decision.candidate_tools) or 'none'}",
            f"- selected tool: {decision.selected_tool or 'none'}",
            f"- next actions: {', '.join(decision.next_actions) or 'none'}",
            f"- next expected state: {decision.next_expected_state}",
            (
                "- instruction: prefer candidate tools first; if selected tool is load_module, "
                "call load_module with the relevant candidate modules before describing file inspection, edits, or verification."
            ),
        ]

        if current_task:
            execution_state = current_task.get("execution_state", {}) or {}
            lines.extend(
                [
                    "[TASK_STATE]",
                    f"- task_id: {current_task.get('task_id')}",
                    f"- status: {current_task.get('status')}",
                    f"- current_step: {current_task.get('current_step')}",
                    f"- phase: {execution_state.get('phase', 'unknown')}",
                    f"- recovery_mode: {execution_state.get('recovery_mode', 'none')}",
                    f"- blockage_reason: {execution_state.get('blockage_reason', 'none')}",
                    f"- next_actions: {', '.join(execution_state.get('next_actions', [])) or 'none'}",
                ]
            )

        if journal_summary:
            lines.extend(["[RECENT_EXECUTION]", journal_summary])

        if recovery_plan:
            lines.extend(
                [
                    "[RECOVERY_HINT]",
                    f"- blockage_reason: {recovery_plan.get('blockage_reason')}",
                    f"- next_actions: {', '.join(recovery_plan.get('next_actions', [])) or 'none'}",
                    f"- summary: {recovery_plan.get('summary')}",
                ]
            )
        if replan_suggestion:
            lines.extend([
                "[REPLAN_HINT]",
                f"- should_replan: {replan_suggestion.get('should_replan')}",
                f"- suggested_phase: {replan_suggestion.get('suggested_phase')}",
                f"- next_actions: {', '.join(replan_suggestion.get('next_actions', [])) or 'none'}",
                f"- reasoning: {replan_suggestion.get('reasoning_summary', '')}",
            ])
        if skill_guidance:
            lines.extend(["[RELEVANT_SKILLS]", skill_guidance])

        return "\n".join(lines) + "\n"

    def default_steps_for_goal(self, goal: str) -> List[str]:
        return [
            f"Analyze request: {goal}",
            "Locate the relevant files, modules, or tools",
            "Apply the required change or execute the action",
            "Verify the outcome and summarize the result",
        ]

    @staticmethod
    def _with_management_tool_fallback(
        candidate_tools: List[str],
        candidate_modules: List[str],
        tool_schemas: List[dict],
    ) -> List[str]:
        if candidate_tools or not candidate_modules:
            return list(candidate_tools)

        available_tools = {
            (schema.get("function", {}) or {}).get("name", "")
            for schema in tool_schemas or []
        }
        if "load_module" not in available_tools:
            return []

        return ["load_module"]

    def _infer_mode(
        self,
        user_input: str,
        current_task: Optional[Dict[str, Any]],
        last_result: Optional[Dict[str, Any]],
    ) -> str:
        text = (user_input or "").lower()
        if current_task and current_task.get("status") == "in_progress":
            return "continue_task"
        if any(token in text for token in [" and ", "然后", "再", "并", "同时", "集成"]):
            return "multi_step"
        if last_result and last_result.get("success") is False:
            return "continue_task"
        return "single_step"

    @staticmethod
    def _build_next_actions(
        phase: str,
        candidate_tools: List[str],
        *,
        recovery_mode: str = "none",
    ) -> List[str]:
        phase_state = normalize_phase_state(phase, recovery_mode)
        phase = phase_state["phase"]
        recovery_mode = phase_state["recovery_mode"]
        if phase == "plan":
            return ["search_more_context", "inspect_candidate_files", "choose_tool"]
        if phase == "act":
            return ["execute_selected_tool", "verify_changes", "update_task_state"]
        if phase == "verify":
            return ["run_validation", "inspect_failures", "retry_same_tool"]
        if phase == "recover":
            if recovery_mode == "reflect":
                return ["summarize_failure_pattern", "choose_safer_next_step", "return_to_plan_or_act"]
            if recovery_mode == "analyze":
                return ["clarify_missing_context", "inspect_candidate_files", "choose_next_action"]
            return ["inspect_failures", "isolate_cause", "choose_fix_or_fallback"]
        if phase == "done":
            return ["summarize_outcome"]
        return ["choose_tool", "execute_selected_tool"]

    @staticmethod
    def _build_expected_state(
        phase: str,
        candidate_tools: List[str],
        *,
        recovery_mode: str = "none",
    ) -> str:
        next_tool = candidate_tools[0] if candidate_tools else "tool_selection"
        phase_state = normalize_phase_state(phase, recovery_mode)
        phase = phase_state["phase"]
        recovery_mode = phase_state["recovery_mode"]
        if phase == "recover":
            return f"recover({recovery_mode}) -> {next_tool} -> plan/act/verify"
        if phase == "plan":
            return f"plan -> {next_tool} -> act"
        if phase == "act":
            return f"act -> {next_tool} -> verify"
        if phase == "verify":
            return f"verify -> {next_tool} -> done/recover"
        return f"{phase} -> {next_tool} -> review_result"
