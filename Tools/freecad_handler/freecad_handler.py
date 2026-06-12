#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FreeCAD structured modeling tool for Xenon."""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


SUPPORTED_ACTIONS = {
    "box",
    "cylinder",
    "sphere",
    "cone",
    "torus",
    "line",
    "circle",
    "arc",
    "polyline",
    "rectangle",
    "create_sketch",
    "create_body",
    "pad",
    "pocket",
    "partdesign_linear_pattern",
    "partdesign_polar_pattern",
    "partdesign_mirror",
    "partdesign_thickness",
    "set_properties",
    "fuse",
    "cut",
    "common",
    "extrude",
    "revolve",
    "copy",
    "move",
    "rotate",
    "fillet",
    "chamfer",
    "linear_array",
    "polar_array",
    "mirror",
    "shell",
    "thread_helix",
    "thread",
    "create_assembly",
    "assembly_link",
    "techdraw_page",
    "techdraw_view",
    "techdraw_dimension",
    "remove",
}
ID_REQUIRED_ACTIONS = SUPPORTED_ACTIONS - {"remove", "set_properties"}
SUPPORTED_EXPORTS = {"step", "stp", "iges", "igs", "stl", "obj", "amf", "brep", "brp", "svg", "dxf"}
EXPORT_EXTENSIONS = {
    "step": ".step",
    "stp": ".stp",
    "iges": ".iges",
    "igs": ".igs",
    "stl": ".stl",
    "obj": ".obj",
    "amf": ".amf",
    "brep": ".brep",
    "brp": ".brp",
    "svg": ".svg",
    "dxf": ".dxf",
}
PREVIEW_ORIENTATIONS = {"axonometric", "isometric", "front", "rear", "left", "right", "top", "bottom"}
MAX_ACTIONS = 200
MAX_OBJECTS = 500
MAX_VISUAL_DELAY = 30.0
MAX_FINAL_HOLD_SECONDS = 3600.0
COLLABORATION_SELECTION_TOKENS = {
    "$selection",
    "$selected_object",
    "$selection1",
    "$selection2",
    "$selections",
    "$active_body",
    "$tip",
    "$selected_edges",
    "$selected_faces",
    "$selected_subelements",
}
COMMON_ACTION_FIELDS = {
    "op",
    "id",
    "label",
    "color",
    "transparency",
    "visible",
    "visual_delay",
    "continue_on_error",
}
ACTION_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "box": {
        "required": ["id", "length", "width", "height"],
        "optional": ["position", "direction"],
        "notes": "position is the corner where the box starts; direction is the height axis.",
        "example": {"op": "box", "id": "box", "length": 20, "width": 10, "height": 5, "position": [0, 0, 0]},
    },
    "cylinder": {
        "required": ["id", "radius", "height"],
        "optional": ["position", "direction", "angle"],
        "notes": "position is the center of the cylinder base.",
        "example": {"op": "cylinder", "id": "pin", "radius": 3, "height": 20, "position": [0, 0, 0]},
    },
    "cone": {
        "required": ["id", "radius1", "height"],
        "optional": ["radius2", "position", "direction", "angle"],
        "notes": "position is the center of the radius1 base.",
    },
    "sphere": {
        "required": ["id", "radius"],
        "optional": ["center", "axis", "angle1", "angle2", "angle3"],
        "notes": "Use center, not position.",
    },
    "torus": {
        "required": ["id", "radius1", "radius2"],
        "optional": ["center", "position", "axis", "angle1", "angle2", "angle3"],
        "notes": "center is canonical. position is accepted as an alias for center. Full-torus defaults are angle1=0, angle2=360, angle3=360.",
        "example": {"op": "torus", "id": "ring", "radius1": 10, "radius2": 2, "center": [30, 40, 50]},
    },
    "copy": {
        "required": ["id", "source"],
        "optional": ["keep_source"],
        "notes": "Use source, never target.",
    },
    "move": {
        "required": ["id", "source", "vector"],
        "optional": ["keep_source"],
        "notes": "Use source, never target. Creates a translated copy.",
    },
    "rotate": {
        "required": ["id", "source", "angle"],
        "optional": ["base", "axis", "keep_source"],
        "notes": "Use source, never target. angle is degrees.",
        "example": {"op": "rotate", "id": "turned", "source": "part", "base": [0, 0, 0], "axis": [0, 0, 1], "angle": 45},
    },
    "fillet": {
        "required": ["id", "source", "radius", "edges"],
        "optional": ["keep_source", "all_edges", "unsafe_allow_large_radius"],
        "notes": "edges is a non-empty array of unique 1-based integers. Use all_edges=true only deliberately.",
        "example": {"op": "fillet", "id": "rounded", "source": "box", "radius": 1, "edges": [1, 2, 3, 4]},
    },
    "chamfer": {
        "required": ["id", "source", "size", "edges"],
        "optional": ["keep_source", "all_edges", "unsafe_allow_large_size"],
        "notes": "edges is a non-empty array of unique 1-based integers. Use all_edges=true only deliberately.",
        "example": {"op": "chamfer", "id": "beveled", "source": "box", "size": 1, "edges": [1, 2]},
    },
    "extrude": {
        "required": ["id", "source"],
        "optional": ["vector", "length", "make_face", "keep_source"],
        "notes": "Provide exactly one of vector or length.",
    },
    "revolve": {
        "required": ["id", "source"],
        "optional": ["base", "axis", "angle", "make_face", "keep_source"],
    },
    "fuse": {
        "required": ["id", "base"],
        "optional": ["tool", "tools", "refine", "keep_sources"],
    },
    "cut": {
        "required": ["id", "base"],
        "optional": ["tool", "tools", "refine", "keep_sources"],
    },
    "common": {
        "required": ["id", "base"],
        "optional": ["tool", "tools", "refine", "keep_sources"],
    },
}
ACTION_CONTRACTS.update(
    {
        "line": {"required": ["id", "start", "end"], "optional": []},
        "circle": {"required": ["id", "radius"], "optional": ["center", "normal"]},
        "arc": {"required": ["id", "start", "mid", "end"], "optional": []},
        "polyline": {"required": ["id", "points"], "optional": ["closed", "face"]},
        "rectangle": {"required": ["id", "width", "height"], "optional": ["x", "y", "z", "face"]},
        "create_sketch": {
            "required": ["id", "geometry"],
            "optional": ["constraints", "body", "support", "placement"],
        },
        "create_body": {"required": ["id"], "optional": []},
        "pad": {
            "required": ["id", "body", "profile", "length"],
            "optional": ["reversed", "midplane", "type", "length2"],
        },
        "pocket": {
            "required": ["id", "body", "profile", "length"],
            "optional": ["reversed", "midplane", "type", "length2"],
        },
        "set_properties": {"required": ["object", "properties"], "optional": ["visible"]},
        "linear_array": {
            "required": ["id", "source", "count"],
            "optional": ["step", "vector", "fuse", "keep_source"],
        },
        "polar_array": {
            "required": ["id", "source", "count"],
            "optional": ["center", "axis", "angle", "fuse", "keep_source"],
        },
        "mirror": {
            "required": ["id", "source"],
            "optional": ["base", "normal", "include_source", "fuse", "keep_source"],
        },
        "shell": {
            "required": ["id", "source", "faces", "thickness"],
            "optional": ["inward", "tolerance", "keep_source"],
        },
        "thread_helix": {
            "required": ["id", "radius", "pitch", "height"],
            "optional": ["angle", "left_handed"],
        },
        "thread": {
            "required": ["id", "radius", "pitch", "height"],
            "optional": ["depth", "inward", "angle", "left_handed", "source", "mode", "keep_source"],
        },
        "partdesign_linear_pattern": {
            "required": ["id", "body"],
            "optional": ["original", "originals", "direction", "length", "occurrences", "count", "refine"],
        },
        "partdesign_polar_pattern": {
            "required": ["id", "body"],
            "optional": ["original", "originals", "axis", "angle", "occurrences", "count", "refine"],
        },
        "partdesign_mirror": {
            "required": ["id", "body"],
            "optional": ["original", "originals", "plane", "plane_reference"],
        },
        "partdesign_thickness": {
            "required": ["id", "body", "source", "faces", "thickness"],
            "optional": ["reversed", "mode", "join"],
        },
        "create_assembly": {"required": ["id"], "optional": ["create_joint_group"]},
        "assembly_link": {"required": ["id", "assembly", "source"], "optional": ["placement"]},
        "techdraw_page": {"required": ["id"], "optional": ["template_path", "scale"]},
        "techdraw_view": {
            "required": ["id", "page"],
            "optional": ["source", "sources", "direction", "x", "y", "scale", "rotation"],
        },
        "techdraw_dimension": {
            "required": ["id", "page", "view"],
            "optional": [
                "dimension_type",
                "measure_type",
                "references",
                "format_spec",
                "arbitrary",
                "equal_tolerance",
                "over_tolerance",
                "under_tolerance",
            ],
        },
        "remove": {"required": ["object"], "optional": []},
    }
)


