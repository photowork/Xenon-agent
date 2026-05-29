from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple


TERMINAL_SUBTASK_STATUSES = {"completed", "failed", "cancelled", "skipped"}
TERMINAL_RUN_STATUSES = {
    "cancelled",
    "completed",
    "completed_with_cancellations",
    "completed_with_failures",
    "failed",
    "integrated",
    "skipped",
}
ROLE_TOOL_MAP = {
    "planner": ["memory_query", "code_navigator"],
    "executor": ["terminal_handler", "code_editor", "file_manager"],
    "verifier": ["terminal_handler", "debug_handler"],
    "researcher": ["memory_query", "file_manager"],
    "critic": ["code_navigator", "debug_handler"],
    "integrator": ["file_manager", "terminal_handler"],
}
DEFAULT_ROLES: Tuple[str, ...] = tuple(ROLE_TOOL_MAP.keys())
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_STALE_SECONDS = 60.0
LOCK_POLL_SECONDS = 0.05


def split_task_into_subtasks(
    parent_task: Dict[str, Any],
    *,
    max_subtasks: int = 2,
) -> List[Dict[str, Any]]:
    parent_task_id = str(parent_task.get("task_id") or "task")
    objective = str(parent_task.get("objective") or parent_task.get("description") or "").strip()
    steps = [
        str(step.get("description") or "").strip()
        for step in parent_task.get("steps", [])
        if str(step.get("description") or "").strip()
    ]

    count = max(1, min(int(max_subtasks or 1), 6))
    if count > 1 and len(steps) <= 1:
        steps = steps or [objective or "Analyze the parent task"]
        steps = [
            f"Analyze scope and risks for: {objective or steps[0]}",
            f"Execute or verify the smallest safe path for: {objective or steps[0]}",
        ]
    if steps:
        count = min(count, len(steps))

    chunks = _chunk_steps(steps or [objective or "Continue the parent task"], count)
    roles = _roles_for_count(len(chunks))
    created_at = _now()

    subtasks: List[Dict[str, Any]] = []
    for index, assigned_steps in enumerate(chunks, 1):
        role = roles[index - 1]
        seed = f"{parent_task_id}:{index}:{role}:{'|'.join(assigned_steps)}"
        subtask_id = f"sub_{hashlib.md5(seed.encode('utf-8')).hexdigest()[:10]}"
        subtask_objective = _build_subtask_objective(
            parent_objective=objective,
            role=role,
            assigned_steps=assigned_steps,
        )
        subtasks.append(
            {
                "subtask_id": subtask_id,
                "parent_task_id": parent_task_id,
                "role": role,
                "objective": subtask_objective,
                "assigned_steps": assigned_steps,
                "allowed_tools": _default_tools_for_role(role),
                "status": "queued",
                "attempts": 0,
                "result": None,
                "created_at": created_at,
                "updated_at": created_at,
                "context": {
                    "parent_objective": objective,
                    "parent_description": parent_task.get("description", ""),
                    "subtask_index": index,
                    "subtask_count": len(chunks),
                },
            }
        )
    return subtasks


def build_subagent_prompt(subtask: Dict[str, Any]) -> str:
    lines = [
        "[MULTI_AGENT_SUBTASK]",
        "You are an isolated Xenon sub-agent. Work only on this subtask.",
        f"subtask_id: {subtask.get('subtask_id', '')}",
        f"role: {subtask.get('role', 'worker')}",
        f"objective: {subtask.get('objective', '')}",
        "allowed_tools: " + ", ".join(subtask.get("allowed_tools") or ["none"]),
    ]
    assigned_steps = subtask.get("assigned_steps") or []
    if assigned_steps:
        lines.append("assigned_steps:")
        lines.extend(f"- {step}" for step in assigned_steps)
    lines.extend(
        [
            "",
            "Return a concise structured result with:",
            "- success: true/false",
            "- summary: what you completed or learned",
            "- artifacts: changed paths or outputs, if any",
            "- conflicts: anything the parent agent must resolve",
        ]
    )
    return "\n".join(lines).strip()


