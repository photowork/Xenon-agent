from __future__ import annotations

import ast
import re
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set


TOOL_RESULT_COMPRESSED_PREFIX = "[TOOL_RESULT_COMPRESSED]"
TOOL_RESULT_EXTERNALIZED_PREFIX = "[TOOL_RESULT_EXTERNALIZED]"
TOOL_RESULT_ARCHIVE_NOTE = "[Full raw tool result is preserved in the archived conversation history.]"
TOOL_ARGUMENT_OMISSION_KEY = "__xenon_omitted_after_successful_write__"
WRITE_PAYLOAD_COMPACTED_KEY = "context_compacted"
BASE64_REDACTED_PREFIX = "[BASE64_REDACTED"
BASE64_MIN_TOKEN_CHARS = 256
MUTATING_TOOL_NAME_PARTS = (
    "write",
    "create",
    "append",
    "insert",
    "str_replace",
    "replace",
    "delete",
    "move",
    "copy",
)
DATA_URI_BASE64_RE = re.compile(
    r"data:(?P<mime>[-\w.+/]+)?;base64,(?P<data>[A-Za-z0-9+/=\s]{128,})",
    re.IGNORECASE,
)
BASE64_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9+/=])(?P<data>[A-Za-z0-9+/]{256,}={0,2})(?![A-Za-z0-9+/=])"
)
PATH_SUMMARY_KEYS = {
    "path",
    "file_path",
    "raw_result_path",
    "source_path",
    "destination_path",
    "working_dir",
}
INLINE_CONTENT_TOOL_PARTS = (
    "read_file_content",
    "read_file_chunked",
    "view_code",
)
INLINE_CONTENT_MAX_CHARS = 6500
INLINE_CONTENT_MAX_TOTAL_CHARS = 9000
EXTERNALIZED_PREVIEW_CHARS = 1200


class CompactedToolArgumentError(ValueError):
    """Kept for import compatibility; write payload compaction is disabled."""

    def __init__(self, tool_name: str, fields: List[str]):
        self.tool_name = tool_name
        self.fields = fields
        joined_fields = ", ".join(fields)
        super().__init__(
            f"Tool arguments contain compacted context placeholders in: {joined_fields}."
        )


def redact_base64_payloads(value: Any, *, min_chars: int = BASE64_MIN_TOKEN_CHARS) -> Any:
    if isinstance(value, dict):
        return {key: redact_base64_payloads(item, min_chars=min_chars) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_base64_payloads(item, min_chars=min_chars) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_base64_payloads(item, min_chars=min_chars) for item in value)
    if not isinstance(value, str):
        return value

    return _redact_base64_in_text(value, min_chars=min_chars)


def _redact_base64_in_text(text: str, *, min_chars: int = BASE64_MIN_TOKEN_CHARS) -> str:
    if not text or len(text) < min_chars:
        return text

    def replace_data_uri(match: re.Match[str]) -> str:
        raw = match.group("data")
        mime = match.group("mime") or "application/octet-stream"
        compact_len = len("".join(raw.split()))
        return f"{BASE64_REDACTED_PREFIX} mime={mime} chars={compact_len}]"

    redacted = DATA_URI_BASE64_RE.sub(replace_data_uri, text)

    def replace_token(match: re.Match[str]) -> str:
        token = match.group("data")
        if not _looks_like_base64_token(token, min_chars=min_chars):
            return token
        return f"{BASE64_REDACTED_PREFIX} chars={len(token)}]"

    return BASE64_TOKEN_RE.sub(replace_token, redacted)


def _looks_like_base64_token(token: str, *, min_chars: int = BASE64_MIN_TOKEN_CHARS) -> bool:
    compact = "".join(str(token).split())
    if len(compact) < min_chars:
        return False
    if len(compact) % 4 not in (0, 2, 3):
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", compact):
        return False

    # Require a mixed alphabet so ordinary long identifiers are not redacted.
    classes = sum(
        1
        for pattern in (r"[A-Z]", r"[a-z]", r"[0-9]", r"[+/=]")
        if re.search(pattern, compact)
    )
    return classes >= 3


