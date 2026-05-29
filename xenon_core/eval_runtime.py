from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence


CommandRunner = Callable[[Sequence[str], Path, int], Dict[str, Any]]


def _decode_output(byte_data: Optional[bytes]) -> str:
    """智能解码 subprocess 输出：UTF-8 → GBK → 系统默认(replace) → latin1(replace)"""
    if not byte_data:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return byte_data.decode(encoding)
        except UnicodeDecodeError:
            continue
    try:
        return byte_data.decode(sys.getdefaultencoding(), errors="replace")
    except Exception:
        return byte_data.decode("latin1", errors="replace")


def load_eval_scenarios(path: str | Path) -> List[Dict[str, Any]]:
    scenario_path = Path(path)
    payload = json.loads(scenario_path.read_text(encoding="utf-8"))
    scenarios = payload.get("scenarios", payload) if isinstance(payload, dict) else payload
    if not isinstance(scenarios, list):
        raise ValueError("eval scenario file must contain a list or a {'scenarios': [...]} object")

    seen_ids = set()
    normalized = []
    for index, scenario in enumerate(scenarios, 1):
        item = _normalize_scenario(scenario, index=index)
        if item["id"] in seen_ids:
            raise ValueError(f"duplicate eval scenario id: {item['id']}")
        seen_ids.add(item["id"])
        normalized.append(item)
    return normalized


