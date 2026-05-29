from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_REQUIRED_MODULES = (
    "openai",
    "tiktoken",
    "watchdog",
    "playwright",
)


def _expected_venv_python(project_root: Path) -> Path:
    if os.name == "nt":
        return project_root / "venv" / "Scripts" / "python.exe"
    return project_root / "venv" / "bin" / "python"


def collect_runtime_health(
    project_root: Optional[Path] = None,
    required_modules: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    root = Path(project_root or Path.cwd()).resolve()
    executable = Path(sys.executable).resolve()
    expected_python = _expected_venv_python(root)
    module_entries: List[Dict[str, str]] = []

    for module_name in tuple(required_modules or DEFAULT_REQUIRED_MODULES):
        entry = {
            "name": module_name,
            "status": "missing",
            "error": "",
        }
        try:
            importlib.import_module(module_name)
            entry["status"] = "ok"
        except Exception as exc:
            entry["status"] = "missing"
            entry["error"] = f"{type(exc).__name__}: {exc}"
        module_entries.append(entry)

    ok_count = sum(1 for entry in module_entries if entry["status"] == "ok")

    return {
        "project_root": str(root),
        "python_executable": str(executable),
        "python_version": sys.version.split()[0],
        "expected_venv_python": str(expected_python),
        "expected_venv_exists": expected_python.exists(),
        "using_expected_venv": expected_python.exists() and executable == expected_python.resolve(),
        "module_checks": module_entries,
        "ok_count": ok_count,
        "missing_count": len(module_entries) - ok_count,
    }


def format_runtime_health_report(report: Dict[str, Any]) -> str:
    lines = [
        "[RUNTIME HEALTH]",
        f"- python: {report.get('python_executable')}",
        f"- version: {report.get('python_version')}",
        f"- expected venv python: {report.get('expected_venv_python')}",
        f"- using expected venv: {'yes' if report.get('using_expected_venv') else 'no'}",
        f"- dependency status: {report.get('ok_count', 0)} ok / {report.get('missing_count', 0)} missing",
    ]

    for entry in report.get("module_checks", []):
        suffix = ""
        if entry.get("error"):
            suffix = f" ({entry['error']})"
        lines.append(f"  - {entry.get('name')}: {entry.get('status')}{suffix}")

    return "\n".join(lines)


def format_tool_load_report(report: Optional[Dict[str, Any]], max_failures: int = 8) -> str:
    if not report:
        return "[TOOL LOAD REPORT]\n- unavailable"

    successes = report.get("successes", [])
    failures = report.get("failures", [])
    modules = report.get("module_names", [])
    lines = [
        "[TOOL LOAD REPORT]",
        f"- tools dir: {report.get('tools_dir', 'unknown')}",
        f"- module files scanned: {report.get('module_file_count', 0)}",
        f"- active modules: {len(modules)}",
        f"- manager instances: {len(successes)}",
        f"- callable tools: {report.get('tool_schema_count', 0)}",
        f"- load failures: {len(failures)}",
    ]

    if modules:
        lines.append(f"- active module names: {', '.join(modules[:12])}")

    if failures:
        lines.append("- failures:")
        for failure in failures[:max_failures]:
            module_name = failure.get("module_name") or Path(failure.get("path", "")).stem or "unknown"
            manager_name = failure.get("manager_class")
            target = f"{module_name}.{manager_name}" if manager_name else module_name
            lines.append(f"  - {target}: {failure.get('error', 'unknown error')}")

        remaining = len(failures) - min(len(failures), max_failures)
        if remaining > 0:
            lines.append(f"  - ... {remaining} more failure(s)")

    return "\n".join(lines)