def normalize_subagent_result(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        normalized = dict(result)
    else:
        normalized = {"success": True, "summary": str(result)}

    normalized["success"] = bool(normalized.get("success", True))
    summary = normalized.get("summary")
    if summary is None:
        summary = normalized.get("output") or normalized.get("result") or ""
    normalized["summary"] = str(summary)
    normalized["artifacts"] = _normalize_list(normalized.get("artifacts"))
    normalized["conflicts"] = _normalize_list(normalized.get("conflicts"))
    normalized["metadata"] = dict(normalized.get("metadata") or {})
    return normalized


def detect_result_conflicts(subtasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    conflicts: List[Dict[str, Any]] = []
    artifact_index: Dict[str, Dict[str, Any]] = {}

    for subtask in subtasks:
        result = subtask.get("result") or {}
        for conflict in result.get("conflicts") or []:
            conflicts.append(
                {
                    "type": "reported_conflict",
                    "subtask_id": subtask.get("subtask_id"),
                    "detail": conflict,
                }
            )

        for artifact in result.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            artifact_key = artifact.get("path") or artifact.get("name")
            if not artifact_key:
                continue
            artifact_hash = artifact.get("content_hash") or artifact.get("hash") or _value_hash(artifact)
            previous = artifact_index.get(artifact_key)
            if previous and previous.get("hash") != artifact_hash:
                conflicts.append(
                    {
                        "type": "artifact_conflict",
                        "artifact": artifact_key,
                        "subtasks": [previous.get("subtask_id"), subtask.get("subtask_id")],
                    }
                )
            else:
                artifact_index[artifact_key] = {
                    "hash": artifact_hash,
                    "subtask_id": subtask.get("subtask_id"),
                }

    return conflicts


class MultiAgentRuntime:
    def __init__(self, queue_path: str = "Tasks/multi_agent_queue.json"):
        self.queue_path = Path(queue_path)
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = self.queue_path.with_suffix(self.queue_path.suffix + ".lock")
        self._thread_lock = RLock()

    def create_run(
        self,
        parent_task: Dict[str, Any],
        *,
        max_subtasks: int = 2,
    ) -> Dict[str, Any]:
        subtasks = split_task_into_subtasks(parent_task, max_subtasks=max_subtasks)
        created_at = _now()
        run_seed = f"{parent_task.get('task_id', 'task')}:{created_at}:{len(subtasks)}"
        run = {
            "run_id": f"mar_{hashlib.md5(run_seed.encode('utf-8')).hexdigest()[:12]}",
            "parent_task_id": parent_task.get("task_id"),
            "parent_objective": parent_task.get("objective") or parent_task.get("description") or "",
            "status": "queued",
            "subtasks": subtasks,
            "integration": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
        with self._state_transaction() as state:
            state.setdefault("runs", []).append(run)
        return copy.deepcopy(run)

    def get_status(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        state = self.load_state()
        runs = state.get("runs", [])
        selected = self._find_run(runs, run_id) if run_id else self._latest_run(runs)
        return {
            "has_runs": bool(runs),
            "active_run": self._summarize_run(selected) if selected else None,
            "runs": [self._summarize_run(run) for run in runs[-5:]],
        }

    def load_state(self) -> Dict[str, Any]:
        """Return a consistent snapshot of the persisted queue state."""
        with self._state_file_lock():
            return self._load_state_unlocked()

    def latest_resumable_run_id(self) -> Optional[str]:
        state = self.load_state()
        run = self._latest_resumable_run(state.get("runs", []))
        return run.get("run_id") if run else None

    def available_roles(self) -> List[str]:
        return list(DEFAULT_ROLES)

    def summarize_run(self, run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return self._summarize_run(run)

    def validate_roles(self, roles: Optional[Sequence[str]]) -> Dict[str, Any]:
        requested = [str(role).strip().lower() for role in (roles or []) if str(role).strip()]
        unique_requested = list(dict.fromkeys(requested))
        known = set(DEFAULT_ROLES)
        unknown = sorted(role for role in unique_requested if role not in known)
        return {
            "success": not unknown,
            "roles": unique_requested,
            "unknown_roles": unknown,
            "available_roles": self.available_roles(),
        }

    def apply_role_filter(self, run_id: Optional[str], allowed_roles: Sequence[str]) -> Dict[str, Any]:
        validation = self.validate_roles(allowed_roles)
        if not validation["success"]:
            return {"success": False, "reason": "unknown_allowed_roles", **validation}

        allowed = set(validation["roles"])
        if not allowed:
            return {"success": False, "reason": "empty_allowed_roles", **validation}

        with self._state_transaction() as state:
            run = self._find_run(state.get("runs", []), run_id)
            if not run:
                return {"success": False, "reason": "no_multi_agent_run"}

            skipped_roles: List[str] = []
            for subtask in run.get("subtasks", []):
                role = str(subtask.get("role", "")).lower()
                if role not in allowed and subtask.get("status") == "queued":
                    skipped_roles.append(role)
                    subtask["status"] = "skipped"
                    subtask["updated_at"] = _now()
                    subtask["result"] = {
                        "success": True,
                        "summary": f"Skipped because role '{role}' is outside allowed_roles.",
                        "artifacts": [],
                        "conflicts": [],
                        "metadata": {"allowed_roles": sorted(allowed)},
                    }
            run["status"] = self._derive_run_status(run)
            run["updated_at"] = _now()
            return {
                "success": True,
                "run": copy.deepcopy(run),
                "summary": self._summarize_run(run),
                "allowed_roles": sorted(allowed),
                "skipped_roles": skipped_roles,
            }

    def clear_runs(
        self,
        *,
        run_id: Optional[str] = None,
        statuses: Optional[Sequence[str]] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        removable_statuses = set(statuses or ["integrated"])
        with self._state_transaction() as state:
            runs = state.get("runs", [])
            if run_id:
                target = self._find_run(runs, run_id)
                if not target:
                    return {"success": False, "reason": f"run_not_found:{run_id}"}
                if target.get("status") not in TERMINAL_RUN_STATUSES and not force:
                    return {
                        "success": False,
                        "reason": "active_run_requires_force",
                        "run_id": run_id,
                        "status": target.get("status"),
                    }
                state["runs"] = [run for run in runs if run.get("run_id") != run_id]
                return {"success": True, "destroyed": [run_id], "remaining_count": len(state["runs"])}

            kept = [run for run in runs if run.get("status") not in removable_statuses]
            destroyed = [run.get("run_id") for run in runs if run.get("status") in removable_statuses]
            state["runs"] = kept
            return {
                "success": True,
                "destroyed_count": len(destroyed),
                "destroyed": destroyed,
                "remaining_count": len(kept),
            }

    def cancel_run(self, run_id: Optional[str] = None, reason: str = "") -> Dict[str, Any]:
        cancel_reason = reason.strip() or "cancelled_by_user"
        with self._state_transaction() as state:
            run = self._find_run(state.get("runs", []), run_id) if run_id else self._latest_resumable_run(state.get("runs", []))
            if not run:
                return {"success": False, "status": "idle", "reason": "no_multi_agent_run"}

            cancelled: List[str] = []
            for subtask in run.get("subtasks", []):
                if subtask.get("status") in {"queued", "running"}:
                    subtask["status"] = "cancelled"
                    subtask["updated_at"] = _now()
                    subtask["result"] = {
                        "success": False,
                        "summary": cancel_reason,
                        "artifacts": [],
                        "conflicts": [],
                        "metadata": {"cancelled": True},
                    }
                    cancelled.append(subtask.get("subtask_id"))

            run["status"] = self._derive_run_status(run)
            run["updated_at"] = _now()
            return {
                "success": bool(cancelled),
                "status": run["status"],
                "run_id": run.get("run_id"),
                "cancelled_count": len(cancelled),
                "cancelled": cancelled,
                "run": self._summarize_run(run),
            }

    def retry_subtasks(
        self,
        *,
        run_id: Optional[str] = None,
        subtask_ids: Optional[Sequence[str]] = None,
        retry_statuses: Sequence[str] = ("failed", "cancelled"),
    ) -> Dict[str, Any]:
        target_ids = {str(item).strip() for item in (subtask_ids or []) if str(item).strip()}
        retryable_statuses = {str(status).strip() for status in retry_statuses if str(status).strip()}
        with self._state_transaction() as state:
            run = self._find_run(state.get("runs", []), run_id) if run_id else self._latest_resumable_run(state.get("runs", []))
            if not run:
                return {"success": False, "status": "idle", "reason": "no_multi_agent_run"}

            retried: List[str] = []
            for subtask in run.get("subtasks", []):
                subtask_id = str(subtask.get("subtask_id") or "")
                if target_ids and subtask_id not in target_ids:
                    continue
                if subtask.get("status") not in retryable_statuses:
                    continue
                subtask["status"] = "queued"
                subtask["result"] = None
                subtask["updated_at"] = _now()
                retried.append(subtask_id)

            if not retried:
                return {
                    "success": False,
                    "status": run.get("status"),
                    "reason": "no_retryable_subtasks",
                    "run_id": run.get("run_id"),
                    "retry_statuses": sorted(retryable_statuses),
                }

            run["integration"] = None
            run["status"] = self._derive_run_status(run)
            run["updated_at"] = _now()
            return {
                "success": True,
                "status": run["status"],
                "run_id": run.get("run_id"),
                "retried_count": len(retried),
                "retried": retried,
                "run": self._summarize_run(run),
            }

    def run_pending(
        self,
        *,
        executor_fn: Callable[[Dict[str, Any]], Any],
        run_id: Optional[str] = None,
        max_subtasks: int = 2,
    ) -> Dict[str, Any]:
        limit = max(1, min(int(max_subtasks or 1), 6))
        executed: List[Dict[str, Any]] = []
        selected_run_id = run_id
        final_status: Optional[str] = None
        final_run_summary: Optional[Dict[str, Any]] = None

        while len(executed) < limit:
            with self._state_transaction() as state:
                run = (
                    self._find_run(state.get("runs", []), selected_run_id)
                    if selected_run_id
                    else self._latest_resumable_run(state.get("runs", []))
                )
                if not run:
                    return {"success": False, "status": "idle", "reason": "no_multi_agent_run"}

                selected_run_id = run.get("run_id")
                selected_subtask: Optional[Dict[str, Any]] = None
                for subtask in run.get("subtasks", []):
                    if subtask.get("status") != "queued":
                        continue
                    subtask["status"] = "running"
                    subtask["attempts"] = int(subtask.get("attempts") or 0) + 1
                    subtask["updated_at"] = _now()
                    run["status"] = "running"
                    run["updated_at"] = _now()
                    selected_subtask = copy.deepcopy(subtask)
                    break

                if not selected_subtask:
                    run["status"] = self._derive_run_status(run)
                    run["updated_at"] = _now()
                    final_status = run["status"]
                    final_run_summary = self._summarize_run(run)
                    break

            try:
                result = normalize_subagent_result(executor_fn(copy.deepcopy(selected_subtask)))
            except Exception as error:
                result = normalize_subagent_result(
                    {
                        "success": False,
                        "summary": str(error),
                        "metadata": {"exception_type": type(error).__name__},
                    }
                )

            with self._state_transaction() as state:
                run = self._find_run(state.get("runs", []), selected_run_id)
                subtask = self._find_subtask(run, selected_subtask.get("subtask_id")) if run else None
                if subtask:
                    if subtask.get("status") != "cancelled":
                        subtask["result"] = result
                        subtask["status"] = "completed" if result.get("success") else "failed"
                        subtask["updated_at"] = _now()
                    executed.append(copy.deepcopy(subtask))
                if run:
                    run["status"] = self._derive_run_status(run)
                    run["updated_at"] = _now()
                    final_status = run["status"]
                    final_run_summary = self._summarize_run(run)

        return {
            "success": True,
            "status": final_status,
            "run_id": selected_run_id,
            "executed_count": len(executed),
            "executed": executed,
            "run": final_run_summary,
        }

    def integrate_run(
        self,
        *,
        task_chain_manager: Any,
        execution_journal: Any,
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._state_transaction() as state:
            run = self._find_run(state.get("runs", []), run_id) if run_id else self._latest_resumable_run(state.get("runs", []))
            if not run:
                return {"success": False, "status": "idle", "reason": "no_multi_agent_run"}

            integration = build_integration_result(run)
            run["integration"] = integration
            run["status"] = "integrated"
            run["updated_at"] = _now()
            saved_run = copy.deepcopy(run)

        next_actions = integration["next_actions"]
        blockage_reason = "multi_agent_conflict" if integration["conflicts"] else None
        sync_ok = False
        if task_chain_manager:
            sync_ok = bool(
                task_chain_manager.sync_execution_state(
                    phase="integrate",
                    blockage_reason=blockage_reason,
                    next_actions=next_actions,
                    mode="multi_agent",
                    reasoning_summary=integration["summary"],
                    last_reflection=integration,
                )
            )

        _safe_log_multi_agent_event(
            execution_journal,
            event_type="integration",
            run=saved_run,
            status=integration["status"],
            payload=integration,
        )
        return {
            "success": True,
            "status": integration["status"],
            "run_id": saved_run.get("run_id"),
            "sync_ok": sync_ok,
            "integration": integration,
        }

    def log_plan(self, execution_journal: Any, run: Dict[str, Any]) -> None:
        _safe_log_multi_agent_event(
            execution_journal,
            event_type="plan",
            run=run,
            status=run.get("status", "queued"),
            payload={"subtask_count": len(run.get("subtasks", []))},
        )

    def _load_state(self) -> Dict[str, Any]:
        return self.load_state()

    def _save_state(self, state: Dict[str, Any]) -> None:
        with self._state_file_lock():
            self._save_state_unlocked(state)

    @contextmanager
    def _state_transaction(self) -> Iterator[Dict[str, Any]]:
        with self._state_file_lock():
            state = self._load_state_unlocked()
            yield state
            self._save_state_unlocked(state)

    @contextmanager
    def _state_file_lock(self) -> Iterator[None]:
        with self._thread_lock:
            self.queue_path.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = self._acquire_file_lock()
            try:
                yield
            finally:
                os.close(lock_fd)
                try:
                    self._lock_path.unlink()
                except FileNotFoundError:
                    pass

    def _acquire_file_lock(self) -> int:
        started_at = time.monotonic()
        while True:
            try:
                lock_fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(lock_fd, f"{os.getpid()} {time.time()}".encode("ascii", errors="ignore"))
                return lock_fd
            except FileExistsError:
                if self._lock_is_stale():
                    try:
                        self._lock_path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                if time.monotonic() - started_at >= LOCK_TIMEOUT_SECONDS:
                    raise TimeoutError(f"Timed out waiting for multi-agent queue lock: {self._lock_path}")
                time.sleep(LOCK_POLL_SECONDS)

    def _lock_is_stale(self) -> bool:
        try:
            return (time.time() - self._lock_path.stat().st_mtime) > LOCK_STALE_SECONDS
        except FileNotFoundError:
            return False

    def _load_state_unlocked(self) -> Dict[str, Any]:
        if not self.queue_path.exists():
            return {"version": 1, "runs": []}
        try:
            return json.loads(self.queue_path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "runs": []}

    def _save_state_unlocked(self, state: Dict[str, Any]) -> None:
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(state, ensure_ascii=False, indent=2)
        temp_path: Optional[Path] = None
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=str(self.queue_path.parent),
            prefix=f".{self.queue_path.name}.",
            suffix=".tmp",
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        try:
            os.replace(temp_path, self.queue_path)
        finally:
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass

    def _find_run(self, runs: List[Dict[str, Any]], run_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not run_id:
            return None
        for run in runs:
            if run.get("run_id") == run_id:
                return run
        return None

    def _find_subtask(self, run: Optional[Dict[str, Any]], subtask_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not run or not subtask_id:
            return None
        for subtask in run.get("subtasks", []):
            if subtask.get("subtask_id") == subtask_id:
                return subtask
        return None

    def _latest_run(self, runs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        return runs[-1] if runs else None

    def _latest_resumable_run(self, runs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for run in reversed(runs):
            if run.get("status") != "integrated":
                return run
        return None

    def _derive_run_status(self, run: Dict[str, Any]) -> str:
        statuses = [subtask.get("status") for subtask in run.get("subtasks", [])]
        if not statuses:
            return "failed"
        if all(status == "cancelled" for status in statuses):
            return "cancelled"
        if all(status == "skipped" for status in statuses):
            return "skipped"
        if any(status == "queued" for status in statuses):
            return "running"
        if any(status == "running" for status in statuses):
            return "running"
        if any(status == "failed" for status in statuses):
            return "completed_with_failures"
        if any(status == "cancelled" for status in statuses):
            return "completed_with_cancellations"
        if all(status in {"completed", "skipped"} for status in statuses):
            return "completed"
        return "running"

    def _summarize_run(self, run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not run:
            return None
        subtasks = run.get("subtasks", [])
        return {
            "run_id": run.get("run_id"),
            "parent_task_id": run.get("parent_task_id"),
            "status": run.get("status"),
            "subtask_count": len(subtasks),
            "queued": sum(1 for item in subtasks if item.get("status") == "queued"),
            "running": sum(1 for item in subtasks if item.get("status") == "running"),
            "completed": sum(1 for item in subtasks if item.get("status") == "completed"),
            "failed": sum(1 for item in subtasks if item.get("status") == "failed"),
            "cancelled": sum(1 for item in subtasks if item.get("status") == "cancelled"),
            "skipped": sum(1 for item in subtasks if item.get("status") == "skipped"),
            "subtasks": [
                {
                    "subtask_id": item.get("subtask_id"),
                    "role": item.get("role"),
                    "objective": item.get("objective"),
                    "status": item.get("status"),
                    "summary": ((item.get("result") or {}).get("summary") or "")[:240],
                }
                for item in subtasks
            ],
            "updated_at": run.get("updated_at"),
        }


def build_integration_result(run: Dict[str, Any]) -> Dict[str, Any]:
    subtasks = run.get("subtasks", [])
    completed = [item for item in subtasks if item.get("status") == "completed"]
    failed = [item for item in subtasks if item.get("status") == "failed"]
    cancelled = [item for item in subtasks if item.get("status") == "cancelled"]
    skipped = [item for item in subtasks if item.get("status") == "skipped"]
    conflicts = detect_result_conflicts(completed)

    status = "completed"
    if conflicts:
        status = "completed_with_conflicts"
    elif failed:
        status = "completed_with_failures"
    elif cancelled and completed:
        status = "completed_with_cancellations"
    elif cancelled and not completed:
        status = "cancelled"
    elif skipped and not completed:
        status = "skipped"

    summary_lines = [
        f"Multi-agent run {run.get('run_id')} integrated.",
        (
            f"Completed subtasks: {len(completed)}; failed subtasks: {len(failed)}; "
            f"cancelled subtasks: {len(cancelled)}; skipped subtasks: {len(skipped)}; "
            f"conflicts: {len(conflicts)}."
        ),
    ]
    for subtask in completed:
        result = subtask.get("result") or {}
        summary_lines.append(f"- {subtask.get('role')}: {result.get('summary', '')[:300]}")
    for subtask in failed:
        result = subtask.get("result") or {}
        summary_lines.append(f"- failed {subtask.get('role')}: {result.get('summary', '')[:300]}")

    next_actions: List[str] = []
    if conflicts:
        next_actions.append("resolve_multi_agent_conflicts")
    if failed:
        next_actions.append("retry_or_reassign_failed_subtasks")
    if cancelled:
        next_actions.append("review_cancelled_subtasks")
    if not next_actions:
        next_actions.append("verify_integrated_multi_agent_result")

    return {
        "status": status,
        "summary": "\n".join(summary_lines),
        "completed_count": len(completed),
        "failed_count": len(failed),
        "cancelled_count": len(cancelled),
        "skipped_count": len(skipped),
        "conflicts": conflicts,
        "next_actions": next_actions,
        "subtask_results": [
            {
                "subtask_id": item.get("subtask_id"),
                "role": item.get("role"),
                "status": item.get("status"),
                "result": item.get("result"),
            }
            for item in subtasks
        ],
    }


def _chunk_steps(steps: List[str], count: int) -> List[List[str]]:
    if count <= 1:
        return [steps]
    chunk_size = max(1, math.ceil(len(steps) / count))
    return [steps[index : index + chunk_size] for index in range(0, len(steps), chunk_size)]


def _roles_for_count(count: int) -> List[str]:
    roles = ["planner", "executor", "verifier", "researcher", "critic", "integrator"]
    return roles[:count]


def _build_subtask_objective(*, parent_objective: str, role: str, assigned_steps: List[str]) -> str:
    step_text = "; ".join(assigned_steps)
    if parent_objective:
        return f"{role}: {step_text} | parent_goal: {parent_objective}"
    return f"{role}: {step_text}"


def _default_tools_for_role(role: str) -> List[str]:
    return list(ROLE_TOOL_MAP.get(role, []))


def _normalize_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _value_hash(value: Any) -> str:
    return hashlib.md5(json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now().isoformat()


def _safe_log_multi_agent_event(
    execution_journal: Any,
    *,
    event_type: str,
    run: Dict[str, Any],
    status: str,
    payload: Dict[str, Any],
) -> None:
    log_fn = getattr(execution_journal, "log_multi_agent_event", None)
    if not callable(log_fn):
        return
    try:
        log_fn(
            event_type=event_type,
            run_id=run.get("run_id"),
            parent_task_id=run.get("parent_task_id"),
            status=status,
            subtask_count=len(run.get("subtasks", [])),
            payload=payload,
        )
    except Exception:
        return
