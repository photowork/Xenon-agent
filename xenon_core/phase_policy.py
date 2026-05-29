from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

MAIN_PHASES = {"plan", "act", "verify", "recover", "done", "idle"}
RECOVERY_MODES = {"none", "debug", "analyze", "reflect"}

LEGACY_PHASE_TO_MAIN = {
    "analyze": "plan",
    "locate": "plan",
    "edit": "act",
    "execute": "act",
    "integrate": "act",
    "deploy": "act",
    "maintenance": "act",
    "test": "verify",
    "verify": "verify",
    "debug": "recover",
    "reflect": "recover",
    "recover": "recover",
    "completed": "done",
    "complete": "done",
    "done": "done",
    "idle": "idle",
}


def normalize_recovery_mode(value: Any, default: str = "none") -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in RECOVERY_MODES else default


def normalize_phase_state(
    phase: Any,
    recovery_mode: Any = None,
    *,
    recovery: bool = False,
) -> Dict[str, str]:
    """Normalize old phase labels into progress phase + recovery mode.

    Main phase describes execution progress. recovery_mode describes the temporary
    cognitive/recovery tactic used inside the recover phase.
    """
    raw_phase = str(phase or "").strip().lower()
    mode = normalize_recovery_mode(recovery_mode)

    if raw_phase in MAIN_PHASES:
        main_phase = raw_phase
    else:
        main_phase = LEGACY_PHASE_TO_MAIN.get(raw_phase, "plan")

    if raw_phase in {"debug", "reflect"}:
        mode = raw_phase
    elif raw_phase == "analyze" and (recovery or mode != "none"):
        main_phase = "recover"
        mode = "analyze" if mode == "none" else mode
    elif main_phase != "recover":
        mode = "none"
    elif mode == "none":
        mode = "debug"

    return {"phase": main_phase, "recovery_mode": mode}


def normalize_execution_state(state: Optional[Dict[str, Any]], *, recovery: bool = False) -> Dict[str, Any]:
    data = dict(state or {})
    normalized = normalize_phase_state(
        data.get("phase", "plan"),
        data.get("recovery_mode"),
        recovery=recovery,
    )
    data.update(normalized)
    return data


def router_phase_for(phase: Any, recovery_mode: Any = None) -> str:
    normalized = normalize_phase_state(phase, recovery_mode)
    main_phase = normalized["phase"]
    mode = normalized["recovery_mode"]

    if main_phase == "recover":
        return mode if mode in {"debug", "analyze", "reflect"} else "debug"
    if main_phase == "plan":
        return "analyze"
    if main_phase == "act":
        return "edit"
    if main_phase == "verify":
        return "test"
    return "analyze"


def choose_recovery_mode(
    *,
    alert_types: Optional[Iterable[str]] = None,
    blockage_reason: Any = "",
    failure_detected: bool = False,
    lessons_reflect: bool = False,
) -> str:
    alerts = {str(alert or "").strip().lower() for alert in (alert_types or [])}
    blockage = str(blockage_reason or "").strip().lower()

    if "loop_detected" in alerts or lessons_reflect:
        return "reflect"
    if blockage in {"needs_context", "context_overflow", "missing_context", "needs_user_input"}:
        return "analyze"
    if any(token in blockage for token in ("evidence", "scope", "unknown", "missing")):
        return "analyze"
    if failure_detected or "tool_failure" in alerts or blockage:
        return "debug"
    return "analyze"