def _success(message: str = "", **data: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {"success": True, "message": message}
    result.update(data)
    return result


def _failure(error: Exception | str, **data: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {"success": False, "error": str(error)}
    result.update(data)
    return result


def _version_key(path: Path) -> Tuple[int, ...]:
    numbers = re.findall(r"\d+", path.parent.parent.name)
    return tuple(int(item) for item in numbers) or (0,)


def discover_freecad() -> Dict[str, str]:
    """Find a FreeCAD installation and its bundled Python interpreter."""
    candidates: List[Path] = []
    configured = os.environ.get("FREECAD_PYTHON", "").strip()
    if configured:
        candidates.append(Path(configured))

    configured_home = os.environ.get("FREECAD_HOME", "").strip()
    if configured_home:
        home = Path(configured_home)
        candidates.extend([home / "bin" / "python.exe", home / "python.exe"])

    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root_value = os.environ.get(env_name, "").strip()
        if not root_value:
            continue
        root = Path(root_value)
        try:
            for directory in root.glob("FreeCAD*"):
                candidates.extend([directory / "bin" / "python.exe", directory / "python.exe"])
        except OSError:
            continue

    existing = sorted({item.resolve() for item in candidates if item.is_file()}, key=_version_key, reverse=True)
    if not existing:
        raise FileNotFoundError(
            "FreeCAD was not found. Install FreeCAD or set FREECAD_PYTHON to its bundled python.exe."
        )
    python_path = existing[0]
    bin_dir = python_path.parent
    gui_path = bin_dir / "freecad.exe"
    cmd_path = bin_dir / "freecadcmd.exe"
    return {
        "python_path": str(python_path),
        "bin_dir": str(bin_dir),
        "home_dir": str(bin_dir.parent),
        "gui_path": str(gui_path) if gui_path.is_file() else "",
        "cmd_path": str(cmd_path) if cmd_path.is_file() else "",
    }


class FreeCADAutomation:
    """Validate requests and execute them in FreeCAD's isolated Python process."""

    def __init__(self, workspace_root: Optional[str] = None):
        self.workspace_root = Path(workspace_root).resolve() if workspace_root else Path(__file__).resolve().parents[2]
        self.output_root = (self.workspace_root / "output" / "freecad").resolve()
        self.jobs_root = (self.output_root / ".jobs").resolve()
        self.worker_path = Path(__file__).with_name("freecad_worker.py").resolve()
        self.live_bridge_path = Path(__file__).with_name("freecad_live_bridge.py").resolve()
        self.live_config_path = (self.output_root / "live_bridge.json").resolve()

    def installation(self) -> Dict[str, str]:
        return discover_freecad()

    def _allowed_path(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.workspace_root)
            return True
        except ValueError:
            return False

    def resolve_path(
        self,
        value: str,
        *,
        default: Optional[Path] = None,
        allow_external_paths: bool = False,
        must_exist: bool = False,
    ) -> Path:
        text = str(value or "").strip()
        path = Path(text).expanduser() if text else default
        if path is None:
            raise ValueError("A file path is required")
        if not path.is_absolute():
            path = self.workspace_root / path
        path = path.resolve()
        if not allow_external_paths and not self._allowed_path(path):
            raise PermissionError(
                f"Path is outside the Xenon workspace: {path}. "
                "Set allow_external_paths=true only when explicitly intended."
            )
        if must_exist and not path.exists():
            raise FileNotFoundError(f"File does not exist: {path}")
        return path

    @staticmethod
    def _safe_document_name(name: str) -> str:
        text = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name or "").strip()).strip("_")
        return text or f"FreeCAD_{time.strftime('%Y%m%d_%H%M%S')}"

    def normalize_exports(
        self,
        exports: List[Any],
        document_name: str,
        *,
        allow_external_paths: bool,
    ) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for raw_spec in exports or []:
            spec = {"format": raw_spec} if isinstance(raw_spec, str) else dict(raw_spec)
            fmt = str(spec.get("format") or "").strip().lower().lstrip(".")
            if fmt not in SUPPORTED_EXPORTS:
                raise ValueError(f"Unsupported export format '{fmt}'. Supported: {sorted(SUPPORTED_EXPORTS)}")
            default_path = self.output_root / f"{document_name}{EXPORT_EXTENSIONS[fmt]}"
            file_path = self.resolve_path(
                str(spec.get("file_path") or ""),
                default=default_path,
                allow_external_paths=allow_external_paths,
            )
            normalized.append(
                {
                    "format": fmt,
                    "file_path": str(file_path),
                    "object_ids": list(spec.get("object_ids") or []),
                    "continue_on_error": bool(spec.get("continue_on_error", False)),
                }
            )
        return normalized

    def normalize_preview(
        self,
        preview: Optional[Dict[str, Any]],
        document_name: str,
        *,
        allow_external_paths: bool,
    ) -> Optional[Dict[str, Any]]:
        if not preview:
            return None
        spec = dict(preview)
        orientation = str(spec.get("orientation") or "axonometric").strip().lower()
        if orientation not in PREVIEW_ORIENTATIONS:
            raise ValueError(f"Unsupported orientation '{orientation}'")
        file_path = self.resolve_path(
            str(spec.get("file_path") or ""),
            default=self.output_root / f"{document_name}.png",
            allow_external_paths=allow_external_paths,
        )
        width = int(spec.get("width", 1200))
        height = int(spec.get("height", 900))
        if width < 64 or height < 64 or width > 8192 or height > 8192:
            raise ValueError("Preview width and height must be between 64 and 8192")
        return {
            "file_path": str(file_path),
            "orientation": orientation,
            "width": width,
            "height": height,
            "background": str(spec.get("background") or "Current"),
            "required": bool(spec.get("required", False)),
        }

    @staticmethod
    def validate_actions(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not isinstance(actions, list):
            raise TypeError("actions must be a list of dictionaries")
        if len(actions) > MAX_ACTIONS:
            raise ValueError(f"Too many actions: {len(actions)}. Maximum is {MAX_ACTIONS}.")
        result = []
        action_ids = set()
        for index, raw_action in enumerate(actions, 1):
            if not isinstance(raw_action, dict):
                raise TypeError(f"actions[{index - 1}] must be a dictionary")
            action = dict(raw_action)
            op = str(action.get("op") or "").strip().lower()
            if op not in SUPPORTED_ACTIONS:
                raise ValueError(f"Unsupported operation '{op}' at action {index}")
            action["op"] = op
            contract = ACTION_CONTRACTS.get(op)
            if "target" in action and op in {
                "copy",
                "move",
                "rotate",
                "fillet",
                "chamfer",
                "extrude",
                "revolve",
                "shell",
                "linear_array",
                "polar_array",
                "mirror",
            }:
                raise ValueError(
                    f"Action {index} ({op}) uses 'source', not 'target'. "
                    f"Replace target={action['target']!r} with source={action['target']!r}."
                )
            if contract:
                required = set(contract.get("required") or [])
                if op in {"fillet", "chamfer"} and action.get("all_edges") is True:
                    required.discard("edges")
                missing = [name for name in sorted(required) if name not in action or action[name] in (None, "")]
                if missing:
                    raise ValueError(
                        f"Action {index} ({op}) is missing required parameter(s): {', '.join(missing)}. "
                        f"{contract.get('notes', '')}".strip()
                    )
                allowed = COMMON_ACTION_FIELDS | set(contract.get("required") or []) | set(contract.get("optional") or [])
                unknown = sorted(set(action) - allowed)
                if unknown:
                    raise ValueError(
                        f"Action {index} ({op}) has unknown parameter(s): {', '.join(unknown)}. "
                        f"Allowed parameters: {', '.join(sorted(allowed))}."
                    )
            if op in ID_REQUIRED_ACTIONS:
                object_id = str(action.get("id") or "").strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", object_id):
                    raise ValueError(
                        f"Action {index} requires an id matching [A-Za-z_][A-Za-z0-9_]*"
                    )
                if object_id in action_ids:
                    raise ValueError(f"Duplicate action id '{object_id}' at action {index}")
                action_ids.add(object_id)
            if op in {"box", "cylinder", "cone", "sphere", "torus", "fillet", "chamfer"}:
                positive_fields = {
                    "box": ("length", "width", "height"),
                    "cylinder": ("radius", "height"),
                    "cone": ("radius1", "height"),
                    "sphere": ("radius",),
                    "torus": ("radius1", "radius2"),
                    "fillet": ("radius",),
                    "chamfer": ("size",),
                }[op]
                for field in positive_fields:
                    if float(action[field]) <= 0:
                        raise ValueError(f"Action {index} ({op}) parameter '{field}' must be greater than zero")
            if op == "torus" and "position" in action and "center" in action:
                raise ValueError(f"Action {index} (torus) must use either center or position, not both")
            if op in {"fillet", "chamfer"}:
                edges = action.get("edges")
                if action.get("all_edges") is True and edges:
                    raise ValueError(f"Action {index} ({op}) must use either edges or all_edges=true, not both")
                if action.get("all_edges") is not True:
                    if not isinstance(edges, list) or not edges:
                        raise ValueError(
                            f"Action {index} ({op}) requires a non-empty edges array of 1-based integers"
                        )
                    if any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in edges):
                        raise ValueError(
                            f"Action {index} ({op}) edges must contain only 1-based positive integers"
                        )
                    if len(set(edges)) != len(edges):
                        raise ValueError(f"Action {index} ({op}) edges must not contain duplicates")
                    if len(edges) > 64:
                        raise ValueError(f"Action {index} ({op}) accepts at most 64 explicitly selected edges")
            if op == "extrude" and ("vector" in action) == ("length" in action):
                raise ValueError(f"Action {index} (extrude) requires exactly one of vector or length")
            if op == "linear_array" and "step" in action and "vector" in action:
                raise ValueError(f"Action {index} (linear_array) must use either step or vector, not both")
            for vector_name in (
                "position",
                "center",
                "direction",
                "axis",
                "base",
                "vector",
                "step",
                "normal",
                "start",
                "mid",
                "end",
            ):
                if vector_name not in action:
                    continue
                # base in fuse/cut/common is an object name (string), not a vector
                if vector_name == "base" and op in {"fuse", "cut", "common"}:
                    continue
                vector = action[vector_name]
                if (
                    (op == "partdesign_linear_pattern" and vector_name == "direction")
                    or (op == "partdesign_polar_pattern" and vector_name == "axis")
                ) and (
                    str(vector).strip().lower() in {"x", "y", "z"} or isinstance(vector, dict)
                ):
                    continue
                if not isinstance(vector, (list, tuple)) or len(vector) != 3:
                    raise ValueError(
                        f"Action {index} ({op}) parameter '{vector_name}' must be a three-number vector"
                    )
                    try:
                        numeric_vector = [float(item) for item in vector]
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"Action {index} ({op}) parameter '{vector_name}' must contain only numbers"
                        ) from exc
                    if vector_name in {"direction", "axis", "normal", "vector", "step"} and not any(
                        abs(item) > 1e-12 for item in numeric_vector
                    ):
                        raise ValueError(
                            f"Action {index} ({op}) parameter '{vector_name}' must not be a zero vector"
                        )
            if "visual_delay" in action:
                visual_delay = float(action["visual_delay"])
                if visual_delay < 0 or visual_delay > MAX_VISUAL_DELAY:
                    raise ValueError(
                        f"Action {index} visual_delay must be between 0 and {MAX_VISUAL_DELAY} seconds"
                    )
                action["visual_delay"] = visual_delay
            result.append(action)
        return result

    @staticmethod
    def normalize_collaboration_action(action: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize one selection-aware action for the connected GUI session."""
        if not isinstance(action, dict):
            raise TypeError("action must be a dictionary")
        normalized = dict(action)
        op = str(normalized.get("op") or "").strip().lower()
        if op not in SUPPORTED_ACTIONS:
            raise ValueError(f"Unsupported operation '{op}'")
        normalized["op"] = op

        if op in ID_REQUIRED_ACTIONS and not str(normalized.get("id") or "").strip():
            normalized["id"] = f"Assist_{op.title().replace('_', '')}_{uuid.uuid4().hex[:8]}"

        source_operations = {
            "copy",
            "move",
            "rotate",
            "fillet",
            "chamfer",
            "extrude",
            "revolve",
            "shell",
            "linear_array",
            "polar_array",
            "mirror",
            "thread",
            "techdraw_view",
        }
        if op in source_operations and "source" not in normalized and "sources" not in normalized:
            normalized["source"] = "$selection"
        if op in {"set_properties", "remove"} and "object" not in normalized:
            normalized["object"] = "$selection"
        if op in {"fuse", "cut", "common"}:
            normalized.setdefault("base", "$selection1")
            if "tool" not in normalized and "tools" not in normalized:
                normalized["tool"] = "$selection2"
        if op in {"pad", "pocket"}:
            normalized.setdefault("body", "$active_body")
            normalized.setdefault("profile", "$selection")
        if op in {
            "partdesign_linear_pattern",
            "partdesign_polar_pattern",
            "partdesign_mirror",
        }:
            normalized.setdefault("body", "$active_body")
            if "original" not in normalized and "originals" not in normalized:
                normalized["original"] = "$selection"
        if op == "partdesign_thickness":
            normalized.setdefault("body", "$active_body")
            normalized.setdefault("source", "$selection")
            normalized.setdefault("faces", "$selected_faces")
        if op in {"fillet", "chamfer"} and "edges" not in normalized and normalized.get("all_edges") is not True:
            normalized["edges"] = "$selected_edges"
        if op == "shell" and "faces" not in normalized:
            normalized["faces"] = "$selected_faces"

        # Strict validation still runs before the request reaches FreeCAD. Selection
        # placeholders are represented by harmless sample values during validation
        # and restored afterward for resolution inside the active GUI session.
        validation_probe = dict(normalized)
        placeholder_fields = {}
        placeholder_samples = {
            "$selected_edges": [1],
            "$selected_faces": [1],
            "$selected_subelements": ["Edge1"],
            "$selections": ["Selection1"],
        }
        for field, value in list(validation_probe.items()):
            if isinstance(value, str) and value in placeholder_samples:
                placeholder_fields[field] = value
                validation_probe[field] = placeholder_samples[value]
        validated = FreeCADAutomation.validate_actions([validation_probe])[0]
        for field, placeholder in placeholder_fields.items():
            validated[field] = placeholder
        return validated

    @staticmethod
    def normalize_visual_settings(
        visual_mode: bool,
        step_delay: float,
        fit_after_each_step: bool,
        keep_gui_open: bool,
        final_hold_seconds: float,
    ) -> Dict[str, Any]:
        delay = float(step_delay)
        final_hold = float(final_hold_seconds)
        if delay < 0 or delay > MAX_VISUAL_DELAY:
            raise ValueError(f"step_delay must be between 0 and {MAX_VISUAL_DELAY} seconds")
        if final_hold < 0 or final_hold > MAX_FINAL_HOLD_SECONDS:
            raise ValueError(
                f"final_hold_seconds must be between 0 and {MAX_FINAL_HOLD_SECONDS} seconds"
            )
        return {
            "visual_mode": bool(visual_mode),
            "step_delay": delay,
            "fit_after_each_step": bool(fit_after_each_step),
            "keep_gui_open": bool(keep_gui_open),
            "final_hold_seconds": final_hold,
        }

    def execute(self, request: Dict[str, Any], timeout: int = 300) -> Dict[str, Any]:
        installation = self.installation()
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        job_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        job_dir = self.jobs_root / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        request_path = job_dir / "request.json"
        response_path = job_dir / "response.json"
        log_path = job_dir / "freecad.log"
        request_path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")

        environment = os.environ.copy()
        environment["PATH"] = installation["bin_dir"] + os.pathsep + environment.get("PATH", "")
        command = [installation["python_path"], str(self.worker_path), str(request_path), str(response_path)]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.workspace_root),
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1, min(int(timeout), 3600)),
                creationflags=creationflags,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return _failure(
                f"FreeCAD task timed out after {timeout} seconds",
                job_id=job_id,
                job_dir=str(job_dir),
                stdout=(exc.stdout or "")[-4000:],
                stderr=(exc.stderr or "")[-4000:],
            )
        log_path.write_text(
            f"command: {command!r}\nreturn_code: {completed.returncode}\n\n"
            f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}\n",
            encoding="utf-8",
        )
        if not response_path.exists():
            return _failure(
                "FreeCAD worker did not create a response",
                job_id=job_id,
                job_dir=str(job_dir),
                return_code=completed.returncode,
                stdout=completed.stdout[-4000:],
                stderr=completed.stderr[-4000:],
            )
        try:
            response = json.loads(response_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return _failure(exc, job_id=job_id, job_dir=str(job_dir), log_path=str(log_path))
        response["job_id"] = job_id
        response["job_dir"] = str(job_dir)
        response["log_path"] = str(log_path)
        response["return_code"] = completed.returncode
        if completed.returncode != 0 and response.get("success"):
            response["success"] = False
            response["error"] = (
                f"FreeCAD worker exited with code {completed.returncode} after creating a response. "
                f"Inspect the worker log: {log_path}"
            )
        return response

    @staticmethod
    def reserve_local_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            return int(server.getsockname()[1])

    def collaboration_addon_dir(self) -> Path:
        if os.name == "nt":
            app_data = os.environ.get("APPDATA")
            if not app_data:
                raise RuntimeError("APPDATA is unavailable; cannot locate the FreeCAD user addon directory")
            base = Path(app_data).resolve() / "FreeCAD"
            try:
                home_dir = str(self.installation().get("home_dir") or "")
            except Exception:
                home_dir = ""
            match = re.search(r"FreeCAD\s+(\d+)\.(\d+)", home_dir, re.IGNORECASE)
            if match:
                base = base / f"v{match.group(1)}-{match.group(2)}"
            else:
                version_dirs = sorted(
                    (path for path in base.glob("v*-*") if path.is_dir()),
                    key=lambda path: _version_key(path),
                    reverse=True,
                )
                if version_dirs:
                    base = version_dirs[0]
            return base / "Mod" / "XenonCollaboration"
        home = Path.home()
        candidates = [
            home / ".local" / "share" / "FreeCAD" / "Mod" / "XenonCollaboration",
            home / ".FreeCAD" / "Mod" / "XenonCollaboration",
        ]
        return candidates[0]

    def install_collaboration_addon(self, overwrite: bool = True) -> Dict[str, Any]:
        addon_dir = self.collaboration_addon_dir()
        init_path = addon_dir / "Init.py"
        init_gui_path = addon_dir / "InitGui.py"
        bootstrap_path = addon_dir / "XenonCollaboration.py"
        package_path = addon_dir / "package.xml"
        if not overwrite and (
            init_path.exists()
            or init_gui_path.exists()
            or bootstrap_path.exists()
            or package_path.exists()
        ):
            raise FileExistsError(f"Xenon collaboration addon already exists: {addon_dir}")
        addon_dir.mkdir(parents=True, exist_ok=True)
        init_path.write_text("# Xenon FreeCAD collaboration addon\n", encoding="utf-8")
        package_path.write_text(
            """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<package format="1" xmlns="https://wiki.freecad.org/Package_Metadata">
    <name>Xenon Collaboration</name>
    <description>Attach the active FreeCAD GUI session to Xenon's structured collaboration tools.</description>
    <version>1.0.0</version>
    <maintainer>Xenon</maintainer>
    <license>MIT</license>
    <content>
        <workbench>
            <name>XenonCollaboration</name>
            <subdirectory>./</subdirectory>
        </workbench>
    </content>
</package>
""",
            encoding="utf-8",
        )
        bootstrap = f'''"""Auto-attach the current FreeCAD GUI to Xenon's collaboration bridge."""

import importlib.util
import os
import sys

import FreeCAD
from PySide import QtCore


_BRIDGE_PATH = {str(self.live_bridge_path)!r}
_CONFIG_PATH = {str(self.live_config_path)!r}
_MODULE_NAME = "xenon_freecad_live_bridge"


class _XenonCollaborationWatcher(QtCore.QObject):
    def __init__(self):
        super().__init__()
        self.signature = None
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1000)
        QtCore.QTimer.singleShot(0, self.poll)

    def module(self):
        module = sys.modules.get(_MODULE_NAME)
        if module is not None:
            return module
        spec = importlib.util.spec_from_file_location(_MODULE_NAME, _BRIDGE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE_NAME] = module
        spec.loader.exec_module(module)
        return module

    def poll(self):
        if not os.path.isfile(_CONFIG_PATH) or not os.path.isfile(_BRIDGE_PATH):
            return
        stat = os.stat(_CONFIG_PATH)
        signature = (stat.st_mtime_ns, stat.st_size)
        module = self.module()
        existing = getattr(module, "XENON_LIVE_BRIDGE", None)
        active = existing is not None and not existing.stopping.is_set()
        if active and signature == self.signature:
            return
        if active:
            existing.stopping.set()
            existing.timer.stop()
        self.signature = signature
        module.start_bridge(_CONFIG_PATH)
        FreeCAD.Console.PrintMessage("[Xenon] Collaboration bridge attached to current session.\\n")


