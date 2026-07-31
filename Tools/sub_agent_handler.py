#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Sub-agent tool facade backed by xenon_core.multi_agent_runtime.

This module is intentionally thin: it does not own a separate runner script and
does not call the LLM API directly. Planning, queue persistence, execution state,
and integration all flow through the core runtime used by the main Xenon agent.
"""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from xenon_core.multi_agent_runtime import MultiAgentRuntime, TERMINAL_RUN_STATUSES, build_integration_result


class SubAgentToolManager:
    """Core-backed sub-agent orchestration tools."""

    def __init__(self) -> None:
        self._project_root = Path(__file__).resolve().parent.parent
        self._queue_path = self._project_root / "Tasks" / "multi_agent_queue.json"
        self.runtime = MultiAgentRuntime(queue_path=str(self._queue_path))
        self.host_agent: Optional[Any] = None

    def _attach_agent(self, agent: Any) -> None:
        """Attach the running Xenon agent so execution can reuse the core path."""
        self.host_agent = agent
        runtime = getattr(agent, "multi_agent_runtime", None)
        if isinstance(runtime, MultiAgentRuntime):
            self.runtime = runtime

    def status(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        """查看多智能体运行状态。

        :param run_id: 可选运行 ID；不填则查看最近一次运行。
        """
        return {"success": True, **self.runtime.get_status(run_id=run_id)}

    def plan(
        self,
        task_description: str = "",
        task_id: Optional[str] = None,
        max_subtasks: int = 2,
        steps: Optional[List[str]] = None,
        allowed_roles: Optional[List[str]] = None,
        use_current_task: bool = True,
    ) -> Dict[str, Any]:
        """创建一个核心多智能体运行队列。

        :param task_description: 要拆分的任务描述；留空时可使用当前任务链任务。
        :param task_id: 可选任务 ID；留空会自动生成。
        :param max_subtasks: 最大子任务数量，范围 1-6。
        :param steps: 可选的明确步骤列表，用于比纯描述更稳定地拆分。
        :param allowed_roles: 可选角色白名单，如 ["planner", "executor"]。
        :param use_current_task: task_description 为空时是否优先使用当前任务链。
        """
        role_validation = self.runtime.validate_roles(allowed_roles)
        if allowed_roles and not role_validation.get("success"):
            return {
                "success": False,
                "status": "invalid_input",
                "reason": "unknown_allowed_roles",
                "unknown_roles": role_validation.get("unknown_roles", []),
                "available_roles": role_validation.get("available_roles", []),
                "message": "allowed_roles 包含未知角色，请先用 role_guide 查看可选角色。",
            }

        parent_task = self._resolve_parent_task(
            task_description=task_description,
            task_id=task_id,
            steps=steps,
            use_current_task=use_current_task,
        )
        if not parent_task:
            return {
                "success": False,
                "status": "idle",
                "reason": "no_task_description_or_current_task",
                "message": "请提供 task_description，或先在任务链中创建当前任务。",
            }

        run = self.runtime.create_run(parent_task, max_subtasks=self._bounded_subtask_count(max_subtasks))
        role_filter: Optional[Dict[str, Any]] = None
        if allowed_roles:
            role_filter = self.runtime.apply_role_filter(run.get("run_id"), allowed_roles)
            if not role_filter.get("success"):
                return role_filter
            run = role_filter.get("run") or run

        response = {
            "success": True,
            "status": run.get("status"),
            "run_id": run.get("run_id"),
            "task_id": run.get("parent_task_id"),
            "run": self._summarize_run(run),
            "message": f"已创建 {len(run.get('subtasks', []))} 个核心子任务。",
        }
        if role_filter:
            response["role_filter"] = {
                "allowed_roles": role_filter.get("allowed_roles", []),
                "skipped_roles": role_filter.get("skipped_roles", []),
            }
        return response

    def run(
        self,
        run_id: Optional[str] = None,
        max_subtasks: int = 2,
        auto_integrate: bool = True,
    ) -> Dict[str, Any]:
        """执行待处理子任务，复用宿主 Xenon agent 的核心执行回调。

        :param run_id: 可选运行 ID；不填则执行最近可继续运行。
        :param max_subtasks: 本轮最多执行的子任务数量，范围 1-6。
        :param auto_integrate: 全部可执行子任务完成后是否自动整合。
        """
        executor_fn = self._get_host_executor()
        if executor_fn is None:
            return {
                "success": False,
                "status": "needs_host_agent",
                "reason": "sub_agent_tool_not_attached_to_agent",
                "message": (
                    "该工具已使用 xenon_core 队列，但执行需要宿主 AIAgent 注入；"
                    "请通过主程序加载工具，或使用 /agents run。"
                ),
            }

        selected_run_id = run_id or self._latest_resumable_run_id()
        if not selected_run_id:
            planned = self.plan(max_subtasks=max_subtasks, use_current_task=True)
            if not planned.get("success"):
                return planned
            selected_run_id = planned.get("run_id")

        execution = self.runtime.run_pending(
            run_id=selected_run_id,
            max_subtasks=self._bounded_subtask_count(max_subtasks),
            executor_fn=executor_fn,
        )
        result: Dict[str, Any] = {
            "success": execution.get("success", False),
            "status": execution.get("status"),
            "run_id": execution.get("run_id"),
            "executed_count": execution.get("executed_count", 0),
            "execution_mode": "sequential_batch",
            "requested_max_subtasks": self._bounded_subtask_count(max_subtasks),
            "execution": self._compact_execution(execution),
        }

        if auto_integrate and execution.get("status") in {"completed", "completed_with_failures", "skipped"}:
            result["integration"] = self.integrate(run_id=execution.get("run_id"), include_results=False)
            result["status"] = result["integration"].get("status", result["status"])

        return result

    def integrate(
        self,
        run_id: Optional[str] = None,
        include_results: bool = True,
    ) -> Dict[str, Any]:
        """整合子智能体结果，并同步任务链执行状态（如宿主 agent 可用）。

        :param run_id: 可选运行 ID；不填则整合最近可继续运行。
        :param include_results: 是否返回每个子任务的完整结果。
        """
        integration = self.runtime.integrate_run(
            run_id=run_id,
            task_chain_manager=getattr(self.host_agent, "task_chain_manager", None),
            execution_journal=getattr(self.host_agent, "execution_journal", None),
        )
        if not integration.get("success"):
            return integration

        payload = dict(integration.get("integration") or {})
        response = {
            "success": True,
            "status": integration.get("status"),
            "run_id": integration.get("run_id"),
            "sync_ok": integration.get("sync_ok", False),
            "summary": payload.get("summary", ""),
            "completed_count": payload.get("completed_count", 0),
            "failed_count": payload.get("failed_count", 0),
            "cancelled_count": payload.get("cancelled_count", 0),
            "skipped_count": payload.get("skipped_count", 0),
            "conflicts": payload.get("conflicts", []),
            "next_actions": payload.get("next_actions", []),
        }
        if include_results:
            response["subtask_results"] = payload.get("subtask_results", [])
        return response

    def clear(
        self,
        run_id: Optional[str] = None,
        clear_all_terminal: bool = False,
        force: bool = False,
    ) -> Dict[str, Any]:
        """清理多智能体队列。

        :param run_id: 指定运行 ID；不填时默认只清理已整合运行。
        :param clear_all_terminal: 为 true 时清理 completed/failed/skipped 等终态运行。
        :param force: 指定 run_id 且运行仍活跃时，是否强制删除。
        """
        statuses = sorted(TERMINAL_RUN_STATUSES) if clear_all_terminal else ["integrated"]
        return self.runtime.clear_runs(run_id=run_id, statuses=statuses, force=force)

    def cancel(self, run_id: Optional[str] = None, reason: str = "") -> Dict[str, Any]:
        """取消排队中或运行中的子任务。"""
        return self.runtime.cancel_run(run_id=run_id, reason=reason)

    def retry_failed(
        self,
        run_id: Optional[str] = None,
        subtask_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """将失败/已取消的子任务重新放回队列。"""
        subtask_ids = [subtask_id] if subtask_id else None
        return self.runtime.retry_subtasks(run_id=run_id, subtask_ids=subtask_ids)

    def create_and_run(
        self,
        task_description: str = "",
        task_id: Optional[str] = None,
        max_subtasks: int = 2,
        steps: Optional[List[str]] = None,
        allowed_roles: Optional[List[str]] = None,
        auto_integrate: bool = True,
    ) -> Dict[str, Any]:
        """一站式创建并执行核心多智能体运行。

        :param task_description: 要拆分的任务描述；留空时可使用当前任务链任务。
        :param task_id: 可选任务 ID。
        :param max_subtasks: 最大子任务数量，范围 1-6。
        :param steps: 可选步骤列表。
        :param allowed_roles: 可选角色白名单。
        :param auto_integrate: 完成后是否自动整合。
        """
        planned = self.plan(
            task_description=task_description,
            task_id=task_id,
            max_subtasks=max_subtasks,
            steps=steps,
            allowed_roles=allowed_roles,
            use_current_task=True,
        )
        if not planned.get("success"):
            return planned

        executed = self.run(
            run_id=planned.get("run_id"),
            max_subtasks=max_subtasks,
            auto_integrate=auto_integrate,
        )
        return {
            "success": bool(executed.get("success")) and executed.get("status") == "completed",
            "run_id": planned.get("run_id"),
            "plan": planned.get("run"),
            "status": executed.get("status"),
            "executed_count": executed.get("executed_count", 0),
            "execution": executed.get("execution"),
            "integration": executed.get("integration"),
        }

    def role_guide(self) -> Dict[str, Any]:
        """查看默认角色和工具意图，帮助选择 allowed_roles。"""
        return {
            "success": True,
            "execution_mode": "sequential_batch",
            "roles": [
                {"role": "planner", "default_tools": ["memory_query", "code_editor_handler"]},
                {"role": "executor", "default_tools": ["terminal_handler", "code_editor"]},
                {"role": "verifier", "default_tools": ["terminal_handler", "debug_handler"]},
                {"role": "researcher", "default_tools": ["memory_query"]},
                {"role": "critic", "default_tools": ["code_editor_handler", "debug_handler"]},
                {"role": "integrator", "default_tools": ["terminal_handler"]},
            ],
        }

    # Backwards-compatible names for older prompts/tool calls.
    def create_sub_agents(
        self,
        task_description: str,
        task_id: Optional[str] = None,
        max_subtasks: int = 2,
        allowed_roles: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """兼容旧接口：创建子任务队列。"""
        return self.plan(
            task_description=task_description,
            task_id=task_id,
            max_subtasks=max_subtasks,
            allowed_roles=allowed_roles,
            use_current_task=False,
        )

    def execute_sub_agents(
        self,
        run_id: Optional[str] = None,
        max_parallel: int = 2,
    ) -> Dict[str, Any]:
        """兼容旧接口：执行待处理子任务；max_parallel 是顺序批处理上限。"""
        return self.run(run_id=run_id, max_subtasks=max_parallel, auto_integrate=False)

    def get_sub_agent_status(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        """兼容旧接口：查看状态。"""
        return self.status(run_id=run_id)

    def integrate_sub_agent_results(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        """兼容旧接口：整合结果。"""
        return self.integrate(run_id=run_id)

    def destroy_sub_agents(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        """兼容旧接口：清理运行。"""
        return self.clear(run_id=run_id)

    def _resolve_parent_task(
        self,
        *,
        task_description: str,
        task_id: Optional[str],
        steps: Optional[List[str]],
        use_current_task: bool,
    ) -> Optional[Dict[str, Any]]:
        if use_current_task and not task_description.strip():
            current = self._current_task()
            if current:
                return copy.deepcopy(current)

        description = task_description.strip()
        if not description:
            return None

        resolved_task_id = task_id or f"tool_{hashlib.md5(description.encode('utf-8')).hexdigest()[:10]}"
        return {
            "task_id": resolved_task_id,
            "objective": description,
            "description": description,
            "steps": [{"description": step} for step in (steps or []) if str(step).strip()],
        }

    def _current_task(self) -> Optional[Dict[str, Any]]:
        manager = getattr(self.host_agent, "task_chain_manager", None)
        get_current_task = getattr(manager, "get_current_task", None)
        if not callable(get_current_task):
            return None
        try:
            wrapper = get_current_task()
        except Exception:
            return None
        if isinstance(wrapper, dict) and isinstance(wrapper.get("task"), dict):
            return wrapper["task"]
        return None

    def _get_host_executor(self) -> Optional[Any]:
        executor = getattr(self.host_agent, "_execute_multi_agent_subtask", None)
        return executor if callable(executor) else None

    def _latest_resumable_run_id(self) -> Optional[str]:
        return self.runtime.latest_resumable_run_id()

    def _apply_role_filter(self, run_id: Optional[str], allowed_roles: List[str]) -> Dict[str, Any]:
        result = self.runtime.apply_role_filter(run_id, allowed_roles)
        return result.get("run") or {}

    def _summarize_run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        summary = self.runtime.summarize_run(run) or {}
        if run.get("integration"):
            summary["integration_status"] = run["integration"].get("status")
        return summary

    def _compact_execution(self, execution: Dict[str, Any]) -> Dict[str, Any]:
        compact = {
            "success": execution.get("success"),
            "status": execution.get("status"),
            "run_id": execution.get("run_id"),
            "executed_count": execution.get("executed_count", 0),
            "executed": [],
            "run": execution.get("run"),
        }
        for subtask in execution.get("executed", []):
            result = subtask.get("result") or {}
            compact["executed"].append(
                {
                    "subtask_id": subtask.get("subtask_id"),
                    "role": subtask.get("role"),
                    "status": subtask.get("status"),
                    "summary": str(result.get("summary", ""))[:500],
                    "conflicts": result.get("conflicts", []),
                    "artifacts": result.get("artifacts", []),
                }
            )
        return compact

    @staticmethod
    def _bounded_subtask_count(value: int) -> int:
        try:
            return max(1, min(int(value or 1), 6))
        except Exception:
            return 2


def summarize_run_without_mutating(run: Dict[str, Any]) -> Dict[str, Any]:
    """Utility kept for tests and external callers that need a quick integration view."""
    return build_integration_result(copy.deepcopy(run))