def filter_eval_scenarios(
    scenarios: List[Dict[str, Any]],
    *,
    category: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    wanted_tags = {tag.strip() for tag in (tags or []) if tag and tag.strip()}
    result = []
    for scenario in scenarios:
        if category and scenario.get("category") != category:
            continue
        scenario_tags = set(scenario.get("tags") or [])
        if wanted_tags and not wanted_tags.issubset(scenario_tags):
            continue
        result.append(scenario)
    return result


def run_eval_suite(
    scenarios: List[Dict[str, Any]],
    *,
    project_root: str | Path,
    command_runner: Optional[CommandRunner] = None,
    fail_fast: bool = False,
) -> Dict[str, Any]:
    root = Path(project_root)
    runner = command_runner or run_command
    results = []
    started_at = datetime.now().isoformat()

    for scenario in scenarios:
        result = run_eval_scenario(scenario, project_root=root, command_runner=runner)
        results.append(result)
        if fail_fast and not result["passed"]:
            break

    metrics = aggregate_eval_metrics(results)
    return {
        "started_at": started_at,
        "finished_at": datetime.now().isoformat(),
        "metrics": metrics,
        "results": results,
    }


def run_eval_scenario(
    scenario: Dict[str, Any],
    *,
    project_root: str | Path,
    command_runner: CommandRunner,
) -> Dict[str, Any]:
    root = Path(project_root)
    command = resolve_command(scenario["command"], project_root=root)
    timeout_seconds = int(scenario.get("timeout_seconds") or 120)
    started = time.perf_counter()
    execution = command_runner(command, root, timeout_seconds)
    duration = time.perf_counter() - started

    output = f"{execution.get('stdout', '')}\n{execution.get('stderr', '')}"
    expected_exit_code = int(scenario.get("expected_exit_code", 0))
    required_output = list(scenario.get("required_output_contains") or [])
    forbidden_output = list(scenario.get("forbidden_output_contains") or [])

    checks = {
        "exit_code": execution.get("returncode") == expected_exit_code,
        "required_output": all(token in output for token in required_output),
        "forbidden_output": all(token not in output for token in forbidden_output),
    }
    passed = all(checks.values())

    return {
        "id": scenario["id"],
        "category": scenario["category"],
        "description": scenario.get("description", ""),
        "tags": scenario.get("tags", []),
        "weight": scenario.get("weight", 1.0),
        "command": command,
        "expected_exit_code": expected_exit_code,
        "returncode": execution.get("returncode"),
        "passed": passed,
        "checks": checks,
        "duration_seconds": round(duration, 3),
        "stdout_tail": _tail(execution.get("stdout", "")),
        "stderr_tail": _tail(execution.get("stderr", "")),
        "timed_out": bool(execution.get("timed_out")),
    }


def run_command(command: Sequence[str], project_root: Path, timeout_seconds: int) -> Dict[str, Any]:
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(project_root),
            capture_output=True,
            timeout=timeout_seconds,
            shell=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": _decode_output(completed.stdout),
            "stderr": _decode_output(completed.stderr),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as error:
        return {
            "returncode": 124,
            "stdout": _decode_output(error.stdout) if error.stdout else "",
            "stderr": _decode_output(error.stderr) if error.stderr else f"Timed out after {timeout_seconds}s",
            "timed_out": True,
        }


def resolve_command(command: Sequence[str], *, project_root: Path) -> List[str]:
    replacements = {
        "{python}": sys.executable,
        "{project_root}": str(project_root),
    }
    return [replacements.get(str(part), str(part)) for part in command]


def aggregate_eval_metrics(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(results)
    passed = sum(1 for item in results if item.get("passed"))
    failed = total - passed
    total_weight = sum(float(item.get("weight", 1.0)) for item in results)
    passed_weight = sum(float(item.get("weight", 1.0)) for item in results if item.get("passed"))
    durations = [float(item.get("duration_seconds") or 0) for item in results]

    categories: Dict[str, Dict[str, Any]] = {}
    for item in results:
        category = item.get("category", "uncategorized")
        bucket = categories.setdefault(category, {"total": 0, "passed": 0, "failed": 0})
        bucket["total"] += 1
        if item.get("passed"):
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1

    for bucket in categories.values():
        bucket["success_rate"] = _ratio(bucket["passed"], bucket["total"])

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "success_rate": _ratio(passed, total),
        "total_weight": round(total_weight, 3),
        "passed_weight": round(passed_weight, 3),
        "weighted_success_rate": _ratio(passed_weight, total_weight),
        "average_duration_seconds": round(sum(durations) / total, 3) if total else 0.0,
        "categories": categories,
        "failed_scenarios": [item["id"] for item in results if not item.get("passed")],
    }


def render_eval_report(report: Dict[str, Any]) -> str:
    metrics = report.get("metrics", {})
    lines = [
        "# Xenon Eval Report",
        "",
        f"- Started: {report.get('started_at', '')}",
        f"- Finished: {report.get('finished_at', '')}",
        f"- Success rate: {metrics.get('success_rate', 0)}",
        f"- Weighted success rate: {metrics.get('weighted_success_rate', 0)}",
        f"- Passed: {metrics.get('passed', 0)} / {metrics.get('total', 0)}",
        f"- Average duration seconds: {metrics.get('average_duration_seconds', 0)}",
        "",
        "## Categories",
    ]
    for category, bucket in sorted((metrics.get("categories") or {}).items()):
        lines.append(
            f"- {category}: {bucket.get('passed', 0)}/{bucket.get('total', 0)} "
            f"({bucket.get('success_rate', 0)})"
        )

    lines.extend(["", "## Scenarios"])
    for item in report.get("results", []):
        mark = "PASS" if item.get("passed") else "FAIL"
        lines.append(
            f"- [{mark}] {item.get('id')} ({item.get('category')}): "
            f"{item.get('description', '')} [{item.get('duration_seconds')}s]"
        )
        if not item.get("passed"):
            lines.append(f"  - returncode={item.get('returncode')} checks={item.get('checks')}")
            if item.get("stderr_tail"):
                lines.append(f"  - stderr_tail: `{item.get('stderr_tail')}`")
    return "\n".join(lines).strip() + "\n"


def write_eval_report(
    report: Dict[str, Any],
    *,
    output_dir: str | Path,
    basename: Optional[str] = None,
) -> Dict[str, str]:
    target_dir = Path(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = basename or datetime.now().strftime("eval_report_%Y%m%d_%H%M%S")
    json_path = target_dir / f"{safe_name}.json"
    md_path = target_dir / f"{safe_name}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_eval_report(report), encoding="utf-8")
    (target_dir / "latest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (target_dir / "latest.md").write_text(render_eval_report(report), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path)}


def _normalize_scenario(scenario: Dict[str, Any], *, index: int) -> Dict[str, Any]:
    if not isinstance(scenario, dict):
        raise ValueError(f"scenario #{index} must be an object")
    scenario_id = str(scenario.get("id") or "").strip()
    if not scenario_id:
        raise ValueError(f"scenario #{index} is missing id")
    command = scenario.get("command")
    if not isinstance(command, list) or not command:
        raise ValueError(f"scenario {scenario_id} must define a non-empty command list")
    return {
        "id": scenario_id,
        "category": str(scenario.get("category") or "uncategorized"),
        "description": str(scenario.get("description") or ""),
        "command": [str(part) for part in command],
        "expected_exit_code": int(scenario.get("expected_exit_code", 0)),
        "required_output_contains": list(scenario.get("required_output_contains") or []),
        "forbidden_output_contains": list(scenario.get("forbidden_output_contains") or []),
        "timeout_seconds": int(scenario.get("timeout_seconds") or 120),
        "weight": float(scenario.get("weight", 1.0)),
        "tags": list(scenario.get("tags") or []),
    }


def _ratio(numerator: float, denominator: float) -> float:
    if not denominator:
        return 0.0
    return round(float(numerator) / float(denominator), 4)


def _tail(text: str, *, max_chars: int = 1200) -> str:
    if not text:
        return ""
    normalized = text.strip()
    return normalized[-max_chars:]
