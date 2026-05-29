from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Type

from xenon_core.boot_report import render_cli_startup_report
from xenon_core.runtime_health import collect_runtime_health

logger = logging.getLogger(__name__)

DEFAULT_INPUT_PROMPT = "\033[38;2;247;154;215m你: \033[0m\033[38;2;247;154;215m"
DEFAULT_EXIT_COMMANDS = ("quit", "exit", "退出")


def run_interactive_agent_session(
    agent: Any,
    *,
    project_root: Path,
    model: str,
    thinking_enabled: bool,
    streaming_enabled: bool,
    interrupted_exception_cls: Type[BaseException],
    app_version: str | None = None,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., Any] = print,
    input_prompt: str = DEFAULT_INPUT_PROMPT,
    exit_commands: Optional[Sequence[str]] = None,
) -> None:
    agent.tool_manager.start_file_watcher()
    agent._setup_signal_handler()
    _print_startup_report(
        agent=agent,
        project_root=project_root,
        model=model,
        thinking_enabled=thinking_enabled,
        streaming_enabled=streaming_enabled,
        print_fn=print_fn,
        app_version=app_version,
    )

    normalized_exit_commands = {
        command.lower() for command in (exit_commands or DEFAULT_EXIT_COMMANDS)
    }

    try:
        while True:
            try:
                agent.interrupted = False
                user_input = _read_user_input(
                    input_fn=input_fn,
                    print_fn=print_fn,
                    prompt=input_prompt,
                )
                if user_input is None:
                    print_fn("\n再见!")
                    break

                if user_input.lower() in normalized_exit_commands:
                    print_fn("再见!")
                    break

                if not user_input:
                    continue

                # ── 自主执行命令 ──────────────────────────────
                if user_input.startswith("/auto"):
                    _handle_autonomous_command(
                        agent=agent,
                        user_input=user_input,
                        print_fn=print_fn,
                    )
                    continue

                if user_input == "/tick":
                    result = agent.autonomous_tick()
                    print_fn(f"\n[自主单步] 状态={result.get('status')} 原因={result.get('reason') or result.get('stop_reason') or '-'}")
                    continue

                # ── Phase 5: 交付闭环命令 ────────────────────────
                if user_input.startswith("/agents"):
                    _handle_multi_agent_command(
                        agent=agent,
                        user_input=user_input,
                        print_fn=print_fn,
                    )
                    continue

                if user_input.startswith("/deliver") or user_input.startswith("/delivery"):
                    _handle_delivery_command(agent=agent, print_fn=print_fn)
                    continue

                if user_input.startswith("/git"):
                    _handle_git_status_command(agent=agent, print_fn=print_fn)
                    continue
                # ────────────────────────────────────────────────

                agent.chat(user_input)

            except KeyboardInterrupt:
                print_fn("\n")
                agent.interrupted = False
                continue

            except interrupted_exception_cls:
                print_fn("\n\033[93m[操作已中断，返回输入]\033[0m\n")
                agent.interrupted = False
                continue

    except Exception as error:
        logger.error("主循环发生错误: %s", error)
        print_fn(f"\n\033[91m发生错误: {error}\033[0m")

    finally:
        print_fn("\n\033[38;2;211;161;83m正在清理资源...\033[0m")
        agent._restore_signal_handler()
        agent.tool_manager.stop_file_watcher()
        print_fn("\033[38;2;211;161;83m已退出\033[0m")


def _print_startup_report(
    *,
    agent: Any,
    project_root: Path,
    model: str,
    thinking_enabled: bool,
    streaming_enabled: bool,
    print_fn: Callable[..., Any],
    app_version: str | None = None,
) -> None:
    context_manager = getattr(agent, "context_manager", None)
    context_status = context_manager.get_context_status() if context_manager else None
    tool_load_report = agent.tool_manager.get_load_report()
    runtime_health = collect_runtime_health(project_root=project_root)
    print_fn(
        render_cli_startup_report(
            model=model,
            thinking_enabled=thinking_enabled,
            streaming_enabled=streaming_enabled,
            context_status=context_status,
            runtime_health=runtime_health,
            tool_load_report=tool_load_report,
            app_version=app_version,
        )
    )


def _read_user_input(
    *,
    input_fn: Callable[[str], str],
    print_fn: Callable[..., Any],
    prompt: str,
) -> Optional[str]:
    try:
        raw_input = input_fn(prompt)
        print_fn("\033[0m", end="")
    except EOFError:
        return None

    if raw_input is None:
        return ""
    return raw_input.strip()