def _parse_payload_text(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        try:
            return ast.literal_eval(text)
        except Exception:
            return None


def _compact_path_for_context(value: Any) -> str:
    text = str(value).replace("\\", "/")
    for marker in (
        "/generated/tool_results/",
        "/work/",
        "/logs/",
        "/Tools/",
        "/xenon_core/",
        "/tests/",
    ):
        index = text.find(marker)
        if index >= 0:
            return text[index + 1 :]
    return text


def _summarize_value(value: Any, truncate_fn: Callable[[str, int], str]) -> str:
    value = redact_base64_payloads(value)
    if isinstance(value, dict):
        return f"dict[{len(value)}]"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if isinstance(value, tuple):
        return f"tuple[{len(value)}]"
    return truncate_fn(str(value), 80)


def _summarize_path_value(value: Any, truncate_fn: Callable[[str, int], str]) -> str:
    value = redact_base64_payloads(value)
    if isinstance(value, (dict, list, tuple)):
        return _summarize_value(value, truncate_fn)
    return truncate_fn(_compact_path_for_context(value), 100)


def _looks_like_mutating_tool(tool_name: str) -> bool:
    lowered = (tool_name or "").lower()
    return any(part in lowered for part in MUTATING_TOOL_NAME_PARTS)


def _looks_like_inline_content_tool(tool_name: str) -> bool:
    lowered = (tool_name or "").lower()
    return any(part in lowered for part in INLINE_CONTENT_TOOL_PARTS)


def _truncate_content_preview(text: str, max_chars: int) -> str:
    text = str(text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]"


def _build_inline_content_tool_result(
    parsed: Any,
    *,
    tool_name: str,
    max_content_chars: int = INLINE_CONTENT_MAX_CHARS,
    max_total_chars: int = INLINE_CONTENT_MAX_TOTAL_CHARS,
) -> Optional[str]:
    if not _looks_like_inline_content_tool(tool_name):
        return None
    if not isinstance(parsed, dict):
        return None

    content = parsed.get("content")
    if not isinstance(content, str) or not content:
        return None

    redacted_content = str(redact_base64_payloads(content))
    if len(redacted_content) > max_content_chars:
        return None

    lines: List[str] = []
    for key in (
        "success",
        "message",
        "path",
        "file_path",
        "line_count",
        "size",
        "chunk_index",
        "total_chunks",
        "start_char",
        "end_char",
        "has_more",
        "is_last_chunk",
        "next_chunk_index",
        "remaining_chunks",
    ):
        value = parsed.get(key)
        if value in (None, "", [], {}):
            continue
        if key in PATH_SUMMARY_KEYS:
            value = _compact_path_for_context(value)
        lines.append(f"{key}={value}")

    if not lines:
        lines.append("success=True")

    inline = "\n".join(lines) + "\ncontent:\n" + redacted_content
    if len(inline) > max_total_chars:
        return None
    return inline


def _build_externalized_preview(parsed: Any, redacted_text: str) -> str:
    if isinstance(parsed, dict):
        content = parsed.get("content")
        if isinstance(content, str) and content.strip():
            return _truncate_content_preview(content, EXTERNALIZED_PREVIEW_CHARS)
        return ""

    if isinstance(parsed, list):
        return ""

    return _truncate_content_preview(redacted_text, EXTERNALIZED_PREVIEW_CHARS)


def summarize_tool_payload_for_context(
    content: str,
    *,
    max_chars: int = 240,
    max_lines: int = 8,
) -> str:
    if not content:
        return ""

    def _truncate(text: str, truncate_to: int = max_chars) -> str:
        text = text.strip()
        if len(text) > truncate_to:
            return text[:truncate_to].rstrip() + "..."
        return text

    text = str(redact_base64_payloads(content)).strip()
    if not text:
        return ""

    parsed = _parse_payload_text(text)

    if isinstance(parsed, dict):
        interesting_keys = [
            "success",
            "message",
            "summary",
            "error",
            "status",
            "path",
            "file_path",
            "count",
            "line_count",
            "size",
            "chunk_index",
            "total_chunks",
            "chunk_size",
            "has_more",
            "is_last_chunk",
            "next_chunk_index",
            "remaining_chunks",
            "content_complete",
            "source",
            "target",
            "total_paths",
            "paths",
            "model",
            "computed_values",
            "quantitative_effects",
            "equation_evaluation",
            "image",
            "image_data",
            "image_base64",
            "base64",
            "b64_json",
            "audio_data",
            "video_base64",
            "url",
        ]
        parts = []
        for key in interesting_keys:
            if key in parsed and parsed[key] not in (None, "", [], {}):
                if key in PATH_SUMMARY_KEYS:
                    parts.append(f"{key}={_summarize_path_value(parsed[key], _truncate)}")
                else:
                    parts.append(f"{key}={_summarize_value(parsed[key], _truncate)}")

        if not parts:
            remaining_keys = list(parsed.keys())[:6]
            parts.append(f"keys={', '.join(remaining_keys)}")

        return "; ".join(parts)

    if isinstance(parsed, list):
        preview_items = []
        for item in parsed[:3]:
            item = redact_base64_payloads(item)
            if isinstance(item, (dict, list)):
                preview_items.append(_truncate(json.dumps(item, ensure_ascii=False), 80))
            else:
                preview_items.append(_truncate(str(item), 80))
        return f"list[{len(parsed)}]: " + " | ".join(preview_items)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return _truncate(text)

    condensed = "\n".join(lines[:max_lines])
    return _truncate(condensed)


def compact_successful_write_tool_context(
    *,
    messages: List[Dict[str, Any]],
    tool_call_id: str,
    tool_name: str,
    arguments: Dict[str, Any],
    result: Any,
    min_chars: int = 480,
    preview_chars: int = 160,
) -> Dict[str, Any]:
    """No-op: write payload compaction is intentionally disabled."""
    return {
        "compacted": False,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "argument_fields": [],
        "result_fields": [],
        "reason": "write_compaction_disabled",
    }


def build_compact_tool_arguments_preview(
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    min_chars: int = 240,
    preview_chars: int = 120,
) -> Dict[str, Any]:
    """Return arguments with raw base64 payloads redacted for display."""
    return redact_base64_payloads(dict(arguments or {}))


def build_compact_tool_result_preview(
    tool_name: str,
    arguments: Dict[str, Any],
    result: Any,
    *,
    min_chars: int = 240,
    preview_chars: int = 120,
) -> str:
    """Return results with raw base64 payloads redacted for display."""
    return str(redact_base64_payloads(result))


def externalize_tool_result_for_context(
    *,
    content: str,
    tool_name: str,
    tool_call_id: str,
    archive_dir: Path,
    summarize_tool_payload_fn: Callable[[str], str],
    min_chars: int = 4000,
    retention_days: Optional[int] = 7,
    max_files: int = 500,
    max_total_bytes: int = 512 * 1024 * 1024,
    now: Optional[datetime] = None,
) -> str:
    text = str(content or "")
    parsed = _parse_payload_text(text)
    if parsed is not None:
        redacted_parsed = redact_base64_payloads(parsed)
        has_redaction = redacted_parsed != parsed
        redacted_text = str(redacted_parsed) if has_redaction else text
    else:
        redacted_parsed = None
        redacted_text = str(redact_base64_payloads(text))
        has_redaction = redacted_text != text

    inline_content_result = _build_inline_content_tool_result(
        redacted_parsed,
        tool_name=tool_name,
    )
    if inline_content_result is not None:
        return inline_content_result

    if not has_redaction and len(text) < min_chars:
        return text

    if _looks_like_mutating_tool(tool_name):
        return redacted_text

    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    current_time = now or datetime.now()
    timestamp = current_time.strftime("%Y%m%d_%H%M%S_%f")
    safe_tool = _safe_filename_part(tool_name or "tool")
    safe_call = _safe_filename_part(tool_call_id or "call")
    raw_result_path = archive_dir / f"{timestamp}_{safe_tool}_{safe_call}.txt"
    raw_result_path.write_text(text, encoding="utf-8")
    cleanup_externalized_tool_results(
        archive_dir,
        retention_days=retention_days,
        max_files=max_files,
        max_total_bytes=max_total_bytes,
        protected_paths={raw_result_path},
        now=current_time,
    )

    summary = summarize_tool_payload_fn(redacted_text) or summarize_tool_payload_for_context(redacted_text)
    if not summary:
        summary = "raw result externalized"

    preview = _build_externalized_preview(redacted_parsed, redacted_text)
    lines = [
        TOOL_RESULT_EXTERNALIZED_PREFIX,
        f"summary={summary}",
    ]
    if preview:
        lines.extend(["preview:", preview])
    lines.extend(
        [
            f"raw_result_path={raw_result_path.as_posix()}",
            f"raw_result_chars={len(text)}",
        ]
    )
    if has_redaction:
        lines.append("base64_redacted=true")
    return "\n".join(lines)


def cleanup_externalized_tool_results(
    archive_dir: Path,
    *,
    retention_days: Optional[int] = 7,
    max_files: int = 500,
    max_total_bytes: int = 512 * 1024 * 1024,
    protected_paths: Optional[Set[Path]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    archive_dir = Path(archive_dir)
    if not archive_dir.exists():
        return {"deleted_count": 0, "deleted_bytes": 0, "remaining_count": 0, "remaining_bytes": 0}

    protected = {path.resolve() for path in protected_paths or set()}
    current_ts = (now or datetime.now()).timestamp()
    deleted_count = 0
    deleted_bytes = 0

    def list_files() -> List[Path]:
        return [path for path in archive_dir.glob("*.txt") if path.is_file()]

    def delete_file(path: Path) -> None:
        nonlocal deleted_count, deleted_bytes
        if path.resolve() in protected:
            return
        try:
            size = path.stat().st_size
            path.unlink()
            deleted_count += 1
            deleted_bytes += size
        except FileNotFoundError:
            pass

    if retention_days is not None and retention_days > 0:
        cutoff_ts = current_ts - retention_days * 24 * 60 * 60
        for path in list_files():
            try:
                if path.stat().st_mtime < cutoff_ts:
                    delete_file(path)
            except FileNotFoundError:
                continue

    if max_files > 0:
        files = sorted(
            list_files(),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in files[max_files:]:
            delete_file(path)

    if max_total_bytes > 0:
        files = sorted(
            list_files(),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        total = 0
        for path in files:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            if total + size <= max_total_bytes or path.resolve() in protected:
                total += size
                continue
            delete_file(path)

    remaining_files = list_files()
    remaining_bytes = 0
    for path in remaining_files:
        try:
            remaining_bytes += path.stat().st_size
        except FileNotFoundError:
            pass

    return {
        "deleted_count": deleted_count,
        "deleted_bytes": deleted_bytes,
        "remaining_count": len(remaining_files),
        "remaining_bytes": remaining_bytes,
    }


def _safe_filename_part(value: str, limit: int = 80) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    if not safe:
        return "item"
    return safe[:limit]


def sanitize_tool_arguments_for_execution(tool_name: str, arguments: Any) -> Any:
    """Remove old compaction metadata keys without changing executable payloads."""
    if not isinstance(arguments, dict):
        return arguments

    return {
        key: value
        for key, value in arguments.items()
        if key not in (WRITE_PAYLOAD_COMPACTED_KEY, TOOL_ARGUMENT_OMISSION_KEY)
    }


def compress_tool_messages_in_place(
    messages: List[Dict[str, Any]],
    *,
    summarize_tool_payload_fn: Callable[[str], str],
    protected_indices: Optional[Set[int]] = None,
    allow_protected: bool = False,
) -> int:
    protected_indices = protected_indices or set()
    compressed_count = 0

    for index, message in enumerate(messages):
        if message.get("role") != "tool":
            continue
        if not allow_protected and index in protected_indices:
            continue

        content = (message.get("content", "") or "").strip()
        if (
            not content
            or content.startswith(TOOL_RESULT_COMPRESSED_PREFIX)
            or content.startswith(TOOL_RESULT_EXTERNALIZED_PREFIX)
        ):
            continue

        redacted_content = str(redact_base64_payloads(content))
        summary = summarize_tool_payload_fn(redacted_content)
        if not summary:
            continue

        compressed = (
            f"{TOOL_RESULT_COMPRESSED_PREFIX}\n"
            f"{summary}\n"
            f"{TOOL_RESULT_ARCHIVE_NOTE}"
        )
        if len(compressed) >= len(content):
            if redacted_content != content and len(redacted_content) < len(content):
                message["content"] = redacted_content
                compressed_count += 1
            continue

        message["content"] = compressed
        compressed_count += 1

    return compressed_count