XENON_COLLABORATION_WATCHER = None


def start():
    global XENON_COLLABORATION_WATCHER
    if XENON_COLLABORATION_WATCHER is None:
        XENON_COLLABORATION_WATCHER = _XenonCollaborationWatcher()
    return XENON_COLLABORATION_WATCHER
'''
        bootstrap_path.write_text(bootstrap, encoding="utf-8")
        init_gui_path.write_text(
            "import XenonCollaboration\nXenonCollaboration.start()\n",
            encoding="utf-8",
        )
        return {
            "addon_dir": str(addon_dir),
            "init_path": str(init_path),
            "init_gui_path": str(init_gui_path),
            "bootstrap_path": str(bootstrap_path),
            "package_path": str(package_path),
            "restart_required": True,
        }

    def prepare_live_bridge(self, source_path: str = "") -> Dict[str, Any]:
        self.output_root.mkdir(parents=True, exist_ok=True)
        config = {
            "host": "127.0.0.1",
            "port": self.reserve_local_port(),
            "token": secrets.token_urlsafe(24),
            "worker_path": str(self.worker_path),
            "bridge_path": str(self.live_bridge_path),
            "source_path": source_path,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.live_config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        macro_path = self.output_root / "XenonLiveBridge.FCMacro"
        macro_path.write_text(
            "import importlib.util\n"
            f"_spec = importlib.util.spec_from_file_location('xenon_live_bridge', {str(self.live_bridge_path)!r})\n"
            "_module = importlib.util.module_from_spec(_spec)\n"
            "_spec.loader.exec_module(_module)\n"
            f"_module.start_bridge({str(self.live_config_path)!r})\n",
            encoding="utf-8",
        )
        return {**config, "config_path": str(self.live_config_path), "macro_path": str(macro_path)}

    def load_live_bridge(self) -> Dict[str, Any]:
        if not self.live_config_path.is_file():
            raise FileNotFoundError("No live bridge configuration exists. Start or prepare a live session first.")
        return json.loads(self.live_config_path.read_text(encoding="utf-8"))

    def send_live(self, payload: Dict[str, Any], timeout: int = 300) -> Dict[str, Any]:
        config = self.load_live_bridge()
        request = dict(payload)
        request["token"] = config["token"]
        request["bridge_timeout"] = max(1, min(int(timeout), 3600))
        with socket.create_connection(("127.0.0.1", int(config["port"])), timeout=min(timeout, 30)) as connection:
            connection.settimeout(max(1, min(int(timeout), 3600)))
            file = connection.makefile("rwb")
            file.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            file.flush()
            response_line = file.readline()
        if not response_line:
            raise RuntimeError("FreeCAD live bridge closed the connection without a response")
        return json.loads(response_line.decode("utf-8"))


class FreeCADToolManager:
    """
    Create, inspect, export, preview, and open FreeCAD models through structured actions.

    Load module ``freecad_handler`` before use. Length units are millimeters and
    angle units are degrees. Modeling runs in an isolated FreeCAD process so a
    FreeCAD failure does not terminate Xenon.
    """

    def __init__(self):
        self._automation = FreeCADAutomation()

    def describe_capabilities(self) -> Dict[str, Any]:
        """
        Describe supported FreeCAD actions, formats, units, and example arguments.

        :return: FreeCAD capability guide and a complete scenario example.
        """
        return _success(
            "FreeCAD structured modeling capability guide",
            load_module="freecad_handler",
            units={"length": "millimeter", "angle": "degree"},
            execution="Each task runs in an isolated FreeCAD bundled-Python process.",
            live_execution=(
                "start_live_session opens a persistent controlled GUI. "
                "prepare_current_session_bridge creates a macro that attaches Xenon to a GUI already opened by the user. "
                "For human-agent collaboration, inspect_live_context reads the user's current selection and "
                "execute_live_step performs one undoable step without closing, saving, or reframing FreeCAD."
            ),
            visual_execution=(
                "Set visual_mode=true to watch an independent FreeCAD window update after every action. "
                "Set keep_gui_open=true to hand the completed model to a detached viewer so Xenon returns immediately."
            ),
            actions=sorted(SUPPORTED_ACTIONS),
            action_contracts=ACTION_CONTRACTS,
            export_formats=sorted(SUPPORTED_EXPORTS),
            preview_orientations=sorted(PREVIEW_ORIENTATIONS),
            limits={"max_actions": MAX_ACTIONS, "max_objects": MAX_OBJECTS},
            advanced={
                "parameter_editing": "Inspect properties, then use set_properties to recompute existing features.",
                "human_collaboration": [
                    "install_collaboration_addon",
                    "inspect_live_context",
                    "execute_live_step",
                    "live_undo",
                    "live_redo",
                ],
                "selection_placeholders": sorted(COLLABORATION_SELECTION_TOKENS),
                "part_design": ["create_body", "create_sketch", "pad", "pocket", "partdesign_linear_pattern", "partdesign_polar_pattern", "partdesign_mirror", "partdesign_thickness"],
                "techdraw": ["techdraw_page", "techdraw_view", "techdraw_dimension"],
                "assembly": ["create_assembly", "assembly_link"],
                "vision": ["analyze_drawing_image", "create_model_from_drawing", "compare_visuals", "evaluate_visual_iteration"],
            },
            example={
                "document_name": "mounting_plate",
                "actions": [
                    {"op": "box", "id": "plate", "length": 100, "width": 60, "height": 5},
                    {"op": "cylinder", "id": "hole", "radius": 4, "height": 5, "position": [10, 10, 0]},
                    {"op": "cut", "id": "plate_with_hole", "base": "plate", "tool": "hole"},
                ],
                "save_path": "output/freecad/mounting_plate.FCStd",
                "exports": ["step", "stl"],
                "preview": {"orientation": "axonometric"},
                "visual_mode": True,
                "step_delay": 1.0,
            },
        )

    def describe_action(self, action: str) -> Dict[str, Any]:
        """Describe the exact parameter contract for one FreeCAD action."""
        op = str(action or "").strip().lower()
        if op not in SUPPORTED_ACTIONS:
            return _failure(f"Unsupported operation '{op}'", supported_actions=sorted(SUPPORTED_ACTIONS))
        contract = ACTION_CONTRACTS.get(op)
        if contract:
            return _success(f"Parameter contract for {op}", action=op, contract=contract)
        return _success(
            f"{op} is supported but does not yet have a strict published contract",
            action=op,
            contract={"required": ["id"] if op in ID_REQUIRED_ACTIONS else [], "notes": "Consult the README."},
        )

    def validate_scenario(self, actions: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Validate and normalize actions without starting FreeCAD."""
        try:
            normalized = self._automation.validate_actions(actions)
            warnings = []
            for index, action in enumerate(normalized, 1):
                if action["op"] in {"fillet", "chamfer", "shell"}:
                    warnings.append(
                        {
                            "index": index,
                            "op": action["op"],
                            "message": "Inspect object topology first and prefer a small explicit edge/face selection.",
                        }
                    )
                if action["op"] in {"fuse", "cut", "common"}:
                    warnings.append(
                        {
                            "index": index,
                            "op": action["op"],
                            "message": "Boolean success depends on valid intersecting source solids.",
                        }
                    )
            return _success(
                "Scenario parameters are valid; FreeCAD geometry execution has not been attempted",
                actions=normalized,
                warnings=warnings,
            )
        except Exception as exc:
            return _failure(exc)

    def status(self, verify_worker: bool = True, timeout: int = 30) -> Dict[str, Any]:
        """
        Detect FreeCAD and optionally verify its isolated Python worker.

        :param verify_worker: Run a lightweight FreeCAD import/version probe.
        :param timeout: Worker timeout in seconds.
        :return: FreeCAD installation and worker status.
        """
        try:
            installation = self._automation.installation()
            if not verify_worker:
                return _success("FreeCAD installation found", installation=installation)
            result = self._automation.execute({"command": "status"}, timeout)
            result["installation"] = installation
            return result
        except Exception as exc:
            return _failure(exc)

    def execute_scenario(
        self,
        actions: List[Dict[str, Any]],
        document_name: str = "FreeCADScene",
        source_path: str = "",
        save_path: str = "",
        exports: Optional[List[Any]] = None,
        preview: Optional[Dict[str, Any]] = None,
        visual_mode: bool = False,
        step_delay: float = 1.0,
        fit_after_each_step: bool = True,
        keep_gui_open: bool = False,
        final_hold_seconds: float = 2.0,
        overwrite: bool = False,
        allow_external_paths: bool = False,
        timeout: int = 300,
    ) -> Dict[str, Any]:
        """
        Execute an ordered FreeCAD modeling scene, optionally showing every step in a live FreeCAD window.

        Common actions include primitives and booleans, parameter updates,
        Part Design Body/Sketch/Pad/Pocket, arrays, mirror, shell, helix thread
        paths, assembly links, and TechDraw pages/views/dimensions.

        :param actions: Ordered structured modeling action dictionaries.
        :param document_name: New FreeCAD document name and default output basename.
        :param source_path: Optional existing FCStd document to modify.
        :param save_path: Optional FCStd output path. Defaults to output/freecad/<document_name>.FCStd.
        :param exports: Export formats or dictionaries, for example ["step","stl"] or [{"format":"step","file_path":"output/a.step"}].
        :param preview: Optional PNG preview settings with file_path, orientation, width, height, background, and required.
        :param visual_mode: Show an independent FreeCAD window and refresh it after every action.
        :param step_delay: Seconds to pause after each visible action; each action may override this with visual_delay.
        :param fit_after_each_step: Fit and show the model axonometrically after each visible action.
        :param keep_gui_open: Keep the completed model open in a detached viewer while the Xenon tool call returns immediately.
        :param final_hold_seconds: Seconds to keep the visual window open after completion when keep_gui_open is false.
        :param overwrite: Allow replacing existing output files.
        :param allow_external_paths: Allow reading or writing outside the Xenon workspace.
        :param timeout: FreeCAD worker timeout in seconds, maximum 3600.
        :return: Per-action results, output paths, model tree, dimensions, volume, area, and job log.
        """
        try:
            safe_name = self._automation._safe_document_name(document_name)
            normalized_actions = self._automation.validate_actions(actions)
            source = ""
            if source_path:
                source = str(
                    self._automation.resolve_path(
                        source_path,
                        allow_external_paths=allow_external_paths,
                        must_exist=True,
                    )
                )
            default_save = self._automation.output_root / f"{safe_name}.FCStd"
            normalized_save = str(
                self._automation.resolve_path(
                    save_path,
                    default=default_save,
                    allow_external_paths=allow_external_paths,
                )
            )
            normalized_exports = self._automation.normalize_exports(
                exports or [],
                safe_name,
                allow_external_paths=allow_external_paths,
            )
            normalized_preview = self._automation.normalize_preview(
                preview,
                safe_name,
                allow_external_paths=allow_external_paths,
            )
            visual_settings = self._automation.normalize_visual_settings(
                visual_mode,
                step_delay,
                fit_after_each_step,
                keep_gui_open,
                final_hold_seconds,
            )
            request = {
                "command": "scenario",
                "document_name": safe_name,
                "source_path": source,
                "actions": normalized_actions,
                "save_path": normalized_save,
                "exports": normalized_exports,
                "preview": normalized_preview,
                **visual_settings,
                "overwrite": bool(overwrite),
                "max_objects": MAX_OBJECTS,
            }
            return self._automation.execute(request, timeout)
        except Exception as exc:
            return _failure(exc)

    def inspect_document(
        self,
        file_path: str,
        allow_external_paths: bool = False,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """
        Inspect an existing FCStd document and return its object tree and shape measurements.

        :param file_path: Existing FCStd file path.
        :param allow_external_paths: Allow reading outside the Xenon workspace.
        :param timeout: FreeCAD worker timeout in seconds.
        :return: Document metadata, object tree, volume, area, and bounding boxes.
        """
        try:
            source = self._automation.resolve_path(
                file_path,
                allow_external_paths=allow_external_paths,
                must_exist=True,
            )
            return self._automation.execute({"command": "inspect", "source_path": str(source)}, timeout)
        except Exception as exc:
            return _failure(exc)

    def inspect_object_properties(
        self,
        file_path: str,
        object_id: str,
        property_names: Optional[List[str]] = None,
        allow_external_paths: bool = False,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """Inspect editable properties of one object in an existing FCStd document."""
        try:
            source = self._automation.resolve_path(
                file_path,
                allow_external_paths=allow_external_paths,
                must_exist=True,
            )
            return self._automation.execute(
                {
                    "command": "properties",
                    "source_path": str(source),
                    "object": object_id,
                    "property_names": property_names or [],
                },
                timeout,
            )
        except Exception as exc:
            return _failure(exc)

    def inspect_object_topology(
        self,
        file_path: str,
        object_id: str,
        include_faces: bool = True,
        allow_external_paths: bool = False,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """List an object's 1-based edge/face indexes, sizes, positions, and references."""
        try:
            source = self._automation.resolve_path(
                file_path,
                allow_external_paths=allow_external_paths,
                must_exist=True,
            )
            return self._automation.execute(
                {
                    "command": "topology",
                    "source_path": str(source),
                    "object": object_id,
                    "include_faces": bool(include_faces),
                },
                timeout,
            )
        except Exception as exc:
            return _failure(exc)

    def export_document(
        self,
        file_path: str,
        exports: List[Any],
        overwrite: bool = False,
        allow_external_paths: bool = False,
        timeout: int = 300,
    ) -> Dict[str, Any]:
        """
        Export objects from an existing FCStd document to STEP, STL, OBJ, SVG, DXF, or related formats.

        :param file_path: Existing FCStd file path.
        :param exports: Export formats or dictionaries; dictionaries may include file_path and object_ids.
        :param overwrite: Allow replacing existing output files.
        :param allow_external_paths: Allow reading or writing outside the Xenon workspace.
        :param timeout: FreeCAD worker timeout in seconds.
        :return: Export paths and document inspection.
        """
        try:
            source = self._automation.resolve_path(
                file_path,
                allow_external_paths=allow_external_paths,
                must_exist=True,
            )
            name = self._automation._safe_document_name(source.stem)
            normalized_exports = self._automation.normalize_exports(
                exports,
                name,
                allow_external_paths=allow_external_paths,
            )
            request = {
                "command": "scenario",
                "document_name": name,
                "source_path": str(source),
                "actions": [],
                "exports": normalized_exports,
                "overwrite": bool(overwrite),
                "max_objects": MAX_OBJECTS,
            }
            return self._automation.execute(request, timeout)
        except Exception as exc:
            return _failure(exc)

    def render_preview(
        self,
        file_path: str,
        output_path: str = "",
        orientation: str = "axonometric",
        width: int = 1200,
        height: int = 900,
        background: str = "Current",
        overwrite: bool = False,
        allow_external_paths: bool = False,
        timeout: int = 180,
    ) -> Dict[str, Any]:
        """
        Render a PNG preview of an existing FCStd document using FreeCAD GUI in an isolated process.

        :param file_path: Existing FCStd file path.
        :param output_path: PNG output path; defaults to output/freecad/<document>.png.
        :param orientation: axonometric, front, rear, left, right, top, or bottom.
        :param width: Image width in pixels.
        :param height: Image height in pixels.
        :param background: FreeCAD saveImage background mode, normally Current, White, or Transparent.
        :param overwrite: Allow replacing an existing preview.
        :param allow_external_paths: Allow reading or writing outside the Xenon workspace.
        :param timeout: FreeCAD worker timeout in seconds.
        :return: Preview path or a clear preview error.
        """
        try:
            source = self._automation.resolve_path(
                file_path,
                allow_external_paths=allow_external_paths,
                must_exist=True,
            )
            preview = self._automation.normalize_preview(
                {
                    "file_path": output_path,
                    "orientation": orientation,
                    "width": width,
                    "height": height,
                    "background": background,
                    "required": True,
                },
                self._automation._safe_document_name(source.stem),
                allow_external_paths=allow_external_paths,
            )
            request = {
                "command": "scenario",
                "source_path": str(source),
                "actions": [],
                "preview": preview,
                "overwrite": bool(overwrite),
                "max_objects": MAX_OBJECTS,
            }
            return self._automation.execute(request, timeout)
        except Exception as exc:
            return _failure(exc)

    def open_in_gui(
        self,
        file_path: str,
        allow_external_paths: bool = False,
    ) -> Dict[str, Any]:
        """
        Open an existing FCStd document in the interactive FreeCAD GUI.

        :param file_path: Existing FCStd file path.
        :param allow_external_paths: Allow opening a file outside the Xenon workspace.
        :return: Started FreeCAD process information.
        """
        try:
            source = self._automation.resolve_path(
                file_path,
                allow_external_paths=allow_external_paths,
                must_exist=True,
            )
            installation = self._automation.installation()
            gui_path = installation.get("gui_path")
            if not gui_path:
                raise FileNotFoundError("FreeCAD GUI executable was not found")
            process = subprocess.Popen(
                [gui_path, str(source)],
                cwd=str(source.parent),
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            return _success("FreeCAD GUI started", file_path=str(source), process_id=process.pid, gui_path=gui_path)
        except Exception as exc:
            return _failure(exc)

    def prepare_current_session_bridge(
        self,
        source_path: str = "",
        allow_external_paths: bool = False,
        auto_attach_timeout: int = 5,
    ) -> Dict[str, Any]:
        """
        Prepare a macro that connects Xenon to a FreeCAD GUI session already open by the user.

        Run the returned XenonLiveBridge.FCMacro once from FreeCAD's Macro dialog.
        The current active document then remains open and receives live actions.
        """
        try:
            source = ""
            if source_path:
                source = str(
                    self._automation.resolve_path(
                        source_path,
                        allow_external_paths=allow_external_paths,
                        must_exist=True,
                    )
                )
            bridge = self._automation.prepare_live_bridge(source)
            deadline = time.monotonic() + max(0, min(int(auto_attach_timeout), 30))
            last_error = ""
            while time.monotonic() < deadline:
                try:
                    status = self._automation.send_live({"command": "status"}, timeout=2)
                    if status.get("success"):
                        return _success(
                            "Live bridge prepared and automatically attached to the current FreeCAD session.",
                            attached=True,
                            status=status,
                            **bridge,
                        )
                    last_error = str(status.get("error") or status)
                except Exception as exc:
                    last_error = str(exc)
                time.sleep(0.2)
            return _success(
                "Live bridge prepared. An installed Xenon collaboration addon will attach automatically; "
                "otherwise run the returned macro once inside the currently open FreeCAD session.",
                attached=False,
                auto_attach_error=last_error,
                **bridge,
            )
        except Exception as exc:
            return _failure(exc)

    def install_collaboration_addon(self, overwrite: bool = True) -> Dict[str, Any]:
        """
        Install a small FreeCAD user addon that automatically attaches future GUI sessions.

        Restart FreeCAD once after installation. After that, prepare_current_session_bridge
        can connect to a manually opened FreeCAD session without running a macro each time.
        """
        try:
            result = self._automation.install_collaboration_addon(bool(overwrite))
            return _success(
                "Xenon collaboration addon installed. Restart FreeCAD once to enable automatic attachment.",
                **result,
            )
        except Exception as exc:
            return _failure(exc)

    def start_live_session(
        self,
        source_path: str = "",
        allow_external_paths: bool = False,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        """
        Start a persistent visible FreeCAD GUI session controlled through the live bridge.

        Unlike visual_mode, this call returns while the same FreeCAD document remains
        open. Later execute_live_scenario calls modify that active document in place.
        """
        try:
            source = ""
            if source_path:
                source = str(
                    self._automation.resolve_path(
                        source_path,
                        allow_external_paths=allow_external_paths,
                        must_exist=True,
                    )
                )
            bridge = self._automation.prepare_live_bridge(source)
            installation = self._automation.installation()
            gui_path = installation.get("gui_path")
            if not gui_path:
                raise FileNotFoundError("FreeCAD GUI executable was not found")
            process = subprocess.Popen(
                [gui_path, str(self._automation.live_bridge_path), "--pass", bridge["config_path"]],
                cwd=str(self._automation.workspace_root),
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            deadline = time.monotonic() + max(1, min(int(timeout), 120))
            last_error = ""
            while time.monotonic() < deadline:
                try:
                    status = self._automation.send_live({"command": "status"}, timeout=5)
                    return _success(
                        "Persistent FreeCAD live session started",
                        process_id=process.pid,
                        bridge=bridge,
                        status=status,
                    )
                except Exception as exc:
                    last_error = str(exc)
                    time.sleep(0.25)
            raise TimeoutError(f"FreeCAD live bridge did not become ready: {last_error}")
        except Exception as exc:
            return _failure(exc)

    def live_status(self, timeout: int = 10) -> Dict[str, Any]:
        """Check whether the persistent/current-session FreeCAD bridge is reachable."""
        try:
            return self._automation.send_live({"command": "status"}, timeout)
        except Exception as exc:
            return _failure(exc)

    def inspect_live_context(
        self,
        include_document: bool = True,
        include_selection_properties: bool = False,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        """
        Inspect the user's active FreeCAD document, active Body, and current object/edge/face selection.

        Call this before helping with one modeling step. The returned selection can
        be referenced by execute_live_step using $selection, $selected_edges,
        $selected_faces, $active_body, and related placeholders.
        """
        try:
            return self._automation.send_live(
                {
                    "command": "context",
                    "include_document": bool(include_document),
                    "include_selection_properties": bool(include_selection_properties),
                },
                timeout,
            )
        except Exception as exc:
            return _failure(exc)

    def inspect_live_object_properties(
        self,
        object_id: str,
        property_names: Optional[List[str]] = None,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        """Inspect editable properties of an object in the connected active document."""
        try:
            return self._automation.send_live(
                {
                    "command": "properties",
                    "object": object_id,
                    "property_names": property_names or [],
                },
                timeout,
            )
        except Exception as exc:
            return _failure(exc)

    def inspect_live_object_topology(
        self,
        object_id: str,
        include_faces: bool = True,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        """List 1-based edge/face indexes for an object in the connected active document."""
        try:
            return self._automation.send_live(
                {"command": "topology", "object": object_id, "include_faces": bool(include_faces)},
                timeout,
            )
        except Exception as exc:
            return _failure(exc)

    def execute_live_scenario(
        self,
        actions: List[Dict[str, Any]],
        save_path: str = "",
        overwrite: bool = False,
        allow_external_paths: bool = False,
        timeout: int = 300,
    ) -> Dict[str, Any]:
        """
        Modify the active document in the connected FreeCAD GUI without closing it.

        Use set_properties to edit existing feature parameters instead of rebuilding.
        """
        try:
            normalized_actions = self._automation.validate_actions(actions)
            normalized_save = ""
            if save_path:
                normalized_save = str(
                    self._automation.resolve_path(
                        save_path,
                        allow_external_paths=allow_external_paths,
                    )
                )
            return self._automation.send_live(
                {
                    "command": "scenario",
                    "actions": normalized_actions,
                    "save_path": normalized_save,
                    "overwrite": bool(overwrite),
                    "max_objects": MAX_OBJECTS,
                    "visual_mode": True,
                    "step_delay": 0,
                },
                timeout,
            )
        except Exception as exc:
            return _failure(exc)

    def execute_live_step(
        self,
        action: Dict[str, Any],
        select_result: bool = True,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """
        Perform one undoable modeling step in the user's current FreeCAD session.

        The GUI remains open, the document is not automatically saved, and the
        current camera is preserved. Missing source/object references default to
        the user's selection where unambiguous. Selection placeholders include
        $selection, $selection1, $selection2, $selections, $selected_edges,
        $selected_faces, $selected_subelements, $active_body, and $tip.

        Examples:
        {"op": "fillet", "radius": 2}
        {"op": "set_properties", "properties": {"Length": 35}}
        {"op": "cut"}
        """
        try:
            normalized = self._automation.normalize_collaboration_action(action)
            return self._automation.send_live(
                {
                    "command": "collaboration_step",
                    "action": normalized,
                    "select_result": bool(select_result),
                    "max_objects": MAX_OBJECTS,
                    "visual_mode": False,
                },
                timeout,
            )
        except Exception as exc:
            return _failure(exc)

    def live_undo(self, timeout: int = 30) -> Dict[str, Any]:
        """Undo the latest transaction in the connected FreeCAD GUI and leave it open."""
        try:
            return self._automation.send_live({"command": "undo"}, timeout)
        except Exception as exc:
            return _failure(exc)

    def live_redo(self, timeout: int = 30) -> Dict[str, Any]:
        """Redo the latest undone transaction in the connected FreeCAD GUI and leave it open."""
        try:
            return self._automation.send_live({"command": "redo"}, timeout)
        except Exception as exc:
            return _failure(exc)

    def render_live_preview(
        self,
        output_path: str = "",
        orientation: str = "axonometric",
        width: int = 1200,
        height: int = 900,
        background: str = "Current",
        overwrite: bool = True,
        allow_external_paths: bool = False,
        timeout: int = 60,
    ) -> Dict[str, Any]:
        """Render the connected active FreeCAD document without closing its GUI."""
        try:
            preview = self._automation.normalize_preview(
                {
                    "file_path": output_path,
                    "orientation": orientation,
                    "width": width,
                    "height": height,
                    "background": background,
                    "required": True,
                },
                "live_preview",
                allow_external_paths=allow_external_paths,
            )
            return self._automation.send_live(
                {"command": "preview", "preview": preview, "overwrite": bool(overwrite)},
                timeout,
            )
        except Exception as exc:
            return _failure(exc)

    def stop_live_bridge(self, timeout: int = 10) -> Dict[str, Any]:
        """Stop Xenon's live bridge while leaving the user's FreeCAD GUI open."""
        try:
            return self._automation.send_live({"command": "stop_bridge"}, timeout)
        except Exception as exc:
            return _failure(exc)

    def analyze_drawing_image(
        self,
        image_path: str,
        annotated_path: str = "",
        allow_external_paths: bool = False,
    ) -> Dict[str, Any]:
        """
        Detect OCR dimensions, lines, and circles in an engineering drawing image.

        The result is intended for an agent to turn into a reviewed modeling
        scenario; ambiguous dimensions are reported instead of silently guessed.
        """
        try:
            import cv2
            import numpy as np

            source = self._automation.resolve_path(
                image_path,
                allow_external_paths=allow_external_paths,
                must_exist=True,
            )
            image = cv2.imread(str(source))
            if image is None:
                raise ValueError(f"OpenCV could not read image: {source}")
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray, 60, 180)
            raw_lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80, minLineLength=30, maxLineGap=8)
            raw_circles = cv2.HoughCircles(
                gray,
                cv2.HOUGH_GRADIENT,
                dp=1.2,
                minDist=20,
                param1=100,
                param2=30,
                minRadius=3,
                maxRadius=0,
            )
            lines = []
            if raw_lines is not None:
                for x1, y1, x2, y2 in raw_lines[:, 0][:500]:
                    lines.append({"start": [int(x1), int(y1)], "end": [int(x2), int(y2)]})
                    cv2.line(image, (x1, y1), (x2, y2), (0, 180, 0), 1)
            circles = []
            if raw_circles is not None:
                for x, y, radius in np.round(raw_circles[0, :100]).astype(int):
                    circles.append({"center": [int(x), int(y)], "radius_pixels": int(radius)})
                    cv2.circle(image, (x, y), radius, (255, 0, 255), 2)
            ocr_text = ""
            ocr_error = ""
            try:
                import pytesseract

                ocr_text = pytesseract.image_to_string(gray, config="--psm 6")
            except Exception as exc:
                ocr_error = str(exc)
            dimensions = []
            pattern = re.compile(
                r"(?P<prefix>[Rr]|[Ø⌀Φφ]|M)?\s*(?P<value>\d+(?:\.\d+)?)"
                r"(?:\s*[xX×]\s*(?P<count>\d+))?"
                r"(?:\s*(?:±|\+/-)\s*(?P<tolerance>\d+(?:\.\d+)?))?"
            )
            for match in pattern.finditer(ocr_text):
                prefix = match.group("prefix") or ""
                kind = "radius" if prefix.lower() == "r" else "diameter" if prefix else "linear"
                dimensions.append(
                    {
                        "kind": kind,
                        "value": float(match.group("value")),
                        "count": int(match.group("count") or 1),
                        "tolerance": float(match.group("tolerance")) if match.group("tolerance") else None,
                        "raw": match.group(0).strip(),
                    }
                )
            output = ""
            if annotated_path:
                output_path = self._automation.resolve_path(
                    annotated_path,
                    allow_external_paths=allow_external_paths,
                )
                output_path.parent.mkdir(parents=True, exist_ok=True)
                if not cv2.imwrite(str(output_path), image):
                    raise RuntimeError(f"Could not write annotated drawing: {output_path}")
                output = str(output_path)
            return _success(
                "Engineering drawing analyzed",
                image_path=str(source),
                image_size={"width": int(gray.shape[1]), "height": int(gray.shape[0])},
                ocr_text=ocr_text,
                ocr_error=ocr_error,
                dimensions=dimensions,
                lines=lines,
                circles=circles,
                annotated_path=output,
                review_required=True,
            )
        except Exception as exc:
            return _failure(exc)

    def create_model_from_drawing(
        self,
        image_path: str,
        document_name: str = "drawing_model",
        thickness: float = 0,
        save_path: str = "",
        overwrite: bool = False,
        allow_external_paths: bool = False,
        timeout: int = 300,
    ) -> Dict[str, Any]:
        """
        Reconstruct a conservative simple top-view plate model from a drawing image.

        Automatic creation proceeds only when at least width and height dimensions
        are recognized and thickness is supplied or recognized. Complex drawings
        remain review-required and are not silently guessed.
        """
        try:
            analysis = self.analyze_drawing_image(
                image_path,
                allow_external_paths=allow_external_paths,
            )
            if not analysis.get("success"):
                return analysis
            linear = sorted(
                {float(item["value"]) for item in analysis.get("dimensions", []) if item.get("kind") == "linear"},
                reverse=True,
            )
            if len(linear) < 2:
                return _failure(
                    "Automatic modeling requires at least two recognized linear dimensions",
                    analysis=analysis,
                    review_required=True,
                )
            width, height = linear[0], linear[1]
            model_thickness = float(thickness or (linear[2] if len(linear) > 2 else 0))
            if model_thickness <= 0:
                return _failure(
                    "Automatic modeling requires a positive thickness argument or a third linear dimension",
                    analysis=analysis,
                    review_required=True,
                )
            actions: List[Dict[str, Any]] = [
                {"op": "box", "id": "plate", "length": width, "width": height, "height": model_thickness}
            ]
            lines = analysis.get("lines") or []
            points = [point for line in lines for point in (line["start"], line["end"])]
            circles = analysis.get("circles") or []
            diameter_dims = [
                item
                for item in analysis.get("dimensions", [])
                if item.get("kind") == "diameter" and float(item.get("value", 0)) > 0
            ]
            if points and circles and diameter_dims:
                x_min = min(point[0] for point in points)
                x_max = max(point[0] for point in points)
                y_min = min(point[1] for point in points)
                y_max = max(point[1] for point in points)
                x_scale = width / max(1, x_max - x_min)
                y_scale = height / max(1, y_max - y_min)
                requested_holes = []
                for item in diameter_dims:
                    requested_holes.extend([float(item["value"])] * int(item.get("count", 1)))
                current = "plate"
                for index, (circle, diameter) in enumerate(zip(circles, requested_holes), 1):
                    center_x = (circle["center"][0] - x_min) * x_scale
                    center_y = height - (circle["center"][1] - y_min) * y_scale
                    hole = f"recognized_hole_{index}"
                    cut = f"recognized_cut_{index}"
                    actions.append(
                        {
                            "op": "cylinder",
                            "id": hole,
                            "radius": diameter / 2,
                            "height": model_thickness,
                            "position": [center_x, center_y, 0],
                        }
                    )
                    actions.append({"op": "cut", "id": cut, "base": current, "tool": hole})
                    current = cut
            result = self.execute_scenario(
                actions,
                document_name=document_name,
                save_path=save_path,
                overwrite=overwrite,
                allow_external_paths=allow_external_paths,
                timeout=timeout,
            )
            result["analysis"] = analysis
            result["review_required"] = True
            result["reconstruction_scope"] = "simple_top_view_plate"
            return result
        except Exception as exc:
            return _failure(exc)

    def compare_visuals(
        self,
        reference_image: str,
        rendered_image: str,
        difference_path: str = "",
        allow_external_paths: bool = False,
    ) -> Dict[str, Any]:
        """Compare a rendered FreeCAD preview with a reference image using edges and pixels."""
        try:
            import cv2
            import numpy as np

            reference_path = self._automation.resolve_path(
                reference_image,
                allow_external_paths=allow_external_paths,
                must_exist=True,
            )
            rendered_path = self._automation.resolve_path(
                rendered_image,
                allow_external_paths=allow_external_paths,
                must_exist=True,
            )
            reference = cv2.imread(str(reference_path), cv2.IMREAD_GRAYSCALE)
            rendered = cv2.imread(str(rendered_path), cv2.IMREAD_GRAYSCALE)
            if reference is None or rendered is None:
                raise ValueError("One or both comparison images could not be read")
            rendered = cv2.resize(rendered, (reference.shape[1], reference.shape[0]))
            ref_edges = cv2.Canny(reference, 60, 180) > 0
            rendered_edges = cv2.Canny(rendered, 60, 180) > 0
            union = np.logical_or(ref_edges, rendered_edges).sum()
            intersection = np.logical_and(ref_edges, rendered_edges).sum()
            edge_iou = float(intersection / union) if union else 1.0
            mean_error = float(np.mean(cv2.absdiff(reference, rendered)) / 255.0)
            similarity = max(0.0, min(1.0, 0.65 * edge_iou + 0.35 * (1.0 - mean_error)))
            output = ""
            if difference_path:
                output_path = self._automation.resolve_path(
                    difference_path,
                    allow_external_paths=allow_external_paths,
                )
                output_path.parent.mkdir(parents=True, exist_ok=True)
                difference = cv2.absdiff(reference, rendered)
                if not cv2.imwrite(str(output_path), difference):
                    raise RuntimeError(f"Could not write difference image: {output_path}")
                output = str(output_path)
            return _success(
                "Visual comparison completed",
                reference_image=str(reference_path),
                rendered_image=str(rendered_path),
                difference_path=output,
                score=similarity,
                edge_iou=edge_iou,
                mean_pixel_error=mean_error,
                recommendation="continue_iterating" if similarity < 0.9 else "accept_or_review_details",
            )
        except Exception as exc:
            return _failure(exc)

    def evaluate_visual_iteration(
        self,
        file_path: str,
        reference_image: str,
        orientation: str = "axonometric",
        preview_path: str = "",
        difference_path: str = "",
        allow_external_paths: bool = False,
        timeout: int = 180,
    ) -> Dict[str, Any]:
        """Render an FCStd model, compare it to a reference, and return the next-iteration decision."""
        try:
            source = self._automation.resolve_path(
                file_path,
                allow_external_paths=allow_external_paths,
                must_exist=True,
            )
            preview_output = preview_path or str(
                self._automation.output_root / f"{self._automation._safe_document_name(source.stem)}_iteration.png"
            )
            render = self.render_preview(
                str(source),
                output_path=preview_output,
                orientation=orientation,
                overwrite=True,
                allow_external_paths=allow_external_paths,
                timeout=timeout,
            )
            if not render.get("success"):
                return render
            comparison = self.compare_visuals(
                reference_image,
                render["preview_path"],
                difference_path=difference_path,
                allow_external_paths=allow_external_paths,
            )
            return _success("Visual iteration evaluated", render=render, comparison=comparison)
        except Exception as exc:
            return _failure(exc)
