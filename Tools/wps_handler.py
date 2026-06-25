#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
WPS Office live automation tool for Xenon.

The tool attaches to an already-open WPS Writer, Spreadsheets, or Presentation
instance through COM. Changes are applied to the live document and therefore
appear in the WPS window immediately.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import pythoncom
    import win32com.client

    WIN32COM_AVAILABLE = True
except ImportError:
    pythoncom = None
    win32com = None
    WIN32COM_AVAILABLE = False


APP_CONFIG = {
    "writer": {
        "progid": "KWPS.Application",
        "collection": "Documents",
        "active": "ActiveDocument",
        "label": "WPS Writer",
    },
    "spreadsheet": {
        "progid": "KET.Application",
        "collection": "Workbooks",
        "active": "ActiveWorkbook",
        "label": "WPS Spreadsheets",
    },
    "presentation": {
        "progid": "KWPP.Application",
        "collection": "Presentations",
        "active": "ActivePresentation",
        "label": "WPS Presentation",
    },
}

APP_ALIASES = {
    "wps": "writer",
    "word": "writer",
    "writer": "writer",
    "文字": "writer",
    "et": "spreadsheet",
    "excel": "spreadsheet",
    "sheet": "spreadsheet",
    "spreadsheet": "spreadsheet",
    "表格": "spreadsheet",
    "wpp": "presentation",
    "ppt": "presentation",
    "powerpoint": "presentation",
    "presentation": "presentation",
    "演示": "presentation",
}

# Office/WPS late-binding constants used by the high-level helpers.
WD_COLLAPSE_END = 0
WD_COLLAPSE_START = 1
WD_FIND_STOP = 0
WD_STORY = 6
WD_PAGE_BREAK = 7
WD_REPLACE_ONE = 1
WD_REPLACE_ALL = 2
MSO_TEXT_ORIENTATION_HORIZONTAL = 1
PP_LAYOUT_BLANK = 12


