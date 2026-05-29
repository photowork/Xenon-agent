from __future__ import annotations

from typing import Any, Dict, Optional

from xenon_core.runtime_health import format_runtime_health_report, format_tool_load_report


def render_cli_startup_report(
    *,
    model: str,
    thinking_enabled: bool,
    streaming_enabled: bool,
    app_version: Optional[str] = None,
    context_status: Optional[Dict[str, Any]] = None,
    runtime_health: Optional[Dict[str, Any]] = None,
    tool_load_report: Optional[Dict[str, Any]] = None,
) -> str:
    lines = [
        "",
        " \033[96m██╗  ██╗███████╗███╗   ██╗ ██████╗ ███╗   ██╗\033[0m",
        " \033[96m╚██╗██╔╝██╔════╝████╗  ██║██╔═══██╗████╗  ██║\033[0m",
        "  \033[96m╚███╔╝ █████╗  ██╔██╗ ██║██║   ██║██╔██╗ ██║\033[0m",
        "  \033[96m██╔██╗ ██╔══╝  ██║╚██╗██║██║   ██║██║╚██╗██║\033[0m",
        " \033[96m██╔╝ ██╗███████╗██║ ╚████║╚██████╔╝██║ ╚████║\033[0m",
        " \033[96m╚═╝  ╚═╝╚══════╝╚═╝  ╚═══╝ ╚═════╝ ╚═╝  ╚═══╝\033[0m",
        "",
        "\033[38;2;132;87;16m=======================================================\033[0m",
        f"\033[38;2;211;161;83mModel: {model}\033[0m",
        *([f"\033[38;2;211;161;83mVersion: {app_version}\033[0m"] if app_version else []),
        f"\033[38;2;211;161;83m思考模式: {'已启用' if thinking_enabled else '已禁用'}\033[0m",
        f"\033[38;2;211;161;83m流式输出: {'已启用' if streaming_enabled else '已禁用'}\033[0m",
    ]

    lines.extend(_format_context_lines(context_status))
    lines.extend(
        [
            "\033[38;2;211;161;83m说明: 紫色文字为思维链，青色文字为最终回答\033[0m",
            "\033[38;2;211;161;83m中断: 按 Ctrl+C 可中断输出\033[0m",
            "\033[38;2;211;161;83mType 'quit' or 'exit' to exit\033[0m",
            "\033[38;2;132;87;16m=======================================================\033[0m",
            "",
        ]
    )

    if runtime_health:
        lines.append(format_runtime_health_report(runtime_health))
        lines.append("")

    if tool_load_report:
        lines.append(format_tool_load_report(tool_load_report))
        lines.append("")

    return "\n".join(lines)


def _format_context_lines(context_status: Optional[Dict[str, Any]]) -> list[str]:
    if not context_status:
        return ["\033[38;2;255;152;0m上下文管理: 已禁用\033[0m"]

    cleanup_count = context_status.get("cleanup_count", 0)
    token_counter_available = context_status.get("token_counter_available", False)
    return [
        "\033[38;2;211;161;83m上下文管理: 智能体主动管理（自动压缩已启用）\033[0m",
        f"\033[38;2;211;161;83mToken监控: {'已启用' if token_counter_available else '已禁用'}\033[0m",
        f"\033[38;2;211;161;83m清理次数: {cleanup_count}\033[0m",
    ]