def _handle_autonomous_command(
    *,
    agent: Any,
    user_input: str,
    print_fn: Callable[..., Any],
) -> None:
    """处理 /auto [N] 命令，触发自主执行循环。"""
    parts = user_input.strip().split()
    steps = 1
    if len(parts) > 1:
        try:
            steps = max(1, min(int(parts[1]), 10))
        except ValueError:
            print_fn(f"\033[93m用法: /auto [步数 1-10]，例如 /auto 3\033[0m")
            return

    print_fn(f"\n\033[38;2;147;197;253m[自主执行循环] 启动，最大步数={steps}...\033[0m")
    result = agent.run_autonomous_cycle(max_steps=steps)
    _print_autonomous_result(result, print_fn)


def _print_autonomous_result(result: Dict[str, Any], print_fn: Callable[..., Any]) -> None:
    """格式输出自主循环结果。"""
    final_status = result.get("final_status", "unknown")
    executed = result.get("executed_steps", 0)
    requested = result.get("requested_steps", 0)

    color_map = {
        "continued": "\033[38;2;147;197;253m",
        "stopped": "\033[38;2;255;213;128m",
        "blocked": "\033[91m",
        "paused": "\033[38;2;255;213;128m",
        "idle": "\033[90m",
    }
    color = color_map.get(final_status, "\033[0m")

    lines = [
        f"\n{color}── 自主循环结束 ──\033[0m",
        f"  请求步数: {requested}  |  实际执行: {executed}  |  最终状态: {color}{final_status}\033[0m",
    ]

    results = result.get("results", [])
    for i, tick in enumerate(results, 1):
        status_icon = "✓" if tick.get("status") == "continued" else "✗"
        phase = tick.get("phase", "?")
        lines.append(f"  [{status_icon}] 第{i}步 → 阶段={phase} 来源={tick.get('goal_source', '?')}")

    print_fn("\n".join(lines))


def _handle_multi_agent_command(
    *,
    agent: Any,
    user_input: str,
    print_fn: Callable[..., Any],
) -> None:
    parts = user_input.strip().split()
    action = parts[1].lower() if len(parts) > 1 else "status"
    count = _parse_multi_agent_count(parts[2:] if len(parts) > 2 else [], print_fn=print_fn)
    if count is None:
        return

    if action == "status":
        _print_multi_agent_status(agent.get_multi_agent_status(), print_fn)
        return
    if action == "plan":
        result = agent.plan_multi_agent_subtasks(max_subtasks=count)
        if not result.get("success"):
            print_fn(f"\033[93m[multi-agent] {result.get('reason', 'plan failed')}\033[0m")
            return
        _print_multi_agent_status({"active_run": result.get("run"), "runs": [result.get("run")]}, print_fn)
        return
    if action == "run":
        result = agent.run_multi_agent_cycle(max_subtasks=count)
        print_fn(
            f"\n\033[38;2;147;197;253m[multi-agent] run={result.get('run_id')} status={result.get('status')}\033[0m"
        )
        _print_multi_agent_status(agent.get_multi_agent_status(result.get("run_id")), print_fn)
        return

    print_fn("\033[93mUsage: /agents [status|plan|run] [subtasks 1-6]\033[0m")


def _parse_multi_agent_count(parts: Sequence[str], *, print_fn: Callable[..., Any]) -> Optional[int]:
    if not parts:
        return 2
    try:
        return max(1, min(int(parts[0]), 6))
    except ValueError:
        print_fn("\033[93mUsage: /agents [status|plan|run] [subtasks 1-6]\033[0m")
        return None


def _print_multi_agent_status(status: Dict[str, Any], print_fn: Callable[..., Any]) -> None:
    active = status.get("active_run")
    if not active:
        print_fn("\n[multi-agent] no queued runs")
        return

    lines = [
        "\n[multi-agent] status",
        (
            f"  run: {active.get('run_id')} | status: {active.get('status')} | "
            f"subtasks: {active.get('subtask_count')} | done: {active.get('completed')} | "
            f"failed: {active.get('failed')} | queued: {active.get('queued')}"
        ),
    ]
    for subtask in active.get("subtasks", []):
        lines.append(
            f"  - {subtask.get('role')} [{subtask.get('status')}]: {subtask.get('objective', '')[:90]}"
        )
        if subtask.get("summary"):
            lines.append(f"    result: {subtask.get('summary')[:120]}")
    print_fn("\n".join(lines))