def _success(message: str = "", **data: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {"success": True, "message": message}
    result.update(data)
    return result


def _failure(error: Exception | str, **data: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {"success": False, "error": str(error)}
    result.update(data)
    return result


def _normalize_app_type(app_type: str) -> str:
    normalized = APP_ALIASES.get(str(app_type or "").strip().lower())
    if not normalized:
        raise ValueError(
            "app_type must be writer, spreadsheet, or presentation "
            "(aliases: wps/word, et/excel, wpp/ppt)"
        )
    return normalized


def _com_value(value: Any) -> Any:
    """Convert JSON-friendly values to values accepted by COM."""
    if isinstance(value, list):
        return tuple(_com_value(item) for item in value)
    if isinstance(value, dict):
        return {key: _com_value(item) for key, item in value.items()}
    return value


def _serializable(value: Any, depth: int = 0) -> Any:
    """Return a compact JSON-friendly representation of a COM result."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if depth >= 3:
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_serializable(item, depth + 1) for item in value]
    if isinstance(value, dict):
        return {str(key): _serializable(item, depth + 1) for key, item in value.items()}
    try:
        count = getattr(value, "Count", None)
        if isinstance(count, (int, float)):
            return {"com_object": str(value), "count": int(count)}
    except Exception:
        pass
    return str(value)


def _normalize_writer_text(text: Any) -> str:
    """Normalize WPS/Word paragraph marks to regular newlines for tool output."""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


class WpsAutomation:
    """Internal late-bound WPS COM automation helper."""

    @contextmanager
    def com_scope(self):
        if not WIN32COM_AVAILABLE:
            raise RuntimeError("pywin32 is unavailable. Install it with: pip install pywin32")
        pythoncom.CoInitialize()
        try:
            yield
        finally:
            pythoncom.CoUninitialize()

    def connect(
        self,
        app_type: str,
        *,
        visible: bool = True,
        create_if_missing: bool = True,
    ) -> Tuple[Any, str, bool]:
        normalized = _normalize_app_type(app_type)
        config = APP_CONFIG[normalized]
        app = None
        attached = False

        try:
            app = win32com.client.GetActiveObject(config["progid"])
            attached = True
        except Exception:
            if not create_if_missing:
                raise RuntimeError(f"No running {config['label']} instance was found")
            app = win32com.client.Dispatch(config["progid"])

        if visible:
            self.make_visible(app)
        return app, normalized, attached

    def make_visible(self, app: Any) -> None:
        """Best-effort activation across the Writer, Spreadsheet, and Presentation APIs."""
        try:
            app.Visible = True
        except Exception:
            pass
        try:
            app.ActiveWindow.Visible = True
        except Exception:
            pass
        try:
            app.ActiveWindow.Activate()
        except Exception:
            pass

    def get_or_create_file(
        self,
        app: Any,
        app_type: str,
        *,
        create_if_missing: bool = True,
    ) -> Any:
        config = APP_CONFIG[app_type]
        try:
            active = getattr(app, config["active"])
            if active is not None:
                return active
        except Exception:
            pass

        if not create_if_missing:
            raise RuntimeError(f"{config['label']} has no active file")
        return getattr(app, config["collection"]).Add()

    def context_info(self, app: Any, app_type: str, attached: bool) -> Dict[str, Any]:
        config = APP_CONFIG[app_type]
        active_name = ""
        active_path = ""
        application_visible = False
        window_visible: Optional[bool] = None
        try:
            active = getattr(app, config["active"])
            active_name = str(getattr(active, "Name", "") or "")
            active_path = str(getattr(active, "FullName", "") or "")
        except Exception:
            pass
        try:
            application_visible = bool(getattr(app, "Visible", False))
        except Exception:
            pass
        try:
            window_visible = bool(app.ActiveWindow.Visible)
        except Exception:
            pass

        return {
            "app_type": app_type,
            "application": config["label"],
            "attached_to_running_instance": attached,
            "active_name": active_name,
            "active_path": active_path,
            "visible": window_visible if window_visible is not None else application_visible,
            "application_visible": application_visible,
            "window_visible": window_visible,
        }

    def open_file(self, app: Any, app_type: str, file_path: str) -> Any:
        path = str(Path(file_path).expanduser().resolve())
        if not Path(path).exists():
            raise FileNotFoundError(path)
        return getattr(app, APP_CONFIG[app_type]["collection"]).Open(path)

    def save_file(self, app: Any, app_type: str, file_path: str = "") -> Dict[str, Any]:
        target = self.get_or_create_file(app, app_type, create_if_missing=False)
        if file_path:
            output = Path(file_path).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            target.SaveAs(str(output))
        else:
            current_path = str(getattr(target, "FullName", "") or "")
            if not Path(current_path).is_absolute():
                raise ValueError("The active WPS file is unnamed; provide file_path to save it without a dialog")
            target.Save()
        return {
            "saved_path": str(getattr(target, "FullName", file_path) or file_path),
            "active_name": str(getattr(target, "Name", "") or ""),
        }

    def active_sheet(self, app: Any, sheet_name: str = "") -> Any:
        workbook = self.get_or_create_file(app, "spreadsheet")
        if sheet_name:
            return workbook.Worksheets.Item(sheet_name)
        return workbook.ActiveSheet

    def active_slide(self, app: Any, slide_index: int = 0) -> Any:
        presentation = self.get_or_create_file(app, "presentation")
        if slide_index > 0:
            return presentation.Slides.Item(slide_index)
        try:
            return app.ActiveWindow.View.Slide
        except Exception:
            if presentation.Slides.Count == 0:
                return presentation.Slides.Add(1, PP_LAYOUT_BLANK)
            return presentation.Slides.Item(presentation.Slides.Count)

    def write_writer(
        self,
        app: Any,
        text: str,
        mode: str,
        style: Dict[str, Any],
        chunk_size: int,
        interval_ms: int,
    ) -> Dict[str, Any]:
        self.get_or_create_file(app, "writer")
        selection = app.Selection
        normalized_mode = str(mode or "cursor").lower()

        if normalized_mode in {"end", "append"}:
            selection.EndKey(WD_STORY)
        elif normalized_mode in {"start", "prepend"}:
            selection.HomeKey(WD_STORY)
        elif normalized_mode in {"replace_all", "replace"}:
            selection.WholeStory()
            self.apply_writer_style(selection, style)
            selection.TypeText(str(text))
            return {"characters_written": len(str(text)), "mode": normalized_mode}
        elif normalized_mode not in {"cursor", "selection"}:
            raise ValueError("mode must be cursor, selection, end, start, or replace_all")

        self.apply_writer_style(selection, style)
        text = str(text)
        actual_chunk_size = chunk_size if chunk_size > 0 else len(text) or 1
        delay = max(0, interval_ms) / 1000.0
        chunks = 0
        for start in range(0, len(text), actual_chunk_size):
            selection.TypeText(text[start : start + actual_chunk_size])
            chunks += 1
            if delay and start + actual_chunk_size < len(text):
                time.sleep(delay)
        return {
            "characters_written": len(text),
            "chunks_written": chunks,
            "mode": normalized_mode,
        }

    def apply_writer_style(self, selection: Any, style: Dict[str, Any]) -> None:
        if not style:
            return
        font = selection.Font
        paragraph = selection.ParagraphFormat
        font_map = {
            "font_name": "Name",
            "font_size": "Size",
            "bold": "Bold",
            "italic": "Italic",
            "underline": "Underline",
            "color": "Color",
        }
        paragraph_map = {
            "alignment": "Alignment",
            "left_indent": "LeftIndent",
            "right_indent": "RightIndent",
            "first_line_indent": "FirstLineIndent",
            "space_before": "SpaceBefore",
            "space_after": "SpaceAfter",
            "line_spacing": "LineSpacing",
        }
        for key, member in font_map.items():
            if key in style:
                setattr(font, member, _com_value(style[key]))
        for key, member in paragraph_map.items():
            if key in style:
                setattr(paragraph, member, _com_value(style[key]))
        if style.get("style_name"):
            selection.Style = style["style_name"]

    def _duplicate_range(self, range_obj: Any) -> Any:
        duplicate = getattr(range_obj, "Duplicate", None)
        if duplicate is None:
            return range_obj
        candidate = duplicate() if callable(duplicate) else duplicate
        try:
            getattr(candidate, "Find")
        except Exception:
            return range_obj
        return candidate

    def _range_from_bounds(self, document: Any, start: int, end: int) -> Any:
        try:
            return document.Range(int(start), int(end))
        except Exception:
            range_obj = self._duplicate_range(document.Content)
            range_obj.SetRange(int(start), int(end))
            return range_obj

    def _range_bounds(self, range_obj: Any) -> Dict[str, Optional[int]]:
        bounds: Dict[str, Optional[int]] = {"start": None, "end": None}
        for key, member in (("start", "Start"), ("end", "End")):
            try:
                bounds[key] = int(getattr(range_obj, member))
            except Exception:
                bounds[key] = None
        return bounds

    def find_writer_range(
        self,
        document: Any,
        anchor_text: str,
        occurrence: int = 1,
        match_case: bool = False,
        match_whole_word: bool = False,
        match_wildcards: bool = False,
    ) -> Tuple[Any, int]:
        anchor = str(anchor_text or "")
        if not anchor:
            raise ValueError("anchor_text must not be empty")
        occurrence = int(occurrence)
        if occurrence < 1:
            raise ValueError("occurrence must be greater than or equal to 1")

        content_range = self._duplicate_range(document.Content)
        try:
            story_end = int(getattr(document.Content, "End"))
        except Exception:
            story_end = int(getattr(content_range, "End", 0) or 0)

        matches_found = 0
        search_range = content_range
        while True:
            find = search_range.Find
            try:
                find.ClearFormatting()
            except Exception:
                pass
            executed = find.Execute(
                anchor,
                bool(match_case),
                bool(match_whole_word),
                bool(match_wildcards),
                False,
                False,
                True,
                WD_FIND_STOP,
                False,
            )
            found = bool(executed)
            if not found:
                try:
                    found = bool(getattr(find, "Found"))
                except Exception:
                    found = False
            if not found:
                break

            matches_found += 1
            if matches_found == occurrence:
                return search_range, matches_found

            next_start = int(getattr(search_range, "End", story_end))
            if next_start >= story_end:
                break
            search_range = self._range_from_bounds(document, next_start, story_end)

        if matches_found:
            raise ValueError(
                f"Text anchor occurrence {occurrence} was not found: {anchor} "
                f"(matches found: {matches_found})"
            )
        raise ValueError(f"Text anchor was not found: {anchor}")

    def find_writer_text(
        self,
        app: Any,
        find_text: str,
        occurrence: int,
        match_case: bool,
        match_whole_word: bool,
        match_wildcards: bool,
        select_match: bool,
    ) -> Dict[str, Any]:
        document = self.get_or_create_file(app, "writer", create_if_missing=False)
        target_range, matches_found = self.find_writer_range(
            document,
            find_text,
            occurrence,
            match_case,
            match_whole_word,
            match_wildcards,
        )
        if select_match:
            target_range.Select()
        return {
            "found": True,
            "find_text": str(find_text),
            "occurrence": int(occurrence),
            "matches_scanned": matches_found,
            "selected": bool(select_match),
            **self._range_bounds(target_range),
        }

    def insert_writer_text_at_anchor(
        self,
        app: Any,
        anchor_text: str,
        text: str,
        position: str,
        style: Dict[str, Any],
        occurrence: int,
        match_case: bool,
        match_whole_word: bool,
        match_wildcards: bool,
        chunk_size: int,
        interval_ms: int,
    ) -> Dict[str, Any]:
        document = self.get_or_create_file(app, "writer", create_if_missing=False)
        target_range, matches_found = self.find_writer_range(
            document,
            anchor_text,
            occurrence,
            match_case,
            match_whole_word,
            match_wildcards,
        )
        anchor_bounds = self._range_bounds(target_range)
        normalized_position = str(position or "after").strip().lower()
        position_aliases = {
            "after_text": "after",
            "end": "after",
            "append": "after",
            "before_text": "before",
            "start": "before",
            "prepend": "before",
            "replace_anchor": "replace",
            "replace_text": "replace",
        }
        normalized_position = position_aliases.get(normalized_position, normalized_position)
        if normalized_position not in {"after", "before", "replace"}:
            raise ValueError("position must be after, before, or replace")

        edit_range = self._duplicate_range(target_range)
        if normalized_position == "after":
            edit_range.Collapse(WD_COLLAPSE_END)
        elif normalized_position == "before":
            edit_range.Collapse(WD_COLLAPSE_START)

        try:
            edit_range.Select()
        except Exception:
            bounds = self._range_bounds(edit_range)
            app.Selection.SetRange(bounds["start"], bounds["end"])

        selection = app.Selection
        self.apply_writer_style(selection, style)
        text = str(text)
        actual_chunk_size = chunk_size if chunk_size > 0 else len(text) or 1
        delay = max(0, interval_ms) / 1000.0
        chunks = 0
        for start in range(0, len(text), actual_chunk_size):
            selection.TypeText(text[start : start + actual_chunk_size])
            chunks += 1
            if delay and start + actual_chunk_size < len(text):
                time.sleep(delay)

        return {
            "characters_written": len(text),
            "chunks_written": chunks,
            "position": normalized_position,
            "anchor_text": str(anchor_text),
            "occurrence": int(occurrence),
            "matches_scanned": matches_found,
            "anchor_start": anchor_bounds["start"],
            "anchor_end": anchor_bounds["end"],
        }

    def read_writer_text(
        self,
        app: Any,
        scope: str,
        start_char: int,
        max_chars: int,
        normalize_line_endings: bool,
    ) -> Dict[str, Any]:
        document = self.get_or_create_file(app, "writer", create_if_missing=False)
        normalized_scope = str(scope or "document").strip().lower()
        scope_aliases = {
            "all": "document",
            "content": "document",
            "body": "document",
            "doc": "document",
            "selected": "selection",
            "current_selection": "selection",
            "current": "selection",
            "paragraph": "current_paragraph",
            "current_para": "current_paragraph",
            "current-paragraph": "current_paragraph",
        }
        normalized_scope = scope_aliases.get(normalized_scope, normalized_scope)
        if normalized_scope not in {"document", "selection", "current_paragraph"}:
            raise ValueError("scope must be document, selection, or current_paragraph")

        if normalized_scope == "document":
            source_range = document.Content
        elif normalized_scope == "selection":
            source_range = app.Selection.Range
        else:
            try:
                source_range = app.Selection.Paragraphs.Item(1).Range
            except Exception:
                source_range = app.Selection.Range.Paragraphs.Item(1).Range

        raw_text = str(getattr(source_range, "Text", "") or "")
        text = _normalize_writer_text(raw_text) if normalize_line_endings else raw_text
        total_chars = len(text)
        start = max(0, int(start_char))
        if start > total_chars:
            start = total_chars

        limit = int(max_chars)
        if limit > 0:
            end = min(total_chars, start + limit)
        else:
            end = total_chars
        output_text = text[start:end]
        has_more = end < total_chars

        paragraph_count: Optional[int] = None
        if normalized_scope == "document":
            try:
                paragraph_count = int(document.Paragraphs.Count)
            except Exception:
                paragraph_count = None

        return {
            "scope": normalized_scope,
            "text": output_text,
            "start_char": start,
            "end_char": end,
            "characters_returned": len(output_text),
            "total_characters": total_chars,
            "truncated": has_more,
            "has_more": has_more,
            "next_start_char": end if has_more else None,
            "max_chars": limit,
            "normalized_line_endings": bool(normalize_line_endings),
            "paragraph_count": paragraph_count,
            **self._range_bounds(source_range),
        }

    def insert_writer_table(self, app: Any, data: List[List[Any]], rows: int, columns: int) -> Dict[str, Any]:
        document = self.get_or_create_file(app, "writer")
        selection = app.Selection
        if rows <= 0:
            rows = len(data) or 1
        if columns <= 0:
            columns = max((len(row) for row in data), default=1)
        table = document.Tables.Add(selection.Range, rows, columns)
        for row_index, row in enumerate(data[:rows], 1):
            for column_index, value in enumerate(row[:columns], 1):
                table.Cell(row_index, column_index).Range.Text = str(value)
        selection.SetRange(table.Range.End, table.Range.End)
        return {"rows": rows, "columns": columns}

    def write_sheet(
        self,
        app: Any,
        range_address: str,
        values: Any,
        sheet_name: str,
        formula: bool,
        number_format: str,
        auto_fit: bool,
    ) -> Dict[str, Any]:
        sheet = self.active_sheet(app, sheet_name)
        target = sheet.Range(range_address)
        converted = _com_value(values)
        if formula:
            target.Formula = converted
        else:
            target.Value = converted
        if number_format:
            target.NumberFormat = number_format
        if auto_fit:
            target.Columns.AutoFit()
        return {
            "sheet": str(sheet.Name),
            "range": range_address,
            "rows": int(target.Rows.Count),
            "columns": int(target.Columns.Count),
        }

    def add_slide(
        self,
        app: Any,
        title: str,
        body: str,
        layout: int,
        speaker_notes: str,
    ) -> Dict[str, Any]:
        presentation = self.get_or_create_file(app, "presentation")
        index = int(presentation.Slides.Count) + 1
        slide = presentation.Slides.Add(index, int(layout))
        title_written = False
        body_written = False

        if title:
            try:
                slide.Shapes.Title.TextFrame.TextRange.Text = title
                title_written = True
            except Exception:
                shape = slide.Shapes.AddTextbox(MSO_TEXT_ORIENTATION_HORIZONTAL, 40, 30, 640, 70)
                shape.TextFrame.TextRange.Text = title
                shape.TextFrame.TextRange.Font.Size = 28
                shape.TextFrame.TextRange.Font.Bold = True
                title_written = True

        if body:
            try:
                for placeholder_index in range(1, int(slide.Shapes.Placeholders.Count) + 1):
                    placeholder = slide.Shapes.Placeholders.Item(placeholder_index)
                    if not title_written or placeholder.Name != slide.Shapes.Title.Name:
                        placeholder.TextFrame.TextRange.Text = body
                        body_written = True
                        break
            except Exception:
                pass
            if not body_written:
                shape = slide.Shapes.AddTextbox(MSO_TEXT_ORIENTATION_HORIZONTAL, 60, 120, 600, 320)
                shape.TextFrame.TextRange.Text = body
                shape.TextFrame.TextRange.Font.Size = 18
                body_written = True

        if speaker_notes:
            try:
                slide.NotesPage.Shapes.Placeholders.Item(2).TextFrame.TextRange.Text = speaker_notes
            except Exception:
                pass
        return {
            "slide_index": index,
            "layout": int(layout),
            "title_written": title_written,
            "body_written": body_written,
        }

    def resolve_target(self, app: Any, app_type: str, target: str, path: List[Any]) -> Any:
        aliases = {
            "application": app,
            "app": app,
        }
        if app_type == "writer":
            aliases.update(
                {
                    "selection": app.Selection,
                    "document": self.get_or_create_file(app, app_type),
                }
            )
        elif app_type == "spreadsheet":
            workbook = self.get_or_create_file(app, app_type)
            aliases.update(
                {
                    "workbook": workbook,
                    "worksheet": workbook.ActiveSheet,
                    "sheet": workbook.ActiveSheet,
                    "selection": app.Selection,
                }
            )
        else:
            presentation = self.get_or_create_file(app, app_type)
            aliases.update(
                {
                    "presentation": presentation,
                    "slide": self.active_slide(app),
                    "selection": getattr(app.ActiveWindow, "Selection", app),
                }
            )

        target_text = str(target or "application").strip()
        target_members = [member for member in target_text.split(".") if member]
        first_member = target_members[0].lower() if target_members else "application"
        current = aliases.get(first_member)
        if current is not None:
            target_members = target_members[1:]
        else:
            current = app
        for member in target_members:
            current = getattr(current, member)

        for step in path or []:
            if isinstance(step, str):
                current = getattr(current, step)
                continue
            if not isinstance(step, dict) or not step.get("member"):
                raise ValueError("Each path item must be a member name or {'member': ..., 'args': [...]}")
            member = getattr(current, str(step["member"]))
            args = [_com_value(value) for value in step.get("args", [])]
            current = member(*args) if "args" in step or step.get("call") else member
        return current

    def execute_com_operations(self, app: Any, app_type: str, operations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for index, operation in enumerate(operations, 1):
            try:
                target = self.resolve_target(
                    app,
                    app_type,
                    str(operation.get("target", "application")),
                    operation.get("path", []),
                )
                set_values = operation.get("set", {})
                for member, value in set_values.items():
                    setattr(target, str(member), _com_value(value))

                method_result = None
                if operation.get("method"):
                    method = getattr(target, str(operation["method"]))
                    args = [_com_value(value) for value in operation.get("args", [])]
                    kwargs = {str(key): _com_value(value) for key, value in operation.get("kwargs", {}).items()}
                    method_result = method(*args, **kwargs)

                values = {}
                for member in operation.get("get", []):
                    values[str(member)] = _serializable(getattr(target, str(member)))
                results.append(
                    {
                        "index": index,
                        "success": True,
                        "result": _serializable(method_result),
                        "values": values,
                    }
                )
            except Exception as exc:
                results.append({"index": index, "success": False, "error": str(exc)})
                if not operation.get("continue_on_error", False):
                    break
        return results


class WpsToolManager:
    """
    Control a live WPS Office window through COM automation.

    Load module ``wps_handler`` before use. Prefer the high-level methods for
    writing, spreadsheets, and slides. Use ``execute_com`` for WPS functions
    that are not covered by a high-level method.
    """

    def __init__(self):
        self._automation = WpsAutomation()

    def describe_capabilities(self) -> Dict[str, Any]:
        """
        Describe supported WPS applications, usage scenes, and action examples.

        :return: Capability guide and example tool arguments.
        """
        return _success(
            "WPS live automation capability guide",
            load_module="wps_handler",
            live_behavior=(
                "Every call first attaches to the user's running WPS instance. "
                "Writer text, spreadsheet cells, and presentation slides update immediately."
            ),
            applications={
                "writer": (
                    "Live reading, writing, cursor/end/start insertion, text-anchor find/insert, "
                    "styling, tables, save/open, arbitrary COM."
                ),
                "spreadsheet": "Live range values/formulas, formatting through COM, sheets/charts/pivots through COM.",
                "presentation": "Live slide creation, titles/body/notes, shapes/media/animations through COM.",
            },
            scenarios=[
                {
                    "scene": "Continue writing in the document currently open in WPS",
                    "tool": "wps_handler_Wps_writer_write",
                    "arguments": {"text": "New paragraph", "mode": "end", "chunk_size": 20, "interval_ms": 30},
                },
                {
                    "scene": "Read text pasted into the current WPS Writer document",
                    "tool": "wps_handler_Wps_writer_read_text",
                    "arguments": {"scope": "document", "start_char": 0, "max_chars": 12000},
                },
                {
                    "scene": "Insert text after a paragraph already present in WPS Writer",
                    "tool": "wps_handler_Wps_writer_insert_at_text",
                    "arguments": {
                        "anchor_text": "Project Risks",
                        "text": "\nMitigation: add owners and due dates.",
                        "position": "after",
                    },
                },
                {
                    "scene": "Fill a live WPS spreadsheet",
                    "tool": "wps_handler_Wps_spreadsheet_write",
                    "arguments": {"range_address": "A1:B3", "values": [["Name", "Score"], ["A", 95], ["B", 88]]},
                },
                {
                    "scene": "Create a slide in the current WPS presentation",
                    "tool": "wps_handler_Wps_presentation_add_slide",
                    "arguments": {"title": "Weekly Review", "body": "Progress\nRisks\nNext steps"},
                },
                {
                    "scene": "Use an unwrapped WPS feature",
                    "tool": "wps_handler_Wps_execute_com",
                    "arguments": {
                        "app_type": "writer",
                        "operations": [
                            {"target": "selection", "path": ["Font"], "set": {"Bold": True, "Size": 16}},
                            {"target": "selection", "method": "TypeText", "args": ["Live text"]},
                        ],
                    },
                },
            ],
        )

    def status(self, create_if_missing: bool = False) -> Dict[str, Any]:
        """
        Check pywin32, WPS COM registration, and currently reachable WPS applications.

        :param create_if_missing: Whether to launch WPS when no running instance exists. Defaults to false.
        :return: Status for Writer, Spreadsheets, and Presentation.
        """
        if not WIN32COM_AVAILABLE:
            return _failure("pywin32 is unavailable", pywin32=False)
        statuses: Dict[str, Any] = {}
        with self._automation.com_scope():
            for app_type in APP_CONFIG:
                try:
                    app, normalized, attached = self._automation.connect(
                        app_type,
                        visible=False,
                        create_if_missing=create_if_missing,
                    )
                    statuses[app_type] = {
                        "reachable": True,
                        **self._automation.context_info(app, normalized, attached),
                    }
                except Exception as exc:
                    statuses[app_type] = {"reachable": False, "error": str(exc)}
        return _success("WPS status checked", pywin32=True, applications=statuses)

    def open(
        self,
        app_type: str = "writer",
        file_path: str = "",
        create_if_missing: bool = True,
        visible: bool = True,
    ) -> Dict[str, Any]:
        """
        Attach to or launch WPS, then open a file or create an empty active file.

        :param app_type: writer, spreadsheet, or presentation.
        :param file_path: Optional existing file path to open.
        :param create_if_missing: Create a WPS instance and active file when absent.
        :param visible: Show the WPS window.
        :return: Active WPS context.
        """
        try:
            with self._automation.com_scope():
                app, normalized, attached = self._automation.connect(
                    app_type,
                    visible=visible,
                    create_if_missing=create_if_missing,
                )
                if file_path:
                    self._automation.open_file(app, normalized, file_path)
                else:
                    self._automation.get_or_create_file(app, normalized, create_if_missing=create_if_missing)
                return _success(
                    "Connected to live WPS",
                    **self._automation.context_info(app, normalized, attached),
                )
        except Exception as exc:
            return _failure(exc)

    def writer_write(
        self,
        text: str,
        mode: str = "cursor",
        style: Optional[Dict[str, Any]] = None,
        chunk_size: int = 0,
        interval_ms: int = 0,
        create_if_missing: bool = True,
    ) -> Dict[str, Any]:
        """
        Write text into the live WPS Writer document and show it immediately.

        :param text: Text to write.
        :param mode: cursor, selection, end, start, or replace_all.
        :param style: Optional style object, for example {"font_name":"宋体","font_size":12,"bold":true,"alignment":1}.
        :param chunk_size: Characters written per visible chunk; 0 writes all text in one call.
        :param interval_ms: Delay between chunks in milliseconds, useful for visibly streamed writing.
        :param create_if_missing: Launch Writer and create a document when needed.
        :return: Write result and active document information.
        """
        try:
            with self._automation.com_scope():
                app, normalized, attached = self._automation.connect(
                    "writer", visible=True, create_if_missing=create_if_missing
                )
                result = self._automation.write_writer(
                    app,
                    text,
                    mode,
                    style or {},
                    max(0, int(chunk_size)),
                    max(0, int(interval_ms)),
                )
                return _success(
                    "Text written to live WPS Writer",
                    **result,
                    context=self._automation.context_info(app, normalized, attached),
                )
        except Exception as exc:
            return _failure(exc)

    def writer_read_text(
        self,
        scope: str = "document",
        start_char: int = 0,
        max_chars: int = 12000,
        normalize_line_endings: bool = True,
        create_if_missing: bool = False,
    ) -> Dict[str, Any]:
        """
        Read text from the live WPS Writer document, selection, or current paragraph.

        :param scope: document, selection, or current_paragraph.
        :param start_char: Character offset for paged reads.
        :param max_chars: Maximum characters to return; 0 returns all text.
        :param normalize_line_endings: Convert WPS paragraph marks to "\n".
        :param create_if_missing: Launch Writer when needed. Defaults to false.
        :return: Text chunk and pagination metadata.
        """
        try:
            with self._automation.com_scope():
                app, normalized, attached = self._automation.connect(
                    "writer", visible=True, create_if_missing=create_if_missing
                )
                self._automation.get_or_create_file(
                    app, "writer", create_if_missing=create_if_missing
                )
                result = self._automation.read_writer_text(
                    app,
                    scope,
                    int(start_char),
                    int(max_chars),
                    bool(normalize_line_endings),
                )
                return _success(
                    "Text read from live WPS Writer",
                    **result,
                    context=self._automation.context_info(app, normalized, attached),
                )
        except Exception as exc:
            return _failure(exc)

    def writer_find_text(
        self,
        find_text: str,
        occurrence: int = 1,
        match_case: bool = False,
        match_whole_word: bool = False,
        match_wildcards: bool = False,
        select_match: bool = True,
        create_if_missing: bool = False,
    ) -> Dict[str, Any]:
        """
        Find text in the live WPS Writer document and optionally select it.

        :param find_text: Text anchor to search for.
        :param occurrence: 1-based occurrence to find.
        :param match_case: Match case exactly.
        :param match_whole_word: Match whole words only.
        :param match_wildcards: Use WPS/Word wildcard matching.
        :param select_match: Select the found text in WPS when true.
        :param create_if_missing: Launch Writer when needed. Defaults to false.
        :return: Found range start/end information.
        """
        try:
            with self._automation.com_scope():
                app, normalized, attached = self._automation.connect(
                    "writer", visible=True, create_if_missing=create_if_missing
                )
                self._automation.get_or_create_file(
                    app, "writer", create_if_missing=create_if_missing
                )
                result = self._automation.find_writer_text(
                    app,
                    find_text,
                    int(occurrence),
                    bool(match_case),
                    bool(match_whole_word),
                    bool(match_wildcards),
                    bool(select_match),
                )
                return _success(
                    "Text found in live WPS Writer",
                    **result,
                    context=self._automation.context_info(app, normalized, attached),
                )
        except Exception as exc:
            return _failure(exc)

    def writer_insert_at_text(
        self,
        anchor_text: str,
        text: str,
        position: str = "after",
        occurrence: int = 1,
        style: Optional[Dict[str, Any]] = None,
        match_case: bool = False,
        match_whole_word: bool = False,
        match_wildcards: bool = False,
        chunk_size: int = 0,
        interval_ms: int = 0,
        create_if_missing: bool = False,
    ) -> Dict[str, Any]:
        """
        Insert text before/after a text anchor in the live WPS Writer document.

        :param anchor_text: Existing text used as the insertion anchor.
        :param text: Text to insert.
        :param position: after, before, or replace.
        :param occurrence: 1-based occurrence of the anchor to use.
        :param style: Optional style object, same shape as writer_write.
        :param match_case: Match case exactly.
        :param match_whole_word: Match whole words only.
        :param match_wildcards: Use WPS/Word wildcard matching.
        :param chunk_size: Characters written per visible chunk; 0 writes all text in one call.
        :param interval_ms: Delay between chunks in milliseconds.
        :param create_if_missing: Launch Writer when needed. Defaults to false.
        :return: Insert result and anchor range information.
        """
        try:
            with self._automation.com_scope():
                app, normalized, attached = self._automation.connect(
                    "writer", visible=True, create_if_missing=create_if_missing
                )
                self._automation.get_or_create_file(
                    app, "writer", create_if_missing=create_if_missing
                )
                result = self._automation.insert_writer_text_at_anchor(
                    app,
                    anchor_text,
                    text,
                    position,
                    style or {},
                    int(occurrence),
                    bool(match_case),
                    bool(match_whole_word),
                    bool(match_wildcards),
                    max(0, int(chunk_size)),
                    max(0, int(interval_ms)),
                )
                return _success(
                    "Text inserted at WPS Writer anchor",
                    **result,
                    context=self._automation.context_info(app, normalized, attached),
                )
        except Exception as exc:
            return _failure(exc)

    def writer_insert_table(
        self,
        data: Optional[List[List[Any]]] = None,
        rows: int = 0,
        columns: int = 0,
        create_if_missing: bool = True,
    ) -> Dict[str, Any]:
        """
        Insert a table at the live Writer cursor and optionally fill its cells.

        :param data: Two-dimensional table data.
        :param rows: Row count; 0 infers it from data.
        :param columns: Column count; 0 infers it from data.
        :param create_if_missing: Launch Writer and create a document when needed.
        :return: Inserted table dimensions.
        """
        try:
            with self._automation.com_scope():
                app, _, _ = self._automation.connect("writer", visible=True, create_if_missing=create_if_missing)
                result = self._automation.insert_writer_table(app, data or [], int(rows), int(columns))
                return _success("Table inserted into live WPS Writer", **result)
        except Exception as exc:
            return _failure(exc)

    def writer_replace(
        self,
        find_text: str,
        replace_text: str,
        replace_all: bool = True,
        create_if_missing: bool = False,
    ) -> Dict[str, Any]:
        """
        Replace text in the live WPS Writer document.

        :param find_text: Text to find.
        :param replace_text: Replacement text.
        :param replace_all: Replace all occurrences when true.
        :param create_if_missing: Launch Writer and create a document when needed.
        :return: WPS Find.Execute result.
        """
        try:
            with self._automation.com_scope():
                app, _, _ = self._automation.connect("writer", visible=True, create_if_missing=create_if_missing)
                document = self._automation.get_or_create_file(
                    app, "writer", create_if_missing=create_if_missing
                )
                find = document.Content.Find
                find.ClearFormatting()
                find.Replacement.ClearFormatting()
                executed = find.Execute(
                    find_text,
                    False,
                    False,
                    False,
                    False,
                    False,
                    True,
                    1,
                    False,
                    replace_text,
                    WD_REPLACE_ALL if replace_all else WD_REPLACE_ONE,
                )
                return _success("Writer replacement completed", executed=bool(executed))
        except Exception as exc:
            return _failure(exc)

    def spreadsheet_write(
        self,
        range_address: str,
        values: List[Any],
        sheet_name: str = "",
        formula: bool = False,
        number_format: str = "",
        auto_fit: bool = True,
        create_if_missing: bool = True,
    ) -> Dict[str, Any]:
        """
        Write values or formulas to a live WPS spreadsheet range.

        :param range_address: A1-style range such as A1, A1:C4, or D:D.
        :param values: One- or two-dimensional value list matching the range. Use [["text"]] for one cell.
        :param sheet_name: Optional worksheet name; uses the active sheet when empty.
        :param formula: Write to Formula instead of Value when true.
        :param number_format: Optional WPS/Excel number format string.
        :param auto_fit: Auto-fit affected columns.
        :param create_if_missing: Launch Spreadsheets and create a workbook when needed.
        :return: Written range information.
        """
        try:
            with self._automation.com_scope():
                app, normalized, attached = self._automation.connect(
                    "spreadsheet", visible=True, create_if_missing=create_if_missing
                )
                result = self._automation.write_sheet(
                    app,
                    range_address,
                    values,
                    sheet_name,
                    formula,
                    number_format,
                    auto_fit,
                )
                return _success(
                    "Values written to live WPS spreadsheet",
                    **result,
                    context=self._automation.context_info(app, normalized, attached),
                )
        except Exception as exc:
            return _failure(exc)

    def presentation_add_slide(
        self,
        title: str = "",
        body: str = "",
        layout: int = PP_LAYOUT_BLANK,
        speaker_notes: str = "",
        create_if_missing: bool = True,
    ) -> Dict[str, Any]:
        """
        Add a slide to the live WPS presentation and fill title/body text.

        :param title: Slide title.
        :param body: Slide body text.
        :param layout: PowerPoint-compatible slide layout number; 12 is blank.
        :param speaker_notes: Optional speaker notes.
        :param create_if_missing: Launch Presentation and create a deck when needed.
        :return: Created slide information.
        """
        try:
            with self._automation.com_scope():
                app, normalized, attached = self._automation.connect(
                    "presentation", visible=True, create_if_missing=create_if_missing
                )
                result = self._automation.add_slide(app, title, body, int(layout), speaker_notes)
                return _success(
                    "Slide added to live WPS presentation",
                    **result,
                    context=self._automation.context_info(app, normalized, attached),
                )
        except Exception as exc:
            return _failure(exc)

    def execute_com(
        self,
        app_type: str,
        operations: List[Dict[str, Any]],
        create_if_missing: bool = True,
        visible: bool = True,
    ) -> Dict[str, Any]:
        """
        Execute structured COM operations for nearly any WPS Office function.

        Each operation accepts ``target`` (application/app, selection, document,
        workbook, worksheet/sheet, presentation, or slide), optional ``path``,
        ``set``, ``method``, ``args``, ``kwargs``, ``get``, and
        ``continue_on_error``. A path item is either a property name or
        ``{"member":"Item","args":[1]}``.

        Example: [{"target":"selection","path":["Font"],"set":{"Bold":true}},
        {"target":"selection","method":"TypeText","args":["Hello"]}]

        :param app_type: writer, spreadsheet, or presentation.
        :param operations: Structured COM operation list.
        :param create_if_missing: Launch WPS and create an active file when needed.
        :param visible: Show the WPS window.
        :return: Per-operation results and active WPS context.
        """
        try:
            with self._automation.com_scope():
                app, normalized, attached = self._automation.connect(
                    app_type,
                    visible=visible,
                    create_if_missing=create_if_missing,
                )
                self._automation.get_or_create_file(app, normalized, create_if_missing=create_if_missing)
                results = self._automation.execute_com_operations(app, normalized, operations)
                completed = sum(1 for item in results if item.get("success"))
                all_success = completed == len(operations)
                response = _success(
                    "COM operations completed" if all_success else "COM operations stopped after an error",
                    app_type=normalized,
                    completed=completed,
                    requested=len(operations),
                    results=results,
                    context=self._automation.context_info(app, normalized, attached),
                )
                response["success"] = all_success
                return response
        except Exception as exc:
            return _failure(exc)

    def execute_scenario(
        self,
        app_type: str,
        actions: List[Dict[str, Any]],
        auto_save: bool = False,
        save_path: str = "",
        create_if_missing: bool = True,
    ) -> Dict[str, Any]:
        """
        Run a multi-step WPS scene described as ordered action dictionaries.

        Writer actions: read_text, write, find_text, insert_at_text, table,
        page_break. Spreadsheet actions: write. Presentation actions: add_slide.
        All applications also support com and save. This is useful when the
        agent wants one atomic scene-level call.

        :param app_type: writer, spreadsheet, or presentation.
        :param actions: Ordered action dictionaries; each requires an action field.
        :param auto_save: Save after all actions.
        :param save_path: Optional SaveAs path used when auto_save is true.
        :param create_if_missing: Launch WPS and create an active file when needed.
        :return: Per-action results.
        """
        try:
            with self._automation.com_scope():
                app, normalized, attached = self._automation.connect(
                    app_type, visible=True, create_if_missing=create_if_missing
                )
                self._automation.get_or_create_file(app, normalized, create_if_missing=create_if_missing)
                results: List[Dict[str, Any]] = []
                for index, action in enumerate(actions, 1):
                    kind = str(action.get("action", "")).strip().lower()
                    try:
                        if kind == "write" and normalized == "writer":
                            value = self._automation.write_writer(
                                app,
                                str(action.get("text", "")),
                                str(action.get("mode", "cursor")),
                                action.get("style", {}),
                                int(action.get("chunk_size", 0)),
                                int(action.get("interval_ms", 0)),
                            )
                        elif kind in {"read_text", "read"} and normalized == "writer":
                            value = self._automation.read_writer_text(
                                app,
                                str(action.get("scope", "document")),
                                int(action.get("start_char", 0)),
                                int(action.get("max_chars", 12000)),
                                bool(action.get("normalize_line_endings", True)),
                            )
                        elif kind in {"find_text", "find"} and normalized == "writer":
                            value = self._automation.find_writer_text(
                                app,
                                str(action.get("find_text", action.get("anchor_text", ""))),
                                int(action.get("occurrence", 1)),
                                bool(action.get("match_case", False)),
                                bool(action.get("match_whole_word", False)),
                                bool(action.get("match_wildcards", False)),
                                bool(action.get("select_match", True)),
                            )
                        elif (
                            kind
                            in {
                                "insert_at_text",
                                "insert_after_text",
                                "insert_before_text",
                                "replace_at_text",
                            }
                            and normalized == "writer"
                        ):
                            position = str(action.get("position", "after"))
                            if kind == "insert_after_text":
                                position = "after"
                            elif kind == "insert_before_text":
                                position = "before"
                            elif kind == "replace_at_text":
                                position = "replace"
                            value = self._automation.insert_writer_text_at_anchor(
                                app,
                                str(action.get("anchor_text", action.get("find_text", ""))),
                                str(action.get("text", "")),
                                position,
                                action.get("style", {}),
                                int(action.get("occurrence", 1)),
                                bool(action.get("match_case", False)),
                                bool(action.get("match_whole_word", False)),
                                bool(action.get("match_wildcards", False)),
                                max(0, int(action.get("chunk_size", 0))),
                                max(0, int(action.get("interval_ms", 0))),
                            )
                        elif kind == "table" and normalized == "writer":
                            value = self._automation.insert_writer_table(
                                app,
                                action.get("data", []),
                                int(action.get("rows", 0)),
                                int(action.get("columns", 0)),
                            )
                        elif kind == "page_break" and normalized == "writer":
                            app.Selection.InsertBreak(WD_PAGE_BREAK)
                            value = {"inserted": "page_break"}
                        elif kind == "write" and normalized == "spreadsheet":
                            value = self._automation.write_sheet(
                                app,
                                str(action.get("range_address", "A1")),
                                action.get("values", ""),
                                str(action.get("sheet_name", "")),
                                bool(action.get("formula", False)),
                                str(action.get("number_format", "")),
                                bool(action.get("auto_fit", True)),
                            )
                        elif kind == "add_slide" and normalized == "presentation":
                            value = self._automation.add_slide(
                                app,
                                str(action.get("title", "")),
                                str(action.get("body", "")),
                                int(action.get("layout", PP_LAYOUT_BLANK)),
                                str(action.get("speaker_notes", "")),
                            )
                        elif kind == "com":
                            value = {
                                "operations": self._automation.execute_com_operations(
                                    app, normalized, action.get("operations", [])
                                )
                            }
                        elif kind == "save":
                            value = self._automation.save_file(app, normalized, str(action.get("file_path", "")))
                        else:
                            raise ValueError(f"Unsupported action '{kind}' for {normalized}")
                        results.append({"index": index, "action": kind, "success": True, **value})
                    except Exception as exc:
                        results.append({"index": index, "action": kind, "success": False, "error": str(exc)})
                        if not action.get("continue_on_error", False):
                            break

                if auto_save:
                    save_result = self._automation.save_file(app, normalized, save_path)
                    results.append({"index": len(results) + 1, "action": "auto_save", "success": True, **save_result})

                completed = sum(1 for item in results if item.get("success"))
                all_success = completed == len(results)
                response = _success(
                    "WPS scenario completed" if all_success else "WPS scenario stopped after an error",
                    app_type=normalized,
                    completed=completed,
                    results=results,
                    context=self._automation.context_info(app, normalized, attached),
                )
                response["success"] = all_success
                return response
        except Exception as exc:
            return _failure(exc)

    def save(self, app_type: str, file_path: str = "") -> Dict[str, Any]:
        """
        Save the active WPS file, optionally using SaveAs.

        :param app_type: writer, spreadsheet, or presentation.
        :param file_path: Optional SaveAs output path; empty saves the active file in place.
        :return: Saved file information.
        """
        try:
            with self._automation.com_scope():
                app, normalized, attached = self._automation.connect(
                    app_type, visible=True, create_if_missing=False
                )
                result = self._automation.save_file(app, normalized, file_path)
                return _success(
                    "Active WPS file saved",
                    **result,
                    context=self._automation.context_info(app, normalized, attached),
                )
        except Exception as exc:
            return _failure(exc)
