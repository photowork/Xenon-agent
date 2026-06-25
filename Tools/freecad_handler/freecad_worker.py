#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""FreeCAD-side worker used by freecad_handler.

This module intentionally imports FreeCAD only while executing a worker request.
It is launched with FreeCAD's bundled Python interpreter in an isolated process.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _serializable(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if depth >= 8:
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_serializable(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(key): _serializable(item, depth + 1) for key, item in value.items()}
    return str(value)


def _vector(App: Any, value: Any, default: Optional[List[float]] = None) -> Any:
    data = default if value is None else value
    if not isinstance(data, (list, tuple)) or len(data) != 3:
        raise ValueError(f"Expected a three-number vector, got: {data!r}")
    return App.Vector(float(data[0]), float(data[1]), float(data[2]))


def _color(value: Any) -> Optional[tuple]:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("color must be [red, green, blue]")
    channels = [float(item) for item in value]
    if any(item > 1 for item in channels):
        channels = [item / 255.0 for item in channels]
    return tuple(max(0.0, min(1.0, item)) for item in channels)


class FreeCADScene:
    """Execute structured modeling actions inside FreeCAD."""

    def __init__(self, request: Dict[str, Any]):
        import FreeCAD as App
        import Part

        self.App = App
        self.Part = Part
        self.request = request
        self.doc = None
        self.Gui = None
        self.objects: Dict[str, Any] = {}
        self.action_results: List[Dict[str, Any]] = []
        self.max_objects = int(request.get("max_objects", 500))
        self.visual_mode = bool(request.get("visual_mode", False))
        self.step_delay = float(request.get("step_delay", 1.0))
        self.fit_after_each_step = bool(request.get("fit_after_each_step", True))
        self.keep_gui_open = bool(request.get("keep_gui_open", False))
        self.final_hold_seconds = float(request.get("final_hold_seconds", 2.0))

    def open_document(self) -> Any:
        if self.request.get("preview") or self.visual_mode:
            import FreeCADGui as Gui

            Gui.showMainWindow()
            self.Gui = Gui
            self._prepare_gui_window()
        source_path = str(self.request.get("source_path") or "").strip()
        document_name = str(self.request.get("document_name") or "FreeCADScene").strip()
        if source_path:
            self.doc = self.App.openDocument(source_path)
        else:
            safe_name = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in document_name)
            self.doc = self.App.newDocument(safe_name or "FreeCADScene")
        self._refresh_objects()
        if self.visual_mode:
            self._update_live_view("Ready to draw", fit=True)
        return self.doc

    def close_document(self) -> None:
        if self.doc is not None:
            try:
                self.App.closeDocument(self.doc.Name)
            except Exception:
                pass
        if self.Gui is not None:
            try:
                self.Gui.getMainWindow().close()
            except Exception:
                pass

    def _gui_process_events(self) -> None:
        if self.Gui is None:
            return
        try:
            self.Gui.updateGui()
        except Exception:
            pass
        try:
            from PySide import QtWidgets

            app = QtWidgets.QApplication.instance()
            if app is not None:
                app.processEvents()
        except Exception:
            pass

    def _prepare_gui_window(self) -> None:
        if self.Gui is None:
            return
        try:
            from PySide import QtWidgets

            main_window = self.Gui.getMainWindow()
            for dock in main_window.findChildren(QtWidgets.QDockWidget):
                if dock.objectName() in {"Report view", "Python console"}:
                    dock.hide()
        except Exception:
            pass
        self._gui_process_events()

    def _wait_with_events(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, float(seconds))
        while time.monotonic() < deadline:
            self._gui_process_events()
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def _sync_gui_visibility(self) -> None:
        if self.Gui is None:
            return
        for obj in self.doc.Objects:
            try:
                obj.ViewObject.Visibility = self._object_visible(obj)
            except Exception:
                pass

    def _update_live_view(self, message: str = "", *, fit: Optional[bool] = None) -> None:
        if self.Gui is None:
            return
        self._sync_gui_visibility()
        try:
            gui_doc = self.Gui.activeDocument()
            if gui_doc is not None:
                view = gui_doc.activeView()
                should_fit = self.fit_after_each_step if fit is None else fit
                if should_fit:
                    view.viewAxonometric()
                    view.fitAll()
        except Exception:
            pass
        if message:
            try:
                self.Gui.getMainWindow().statusBar().showMessage(message)
            except Exception:
                pass
        self._prepare_gui_window()
        self._gui_process_events()

    def _launch_detached_viewer(self, file_path: str) -> Dict[str, Any]:
        source = Path(file_path).resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Cannot keep GUI open because the saved model does not exist: {source}")
        gui_path = Path(self.App.getHomePath()) / "bin" / "freecad.exe"
        viewer_script = Path(__file__).with_name("freecad_viewer.py").resolve()
        if not gui_path.is_file():
            raise FileNotFoundError(f"FreeCAD GUI executable was not found: {gui_path}")
        if not viewer_script.is_file():
            raise FileNotFoundError(f"FreeCAD viewer script was not found: {viewer_script}")
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        process = subprocess.Popen(
            [str(gui_path), str(viewer_script), "--pass", str(source)],
            cwd=str(source.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
        return {
            "mode": "detached_final_viewer",
            "process_id": process.pid,
            "file_path": str(source),
            "message": "Final model viewer remains open while Xenon continues.",
        }

    def finish_visual_session(self, file_path: str = "") -> Optional[Dict[str, Any]]:
        if not self.visual_mode or self.Gui is None:
            return None
        self._update_live_view("Drawing completed", fit=True)
        if self.keep_gui_open:
            self._wait_with_events(0.3)
            return self._launch_detached_viewer(file_path)
        else:
            self._wait_with_events(self.final_hold_seconds)
        return None

    def _refresh_objects(self) -> None:
        self.objects.clear()
        for obj in self.doc.Objects:
            self.objects[obj.Name] = obj
            self.objects[obj.Label] = obj

    def _register_object(self, obj: Any, action: Optional[Dict[str, Any]] = None) -> Any:
        if action is not None:
            obj.Label = str(action.get("label") or obj.Label)
            self._apply_view_options(obj, action)
        self.objects[obj.Name] = obj
        self.objects[obj.Label] = obj
        return obj

    def _require_object(self, object_id: Any) -> Any:
        key = str(object_id or "").strip()
        obj = self.objects.get(key)
        if obj is None:
            obj = self.doc.getObject(key)
        if obj is None:
            raise ValueError(f"Object '{key}' does not exist")
        return obj

    def _shape(self, object_id: Any) -> Any:
        obj = self._require_object(object_id)
        shape = getattr(obj, "Shape", None)
        if shape is None or shape.isNull():
            raise ValueError(f"Object '{object_id}' has no usable shape")
        return shape

    def _body(self, object_id: Any) -> Any:
        body = self._require_object(object_id)
        if body.TypeId != "PartDesign::Body":
            raise ValueError(f"Object '{object_id}' is not a PartDesign Body")
        return body

    def _placement(self, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip().lower()
            shortcuts = {
                "xy": {"Base": [0, 0, 0], "yaw": 0, "pitch": 0, "roll": 0},
                "xz": {"Base": [0, 0, 0], "yaw": 0, "pitch": 0, "roll": 0},
                "yz": {"Base": [0, 0, 0], "yaw": 0, "pitch": 0, "roll": 0},
            }
            if value in shortcuts:
                value = shortcuts[value]
            else:
                raise ValueError(
                    f"placement string '{value}' is not recognized. "
                    "Use a dictionary: {'Base': [x,y,z], 'axis': [ax,ay,az], 'angle': deg} "
                    "or {'Base': [x,y,z], 'yaw': deg, 'pitch': deg, 'roll': deg}"
                )
        if not isinstance(value, dict):
            raise ValueError(
                "placement must be a dictionary, e.g. "
                "{'Base': [0,0,0], 'axis': [0,0,1], 'angle': 0} or "
                "{'Base': [0,0,0], 'yaw': 0, 'pitch': 0, 'roll': 0}"
            )
        base = _vector(self.App, value.get("position") or value.get("base"), [0, 0, 0])
        if "axis" in value or "angle" in value:
            rotation = self.App.Rotation(
                _vector(self.App, value.get("axis"), [0, 0, 1]),
                float(value.get("angle", 0)),
            )
        else:
            rotation = self.App.Rotation(
                float(value.get("yaw", 0)),
                float(value.get("pitch", 0)),
                float(value.get("roll", 0)),
            )
        return self.App.Placement(base, rotation)

    def _set_property_value(self, obj: Any, name: str, value: Any) -> None:
        if name not in obj.PropertiesList:
            raise ValueError(f"Object '{obj.Name}' has no property '{name}'")
        current = getattr(obj, name)
        if name in {"Placement", "AttachmentOffset"} and isinstance(value, dict):
            setattr(obj, name, self._placement(value))
        elif isinstance(value, dict) and "object" in value:
            target = self._require_object(value["object"])
            subelements = list(value.get("subelements") or value.get("subelements2d") or [])
            setattr(obj, name, (target, subelements) if subelements else target)
        elif isinstance(value, list) and value and all(isinstance(item, dict) and "object" in item for item in value):
            setattr(obj, name, [self._require_object(item["object"]) for item in value])
        elif isinstance(value, (list, tuple)) and len(value) == 3 and hasattr(current, "x"):
            setattr(obj, name, _vector(self.App, value))
        else:
            setattr(obj, name, value)

    def _set_visibility(self, object_ids: Iterable[Any], visible: bool) -> None:
        for object_id in object_ids:
            if not object_id:
                continue
            try:
                obj = self._require_object(object_id)
                self._set_intended_visibility(obj, visible)
            except Exception:
                pass

    @staticmethod
    def _set_intended_visibility(obj: Any, visible: bool) -> None:
        try:
            if "XenonVisible" not in obj.PropertiesList:
                obj.addProperty("App::PropertyBool", "XenonVisible", "Xenon", "Intended object visibility")
            obj.XenonVisible = bool(visible)
        except Exception:
            pass
        try:
            obj.ViewObject.Visibility = bool(visible)
        except Exception:
            pass

    def _add_shape(self, object_id: str, shape: Any, action: Dict[str, Any]) -> Any:
        if len(self.doc.Objects) >= self.max_objects:
            raise ValueError(f"Object limit exceeded ({self.max_objects})")
        if not object_id:
            raise ValueError("Shape-producing actions require a non-empty id")
        if self.doc.getObject(object_id) is not None:
            raise ValueError(f"Object id '{object_id}' already exists")
        obj = self.doc.addObject("Part::Feature", object_id)
        obj.Label = str(action.get("label") or object_id)
        obj.Shape = shape
        self._apply_view_options(obj, action)
        self.objects[object_id] = obj
        self.objects[obj.Label] = obj
        return obj

    def _apply_view_options(self, obj: Any, action: Dict[str, Any]) -> None:
        view = getattr(obj, "ViewObject", None)
        visible = bool(action.get("visible", True))
        self._set_intended_visibility(obj, visible)
        if view is None:
            return
        color = _color(action.get("color"))
        if color is not None:
            try:
                view.ShapeColor = color
            except Exception:
                pass
        if "transparency" in action:
            try:
                view.Transparency = max(0, min(100, int(action["transparency"])))
            except Exception:
                pass

    def _edge_selection(self, shape: Any, indexes: Any, *, all_edges: bool = False) -> List[Any]:
        if all_edges:
            if indexes:
                raise ValueError("Use either edges or all_edges=true, not both")
            if len(shape.Edges) > 64:
                raise ValueError(
                    f"all_edges=true would process {len(shape.Edges)} edges; the safety limit is 64. "
                    "Select a smaller 1-based edges array."
                )
            return list(shape.Edges)
        if not isinstance(indexes, list) or not indexes:
            raise ValueError("A non-empty edges array of 1-based integers is required")
        if len(indexes) > 64:
            raise ValueError("At most 64 edges may be selected in one operation")
        edges = []
        seen = set()
        for raw_index in indexes:
            if isinstance(raw_index, bool) or not isinstance(raw_index, int):
                raise ValueError("Edge indexes must be 1-based integers")
            index = int(raw_index)
            if index < 1 or index > len(shape.Edges):
                raise ValueError(f"Edge index {index} is outside 1..{len(shape.Edges)}")
            if index in seen:
                raise ValueError(f"Edge index {index} is duplicated")
            seen.add(index)
            edges.append(shape.Edges[index - 1])
        return edges

    @staticmethod
    def _safe_feature_size(shape: Any) -> float:
        bounds = shape.BoundBox
        positive = [value for value in (bounds.XLength, bounds.YLength, bounds.ZLength) if value > 1e-9]
        return min(positive) / 2.0 if positive else 0.0

    def _make_edge_treatment(self, action: Dict[str, Any], *, chamfer: bool) -> Any:
        op = "chamfer" if chamfer else "fillet"
        source_id = action.get("source")
        shape = self._shape(source_id)
        field = "size" if chamfer else "radius"
        amount = float(action[field])
        if amount <= 0:
            raise ValueError(f"{op} {field} must be greater than zero")
        safe_size = self._safe_feature_size(shape)
        override = bool(action.get("unsafe_allow_large_size" if chamfer else "unsafe_allow_large_radius", False))
        if safe_size > 0 and amount >= safe_size and not override:
            raise ValueError(
                f"{op} {field} {amount:g} is too large for the source bounding box. "
                f"Use a value smaller than {safe_size:g}, or explicitly set "
                f"{'unsafe_allow_large_size' if chamfer else 'unsafe_allow_large_radius'}=true."
            )
        edges = self._edge_selection(
            shape,
            action.get("edges"),
            all_edges=bool(action.get("all_edges", False)),
        )
        try:
            result = shape.makeChamfer(amount, edges) if chamfer else shape.makeFillet(amount, edges)
        except Exception as exc:
            raise ValueError(
                f"{op} failed for source '{source_id}' with {field}={amount:g} and "
                f"edges={action.get('edges') or 'all'}. Try fewer edges or a smaller {field}. "
                f"FreeCAD reported: {exc}"
            ) from exc
        if result is None or result.isNull() or not result.isValid():
            raise ValueError(
                f"{op} produced an invalid shape. Try fewer edges or a smaller {field}."
            )
        return result

    def _profile_shape(self, source: Any, make_face: bool = True) -> Any:
        shape = source.copy()
        if not make_face:
            return shape
        if shape.ShapeType == "Wire":
            return self.Part.Face(shape)
        if shape.ShapeType == "Edge":
            return shape
        if shape.ShapeType == "Compound" and shape.Edges and not shape.Faces:
            return self.Part.Face(self.Part.Wire(shape.Edges))
        return shape

    def _wire_from_object(self, object_id: Any, context_op: str) -> Any:
        """Extract a single wire from a sketch/profile object for loft/sweep."""
        shape = self._shape(object_id)
        if shape.ShapeType == "Wire":
            return shape
        if getattr(shape, "Wires", None):
            return shape.Wires[0]
        if shape.Edges:
            return self.Part.Wire(shape.Edges)
        raise ValueError(f"{context_op} profile '{object_id}' must contain a wire or edges")

    def _create_sketch(self, action: Dict[str, Any]) -> Any:
        import Sketcher

        object_id = str(action.get("id") or "").strip()
        if not object_id:
            raise ValueError("create_sketch requires id")
        sketch = self.doc.addObject("Sketcher::SketchObject", object_id)
        body_id = action.get("body")
        if body_id:
            self._body(body_id).addObject(sketch)
        support = action.get("support")
        support_planes = {
            "xy": "XY_Plane",
            "xz": "XZ_Plane",
            "yz": "YZ_Plane",
        }
        support_key = None
        if support is not None:
            if isinstance(support, str):
                support_key = support.strip().lower()
                if support_key in support_planes:
                    sketch.AttachmentSupport = (getattr(self.doc, support_planes[support_key]), [""])
                    sketch.MapMode = "FlatFace"
                else:
                    # Treat as object ID (e.g. a datum plane)
                    ref_obj = self._require_object(support)
                    sketch.AttachmentSupport = (ref_obj, [""])
                    sketch.MapMode = "FlatFace"
            elif isinstance(support, dict):
                ref_obj = self._require_object(support.get("object"))
                subs = list(support.get("subelements") or [])
                sketch.AttachmentSupport = (ref_obj, subs)
                sketch.MapMode = str(support.get("map_mode", "FlatFace"))
        if action.get("offset") is not None:
            offset = action["offset"]
            if isinstance(offset, (list, tuple)):
                # Transform global offset to attachment local CS for named planes
                # XZ plane normal = -global Y, YZ plane normal = +global X
                if support_key == "xz":
                    offset = [offset[0], offset[2], -offset[1]]
                elif support_key == "yz":
                    offset = [offset[2], offset[1], offset[0]]
                offset = {"position": offset}
            sketch.AttachmentOffset = self._placement(offset)
        if action.get("placement"):
            sketch.Placement = self._placement(action["placement"])
        for geometry in action.get("geometry", []):
            kind = str(geometry.get("type") or "").strip().lower()
            if kind == "line":
                sketch.addGeometry(
                    self.Part.LineSegment(
                        _vector(self.App, geometry.get("start")),
                        _vector(self.App, geometry.get("end")),
                    ),
                    bool(geometry.get("construction", False)),
                )
            elif kind == "circle":
                center = _vector(self.App, geometry.get("center"), [0, 0, 0])
                normal = _vector(self.App, geometry.get("normal"), [0, 0, 1])
                sketch.addGeometry(
                    self.Part.Circle(center, normal, float(geometry["radius"])),
                    bool(geometry.get("construction", False)),
                )
            elif kind == "arc":
                sketch.addGeometry(
                    self.Part.Arc(
                        _vector(self.App, geometry.get("start")),
                        _vector(self.App, geometry.get("mid")),
                        _vector(self.App, geometry.get("end")),
                    ),
                    bool(geometry.get("construction", False)),
                )
            elif kind in {"bspline", "bezier"}:
                poles = geometry.get("poles")
                if not isinstance(poles, list) or len(poles) < 2:
                    raise ValueError(f"Sketch geometry '{kind}' requires at least 2 poles")
                pole_vectors = [_vector(self.App, p) for p in poles]
                if kind == "bspline":
                    curve = self.Part.BSplineCurve()
                    degree = int(geometry.get("degree", 3))
                    curve.interpolate(pole_vectors)
                    try:
                        curve.setDegree(min(degree, len(pole_vectors) - 1))
                    except Exception:
                        pass
                else:
                    curve = self.Part.BezierCurve()
                    curve.setPoles(pole_vectors)
                    degree = int(geometry.get("degree", len(pole_vectors) - 1))
                    if degree != len(pole_vectors) - 1:
                        try:
                            curve.setDegree(degree)
                        except Exception:
                            pass
                sketch.addGeometry(curve, bool(geometry.get("construction", False)))
            elif kind == "rectangle":
                x = float(geometry.get("x", 0))
                y = float(geometry.get("y", 0))
                z = float(geometry.get("z", 0))
                w = float(geometry.get("width", 10))
                h = float(geometry.get("height", 10))
                corners = [
                    [x, y, z],
                    [x + w, y, z],
                    [x + w, y + h, z],
                    [x, y + h, z],
                ]
                is_construction = bool(geometry.get("construction", False))
                for i in range(4):
                    sketch.addGeometry(
                        self.Part.LineSegment(
                            _vector(self.App, corners[i]),
                            _vector(self.App, corners[(i + 1) % 4]),
                        ),
                        is_construction,
                    )
            elif kind == "polyline":
                points = geometry.get("points")
                if not isinstance(points, list) or len(points) < 2:
                    raise ValueError("Sketch geometry 'polyline' requires at least 2 points")
                is_construction = bool(geometry.get("construction", False))
                for i in range(len(points) - 1):
                    sketch.addGeometry(
                        self.Part.LineSegment(
                            _vector(self.App, points[i]),
                            _vector(self.App, points[i + 1]),
                        ),
                        is_construction,
                    )
                if geometry.get("closed", False):
                    sketch.addGeometry(
                        self.Part.LineSegment(
                            _vector(self.App, points[-1]),
                            _vector(self.App, points[0]),
                        ),
                        is_construction,
                    )
            else:
                raise ValueError(f"Unsupported sketch geometry type '{kind}'")
        if sketch.GeometryCount == 0:
            raise ValueError("create_sketch requires at least one geometry item")
        constraint_names = {
            "horizontal": "Horizontal",
            "vertical": "Vertical",
            "coincident": "Coincident",
            "parallel": "Parallel",
            "perpendicular": "Perpendicular",
            "tangent": "Tangent",
            "equal": "Equal",
            "distance": "Distance",
            "distance_x": "DistanceX",
            "distance_y": "DistanceY",
            "radius": "Radius",
            "diameter": "Diameter",
            "angle": "Angle",
            "block": "Block",
        }
        for constraint in action.get("constraints", []):
            kind = str(constraint.get("type") or "").strip().lower()
            name = constraint_names.get(kind)
            if not name:
                raise ValueError(f"Unsupported sketch constraint type '{kind}'")
            args = constraint.get("args")
            if not isinstance(args, list):
                raise ValueError(f"Sketch constraint '{kind}' requires an args list")
            sketch.addConstraint(Sketcher.Constraint(name, *args))
        self.doc.recompute()
        return self._register_object(sketch, action)

    def _part_design_feature(self, action: Dict[str, Any], type_id: str) -> Any:
        object_id = str(action.get("id") or "").strip()
        body = self._body(action.get("body"))
        profile = self._require_object(action.get("profile"))
        feature = self.doc.addObject(type_id, object_id)
        body.addObject(feature)
        feature.Profile = profile
        feature.Length = float(action.get("length", 10))
        if "reversed" in action:
            feature.Reversed = bool(action["reversed"])
        if "midplane" in action:
            feature.Midplane = bool(action["midplane"])
        if "type" in action:
            type_val = action["type"]
            if isinstance(type_val, str):
                type_map = {
                    "length": "Length",
                    "throughAll": "ThroughAll",
                    "throughFirst": "ThroughFirst",
                    "upToFace": "UpToFace",
                    "twoLengths": "TwoLengths",
                }
                feature.Type = type_map.get(str(type_val).strip(), "Length")
            else:
                feature.Type = int(type_val)
        if "length2" in action and "Length2" in feature.PropertiesList:
            feature.Length2 = float(action["length2"])
        self.doc.recompute()
        self._set_visibility([profile.Name], False)
        return self._register_object(feature, action)

    def _part_design_groove(self, action: Dict[str, Any]) -> Any:
        """PartDesign Groove: revolve a profile and subtract it from the body."""
        object_id = str(action.get("id") or "").strip()
        body = self._body(action.get("body"))
        profile = self._require_object(action.get("profile"))
        feature = self.doc.addObject("PartDesign::Groove", object_id)
        body.addObject(feature)
        feature.Profile = profile
        feature.ReferenceAxis = self._axis_reference(action, "axis", "z")
        feature.Angle = float(action.get("angle", 360))
        if "reversed" in action:
            feature.Reversed = bool(action["reversed"])
        if "midplane" in action:
            feature.Midplane = bool(action["midplane"])
        if "type" in action:
            type_val = action["type"]
            if isinstance(type_val, str):
                type_map = {
                    "length": "Length",
                    "throughAll": "ThroughAll",
                    "throughFirst": "ThroughFirst",
                    "upToFace": "UpToFace",
                    "twoLengths": "TwoLengths",
                }
                feature.Type = type_map.get(str(type_val).strip(), "Length")
            else:
                feature.Type = int(type_val)
        self.doc.recompute()
        self._set_visibility([profile.Name], False)
        return self._register_object(feature, action)

    def _part_design_hole(self, action: Dict[str, Any]) -> Any:
        """PartDesign Hole: standard hole feature (through/blind/counterbore/countersink).

        Maps the tool's user-facing hole_type/thread_type/drill_point enums to
        FreeCAD 1.x's string-valued Hole enumerations (DepthType, HoleCutType,
        ThreadType, DrillPoint). Verified against FreeCAD 1.1.
        """
        object_id = str(action.get("id") or "").strip()
        body = self._body(action.get("body"))
        profile = self._require_object(action.get("profile"))
        feature = self.doc.addObject("PartDesign::Hole", object_id)
        body.addObject(feature)
        feature.Profile = profile

        hole_type = str(action.get("hole_type", "blind")).strip().lower()
        depth_map = {"through": "ThroughAll", "blind": "Dimension"}
        cut_map = {"counterbore": "Counterbore", "countersink": "Countersink"}
        if hole_type in depth_map:
            feature.DepthType = depth_map[hole_type]
            feature.HoleCutType = "None"
        elif hole_type in cut_map:
            feature.DepthType = "Dimension"
            feature.HoleCutType = cut_map[hole_type]
        else:
            raise ValueError(
                f"hole_type must be through/blind/counterbore/countersink, got '{hole_type}'"
            )

        thread_type = str(action.get("thread_type", "none")).strip().lower()
        thread_map = {
            "none": "None",
            "metric": "ISOMetricProfile",
            "metric_fine": "ISOMetricFineProfile",
        }
        if thread_type not in thread_map:
            raise ValueError(f"thread_type must be none/metric/metric_fine, got '{thread_type}'")
        feature.ThreadType = thread_map[thread_type]
        if thread_type != "none":
            feature.Threaded = True

        if "diameter" in action:
            feature.Diameter = float(action["diameter"])
        if "depth" in action:
            feature.Depth = float(action["depth"])
        if "thread_depth" in action:
            feature.ThreadDepth = float(action["thread_depth"])
        if "drill_point" in action:
            # FreeCAD 1.x DrillPoint only has Flat/Angled (no Sphere).
            drill_map = {"flat": "Flat", "angled": "Angled", "sphere": "Angled"}
            drill = str(action["drill_point"]).strip().lower()
            if drill in drill_map:
                feature.DrillPoint = drill_map[drill]
        if "reversed" in action:
            feature.Reversed = bool(action["reversed"])
        if "tapered" in action:
            feature.Tapered = bool(action["tapered"])
        if "model_thread" in action and "ModelThread" in feature.PropertiesList:
            feature.ModelThread = bool(action["model_thread"])

        self.doc.recompute()
        self._set_visibility([profile.Name], False)
        return self._register_object(feature, action)

    def _part_design_pipe(self, action: Dict[str, Any]) -> Any:
        """PartDesign Pipe: parametric sweep along a path wire."""
        object_id = str(action.get("id") or "").strip()
        body = self._body(action.get("body"))
        path = self._require_object(action.get("path"))
        feature = self.doc.addObject("PartDesign::Pipe", object_id)
        body.addObject(feature)
        feature.Spine = path

        profile_ids = []
        if action.get("profiles"):
            profile_ids = list(action["profiles"])
        elif action.get("profile"):
            profile_ids = [action["profile"]]
        if profile_ids:
            feature.Profile = self._require_object(profile_ids[0])
            if len(profile_ids) > 1 and "AuxiliarySpine" in feature.PropertiesList:
                feature.AuxiliarySpine = self._require_object(profile_ids[1])

        transition = str(action.get("transition", "transformed")).strip().lower()
        # FreeCAD 1.x Transition enum: Transformed / Right corner / Round corner
        transition_map = {
            "transformed": "Transformed",
            "linear": "Right corner",
            "frenet": "Round corner",
        }
        if transition in transition_map and "Transition" in feature.PropertiesList:
            feature.Transition = transition_map[transition]

        self.doc.recompute()
        self._set_visibility([path.Name], False)
        if profile_ids and not action.get("keep_source", False):
            self._set_visibility(profile_ids, False)
        return self._register_object(feature, action)

    def _combine_copies(self, copies: List[Any], fuse: bool) -> Any:
        if not copies:
            raise ValueError("At least one copy is required")
        if not fuse:
            return self.Part.makeCompound(copies)
        result = copies[0]
        for shape in copies[1:]:
            result = result.fuse(shape)
        try:
            return result.removeSplitter()
        except Exception:
            return result

    def _techdraw_template_path(self, requested: Any) -> str:
        if requested:
            path = Path(str(requested)).resolve()
        else:
            path = (
                Path(self.App.getResourceDir())
                / "Mod"
                / "TechDraw"
                / "Templates"
                / "ISO"
                / "A4_Landscape_ISO5457_minimal.svg"
            )
        if not path.is_file():
            raise FileNotFoundError(f"TechDraw SVG template does not exist: {path}")
        return str(path)

    def _axis_reference(self, action: Dict[str, Any], key: str = "axis", default: str = "x") -> Any:
        value = action.get(key, default)
        if isinstance(value, dict) and value.get("object"):
            return (self._require_object(value["object"]), list(value.get("subelements") or []))
        axis = str(value).strip().lower()
        axes = {"x": "X_Axis", "y": "Y_Axis", "z": "Z_Axis"}
        if axis not in axes:
            raise ValueError(f"{key} must be x, y, z, or an object reference dictionary")
        return (getattr(self.doc, axes[axis]), [""])

    def _part_design_pattern(self, action: Dict[str, Any], type_id: str) -> Any:
        body = self._body(action.get("body"))
        feature = self.doc.addObject(type_id, str(action.get("id") or ""))
        body.addObject(feature)
        originals = action.get("originals") or [action.get("original")]
        feature.Originals = [self._require_object(item) for item in originals if item]
        if not feature.Originals:
            raise ValueError(f"{action.get('op')} requires original or originals")
        if type_id == "PartDesign::LinearPattern":
            feature.Direction = self._axis_reference(action, "direction", "x")
            feature.Length = float(action.get("length", 10))
        elif type_id == "PartDesign::PolarPattern":
            feature.Axis = self._axis_reference(action, "axis", "z")
            feature.Angle = float(action.get("angle", 360))
        feature.Occurrences = int(action.get("occurrences", action.get("count", 2)))
        feature.Refine = bool(action.get("refine", True))
        self.doc.recompute()
        return self._register_object(feature, action)

    def execute_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        op = str(action.get("op") or "").strip().lower()
        object_id = str(action.get("id") or "").strip()
        Part = self.Part
        App = self.App

        if op == "box":
            shape = Part.makeBox(
                float(action["length"]),
                float(action["width"]),
                float(action["height"]),
                _vector(App, action.get("position"), [0, 0, 0]),
                _vector(App, action.get("direction"), [0, 0, 1]),
            )
            obj = self._add_shape(object_id, shape, action)
        elif op == "cylinder":
            shape = Part.makeCylinder(
                float(action["radius"]),
                float(action["height"]),
                _vector(App, action.get("position"), [0, 0, 0]),
                _vector(App, action.get("direction"), [0, 0, 1]),
                float(action.get("angle", 360)),
            )
            obj = self._add_shape(object_id, shape, action)
        elif op == "sphere":
            shape = Part.makeSphere(
                float(action["radius"]),
                _vector(App, action.get("center"), [0, 0, 0]),
                _vector(App, action.get("axis"), [0, 0, 1]),
                float(action.get("angle1", -90)),
                float(action.get("angle2", 90)),
                float(action.get("angle3", 360)),
            )
            obj = self._add_shape(object_id, shape, action)
        elif op == "cone":
            shape = Part.makeCone(
                float(action["radius1"]),
                float(action.get("radius2", 0)),
                float(action["height"]),
                _vector(App, action.get("position"), [0, 0, 0]),
                _vector(App, action.get("direction"), [0, 0, 1]),
                float(action.get("angle", 360)),
            )
            obj = self._add_shape(object_id, shape, action)
        elif op == "torus":
            center = action.get("center")
            if center is None:
                center = action.get("position")
            shape = Part.makeTorus(
                float(action["radius1"]),
                float(action["radius2"]),
                _vector(App, center, [0, 0, 0]),
                _vector(App, action.get("axis"), [0, 0, 1]),
                float(action.get("angle1", 0)),
                float(action.get("angle2", 360)),
                float(action.get("angle3", 360)),
            )
            obj = self._add_shape(object_id, shape, action)
        elif op == "line":
            obj = self._add_shape(
                object_id,
                Part.makeLine(_vector(App, action.get("start")), _vector(App, action.get("end"))),
                action,
            )
        elif op == "circle":
            edge = Part.Edge(
                Part.Circle(
                    _vector(App, action.get("center"), [0, 0, 0]),
                    _vector(App, action.get("normal"), [0, 0, 1]),
                    float(action["radius"]),
                )
            )
            obj = self._add_shape(object_id, edge, action)
        elif op == "arc":
            edge = Part.Arc(
                _vector(App, action.get("start")),
                _vector(App, action.get("mid")),
                _vector(App, action.get("end")),
            ).toShape()
            obj = self._add_shape(object_id, edge, action)
        elif op in {"polyline", "rectangle"}:
            if op == "rectangle":
                x = float(action.get("x", 0))
                y = float(action.get("y", 0))
                z = float(action.get("z", 0))
                width = float(action["width"])
                height = float(action["height"])
                points = [[x, y, z], [x + width, y, z], [x + width, y + height, z], [x, y + height, z]]
                closed = True
            else:
                points = action.get("points", [])
                closed = bool(action.get("closed", False))
            vectors = [_vector(App, point) for point in points]
            if len(vectors) < 2:
                raise ValueError(f"{op} requires at least two points")
            if closed and vectors[0] != vectors[-1]:
                vectors.append(vectors[0])
            wire = Part.makePolygon(vectors)
            shape = Part.Face(wire) if action.get("face", False) and closed else wire
            obj = self._add_shape(object_id, shape, action)
        elif op in {"bspline", "bezier"}:
            poles = action["poles"]
            pole_vectors = [_vector(App, p) for p in poles]
            if op == "bspline":
                curve = Part.BSplineCurve()
                degree = int(action.get("degree", 3))
                curve.interpolate(pole_vectors)
                try:
                    curve.setDegree(min(degree, len(pole_vectors) - 1))
                except Exception:
                    pass
                if action.get("knots"):
                    try:
                        curve.setKnots([float(k) for k in action["knots"]])
                    except Exception:
                        pass
                if action.get("weights"):
                    try:
                        curve.setWeights([float(w) for w in action["weights"]])
                    except Exception:
                        pass
            else:
                curve = Part.BezierCurve()
                curve.setPoles(pole_vectors)
            shape = curve.toShape()
            if action.get("face", False):
                try:
                    shape = Part.Face(Part.Wire(shape))
                except Exception:
                    pass
            obj = self._add_shape(object_id, shape, action)
        elif op == "create_sketch":
            obj = self._create_sketch(action)
        elif op == "create_body":
            obj = self._register_object(self.doc.addObject("PartDesign::Body", object_id), action)
        elif op in {"create_datum_plane", "create_datum_axis", "create_datum_point"}:
            datum_type = {
                "create_datum_plane": ("PartDesign::Plane", "Plane"),
                "create_datum_axis": ("PartDesign::Line", "Line"),
                "create_datum_point": ("PartDesign::Point", "Point"),
            }[op]
            datum = self.doc.addObject(datum_type[0], object_id)
            body_id = action.get("body")
            if body_id:
                self._body(body_id).addObject(datum)
            attachment = action.get("attachment")
            if attachment is not None:
                if isinstance(attachment, str):
                    axis_map = {"x": "X_Axis", "y": "Y_Axis", "z": "Z_Axis"}
                    plane_map = {"xy": "XY_Plane", "xz": "XZ_Plane", "yz": "YZ_Plane"}
                    key = attachment.strip().lower()
                    if op == "create_datum_plane" and key in plane_map:
                        datum.AttachmentSupport = (getattr(self.doc, plane_map[key]), [""])
                        datum.MapMode = "FlatFace"
                    elif op == "create_datum_axis" and key in axis_map:
                        datum.AttachmentSupport = (getattr(self.doc, axis_map[key]), [""])
                        datum.MapMode = "AxisOfCurves"
                    elif op == "create_datum_point" and key in axis_map:
                        datum.AttachmentSupport = (getattr(self.doc, axis_map[key]), [""])
                        datum.MapMode = "PointOfVertices"
                    else:
                        raise ValueError(f"{op} attachment '{attachment}' is not recognized")
                elif isinstance(attachment, dict):
                    ref_obj = self._require_object(attachment.get("object"))
                    subs = list(attachment.get("subelements") or [])
                    datum.AttachmentSupport = (ref_obj, subs)
                    datum.MapMode = str(attachment.get("map_mode", "Deactivated"))
            if action.get("placement"):
                datum.Placement = self._placement(action["placement"])
            if "offset" in action:
                offset = action["offset"]
                # Transform global offset to attachment local CS for named planes
                if isinstance(offset, (list, tuple)) and isinstance(attachment, str):
                    plane_key = attachment.strip().lower()
                    if op == "create_datum_plane":
                        # XY plane: local CS = global CS (identity)
                        # XZ plane: local X=global X, local Y=global Z, local Z=global Y
                        # YZ plane: local X=global Z, local Y=global Y, local Z=global X
                        if plane_key == "xz":
                            offset = [offset[0], offset[2], -offset[1]]
                        elif plane_key == "yz":
                            offset = [offset[2], offset[1], offset[0]]
                    elif op == "create_datum_axis":
                        if plane_key == "x":
                            offset = [offset[0], offset[2], offset[1]]
                        elif plane_key == "y":
                            offset = [offset[2], offset[1], offset[0]]
                datum.AttachmentOffset = self._placement(
                    {"position": offset} if isinstance(offset, (list, tuple)) else offset
                )
            if op == "create_datum_point" and "position" in action:
                datum.AttachmentOffset = self.App.Placement(_vector(App, action["position"]), self.App.Rotation())
            self.doc.recompute()
            obj = self._register_object(datum, action)
        elif op == "pad":
            obj = self._part_design_feature(action, "PartDesign::Pad")
        elif op == "pocket":
            obj = self._part_design_feature(action, "PartDesign::Pocket")
        elif op == "partdesign_linear_pattern":
            obj = self._part_design_pattern(action, "PartDesign::LinearPattern")
        elif op == "partdesign_polar_pattern":
            obj = self._part_design_pattern(action, "PartDesign::PolarPattern")
        elif op == "partdesign_mirror":
            body = self._body(action.get("body"))
            feature = self.doc.addObject("PartDesign::Mirrored", object_id)
            body.addObject(feature)
            originals = action.get("originals") or [action.get("original")]
            feature.Originals = [self._require_object(item) for item in originals if item]
            plane = str(action.get("plane") or "yz").strip().lower()
            planes = {"xy": "XY_Plane", "xz": "XZ_Plane", "yz": "YZ_Plane"}
            if action.get("plane_reference"):
                feature.MirrorPlane = self._axis_reference(action, "plane_reference", "x")
            elif plane in planes:
                feature.MirrorPlane = (getattr(self.doc, planes[plane]), [""])
            else:
                raise ValueError("partdesign_mirror plane must be xy, xz, or yz")
            self.doc.recompute()
            obj = self._register_object(feature, action)
        elif op == "partdesign_thickness":
            body = self._body(action.get("body"))
            source = self._require_object(action.get("source"))
            feature = self.doc.addObject("PartDesign::Thickness", object_id)
            body.addObject(feature)
            faces = [f"Face{int(item)}" for item in action.get("faces", [])]
            if not faces:
                raise ValueError("partdesign_thickness requires one-based faces")
            feature.Base = (source, faces)
            feature.Value = abs(float(action["thickness"]))
            feature.Reversed = bool(action.get("reversed", True))
            feature.Mode = int(action.get("mode", 0))
            feature.Join = int(action.get("join", 0))
            self.doc.recompute()
            obj = self._register_object(feature, action)
        elif op == "set_properties":
            obj = self._require_object(action.get("object"))
            properties = action.get("properties")
            if not isinstance(properties, dict) or not properties:
                raise ValueError("set_properties requires a non-empty properties dictionary")
            for name, value in properties.items():
                self._set_property_value(obj, str(name), value)
            if "visible" in action:
                self._set_intended_visibility(obj, bool(action["visible"]))
            self.doc.recompute()
        elif op == "linear_array":
            source_id = action.get("source")
            source = self._shape(source_id)
            count = int(action.get("count", 2))
            if count < 1 or count > 200:
                raise ValueError("linear_array count must be between 1 and 200")
            step = _vector(App, action.get("step") or action.get("vector"), [10, 0, 0])
            copies = []
            for index in range(count):
                copy_shape = source.copy()
                copy_shape.translate(step * index)
                copies.append(copy_shape)
            obj = self._add_shape(object_id, self._combine_copies(copies, bool(action.get("fuse", False))), action)
            if not action.get("keep_source", False):
                self._set_visibility([source_id], False)
        elif op == "polar_array":
            source_id = action.get("source")
            source = self._shape(source_id)
            count = int(action.get("count", 4))
            if count < 1 or count > 200:
                raise ValueError("polar_array count must be between 1 and 200")
            total_angle = float(action.get("angle", 360))
            copies = []
            for index in range(count):
                copy_shape = source.copy()
                copy_shape.rotate(
                    _vector(App, action.get("center"), [0, 0, 0]),
                    _vector(App, action.get("axis"), [0, 0, 1]),
                    total_angle * index / count,
                )
                copies.append(copy_shape)
            obj = self._add_shape(object_id, self._combine_copies(copies, bool(action.get("fuse", False))), action)
            if not action.get("keep_source", False):
                self._set_visibility([source_id], False)
        elif op == "mirror":
            source_id = action.get("source")
            mirrored = self._shape(source_id).mirror(
                _vector(App, action.get("base"), [0, 0, 0]),
                _vector(App, action.get("normal"), [1, 0, 0]),
            )
            shapes = [mirrored]
            if action.get("include_source", False):
                shapes.insert(0, self._shape(source_id).copy())
            result = self._combine_copies(shapes, bool(action.get("fuse", False)))
            obj = self._add_shape(object_id, result, action)
            if not action.get("keep_source", False):
                self._set_visibility([source_id], False)
        elif op == "shell":
            source_id = action.get("source")
            shape = self._shape(source_id)
            face_indexes = action.get("faces")
            if not face_indexes:
                raise ValueError("shell requires one-based faces to remove")
            faces = []
            for raw_index in face_indexes:
                index = int(raw_index)
                if index < 1 or index > len(shape.Faces):
                    raise ValueError(f"Face index {index} is outside 1..{len(shape.Faces)}")
                faces.append(shape.Faces[index - 1])
            thickness = abs(float(action["thickness"]))
            if action.get("inward", True):
                thickness = -thickness
            result = shape.makeThickness(faces, thickness, float(action.get("tolerance", 0.01)))
            obj = self._add_shape(object_id, result, action)
            if not action.get("keep_source", False):
                self._set_visibility([source_id], False)
        elif op == "thread_helix":
            helix = Part.makeHelix(
                float(action["pitch"]),
                float(action["height"]),
                float(action["radius"]),
                float(action.get("angle", 0)),
                bool(action.get("left_handed", False)),
            )
            obj = self._add_shape(object_id, helix, action)
        elif op == "thread":
            pitch = float(action["pitch"])
            height = float(action["height"])
            radius = float(action["radius"])
            depth = abs(float(action.get("depth", pitch * 0.4)))
            inward = bool(action.get("inward", False))
            sign = -1 if inward else 1
            helix = Part.makeHelix(
                pitch,
                height,
                radius,
                float(action.get("angle", 0)),
                bool(action.get("left_handed", False)),
            )
            path = Part.Wire(helix.Edges)
            profile = Part.Wire(
                Part.makePolygon(
                    [
                        App.Vector(radius, 0, 0),
                        App.Vector(radius + sign * depth, 0, pitch / 2),
                        App.Vector(radius, 0, pitch),
                        App.Vector(radius, 0, 0),
                    ]
                ).Edges
            )
            result = path.makePipeShell([profile], True, False)
            source_id = action.get("source")
            if source_id:
                mode = str(action.get("mode") or ("cut" if inward else "fuse")).lower()
                source = self._shape(source_id)
                if mode == "cut":
                    result = source.cut(result)
                elif mode == "fuse":
                    result = source.fuse(result)
                else:
                    raise ValueError("thread mode must be cut or fuse when source is provided")
                if not action.get("keep_source", False):
                    self._set_visibility([source_id], False)
            obj = self._add_shape(object_id, result, action)
        elif op == "create_assembly":
            assembly = self.doc.addObject("Assembly::AssemblyObject", object_id)
            if action.get("create_joint_group", True):
                assembly.newObject("Assembly::JointGroup", f"{object_id}_Joints")
            obj = self._register_object(assembly, action)
        elif op == "assembly_link":
            assembly = self._require_object(action.get("assembly"))
            source = self._require_object(action.get("source"))
            link = assembly.newObject("App::Link", object_id)
            link.LinkedObject = source
            if action.get("placement"):
                link.Placement = self._placement(action["placement"])
            obj = self._register_object(link, action)
        elif op == "techdraw_page":
            page = self.doc.addObject("TechDraw::DrawPage", object_id)
            template = self.doc.addObject("TechDraw::DrawSVGTemplate", f"{object_id}_Template")
            template.Template = self._techdraw_template_path(action.get("template_path"))
            page.Template = template
            if "scale" in action:
                page.Scale = float(action["scale"])
            self._register_object(template)
            obj = self._register_object(page, action)
        elif op == "techdraw_view":
            page = self._require_object(action.get("page"))
            sources = action.get("sources") or [action.get("source")]
            view = self.doc.addObject("TechDraw::DrawViewPart", object_id)
            page.addView(view)
            view.Source = [self._require_object(item) for item in sources if item]
            if "direction" in action:
                direction = action["direction"]
                if isinstance(direction, str):
                    dir_map = {
                        "x": [1, 0, 0], "y": [0, 1, 0], "z": [0, 0, 1],
                        "top": [0, 0, 1], "bottom": [0, 0, -1],
                        "front": [0, -1, 0], "rear": [0, 1, 0],
                        "left": [-1, 0, 0], "right": [1, 0, 0],
                    }
                    view.Direction = _vector(App, dir_map.get(direction.strip().lower(), [0, 0, 1]))
                else:
                    view.Direction = _vector(App, direction)
            for name in ("X", "Y", "Scale", "Rotation"):
                if name.lower() in action:
                    setattr(view, name, float(action[name.lower()]))
            obj = self._register_object(view, action)
        elif op == "techdraw_dimension":
            page = self._require_object(action.get("page"))
            view = self._require_object(action.get("view"))
            dimension = self.doc.addObject("TechDraw::DrawViewDimension", object_id)
            page.addView(dimension)
            dimension.Type = str(action.get("dimension_type") or "Distance")
            dimension.MeasureType = str(action.get("measure_type") or "Projected")
            references = list(action.get("references") or ["Edge1"])
            dimension.References2D = [(view, str(reference)) for reference in references]
            if action.get("format_spec"):
                dimension.FormatSpec = str(action["format_spec"])
            if "arbitrary" in action:
                dimension.Arbitrary = bool(action["arbitrary"])
            if "over_tolerance" in action or "under_tolerance" in action:
                dimension.EqualTolerance = bool(action.get("equal_tolerance", False))
                dimension.OverTolerance = float(action.get("over_tolerance", 0))
                dimension.UnderTolerance = float(action.get("under_tolerance", 0))
            obj = self._register_object(dimension, action)
        elif op == "techdraw_section_view":
            page = self._require_object(action.get("page"))
            sources = action.get("sources") or [action.get("source")]
            section = self.doc.addObject("TechDraw::DrawViewSection", object_id)
            page.addView(section)
            section.Source = [self._require_object(item) for item in sources if item]
            section_dir = action.get("section_direction", "z")
            if isinstance(section_dir, str):
                dir_map = {"x": [1,0,0], "y": [0,1,0], "z": [0,0,1]}
                section.Direction = _vector(App, dir_map.get(section_dir.strip().lower(), [0,0,1]))
            else:
                section.Direction = _vector(App, section_dir)
            if action.get("section_normal"):
                section.SectionNormal = _vector(App, action["section_normal"])
            if action.get("section_origin"):
                section.SectionOrigin = _vector(App, action["section_origin"])
            if "scale" in action:
                section.Scale = float(action["scale"])
            for name in ("X", "Y"):
                if name.lower() in action:
                    setattr(section, name, float(action[name.lower()]))
            if action.get("label"):
                section.Label = str(action["label"])
            obj = self._register_object(section, action)
        elif op == "techdraw_annotation":
            page = self._require_object(action.get("page"))
            annotation = self.doc.addObject("TechDraw::DrawViewAnnotation", object_id)
            page.addView(annotation)
            annotation.Text = [str(action["text"])]
            for name in ("X", "Y"):
                if name.lower() in action:
                    setattr(annotation, name, float(action[name.lower()]))
            if "font_size" in action and "TextSize" in annotation.PropertiesList:
                annotation.TextSize = float(action["font_size"])
            obj = self._register_object(annotation, action)
        elif op == "techdraw_balloon":
            page = self._require_object(action.get("page"))
            view = self._require_object(action.get("view"))
            balloon = self.doc.addObject("TechDraw::DrawViewBalloon", object_id)
            page.addView(balloon)
            balloon.SourceView = view
            balloon.Text = str(action["text"])
            if "x" in action:
                balloon.X = float(action["x"])
            if "y" in action:
                balloon.Y = float(action["y"])
            if "origin_x" in action and "OriginX" in balloon.PropertiesList:
                balloon.OriginX = float(action["origin_x"])
            if "origin_y" in action and "OriginY" in balloon.PropertiesList:
                balloon.OriginY = float(action["origin_y"])
            # Note: DrawViewBalloon has no font size property in FreeCAD 1.x
            # (only TextWrapLen); font_size is silently ignored.
            obj = self._register_object(balloon, action)
        elif op in {"fuse", "cut", "common"}:
            base_id = action.get("base")
            base = self._shape(base_id)
            tools = action.get("tools")
            if tools is None:
                tools = [action.get("tool")]
            shapes = [self._shape(item) for item in tools if item]
            if not shapes:
                raise ValueError(f"{op} requires tool or tools")
            shape = base
            for tool_shape in shapes:
                if op == "fuse":
                    shape = shape.fuse(tool_shape)
                elif op == "cut":
                    shape = shape.cut(tool_shape)
                else:
                    shape = shape.common(tool_shape)
            if action.get("refine", True):
                try:
                    shape = shape.removeSplitter()
                except Exception:
                    pass
            if len(shape.Solids) == 1 and shape.ShapeType != "Solid":
                shape = shape.Solids[0]
            obj = self._add_shape(object_id, shape, action)
            if not action.get("keep_sources", False):
                self._set_visibility([base_id, *tools], False)
        elif op == "extrude":
            source_id = action.get("source")
            source = self._profile_shape(
                self._shape(source_id),
                bool(action.get("make_face", True)),
            )
            vector = action.get("vector")
            if vector is None:
                vector = [0, 0, float(action["length"])]
            obj = self._add_shape(object_id, source.extrude(_vector(App, vector)), action)
            if not action.get("keep_source", False):
                self._set_visibility([source_id], False)
        elif op == "revolve":
            source_id = action.get("source")
            source = self._profile_shape(
                self._shape(source_id),
                bool(action.get("make_face", True)),
            )
            shape = source.revolve(
                _vector(App, action.get("base"), [0, 0, 0]),
                _vector(App, action.get("axis"), [0, 0, 1]),
                float(action.get("angle", 360)),
            )
            obj = self._add_shape(object_id, shape, action)
            if not action.get("keep_source", False):
                self._set_visibility([source_id], False)
        elif op == "groove":
            obj = self._part_design_groove(action)
        elif op == "hole":
            obj = self._part_design_hole(action)
        elif op == "partdesign_pipe":
            obj = self._part_design_pipe(action)
        elif op == "loft":
            source_ids = list(action.get("profiles") or [])
            if len(source_ids) < 2:
                raise ValueError("loft requires at least 2 profiles")
            wires = [self._wire_from_object(sid, "loft") for sid in source_ids]
            ruled = bool(action.get("ruled", False))
            closed = bool(action.get("closed", False))
            try:
                shape = self.Part.makeLoft(wires, ruled, closed)
            except Exception as exc:
                raise ValueError(
                    f"loft failed for profiles {source_ids}. "
                    f"Ensure profiles are non-self-intersecting and ordered consistently. "
                    f"FreeCAD reported: {exc}"
                ) from exc
            if shape is None or shape.isNull() or not shape.isValid():
                raise ValueError("loft produced an invalid shape. Check profile ordering and continuity.")
            obj = self._add_shape(object_id, shape, action)
            if not action.get("keep_profiles", False):
                self._set_visibility(source_ids, False)
        elif op == "sweep":
            path_id = action.get("path")
            if not path_id:
                raise ValueError("sweep requires a path")
            profile_ids = [pid for pid in (action.get("profiles") or [action.get("profile")]) if pid]
            if not profile_ids:
                raise ValueError("sweep requires at least one profile")
            path_wire = self._wire_from_object(path_id, "sweep path")
            profile_wires = [self._wire_from_object(pid, "sweep") for pid in profile_ids]
            make_solid = bool(action.get("make_solid", True))
            is_frenet = bool(action.get("frenet", True))
            try:
                shape = path_wire.makePipeShell(profile_wires, make_solid, is_frenet)
            except Exception as exc:
                raise ValueError(
                    f"sweep failed for path '{path_id}' with profiles {profile_ids}. "
                    f"Ensure the path is a single continuous wire and profiles are planar. "
                    f"FreeCAD reported: {exc}"
                ) from exc
            if shape is None or shape.isNull() or not shape.isValid():
                raise ValueError("sweep produced an invalid shape. Check path continuity and profile orientation.")
            obj = self._add_shape(object_id, shape, action)
            if not action.get("keep_source", False):
                self._set_visibility([path_id, *profile_ids], False)
        elif op in {"copy", "move", "rotate"}:
            source_id = action.get("source")
            shape = self._shape(source_id).copy()
            if op == "move":
                shape.translate(_vector(App, action.get("vector")))
            elif op == "rotate":
                shape.rotate(
                    _vector(App, action.get("base"), [0, 0, 0]),
                    _vector(App, action.get("axis"), [0, 0, 1]),
                    float(action["angle"]),
                )
            obj = self._add_shape(object_id, shape, action)
            if op != "copy" and not action.get("keep_source", False):
                self._set_visibility([source_id], False)
        elif op == "fillet":
            source_id = action.get("source")
            result = self._make_edge_treatment(action, chamfer=False)
            obj = self._add_shape(object_id, result, action)
            if not action.get("keep_source", False):
                self._set_visibility([source_id], False)
        elif op == "chamfer":
            source_id = action.get("source")
            result = self._make_edge_treatment(action, chamfer=True)
            obj = self._add_shape(object_id, result, action)
            if not action.get("keep_source", False):
                self._set_visibility([source_id], False)
        elif op == "shape_string":
            import Draft
            ss = Draft.make_shapestring(
                str(action["string"]),
                str(action["font_path"]),
                float(action.get("size", 10)),
            )
            ss.Label = str(action.get("label") or object_id)
            extrude = float(action.get("extrude", 0))
            if extrude > 0:
                import Part
                wire = ss.Shape.Wires[0] if ss.Shape.Wires else ss.Shape
                shape = wire.extrude(App.Vector(0, 0, extrude))
                solid_obj = self.doc.addObject("Part::Feature", object_id)
                solid_obj.Label = ss.Label
                solid_obj.Shape = shape
                self.doc.removeObject(ss.Name)
                self._apply_view_options(solid_obj, action)
                obj = self._register_object(solid_obj, action)
            else:
                ss.Name = object_id if not self.doc.getObject(object_id) else ss.Name
                obj = self._register_object(ss, action)
            if action.get("placement"):
                obj.Placement = self._placement(action["placement"])
            elif action.get("position"):
                obj.Placement = App.Placement(_vector(App, action["position"]), App.Rotation())
        elif op == "draft_array":
            import Draft
            source = self._require_object(action.get("source"))
            array_type = str(action.get("array_type", "orthogonal")).strip().lower()
            if array_type == "orthogonal":
                count_x = int(action.get("count_x", 2))
                count_y = int(action.get("count_y", 1))
                count_z = int(action.get("count_z", 1))
                interval_x = _vector(App, action.get("interval_x"), [10, 0, 0])
                interval_y = _vector(App, action.get("interval_y"), [0, 10, 0])
                interval_z = _vector(App, action.get("interval_z"), [0, 0, 10])
                arr = Draft.make_array(source, interval_x, interval_y, interval_z, count_x, count_y, count_z)
            elif array_type == "polar":
                count = int(action.get("count", 4))
                angle = float(action.get("angle", 360))
                center = _vector(App, action.get("axis"), [0, 0, 0]) if "axis" in action else _vector(App, action.get("center"), [0, 0, 0])
                arr = Draft.make_array(source, center, angle, count, use_link=True)
            else:
                raise ValueError(f"draft_array array_type '{array_type}' not supported (use orthogonal/polar)")
            arr.Label = str(action.get("label") or object_id)
            obj = self._register_object(arr, action)
        elif op == "draft_clone":
            import Draft
            source = self._require_object(action.get("source"))
            clone = Draft.make_clone(source)
            clone.Label = str(action.get("label") or object_id)
            if action.get("placement"):
                clone.Placement = self._placement(action["placement"])
            obj = self._register_object(clone, action)
        elif op == "import_dxf":
            import Draft
            dxf_path = self._automation.resolve_path(action["file_path"], must_exist=True) if hasattr(self, "_automation") else Path(str(action["file_path"])).resolve()
            if not dxf_path.is_file():
                raise FileNotFoundError(f"DXF file not found: {dxf_path}")
            layer_filter = action.get("layer_filter")
            if layer_filter and isinstance(layer_filter, list):
                Draft.dxf_import(str(dxf_path), layername=layer_filter)
            else:
                Draft.dxf_import(str(dxf_path))
            imported = [o for o in self.doc.Objects if o not in self.objects.values()][-1:]
            if imported:
                imported[0].Label = str(action.get("label") or object_id)
                obj = self._register_object(imported[0], action)
            else:
                raise ValueError("DXF import produced no objects")
        elif op == "remove":
            target = self._require_object(action.get("object"))
            removed_name = target.Name
            self.doc.removeObject(target.Name)
            self._refresh_objects()
            return {"op": op, "removed": removed_name}
        else:
            raise ValueError(f"Unsupported operation '{op}'")

        self.doc.recompute()
        result = {"op": op, "id": obj.Name, "label": obj.Label, "type_id": obj.TypeId}
        shape = getattr(obj, "Shape", None)
        if shape is not None:
            result["shape"] = self.shape_info(shape)
        return result

    def run_actions(self) -> List[Dict[str, Any]]:
        actions = self.request.get("actions", [])
        total = len(actions)
        for index, action in enumerate(actions, 1):
            transaction_open = False
            try:
                try:
                    self.doc.openTransaction(f"Xenon {index}: {action.get('op', '')}")
                    transaction_open = True
                except Exception:
                    transaction_open = False
                result = self.execute_action(action)
                if transaction_open:
                    self.doc.commitTransaction()
                self.action_results.append({"index": index, "success": True, **result})
                if self.visual_mode:
                    label = result.get("id") or result.get("removed") or action.get("id") or ""
                    self._update_live_view(
                        f"Step {index}/{total}: {action.get('op', '')} {label}".strip()
                    )
                    self._wait_with_events(float(action.get("visual_delay", self.step_delay)))
            except Exception as exc:
                if transaction_open:
                    try:
                        self.doc.abortTransaction()
                    except Exception:
                        pass
                self._refresh_objects()
                self.action_results.append(
                    {
                        "index": index,
                        "success": False,
                        "op": str(action.get("op") or ""),
                        "id": str(action.get("id") or ""),
                        "error": str(exc),
                    }
                )
                if self.visual_mode:
                    self._update_live_view(f"Step {index}/{total} failed: {exc}")
                    self._wait_with_events(float(action.get("visual_delay", self.step_delay)))
                if not action.get("continue_on_error", False):
                    break
        return self.action_results

    @staticmethod
    def shape_info(shape: Any) -> Dict[str, Any]:
        if shape is None or shape.isNull():
            return {"shape_type": "Null"}
        bounds = shape.BoundBox
        return {
            "shape_type": shape.ShapeType,
            "solids": len(shape.Solids),
            "faces": len(shape.Faces),
            "edges": len(shape.Edges),
            "vertices": len(shape.Vertexes),
            "volume": float(shape.Volume),
            "area": float(shape.Area),
            "length": float(shape.Length),
            "bound_box": {
                "x_min": float(bounds.XMin),
                "y_min": float(bounds.YMin),
                "z_min": float(bounds.ZMin),
                "x_length": float(bounds.XLength),
                "y_length": float(bounds.YLength),
                "z_length": float(bounds.ZLength),
            },
            "valid": bool(shape.isValid()),
        }

    def inspect(self) -> Dict[str, Any]:
        self.doc.recompute()
        objects = []
        for obj in self.doc.Objects:
            item = {
                "name": obj.Name,
                "label": obj.Label,
                "type_id": obj.TypeId,
                "visible": self._object_visible(obj),
            }
            shape = getattr(obj, "Shape", None)
            if shape is not None:
                item["shape"] = self.shape_info(shape)
            objects.append(item)
        return {
            "document": {
                "name": self.doc.Name,
                "label": self.doc.Label,
                "file_name": self.doc.FileName,
                "object_count": len(objects),
            },
            "objects": objects,
        }

    def object_properties(self, object_id: Any, property_names: Iterable[Any]) -> Dict[str, Any]:
        obj = self._require_object(object_id)
        requested = [str(item) for item in property_names if str(item).strip()]
        names = requested or list(obj.PropertiesList)
        properties = {}
        for name in names:
            if name not in obj.PropertiesList:
                properties[name] = {"error": "property does not exist"}
                continue
            value = getattr(obj, name)
            item = {"type_id": obj.getTypeIdOfProperty(name)}
            if hasattr(value, "Value"):
                item["value"] = float(value.Value)
                item["display"] = str(value)
            elif hasattr(value, "Name") and hasattr(value, "TypeId"):
                item["value"] = value.Name
            else:
                item["value"] = _serializable(value)
            properties[name] = item
        return {
            "object": {"name": obj.Name, "label": obj.Label, "type_id": obj.TypeId},
            "properties": properties,
        }

    def object_topology(self, object_id: Any, include_faces: bool = True) -> Dict[str, Any]:
        obj = self._require_object(object_id)
        shape = self._shape(object_id)

        def point(vector: Any) -> List[float]:
            return [float(vector.x), float(vector.y), float(vector.z)]

        edges = []
        for index, edge in enumerate(shape.Edges, 1):
            bounds = edge.BoundBox
            vertices = [point(vertex.Point) for vertex in edge.Vertexes]
            item = {
                "index": index,
                "reference": f"Edge{index}",
                "length": float(edge.Length),
                "vertices": vertices,
                "bound_box": {
                    "x_min": float(bounds.XMin),
                    "y_min": float(bounds.YMin),
                    "z_min": float(bounds.ZMin),
                    "x_length": float(bounds.XLength),
                    "y_length": float(bounds.YLength),
                    "z_length": float(bounds.ZLength),
                },
            }
            try:
                item["center"] = point(edge.CenterOfMass)
            except Exception:
                pass
            edges.append(item)
        faces = []
        if include_faces:
            for index, face in enumerate(shape.Faces, 1):
                bounds = face.BoundBox
                faces.append(
                    {
                        "index": index,
                        "reference": f"Face{index}",
                        "area": float(face.Area),
                        "center": point(face.CenterOfMass),
                        "bound_box": {
                            "x_min": float(bounds.XMin),
                            "y_min": float(bounds.YMin),
                            "z_min": float(bounds.ZMin),
                            "x_length": float(bounds.XLength),
                            "y_length": float(bounds.YLength),
                            "z_length": float(bounds.ZLength),
                        },
                    }
                )
        return {
            "object": {"name": obj.Name, "label": obj.Label, "type_id": obj.TypeId},
            "shape": self.shape_info(shape),
            "edges": edges,
            "faces": faces,
            "selection_note": "Use the numeric edge index in fillet/chamfer edges; indexes are 1-based.",
        }

    def live_context(
        self,
        include_document: bool = True,
        include_selection_properties: bool = False,
    ) -> Dict[str, Any]:
        if self.Gui is None:
            raise RuntimeError("Live context requires a FreeCAD GUI session")

        try:
            selected = list(self.Gui.Selection.getSelectionEx(self.doc.Name))
        except Exception:
            selected = list(self.Gui.Selection.getSelectionEx())

        selection = []
        selected_object_names = []
        for selected_item in selected:
            obj = getattr(selected_item, "Object", None)
            if obj is None:
                continue
            if obj.Name not in selected_object_names:
                selected_object_names.append(obj.Name)
            subelement_names = [str(item) for item in getattr(selected_item, "SubElementNames", [])]
            subobjects = list(getattr(selected_item, "SubObjects", []))
            subelements = []
            for index, name in enumerate(subelement_names):
                subobject = subobjects[index] if index < len(subobjects) else None
                detail = {"reference": name}
                match = re.fullmatch(r"(Edge|Face|Vertex)(\d+)", name)
                if match:
                    detail["kind"] = match.group(1).lower()
                    detail["index"] = int(match.group(2))
                if subobject is not None:
                    detail["shape_type"] = str(getattr(subobject, "ShapeType", type(subobject).__name__))
                    for attr in ("Length", "Area"):
                        try:
                            detail[attr.lower()] = float(getattr(subobject, attr))
                        except Exception:
                            pass
                    try:
                        center = subobject.CenterOfMass
                        detail["center"] = [float(center.x), float(center.y), float(center.z)]
                    except Exception:
                        pass
                subelements.append(detail)
            entry = {
                "object": {"name": obj.Name, "label": obj.Label, "type_id": obj.TypeId},
                "subelements": subelements,
                "edges": [
                    item["index"] for item in subelements if item.get("kind") == "edge"
                ],
                "faces": [
                    item["index"] for item in subelements if item.get("kind") == "face"
                ],
                "vertices": [
                    item["index"] for item in subelements if item.get("kind") == "vertex"
                ],
            }
            if include_selection_properties:
                entry["properties"] = self.object_properties(obj.Name, [])["properties"]
            selection.append(entry)

        active_body = None
        try:
            gui_doc = self.Gui.activeDocument()
            view = gui_doc.activeView() if gui_doc is not None else None
            active_body = view.getActiveObject("pdbody") if view is not None else None
        except Exception:
            active_body = None
        if active_body is not None:
            try:
                if active_body.Document is not self.doc or self.doc.getObject(active_body.Name) is None:
                    active_body = None
            except Exception:
                active_body = None
        if active_body is None and selected_object_names:
            primary = self.doc.getObject(selected_object_names[0])
            for candidate in self.doc.Objects:
                if candidate.TypeId != "PartDesign::Body":
                    continue
                try:
                    if primary in list(candidate.Group):
                        active_body = candidate
                        break
                except Exception:
                    pass

        try:
            modified = not bool(self.doc.isSaved())
        except Exception:
            modified = not bool(self.doc.FileName)

        context = {
            "active_document": {
                "name": self.doc.Name,
                "label": self.doc.Label,
                "file_name": self.doc.FileName,
                "modified": modified,
            },
            "selection": selection,
            "selected_object_names": selected_object_names,
            "primary_selection": selected_object_names[0] if selected_object_names else "",
            "active_body": active_body.Name if active_body is not None else "",
            "tip": (
                active_body.Tip.Name
                if active_body is not None and getattr(active_body, "Tip", None) is not None
                else ""
            ),
            "selection_placeholders": {
                "$selection": "first selected object",
                "$selection1/$selection2": "selected objects by 1-based order",
                "$selections": "all selected object names",
                "$selected_edges": "selected edge indexes, filtered to the source object when possible",
                "$selected_faces": "selected face indexes, filtered to the source object when possible",
                "$selected_subelements": "selected Edge/Face/Vertex references",
                "$active_body": "active Part Design Body",
                "$tip": "tip feature of the active Body",
            },
        }
        if include_document:
            context.update(self.inspect())
        return context

    def resolve_live_action(self, action: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(action, dict):
            raise TypeError("collaboration action must be a dictionary")
        selection = list(context.get("selection") or [])
        selected_names = list(context.get("selected_object_names") or [])

        def selected_name(index: int = 1) -> str:
            if index < 1 or index > len(selected_names):
                raise ValueError(
                    f"Selection placeholder requires selected object {index}, "
                    f"but only {len(selected_names)} object(s) are selected"
                )
            return selected_names[index - 1]

        def resolve_object_token(value: Any) -> Any:
            if isinstance(value, list):
                return [resolve_object_token(item) for item in value]
            if isinstance(value, dict):
                return {key: resolve_object_token(item) for key, item in value.items()}
            if not isinstance(value, str):
                return value
            token = value.strip().lower()
            if token in {"$selection", "$selected_object"}:
                return selected_name()
            match = re.fullmatch(r"\$selection(\d+)", token)
            if match:
                return selected_name(int(match.group(1)))
            if token == "$selections":
                if not selected_names:
                    raise ValueError("$selections requires at least one selected object")
                return selected_names
            if token == "$active_body":
                if not context.get("active_body"):
                    raise ValueError("$active_body is unavailable; activate or select a Part Design Body")
                return context["active_body"]
            if token == "$tip":
                if not context.get("tip"):
                    raise ValueError("$tip is unavailable; the active Body has no tip feature")
                return context["tip"]
            return value

        resolved = {key: resolve_object_token(value) for key, value in action.items()}
        source_name = str(
            resolved.get("source")
            or resolved.get("object")
            or resolved.get("base")
            or resolved.get("profile")
            or ""
        )

        def selected_subelements(kind: str) -> List[Any]:
            values = []
            for entry in selection:
                name = str((entry.get("object") or {}).get("name") or "")
                if source_name and name != source_name:
                    continue
                if kind == "subelements":
                    values.extend(
                        str(item.get("reference"))
                        for item in entry.get("subelements", [])
                        if item.get("reference")
                    )
                else:
                    values.extend(entry.get(kind, []))
            if not values and source_name:
                for entry in selection:
                    if kind == "subelements":
                        values.extend(
                            str(item.get("reference"))
                            for item in entry.get("subelements", [])
                            if item.get("reference")
                        )
                    else:
                        values.extend(entry.get(kind, []))
            if not values:
                raise ValueError(
                    f"${'selected_subelements' if kind == 'subelements' else 'selected_' + kind} "
                    "requires matching subelements to be selected in FreeCAD"
                )
            return list(dict.fromkeys(values))

        def resolve_subelement_token(value: Any) -> Any:
            if isinstance(value, list):
                return [resolve_subelement_token(item) for item in value]
            if isinstance(value, dict):
                return {key: resolve_subelement_token(item) for key, item in value.items()}
            if value == "$selected_edges":
                return selected_subelements("edges")
            if value == "$selected_faces":
                return selected_subelements("faces")
            if value == "$selected_subelements":
                return selected_subelements("subelements")
            return value

        return {key: resolve_subelement_token(value) for key, value in resolved.items()}

    def refresh_collaboration_view(self, message: str, select_object: str = "") -> None:
        if self.Gui is None:
            return
        if select_object:
            obj = self.doc.getObject(select_object)
            if obj is not None:
                try:
                    self.Gui.Selection.clearSelection()
                    self.Gui.Selection.addSelection(obj)
                except Exception:
                    pass
        try:
            self.Gui.updateGui()
        except Exception:
            pass
        try:
            self.Gui.getMainWindow().statusBar().showMessage(message, 5000)
        except Exception:
            pass

    @staticmethod
    def _object_visible(obj: Any) -> bool:
        try:
            if "XenonVisible" in obj.PropertiesList:
                return bool(obj.XenonVisible)
        except Exception:
            pass
        try:
            return bool(obj.ViewObject.Visibility)
        except Exception:
            return True

    def selected_objects(self, object_ids: Iterable[Any]) -> List[Any]:
        ids = [str(item) for item in object_ids if str(item).strip()]
        if ids:
            return [self._require_object(item) for item in ids]
        visible = [
            obj
            for obj in self.doc.Objects
            if getattr(obj, "Shape", None) is not None and self._object_visible(obj)
        ]
        if visible:
            return visible
        return [obj for obj in self.doc.Objects if getattr(obj, "Shape", None) is not None]

    def save(self, file_path: str, overwrite: bool) -> str:
        path = Path(file_path)
        if path.exists() and not overwrite:
            raise FileExistsError(f"File already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.recompute()
        self.doc.saveAs(str(path))
        return str(path.resolve())

    def export(self, export_spec: Dict[str, Any], overwrite: bool) -> str:
        file_path = Path(export_spec["file_path"])
        if file_path.exists() and not overwrite:
            raise FileExistsError(f"File already exists: {file_path}")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        fmt = str(export_spec.get("format") or file_path.suffix.lstrip(".")).lower()
        objects = self.selected_objects(export_spec.get("object_ids", []))
        if not objects:
            raise ValueError("There are no shape objects to export")

        if fmt in {"step", "stp", "iges", "igs"}:
            import Import

            Import.export(objects, str(file_path))
        elif fmt in {"stl", "obj", "amf"}:
            import Mesh

            Mesh.export(objects, str(file_path))
        elif fmt in {"brep", "brp"}:
            self.Part.export(objects, str(file_path))
        elif fmt == "svg":
            import importSVG

            importSVG.export(objects, str(file_path))
        elif fmt == "dxf":
            import importDXF

            importDXF.export(objects, str(file_path))
        else:
            raise ValueError(f"Unsupported export format '{fmt}'")
        return str(file_path.resolve())

    def render_preview(self, preview_spec: Dict[str, Any], overwrite: bool) -> str:
        file_path = Path(preview_spec["file_path"])
        if file_path.exists() and not overwrite:
            raise FileExistsError(f"File already exists: {file_path}")
        file_path.parent.mkdir(parents=True, exist_ok=True)

        if self.Gui is None:
            raise RuntimeError("FreeCAD GUI was not initialized before opening the document")
        gui_doc = self.Gui.activeDocument()
        if gui_doc is None:
            raise RuntimeError("FreeCAD GUI document is unavailable")
        shape_objects = [obj for obj in self.doc.Objects if getattr(obj, "Shape", None) is not None]
        self._sync_gui_visibility()
        if shape_objects and not any(self._object_visible(obj) for obj in shape_objects):
            self._set_visibility([shape_objects[-1].Name], True)
        view = gui_doc.activeView()
        orientation = str(preview_spec.get("orientation", "axonometric")).lower()
        orientation_methods = {
            "axonometric": "viewAxonometric",
            "isometric": "viewAxonometric",
            "front": "viewFront",
            "rear": "viewRear",
            "left": "viewLeft",
            "right": "viewRight",
            "top": "viewTop",
            "bottom": "viewBottom",
        }
        method_name = orientation_methods.get(orientation)
        if not method_name:
            raise ValueError(f"Unsupported preview orientation '{orientation}'")
        getattr(view, method_name)()
        view.fitAll()
        background = str(preview_spec.get("background", "Current"))
        view.saveImage(
            str(file_path),
            int(preview_spec.get("width", 1200)),
            int(preview_spec.get("height", 900)),
            background,
        )
        if not file_path.exists():
            raise RuntimeError("FreeCAD did not create the preview image")
        return str(file_path.resolve())


def _version_info(App: Any) -> Dict[str, Any]:
    version = App.Version()
    return {
        "version": ".".join(version[:3]),
        "revision": version[3] if len(version) > 3 else "",
        "home_path": App.getHomePath(),
        "resource_dir": App.getResourceDir(),
        "user_app_data_dir": App.getUserAppDataDir(),
        "python": sys.version,
    }


def execute_request(request: Dict[str, Any]) -> Dict[str, Any]:
    import FreeCAD as App

    command = str(request.get("command") or "scenario").strip().lower()
    if command == "status":
        return {"success": True, "message": "FreeCAD worker is available", **_version_info(App)}

    scene = FreeCADScene(request)
    try:
        scene.open_document()
        if command == "inspect":
            return {"success": True, "message": "FreeCAD document inspected", **scene.inspect()}
        if command == "properties":
            return {
                "success": True,
                "message": "FreeCAD object properties inspected",
                **scene.object_properties(request.get("object"), request.get("property_names") or []),
            }
        if command == "topology":
            return {
                "success": True,
                "message": "FreeCAD object topology inspected",
                **scene.object_topology(request.get("object"), bool(request.get("include_faces", True))),
            }

        action_results = scene.run_actions()
        failed_actions = [item for item in action_results if not item.get("success")]
        result: Dict[str, Any] = {
            "success": not failed_actions,
            "message": "FreeCAD scenario completed" if not failed_actions else "FreeCAD scenario stopped after an error",
            "actions": action_results,
        }
        if failed_actions:
            result["error"] = failed_actions[0]["error"]

        save_path = str(request.get("save_path") or "").strip()
        if save_path and not failed_actions:
            result["save_path"] = scene.save(save_path, bool(request.get("overwrite", False)))

        export_results = []
        if not failed_actions:
            for spec in request.get("exports", []):
                try:
                    export_results.append(
                        {
                            "success": True,
                            "format": spec.get("format"),
                            "file_path": scene.export(spec, bool(request.get("overwrite", False))),
                        }
                    )
                except Exception as exc:
                    export_results.append(
                        {
                            "success": False,
                            "format": spec.get("format"),
                            "file_path": spec.get("file_path"),
                            "error": str(exc),
                        }
                    )
                    result["success"] = False
                    result["error"] = str(exc)
                    if not spec.get("continue_on_error", False):
                        break
        if export_results:
            result["exports"] = export_results

        preview_spec = request.get("preview")
        if preview_spec and not failed_actions:
            try:
                result["preview_path"] = scene.render_preview(
                    preview_spec,
                    bool(request.get("overwrite", False)),
                )
            except Exception as exc:
                result["preview_error"] = str(exc)
                if preview_spec.get("required", False):
                    result["success"] = False
                    result["error"] = str(exc)

        result.update(scene.inspect())
        visual_session = scene.finish_visual_session(str(result.get("save_path") or scene.doc.FileName or ""))
        if visual_session:
            result["visual_session"] = visual_session
        return result
    finally:
        scene.close_document()


def execute_active_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a request against the document currently open in FreeCAD GUI."""
    import FreeCAD as App
    import FreeCADGui as Gui

    command = str(request.get("command") or "scenario").strip().lower()
    if command == "status":
        document = App.ActiveDocument
        return {
            "success": True,
            "message": "FreeCAD live bridge is available",
            "active_document": document.Name if document else "",
            **_version_info(App),
        }

    scene = FreeCADScene(request)
    scene.Gui = Gui
    scene.doc = App.ActiveDocument
    if scene.doc is None:
        scene.doc = App.newDocument(str(request.get("document_name") or "XenonLive"))
    scene._refresh_objects()
    if command == "context":
        return {
            "success": True,
            "message": "Active FreeCAD collaboration context inspected",
            **scene.live_context(
                bool(request.get("include_document", True)),
                bool(request.get("include_selection_properties", False)),
            ),
        }
    if command == "inspect":
        return {"success": True, "message": "Active FreeCAD document inspected", **scene.inspect()}
    if command == "properties":
        return {
            "success": True,
            "message": "Active FreeCAD object properties inspected",
            **scene.object_properties(request.get("object"), request.get("property_names") or []),
        }
    if command == "topology":
        return {
            "success": True,
            "message": "Active FreeCAD object topology inspected",
            **scene.object_topology(request.get("object"), bool(request.get("include_faces", True))),
        }
    if command == "preview":
        preview = request.get("preview") or {}
        return {
            "success": True,
            "message": "Active FreeCAD document preview rendered",
            "preview_path": scene.render_preview(preview, bool(request.get("overwrite", False))),
        }
    if command in {"undo", "redo"}:
        method = getattr(scene.doc, command, None)
        if not callable(method):
            raise RuntimeError(f"Active FreeCAD document does not support {command}")
        method()
        scene.doc.recompute()
        scene._refresh_objects()
        scene.refresh_collaboration_view(f"Xenon collaboration: {command} completed")
        return {
            "success": True,
            "message": f"Active FreeCAD document {command} completed",
            "context": scene.live_context(include_document=False),
        }
    if command == "collaboration_step":
        context_before = scene.live_context(include_document=False)
        resolved_action = scene.resolve_live_action(request.get("action") or {}, context_before)
        scene.request["actions"] = [resolved_action]
        action_results = scene.run_actions()
        failed_actions = [item for item in action_results if not item.get("success")]
        result = {
            "success": not failed_actions,
            "message": (
                "Collaborative FreeCAD step completed; GUI remains open and the document was not saved"
                if not failed_actions
                else "Collaborative FreeCAD step failed and was rolled back"
            ),
            "resolved_action": resolved_action,
            "actions": action_results,
            "saved": False,
            "gui_remains_open": True,
            "view_preserved": True,
        }
        if failed_actions:
            result["error"] = failed_actions[0]["error"]
        selected_result = ""
        if not failed_actions and bool(request.get("select_result", True)):
            selected_result = str(action_results[0].get("id") or "")
        scene.refresh_collaboration_view(
            "Xenon collaborative step completed" if not failed_actions else "Xenon collaborative step failed",
            selected_result,
        )
        result["context_after"] = scene.live_context(include_document=False)
        return result

    action_results = scene.run_actions()
    failed_actions = [item for item in action_results if not item.get("success")]
    result: Dict[str, Any] = {
        "success": not failed_actions,
        "message": "Live FreeCAD scenario completed" if not failed_actions else "Live FreeCAD scenario stopped after an error",
        "actions": action_results,
    }
    if failed_actions:
        result["error"] = failed_actions[0]["error"]
    save_path = str(request.get("save_path") or "").strip()
    if save_path and not failed_actions:
        result["save_path"] = scene.save(save_path, bool(request.get("overwrite", False)))
    result.update(scene.inspect())
    scene._update_live_view("Xenon live action completed", fit=True)
    return result


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: freecad_worker.py request.json response.json", file=sys.stderr)
        return 2
    request_path = Path(sys.argv[1])
    response_path = Path(sys.argv[2])
    request: Dict[str, Any] = {}
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        response = execute_request(request)
    except Exception as exc:
        response = {
            "success": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    response_path.parent.mkdir(parents=True, exist_ok=True)
    response_path.write_text(json.dumps(_serializable(response), ensure_ascii=False, indent=2), encoding="utf-8")
    exit_code = 0 if response.get("success") else 1
    # FreeCADGui and some workbenches (notably Assembly) can leave native
    # helper threads waiting during interpreter teardown. The response and
    # model files are already flushed, so skip teardown to avoid a misleading
    # non-zero exit after a successful task.
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        os._exit(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
