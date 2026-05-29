#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DashScope Fun-ASR (Speech-to-Text) tool for Xenon.

Uses the DashScope non-realtime transcription API with the fun-asr model.
Supports direct HTTP/HTTPS/OSS URLs and local files. Local files are uploaded
to DashScope temporary OSS storage before the transcription task is submitted.
Integrates with video_handler.extract_audio for video transcription.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "asr_config.json"
DEFAULT_API_BASE = "https://dashscope.aliyuncs.com/api/v1"
DEFAULT_FILE_ENDPOINT = f"{DEFAULT_API_BASE}/files"
DEFAULT_TRANSCRIPTION_ENDPOINT = (
    f"{DEFAULT_API_BASE}/services/audio/asr/transcription"
)
DEFAULT_OUTPUT_DIR = Path("work") / "asr_outputs"

SUPPORTED_FORMATS = {"wav", "mp3", "m4a", "aac", "flac", "ogg", "opus", "pcm", "amr"}
SUPPORTED_LANGUAGES = [
    "auto", "zh", "en", "ja", "ko", "fr", "de", "es", "pt", "ru", "ar",
    "th", "vi", "id", "ms", "tl", "hi", "it", "nl", "sv", "da", "fi",
    "no", "el", "pl", "cs", "hu", "ro", "bg", "hr", "sk",
]

# ---------------------------------------------------------------------------
# Helper: extract audio helper (calls video_handler if available)
# ---------------------------------------------------------------------------

def _extract_audio_from_video(video_path: str, output_path: str = "", timeout: int = 1800) -> Dict[str, Any]:
    """Try to extract audio using video_handler if available."""
    try:
        from Tools.video_handler import VideoHandler
        handler = VideoHandler()
        return handler.extract_audio(
            input_path=video_path,
            output_path=output_path,
            audio_codec="pcm_s16le",
            bitrate="",
            overwrite=True,
            timeout=timeout,
        )
    except ImportError:
        return {"success": False, "error": "video_handler not available"}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# ASRHandler (core logic)
# ---------------------------------------------------------------------------

class ASRHandler:
    """Internal DashScope ASR transcription client."""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = Path(config_path).expanduser().resolve() if config_path else DEFAULT_CONFIG_PATH

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _format_response(success: bool, **kwargs: Any) -> Dict[str, Any]:
        result: Dict[str, Any] = {"success": success}
        result.update(kwargs)
        return result

    @staticmethod
    def _redact_secret(value: Optional[str]) -> str:
        if not value:
            return ""
        if len(value) <= 8:
            return "***"
        return f"{value[:4]}...{value[-4:]}"

    def _load_config(self, config_path: Optional[str] = None) -> Dict[str, Any]:
        path = Path(config_path).expanduser().resolve() if config_path else self.config_path
        config: Dict[str, Any] = {}
        if path.exists():
            with open(path, "r", encoding="utf-8") as handle:
                config = json.load(handle)

        env_key_name = str(config.get("api_key_env") or "DASHSCOPE_API_KEY")
        api_key = str(config.get("api_key") or os.getenv(env_key_name) or "").strip()
        api_base = str(config.get("api_base") or DEFAULT_API_BASE).rstrip("/")
        transcription_endpoint = str(config.get("transcription_endpoint") or DEFAULT_TRANSCRIPTION_ENDPOINT).rstrip("/")
        legacy_transcription_endpoint = f"{api_base}/services/asr/transcription/transcription"
        if transcription_endpoint == legacy_transcription_endpoint:
            transcription_endpoint = f"{api_base}/services/audio/asr/transcription"

        return {
            "path": str(path),
            "api_key": api_key,
            "api_key_env": env_key_name,
            "api_base": api_base,
            "file_endpoint": str(config.get("file_endpoint") or f"{api_base}/files"),
            "transcription_endpoint": transcription_endpoint,
            "model": str(config.get("model") or "fun-asr"),
            "default_sample_rate": int(config.get("default_sample_rate", 16000)),
            "default_format": str(config.get("default_format") or "wav"),
            "default_language": str(config.get("default_language") or "auto"),
            "default_output_dir": str(config.get("default_output_dir") or DEFAULT_OUTPUT_DIR),
            "timeout": int(config.get("timeout", 600)),
            "poll_interval": int(config.get("poll_interval", 3)),
        }

    def _build_output_path(
        self,
        source_name: str,
        output_path: str,
        output_dir: str,
        suffix: str = ".srt",
    ) -> Path:
        if output_path:
            target = Path(output_path).expanduser()
            if not target.suffix:
                target = target.with_suffix(suffix)
            elif target.suffix.lower() not in (".srt", ".ass", ".json", ".txt"):
                target = target.with_suffix(suffix)
        else:
            stem = Path(source_name).stem
            digest = hashlib.sha1(source_name.encode("utf-8")).hexdigest()[:8]
            filename = f"asr_{stem}_{time.strftime('%Y%m%d_%H%M%S')}_{digest}{suffix}"
            target = Path(output_dir).expanduser() / filename

        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def _format_timestamp(seconds: float) -> str:
        """Convert seconds to SRT timestamp format (HH:MM:SS,mmm)."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        millis = int(round((seconds - int(seconds)) * 1000))
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    def _generate_srt(
        self,
        segments: List[Dict[str, Any]],
        output_path: str,
    ) -> str:
        """Generate SRT subtitle content from segments and write to file."""
        lines: List[str] = []
        for i, seg in enumerate(segments, 1):
            start = seg.get("begin_time", seg.get("start", 0)) / 1000.0
            end = seg.get("end_time", seg.get("end", start + 1.0)) / 1000.0
            text = seg.get("text", "").strip()
            if not text:
                continue
            lines.append(str(i))
            lines.append(f"{self._format_timestamp(start)} --> {self._format_timestamp(end)}")
            lines.append(text)
            lines.append("")

        content = "\n".join(lines)
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return content

    def _generate_json(
        self,
        segments: List[Dict[str, Any]],
        full_text: str,
        output_path: str,
        language: str = "",
        duration: float = 0.0,
    ) -> str:
        """Generate JSON transcript and write to file."""
        payload = {
            "text": full_text,
            "language": language or "auto",
            "duration": duration,
            "segments": segments,
        }
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # -- file upload -------------------------------------------------------

    def _upload_file(self, audio_path: str, config: Dict[str, Any]) -> Dict[str, Any]:
        """Upload a local audio file to DashScope and return its file URL."""
        path = Path(audio_path).expanduser().resolve()
        if not path.exists():
            return self._format_response(False, error=f"File not found: {path}")

        file_size = path.stat().st_size
        file_endpoint = config["file_endpoint"]
        headers = {
            "Authorization": f"Bearer {config['api_key']}",
        }

        try:
            with open(path, "rb") as handle:
                files = {"file": (path.name, handle, "audio/wav")}
                resp = requests.post(
                    file_endpoint,
                    headers=headers,
                    files=files,
                    timeout=config["timeout"],
                )
                resp_text = resp.text
                try:
                    data = resp.json()
                except ValueError:
                    data = {}

            if resp.status_code >= 400:
                return self._format_response(
                    False,
                    status_code=resp.status_code,
                    error=data.get("message", data.get("code", resp_text[:500])),
                    message="File upload to DashScope failed",
                )

            # New API format: data.data.uploaded_files[0].file_id
            file_id = ""
            inner = data.get("data", {})
            if isinstance(inner, dict):
                uploaded = inner.get("uploaded_files", [])
                if uploaded and isinstance(uploaded, list):
                    file_id = uploaded[0].get("file_id", "")

            if file_id:
                # Construct file URL from file_id using the file endpoint base
                file_endpoint_base = config.get("file_endpoint", "https://dashscope.aliyuncs.com/api/v1/files").rstrip("/")
                file_url = f"{file_endpoint_base}/{file_id}"
            else:
                # Fallback to old format: data.output.file_url
                file_url = data.get("output", {}).get("file_url", "")

            if not file_url:
                return self._format_response(
                    False,
                    error="No file_url or file_id in upload response",
                    raw_response=resp_text[:500],
                    message="File upload response missing file_url or file_id",
                )

            return self._format_response(
                True,
                file_url=file_url,
                file_name=path.name,
                file_size=file_size,
                message=f"File uploaded: {path.name}",
            )

        except requests.RequestException as exc:
            return self._format_response(False, error=str(exc), message="File upload request failed")

    def _upload_file_to_temp_oss(
        self,
        file_path: str,
        config: Dict[str, Any],
        model: str = "",
    ) -> Dict[str, Any]:
        """Upload a local file to DashScope temporary OSS and return an oss:// URL."""
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            return self._format_response(False, error=f"File not found: {path}")
        if not path.is_file():
            return self._format_response(False, error=f"Path is not a file: {path}")

        file_size = path.stat().st_size
        model_name = (model or config.get("model") or "fun-asr").strip()
        headers = {
            "Authorization": f"Bearer {config['api_key']}",
        }

        try:
            policy_resp = requests.get(
                f"{config['api_base']}/uploads",
                headers=headers,
                params={"action": "getPolicy", "model": model_name},
                timeout=config["timeout"],
            )
            policy_text = policy_resp.text
            try:
                policy_data = policy_resp.json()
            except ValueError:
                policy_data = {}

            if policy_resp.status_code >= 400:
                return self._format_response(
                    False,
                    status_code=policy_resp.status_code,
                    error=policy_data.get("message", policy_data.get("code", policy_text[:500])),
                    message="Failed to get DashScope temporary upload policy",
                )

            policy = policy_data.get("data") or policy_data.get("output") or policy_data
            if not isinstance(policy, dict):
                return self._format_response(
                    False,
                    error="Upload policy response is not an object",
                    raw_response=policy_text[:500],
                    message="Invalid DashScope upload policy response",
                )

            required_fields = ("upload_host", "upload_dir", "policy", "signature", "oss_access_key_id")
            missing = [field for field in required_fields if not policy.get(field)]
            if missing:
                return self._format_response(
                    False,
                    error=f"Upload policy missing fields: {', '.join(missing)}",
                    raw_response=policy_text[:500],
                    message="Invalid DashScope upload policy response",
                )

            safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", path.stem).strip("._") or "audio"
            suffix = path.suffix.lower() or ".bin"
            digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
            upload_name = f"{safe_stem}_{int(time.time())}_{digest}{suffix}"
            object_key = f"{str(policy['upload_dir']).rstrip('/')}/{upload_name}"

            form_fields = [
                ("key", object_key),
                ("OSSAccessKeyId", str(policy["oss_access_key_id"])),
                ("policy", str(policy["policy"])),
                ("Signature", str(policy["signature"])),
                ("success_action_status", "200"),
                ("x-oss-object-acl", str(policy.get("x_oss_object_acl") or "private")),
                ("x-oss-forbid-overwrite", str(policy.get("x_oss_forbid_overwrite") or "true")),
            ]

            security_token = policy.get("x_oss_security_token") or policy.get("security_token")
            if security_token:
                form_fields.append(("x-oss-security-token", str(security_token)))

            with open(path, "rb") as handle:
                upload_resp = requests.post(
                    str(policy["upload_host"]),
                    data=form_fields,
                    files={"file": (upload_name, handle)},
                    timeout=config["timeout"],
                )

            if upload_resp.status_code >= 400:
                return self._format_response(
                    False,
                    status_code=upload_resp.status_code,
                    error=upload_resp.text[:500],
                    message="Temporary OSS file upload failed",
                )

            return self._format_response(
                True,
                file_url=f"oss://{object_key}",
                file_name=path.name,
                upload_name=upload_name,
                file_size=file_size,
                expire_in_seconds=policy.get("expire_in_seconds"),
                message=f"File uploaded to temporary OSS: {path.name}",
            )

        except requests.RequestException as exc:
            return self._format_response(
                False,
                error=str(exc),
                message="Temporary OSS upload request failed",
            )

    # -- transcription API -------------------------------------------------

    def _submit_transcription(
        self,
        file_url: str,
        config: Dict[str, Any],
        language: str = "",
        sample_rate: int = 0,
        audio_format: str = "",
        diarization: bool = False,
        channel_id: int = -1,
    ) -> Dict[str, Any]:
        """Submit an ASR transcription task and return the task_id."""
        endpoint = config["transcription_endpoint"]
        headers = {
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        if file_url.startswith("oss://"):
            headers["X-DashScope-OssResourceResolve"] = "enable"

        input_data: Dict[str, Any] = {
            "file_urls": [file_url],
        }

        parameters: Dict[str, Any] = {}
        model = config["model"].strip()
        is_fun_asr = model.lower() == "fun-asr"

        lang = (language or config["default_language"]).strip().lower()
        if lang and lang != "auto":
            if is_fun_asr:
                parameters["language_hints"] = [lang]
            else:
                input_data["language"] = lang

        # Fun-ASR infers audio format and sample rate from the file. Keep the
        # legacy hints only when callers explicitly switch back to another model.
        if not is_fun_asr:
            sr = sample_rate or config["default_sample_rate"]
            if sr:
                parameters["sample_rate"] = sr

            fmt = audio_format or config["default_format"]
            if fmt:
                parameters["format"] = fmt

        if diarization:
            parameters["diarization_enabled"] = True

        channel_ids: List[int] = []
        if isinstance(channel_id, list):
            for ch in channel_id:
                try:
                    ch_int = int(ch)
                except (TypeError, ValueError):
                    continue
                if ch_int >= 0:
                    channel_ids.append(ch_int)
        else:
            try:
                ch_int = int(channel_id)
            except (TypeError, ValueError):
                ch_int = -1
            if ch_int >= 0:
                channel_ids = [ch_int]
        if channel_ids:
            parameters["channel_id"] = channel_ids

        payload: Dict[str, Any] = {
            "model": model,
            "input": input_data,
        }
        if parameters:
            payload["parameters"] = parameters

        try:
            resp = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=config["timeout"],
            )
            resp_text = resp.text
            try:
                data = resp.json()
            except ValueError:
                data = {}

            if resp.status_code >= 400:
                return self._format_response(
                    False,
                    status_code=resp.status_code,
                    error=data.get("message", data.get("code", resp_text[:500])),
                    message="Transcription submission failed",
                )

            output = data.get("output", {})
            task_id = output.get("task_id", "")
            task_status = output.get("task_status", "")

            if not task_id:
                # Maybe it's a synchronous response with direct results
                if "results" in output or "result" in output:
                    return self._format_response(True, task_id="", task_status="SUCCEEDED", output=output)
                return self._format_response(
                    False,
                    error="No task_id in response",
                    raw_response=resp_text[:500],
                    message="Transcription response missing task_id",
                )

            return self._format_response(
                True,
                task_id=task_id,
                task_status=task_status or "PENDING",
                request_id=data.get("request_id", ""),
                message=f"Transcription task submitted: {task_id}",
            )

        except requests.RequestException as exc:
            return self._format_response(False, error=str(exc), message="Transcription request failed")

    def _poll_task(
        self,
        task_id: str,
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Poll a transcription task until completion or timeout."""
        task_endpoint = f"{config['api_base']}/tasks/{task_id}"
        headers = {
            "Authorization": f"Bearer {config['api_key']}",
            "X-DashScope-Async": "enable",
        }

        deadline = time.time() + config["timeout"]
        poll_interval = config["poll_interval"]

        while time.time() < deadline:
            try:
                resp = requests.get(
                    task_endpoint,
                    headers=headers,
                    timeout=config["timeout"],
                )
                resp_text = resp.text
                try:
                    data = resp.json()
                except ValueError:
                    data = {}

                if resp.status_code >= 400:
                    return self._format_response(
                        False,
                        task_id=task_id,
                        status_code=resp.status_code,
                        error=data.get("message", resp_text[:500]),
                        message="Task polling failed",
                    )

                output = data.get("output", {})
                task_status = output.get("task_status", "")

                if task_status == "SUCCEEDED":
                    return self._format_response(
                        True,
                        task_id=task_id,
                        task_status="SUCCEEDED",
                        output=output,
                        request_id=data.get("request_id", ""),
                    )

                if task_status == "FAILED":
                    return self._format_response(
                        False,
                        task_id=task_id,
                        task_status="FAILED",
                        error=output.get("message", "Transcription task failed"),
                        message="Transcription task failed",
                    )

                if task_status in ("CANCELED", "CANCELLED"):
                    return self._format_response(
                        False,
                        task_id=task_id,
                        task_status=task_status,
                        error="Task was cancelled",
                        message="Transcription task cancelled",
                    )

                # Still running
                time.sleep(poll_interval)

            except requests.RequestException as exc:
                return self._format_response(
                    False,
                    task_id=task_id,
                    error=str(exc),
                    message="Task polling request failed",
                )

        return self._format_response(
            False,
            task_id=task_id,
            error=f"Timeout after {config['timeout']}s",
            message="Transcription task polling timed out",
        )

    def _download_transcription_results(
        self,
        output: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Download Fun-ASR result JSON files referenced by transcription_url."""
        results = output.get("results") or []
        if isinstance(results, dict):
            results = [results]
        if not isinstance(results, list) or not results:
            return self._format_response(True, output=output)

        downloaded: List[Dict[str, Any]] = []
        transcription_urls: List[str] = []
        subtask_errors: List[Dict[str, Any]] = []

        for item in results:
            if not isinstance(item, dict):
                continue

            status = str(item.get("subtask_status") or item.get("status") or "").upper()
            if status and status not in ("SUCCEEDED", "SUCCESS"):
                subtask_errors.append(item)
                continue

            if "transcripts" in item or "sentences" in item:
                downloaded.append(item)
                continue

            url = str(item.get("transcription_url") or "").strip()
            if not url:
                continue

            try:
                resp = requests.get(url, timeout=config["timeout"])
                resp_text = resp.text
                try:
                    data = resp.json()
                except ValueError:
                    data = {}

                if resp.status_code >= 400:
                    return self._format_response(
                        False,
                        status_code=resp.status_code,
                        error=resp_text[:500],
                        message="Failed to download transcription result",
                    )

                if isinstance(data, dict):
                    downloaded.append(data)
                    transcription_urls.append(url)
                else:
                    return self._format_response(
                        False,
                        error="Transcription result JSON is not an object",
                        message="Failed to parse transcription result",
                    )

            except requests.RequestException as exc:
                return self._format_response(
                    False,
                    error=str(exc),
                    message="Transcription result download failed",
                )

        if downloaded:
            return self._format_response(
                True,
                output={
                    "results": downloaded,
                    "task_output": output,
                    "subtask_results": results,
                },
                transcription_urls=transcription_urls,
            )

        if subtask_errors:
            return self._format_response(
                False,
                error="One or more transcription subtasks failed",
                subtask_errors=subtask_errors,
                message="Transcription completed with failed subtasks",
            )

        return self._format_response(True, output=output)

    # -- result parsing ----------------------------------------------------

    @staticmethod
    def _parse_results_legacy(output: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
        """Extract segments and full text from transcription output."""
        segments: List[Dict[str, Any]] = []
        full_text = ""

        # Try different response formats
        results = output.get("results") or output.get("result") or output.get("transcripts") or []
        if isinstance(results, dict):
            results = [results]

        if not results:
            # Try word-level results
            words = output.get("words") or output.get("word_results") or []
            if words:
                # Group words into sentences (simple heuristic: group by pause or punctuation)
                current_text = ""
                current_start = 0.0
                current_end = 0.0
                for w in words:
                    word_text = w.get("text", w.get("word", "")).strip()
                    if not word_text:
                        continue
                    w_start = w.get("begin_time", w.get("start", current_end)) / 1000.0
                    w_end = w.get("end_time", w.get("end", w_start + 0.2)) / 1000.0

                    if not current_text:
                        current_start = w_start
                    current_text += word_text
                    current_end = w_end

                    # Check for sentence boundary
                    if word_text in ("。", "！", "？", ".", "!", "?", "，", ","):
                        segments.append({
                            "text": current_text,
                            "begin_time": current_start * 1000,
                            "end_time": current_end * 1000,
                            "start": current_start,
                            "end": current_end,
                        })
                        full_text += current_text
                        current_text = ""
                # Flush remaining
                if current_text.strip():
                    segments.append({
                        "text": current_text,
                        "begin_time": current_start * 1000,
                        "end_time": current_end * 1000,
                        "start": current_start,
                        "end": current_end,
                    })
                    full_text += current_text
            else:
                # Fallback: single text field
                single_text = output.get("text") or output.get("full_text") or ""
                if single_text:
                    full_text = single_text
                    segments.append({
                        "text": single_text,
                        "begin_time": 0,
                        "end_time": 0,
                        "start": 0.0,
                        "end": 0.0,
                    })
        else:
            for r in results:
                text = r.get("text", r.get("sentence", r.get("transcript", ""))).strip()
                if not text:
                    continue
                begin = r.get("begin_time", r.get("start", r.get("begin", 0)))
                end = r.get("end_time", r.get("end", r.get("stop", 0)))

                # Convert to milliseconds if needed
                if isinstance(begin, float) and begin < 1000:
                    begin = int(begin * 1000)
                if isinstance(end, float) and end < 1000:
                    end = int(end * 1000)

                seg: Dict[str, Any] = {
                    "text": text,
                    "begin_time": begin,
                    "end_time": end,
                    "start": begin / 1000.0,
                    "end": end / 1000.0,
                }
                segments.append(seg)
                full_text += text

        return segments, full_text.strip()

    @staticmethod
    def _parse_results(output: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
        """Extract segments and full text from Fun-ASR and legacy outputs."""
        segments: List[Dict[str, Any]] = []
        full_text_parts: List[str] = []

        def to_milliseconds(value: Any, default: int = 0) -> int:
            if value in (None, ""):
                return default
            try:
                number = float(value)
            except (TypeError, ValueError):
                return default
            if number < 1000 and not float(number).is_integer():
                number *= 1000
            return int(round(number))

        def append_text(text: Any) -> None:
            cleaned = str(text or "").strip()
            if cleaned:
                full_text_parts.append(cleaned)

        def add_segment(
            text: Any,
            begin: Any = 0,
            end: Any = 0,
            extra: Optional[Dict[str, Any]] = None,
        ) -> None:
            cleaned = str(text or "").strip()
            if not cleaned:
                return
            begin_ms = to_milliseconds(begin)
            end_ms = to_milliseconds(end, begin_ms)
            if end_ms < begin_ms:
                end_ms = begin_ms
            seg: Dict[str, Any] = {
                "text": cleaned,
                "begin_time": begin_ms,
                "end_time": end_ms,
                "start": begin_ms / 1000.0,
                "end": end_ms / 1000.0,
            }
            if extra:
                seg.update({k: v for k, v in extra.items() if v not in (None, "")})
            segments.append(seg)

        def parse_fun_asr_payload(payload: Dict[str, Any]) -> bool:
            found = False
            transcripts = payload.get("transcripts") or []
            if isinstance(transcripts, dict):
                transcripts = [transcripts]

            if isinstance(transcripts, list) and transcripts:
                for transcript in transcripts:
                    if not isinstance(transcript, dict):
                        continue
                    transcript_text = transcript.get("text", "")
                    append_text(transcript_text)
                    channel = transcript.get("channel_id")
                    sentences = transcript.get("sentences") or []

                    if isinstance(sentences, list) and sentences:
                        for sentence in sentences:
                            if not isinstance(sentence, dict):
                                continue
                            add_segment(
                                sentence.get("text", sentence.get("sentence", "")),
                                sentence.get("begin_time", sentence.get("start", 0)),
                                sentence.get("end_time", sentence.get("end", 0)),
                                {
                                    "channel_id": channel,
                                    "speaker_id": sentence.get("speaker_id"),
                                    "words": sentence.get("words"),
                                },
                            )
                        found = True
                    elif transcript_text:
                        duration = (
                            transcript.get("content_duration_in_milliseconds")
                            or payload.get("properties", {}).get("audio_duration")
                            or payload.get("properties", {}).get("content_duration_in_milliseconds")
                            or 0
                        )
                        add_segment(transcript_text, 0, duration, {"channel_id": channel})
                        found = True

            top_sentences = payload.get("sentences") or []
            if isinstance(top_sentences, list) and top_sentences:
                for sentence in top_sentences:
                    if isinstance(sentence, dict):
                        add_segment(
                            sentence.get("text", sentence.get("sentence", "")),
                            sentence.get("begin_time", sentence.get("start", 0)),
                            sentence.get("end_time", sentence.get("end", 0)),
                            {
                                "speaker_id": sentence.get("speaker_id"),
                                "words": sentence.get("words"),
                            },
                        )
                found = True

            return found

        candidate_payloads: List[Dict[str, Any]] = []
        if isinstance(output, dict):
            if "transcripts" in output or "sentences" in output:
                candidate_payloads.append(output)
            raw_results = output.get("results") or output.get("result") or []
            if isinstance(raw_results, dict):
                raw_results = [raw_results]
            if isinstance(raw_results, list):
                for item in raw_results:
                    if isinstance(item, dict) and ("transcripts" in item or "sentences" in item):
                        candidate_payloads.append(item)

        parsed_fun_asr = False
        for payload in candidate_payloads:
            if parse_fun_asr_payload(payload):
                parsed_fun_asr = True

        if parsed_fun_asr:
            if not full_text_parts and segments:
                full_text_parts.append("".join(seg.get("text", "") for seg in segments))
            return segments, "\n".join(full_text_parts).strip()

        # Fallbacks for older/simple response formats.
        results = output.get("results") or output.get("result") or output.get("transcripts") or []
        if isinstance(results, dict):
            results = [results]

        if not results:
            words = output.get("words") or output.get("word_results") or []
            if words:
                current_text = ""
                current_start = 0
                current_end = 0
                for word in words:
                    if not isinstance(word, dict):
                        continue
                    word_text = str(word.get("text", word.get("word", ""))).strip()
                    if not word_text:
                        continue
                    word_start = to_milliseconds(word.get("begin_time", word.get("start", current_end)))
                    word_end = to_milliseconds(word.get("end_time", word.get("end", word_start + 200)))

                    if not current_text:
                        current_start = word_start
                    current_text += word_text
                    current_end = word_end

                    if word_text[-1:] in ("。", "，", "！", "？", ".", "!", "?", ",", ";", "；"):
                        add_segment(current_text, current_start, current_end)
                        append_text(current_text)
                        current_text = ""

                if current_text.strip():
                    add_segment(current_text, current_start, current_end)
                    append_text(current_text)
            else:
                single_text = output.get("text") or output.get("full_text") or ""
                if single_text:
                    append_text(single_text)
                    add_segment(single_text, 0, 0)
        else:
            for item in results:
                if not isinstance(item, dict):
                    continue
                text = item.get("text", item.get("sentence", item.get("transcript", "")))
                if not str(text or "").strip():
                    continue
                begin = item.get("begin_time", item.get("start", item.get("begin", 0)))
                end = item.get("end_time", item.get("end", item.get("stop", 0)))
                add_segment(text, begin, end)
                append_text(text)

        return segments, "".join(full_text_parts).strip()

    # -- public methods ----------------------------------------------------

    def get_config_status(self, config_path: str = "") -> Dict[str, Any]:
        """Return ASR configuration status with the API key redacted."""
        try:
            config = self._load_config(config_path or None)
            return self._format_response(
                True,
                config_path=config["path"],
                api_key_set=bool(config["api_key"]),
                api_key_preview=self._redact_secret(config["api_key"]),
                api_key_env=config["api_key_env"],
                api_base=config["api_base"],
                file_endpoint=config["file_endpoint"],
                transcription_endpoint=config["transcription_endpoint"],
                model=config["model"],
                default_sample_rate=config["default_sample_rate"],
                default_format=config["default_format"],
                default_language=config["default_language"],
                default_output_dir=config["default_output_dir"],
                timeout=config["timeout"],
                poll_interval=config["poll_interval"],
            )
        except Exception as exc:
            return self._format_response(False, error=str(exc), message="Failed to read ASR config")

    def save_config(
        self,
        api_key: str = "",
        api_base: str = DEFAULT_API_BASE,
        file_endpoint: str = DEFAULT_FILE_ENDPOINT,
        transcription_endpoint: str = DEFAULT_TRANSCRIPTION_ENDPOINT,
        model: str = "fun-asr",
        default_sample_rate: int = 16000,
        default_format: str = "wav",
        default_language: str = "auto",
        default_output_dir: str = str(DEFAULT_OUTPUT_DIR),
        timeout: int = 600,
        poll_interval: int = 3,
        config_path: str = "",
    ) -> Dict[str, Any]:
        """Save a DashScope ASR config JSON file.

        :param api_key: DashScope API key. If empty, keeps existing key.
        :param api_base: API base URL.
        :param file_endpoint: File upload endpoint.
        :param transcription_endpoint: Transcription API endpoint.
        :param model: ASR model name, e.g. fun-asr.
        :param default_sample_rate: Default audio sample rate for transcription.
        :param default_format: Default audio format hint.
        :param default_language: Default language (auto, zh, en, ...).
        :param default_output_dir: Directory for output files.
        :param timeout: Transcription polling timeout in seconds.
        :param poll_interval: Polling interval in seconds.
        :param config_path: Optional config path.
        """
        target = Path(config_path).expanduser().resolve() if config_path else self.config_path
        target.parent.mkdir(parents=True, exist_ok=True)

        # Load existing config to preserve api_key if not provided
        existing: Dict[str, Any] = {}
        if target.exists():
            with open(target, "r", encoding="utf-8") as handle:
                try:
                    existing = json.load(handle)
                except json.JSONDecodeError:
                    existing = {}

        api_base_clean = api_base.rstrip("/")
        effective_key = api_key.strip() if api_key else existing.get("api_key", "")

        payload = {
            "api_key": effective_key,
            "api_key_env": "DASHSCOPE_API_KEY",
            "api_base": api_base_clean,
            "file_endpoint": file_endpoint.rstrip("/") or f"{api_base_clean}/files",
            "transcription_endpoint": transcription_endpoint.rstrip("/")
                or f"{api_base_clean}/services/audio/asr/transcription",
            "model": model,
            "default_sample_rate": default_sample_rate,
            "default_format": default_format,
            "default_language": default_language,
            "default_output_dir": default_output_dir,
            "timeout": timeout,
            "poll_interval": poll_interval,
        }

        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

        return self._format_response(
            True,
            config_path=str(target),
            api_key_set=bool(payload["api_key"]),
            api_key_preview=self._redact_secret(payload["api_key"]),
            message="ASR config saved",
        )

    def transcribe_audio(
        self,
        audio_path: str,
        output_path: str = "",
        language: str = "",
        sample_rate: int = 0,
        audio_format: str = "",
        diarization: bool = False,
        channel_id: int = -1,
        generate_srt: bool = True,
        generate_json: bool = False,
        config_path: str = "",
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Transcribe an audio file using DashScope ASR.

        :param audio_path: Path to the audio file (local) or a URL.
        :param output_path: Optional output path for SRT/JSON. Auto-generated if omitted.
        :param language: Language hint (auto, zh, en, ...). Defaults to config.
        :param sample_rate: Audio sample rate in Hz. Defaults to config (16000).
        :param audio_format: Audio format hint (wav, mp3, ...). Auto-detected from extension if empty.
        :param diarization: Enable speaker diarization.
        :param channel_id: Specific audio channel to transcribe (-1 = all/mono).
        :param generate_srt: Generate SRT subtitle file.
        :param generate_json: Generate JSON transcript file.
        :param config_path: Optional config JSON path.
        :param timeout: Transcription timeout in seconds. Defaults to config timeout.
        """
        source = (audio_path or "").strip()
        if not source:
            return self._format_response(False, error="audio_path cannot be empty")

        try:
            config = self._load_config(config_path or None)
        except Exception as exc:
            return self._format_response(False, error=str(exc), message="Failed to load ASR config")

        if not config["api_key"]:
            return self._format_response(
                False,
                error="Missing API key. Set api_key in asr_config.json or DASHSCOPE_API_KEY.",
                message="ASR config is incomplete",
            )

        # Override timeout if provided
        if timeout is not None:
            config["timeout"] = int(timeout)

        # Determine if source is local file or URL
        is_url = source.startswith(("http://", "https://", "oss://"))
        upload_info: Dict[str, Any] = {}

        # Auto-detect format from extension
        if not audio_format and not is_url:
            ext = Path(source).suffix.lower().lstrip(".")
            if ext in SUPPORTED_FORMATS:
                audio_format = ext

        if is_url:
            file_url = source
            file_name = source.split("/")[-1] or "audio"
        elif config["model"].strip().lower() == "fun-asr":
            upload_result = self._upload_file_to_temp_oss(source, config, config["model"])
            if not upload_result["success"]:
                return upload_result
            file_url = upload_result["file_url"]
            file_name = upload_result.get("file_name", Path(source).name)
            upload_info = {
                "file_url": file_url,
                "upload_name": upload_result.get("upload_name", ""),
                "file_size": upload_result.get("file_size", 0),
                "expire_in_seconds": upload_result.get("expire_in_seconds"),
            }
        else:
            upload_result = self._upload_file(source, config)
            if not upload_result["success"]:
                return upload_result
            file_url = upload_result["file_url"]
            file_name = upload_result.get("file_name", Path(source).name)
            upload_info = {
                "file_url": file_url,
                "file_size": upload_result.get("file_size", 0),
            }

        # Submit transcription
        submit_result = self._submit_transcription(
            file_url=file_url,
            config=config,
            language=language,
            sample_rate=sample_rate,
            audio_format=audio_format,
            diarization=diarization,
            channel_id=channel_id,
        )
        if not submit_result["success"]:
            return submit_result

        task_id = submit_result.get("task_id", "")

        # If no task_id, it might be synchronous results
        if not task_id:
            output = submit_result.get("output", {})
        else:
            # Poll for results
            poll_result = self._poll_task(task_id, config)
            if not poll_result["success"]:
                return poll_result
            output = poll_result.get("output", {})

        resolved_result = self._download_transcription_results(output, config)
        if not resolved_result["success"]:
            return resolved_result
        output = resolved_result.get("output", output)

        # Parse results
        segments, full_text = self._parse_results(output)

        if not segments and not full_text:
            return self._format_response(
                True,
                text="",
                segments=[],
                message="Transcription completed but no speech detected",
                source_file=source,
                model=config["model"],
            )

        # Compute duration from last segment
        duration = 0.0
        if segments:
            last = segments[-1]
            duration = max(last.get("end", 0), last.get("end_time", 0) / 1000.0)

        result: Dict[str, Any] = {
            "text": full_text,
            "segments": segments,
            "segment_count": len(segments),
            "duration": duration,
            "language": language or config["default_language"],
            "source_file": source,
            "model": config["model"],
            "message": f"Transcription completed: {len(segments)} segments, {duration:.1f}s audio",
        }
        if task_id:
            result["task_id"] = task_id
        if upload_info:
            result["upload"] = upload_info
        if resolved_result.get("transcription_urls"):
            result["transcription_urls"] = resolved_result["transcription_urls"]

        # Generate output files
        output_dir = config["default_output_dir"]
        srt_path = ""
        json_path = ""

        if generate_srt:
            srt_path = str(self._build_output_path(file_name, output_path, output_dir, ".srt"))
            self._generate_srt(segments, srt_path)
            result["srt_path"] = srt_path
            result["srt_content"] = ""

        if generate_json:
            json_path = str(self._build_output_path(
                file_name,
                output_path.replace(".srt", ".json") if output_path else "",
                output_dir,
                ".json",
            ))
            self._generate_json(segments, full_text, json_path, language or config["default_language"], duration)
            result["json_path"] = json_path

        # Also include text-only output
        txt_path = str(self._build_output_path(file_name, output_path, output_dir, ".txt"))
        txt_dir = Path(txt_path).parent
        txt_dir.mkdir(parents=True, exist_ok=True)
        txt_stem = Path(txt_path).stem
        # Create a .txt alongside, not replacing user's path
        actual_txt = txt_dir / f"{txt_stem}.txt"
        with open(actual_txt, "w", encoding="utf-8") as handle:
            handle.write(full_text)
        result["txt_path"] = str(actual_txt)

        # Read SRT content back for convenience
        if srt_path and os.path.exists(srt_path):
            with open(srt_path, "r", encoding="utf-8") as handle:
                result["srt_content"] = handle.read()

        return self._format_response(True, **result)

    def transcribe_video(
        self,
        video_path: str,
        output_path: str = "",
        language: str = "",
        sample_rate: int = 16000,
        diarization: bool = False,
        generate_srt: bool = True,
        generate_json: bool = False,
        config_path: str = "",
        timeout: Optional[int] = None,
        extract_timeout: int = 1800,
    ) -> Dict[str, Any]:
        """Extract audio from a video and transcribe it.

        :param video_path: Path to the video file.
        :param output_path: Optional output path for SRT.
        :param language: Language hint.
        :param sample_rate: Audio sample rate for extraction/transcription.
        :param diarization: Enable speaker diarization.
        :param generate_srt: Generate SRT subtitle file.
        :param generate_json: Generate JSON transcript file.
        :param config_path: Optional config JSON path.
        :param timeout: Transcription timeout.
        :param extract_timeout: FFmpeg extraction timeout.
        """
        source = (video_path or "").strip()
        if not source:
            return self._format_response(False, error="video_path cannot be empty")

        if source.startswith(("http://", "https://", "oss://")):
            return self.transcribe_audio(
                audio_path=source,
                output_path=output_path,
                language=language,
                sample_rate=sample_rate,
                audio_format=Path(source.split("?", 1)[0]).suffix.lower().lstrip("."),
                diarization=diarization,
                generate_srt=generate_srt,
                generate_json=generate_json,
                config_path=config_path,
                timeout=timeout,
            )

        try:
            config = self._load_config(config_path or None)
        except Exception as exc:
            return self._format_response(False, error=str(exc), message="Failed to load ASR config")

        if config["model"].strip().lower() == "fun-asr":
            return self.transcribe_audio(
                audio_path=source,
                output_path=output_path,
                language=language,
                sample_rate=sample_rate,
                audio_format=Path(source).suffix.lower().lstrip("."),
                diarization=diarization,
                generate_srt=generate_srt,
                generate_json=generate_json,
                config_path=config_path,
                timeout=timeout,
            )

        # Extract audio to WAV
        audio_path = ""
        try:
            extract_result = _extract_audio_from_video(
                video_path=source,
                output_path=audio_path,
                timeout=extract_timeout,
            )
            if not extract_result.get("success"):
                # Fallback: try FFmpeg directly
                audio_path = self._fallback_extract_audio(source, sample_rate, extract_timeout)
                if not audio_path:
                    return self._format_response(
                        False,
                        error=extract_result.get("error", "Audio extraction failed"),
                        message="Could not extract audio from video",
                    )
            else:
                audio_path = extract_result.get("output_path", extract_result.get("output_file", ""))
                if not audio_path:
                    return self._format_response(
                        False,
                        error="Audio extraction returned no output path",
                        message="Audio extraction failed",
                    )
        except Exception as exc:
            return self._format_response(
                False,
                error=str(exc),
                message="Audio extraction failed with exception",
            )

        # Transcribe the extracted audio
        return self.transcribe_audio(
            audio_path=audio_path,
            output_path=output_path,
            language=language,
            sample_rate=sample_rate,
            audio_format="wav",
            diarization=diarization,
            generate_srt=generate_srt,
            generate_json=generate_json,
            config_path=config_path,
            timeout=timeout,
        )

    def _fallback_extract_audio(
        self,
        video_path: str,
        sample_rate: int = 16000,
        timeout: int = 1800,
    ) -> str:
        """Fallback: use FFmpeg directly to extract audio."""
        import subprocess

        video = Path(video_path).expanduser().resolve()
        audio_target = video.parent / f"{video.stem}_audio_asr.wav"

        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(video),
            "-vn",
            "-map", "0:a:0",
            "-c:a", "pcm_s16le",
            "-ar", str(sample_rate),
            "-ac", "1",
            str(audio_target),
        ]

        try:
            subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                check=True,
            )
            if audio_target.exists():
                return str(audio_target)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            pass

        return ""

    def generate_subtitles(
        self,
        segments_or_text: Any,
        output_path: str = "",
        format: str = "srt",
    ) -> Dict[str, Any]:
        """Generate subtitle file from segments or text.

        :param segments_or_text: Either a list of segment dicts, or a JSON string,
                                 or a plain text string (one line = one subtitle).
        :param output_path: Output subtitle file path.
        :param format: Subtitle format (srt or ass).
        """
        # Parse input
        segments: List[Dict[str, Any]] = []

        if isinstance(segments_or_text, str):
            text = segments_or_text.strip()
            if text.startswith("["):
                try:
                    segments = json.loads(text)
                except json.JSONDecodeError:
                    segments = []
            elif text:
                # Plain text: each line as a segment
                lines = [l.strip() for l in text.split("\n") if l.strip()]
                seg_duration = 3.0  # default 3 seconds per segment
                for i, line in enumerate(lines):
                    segments.append({
                        "text": line,
                        "begin_time": i * seg_duration * 1000,
                        "end_time": (i + 1) * seg_duration * 1000,
                        "start": i * seg_duration,
                        "end": (i + 1) * seg_duration,
                    })
        elif isinstance(segments_or_text, list):
            segments = segments_or_text
        else:
            return self._format_response(False, error="Invalid segments_or_text type")

        if not segments:
            return self._format_response(False, error="No segments to generate subtitles from")

        fmt = format.lower()
        if fmt not in ("srt", "ass"):
            return self._format_response(False, error=f"Unsupported subtitle format: {format}")

        config = self._load_config()
        output_dir = config.get("default_output_dir", str(DEFAULT_OUTPUT_DIR))
        suffix = f".{fmt}"

        # Build output path
        target = self._build_output_path("subtitle", output_path, output_dir, suffix)

        if fmt == "srt":
            self._generate_srt(segments, str(target))
        elif fmt == "ass":
            # Basic ASS format
            content = self._generate_ass(segments)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(content)

        return self._format_response(
            True,
            output_path=str(target),
            format=fmt,
            segment_count=len(segments),
            message=f"Subtitle file generated: {target}",
        )

    def _generate_ass(self, segments: List[Dict[str, Any]]) -> str:
        """Generate basic ASS subtitle content."""
        lines = [
            "[Script Info]",
            "ScriptType: v4.00+",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            "Style: Default,Microsoft YaHei,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]

        for seg in segments:
            start = seg.get("begin_time", seg.get("start", 0)) / 1000.0
            end = seg.get("end_time", seg.get("end", start + 1.0)) / 1000.0
            text = seg.get("text", "").strip()
            if not text:
                continue

            start_str = self._format_timestamp_ass(start)
            end_str = self._format_timestamp_ass(end)
            # Escape ASS special chars
            text_safe = text.replace("{", "\\{").replace("}", "\\}")
            lines.append(f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{text_safe}")

        return "\n".join(lines)

    @staticmethod
    def _format_timestamp_ass(seconds: float) -> str:
        """Convert seconds to ASS timestamp format (H:MM:SS.cc)."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        centisecs = int(round((seconds - int(seconds)) * 100))
        return f"{hours}:{minutes:02d}:{secs:02d}.{centisecs:02d}"


# ---------------------------------------------------------------------------
# ASRToolManager (Xenon auto-discovery entry point)
# ---------------------------------------------------------------------------

class ASRToolManager:
    """Xenon auto-discovery entry point for ASR tools."""

    def __init__(self):
        self.handler = ASRHandler()

    def transcribe_audio(
        self,
        audio_path: str,
        output_path: str = "",
        language: str = "",
        sample_rate: int = 0,
        audio_format: str = "",
        diarization: bool = False,
        channel_id: int = -1,
        generate_srt: bool = True,
        generate_json: bool = False,
        config_path: str = "",
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Transcribe an audio file using DashScope ASR.

        :param audio_path: Path to the audio file (local) or a URL.
        :param output_path: Optional output path for SRT/JSON.
        :param language: Language hint (auto, zh, en, ...).
        :param sample_rate: Audio sample rate in Hz.
        :param audio_format: Audio format hint (wav, mp3, ...).
        :param diarization: Enable speaker diarization.
        :param channel_id: Specific audio channel to transcribe (-1 = all).
        :param generate_srt: Generate SRT subtitle file.
        :param generate_json: Generate JSON transcript file.
        :param config_path: Optional config JSON path.
        :param timeout: Transcription timeout in seconds.
        """
        return self.handler.transcribe_audio(
            audio_path=audio_path,
            output_path=output_path,
            language=language,
            sample_rate=sample_rate,
            audio_format=audio_format,
            diarization=diarization,
            channel_id=channel_id,
            generate_srt=generate_srt,
            generate_json=generate_json,
            config_path=config_path,
            timeout=timeout,
        )

    def transcribe_video(
        self,
        video_path: str,
        output_path: str = "",
        language: str = "",
        sample_rate: int = 16000,
        diarization: bool = False,
        generate_srt: bool = True,
        generate_json: bool = False,
        config_path: str = "",
        timeout: Optional[int] = None,
        extract_timeout: int = 1800,
    ) -> Dict[str, Any]:
        """Extract audio from video and transcribe it.

        :param video_path: Path to the video file.
        :param output_path: Optional output path for SRT.
        :param language: Language hint.
        :param sample_rate: Audio sample rate.
        :param diarization: Enable speaker diarization.
        :param generate_srt: Generate SRT subtitle file.
        :param generate_json: Generate JSON transcript file.
        :param config_path: Optional config JSON path.
        :param timeout: Transcription timeout.
        :param extract_timeout: FFmpeg extraction timeout.
        """
        return self.handler.transcribe_video(
            video_path=video_path,
            output_path=output_path,
            language=language,
            sample_rate=sample_rate,
            diarization=diarization,
            generate_srt=generate_srt,
            generate_json=generate_json,
            config_path=config_path,
            timeout=timeout,
            extract_timeout=extract_timeout,
        )

    def generate_subtitles(
        self,
        segments_or_text: Any,
        output_path: str = "",
        format: str = "srt",
    ) -> Dict[str, Any]:
        """Generate subtitle file from segments or text.

        :param segments_or_text: List of segment dicts, JSON string, or plain text.
        :param output_path: Output subtitle file path.
        :param format: Subtitle format (srt or ass).
        """
        return self.handler.generate_subtitles(
            segments_or_text=segments_or_text,
            output_path=output_path,
            format=format,
        )

    def save_config(
        self,
        api_key: str = "",
        api_base: str = DEFAULT_API_BASE,
        file_endpoint: str = DEFAULT_FILE_ENDPOINT,
        transcription_endpoint: str = DEFAULT_TRANSCRIPTION_ENDPOINT,
        model: str = "fun-asr",
        default_sample_rate: int = 16000,
        default_format: str = "wav",
        default_language: str = "auto",
        default_output_dir: str = str(DEFAULT_OUTPUT_DIR),
        timeout: int = 600,
        poll_interval: int = 3,
        config_path: str = "",
    ) -> Dict[str, Any]:
        """Save a DashScope ASR config JSON file.

        :param api_key: DashScope API key. Empty keeps existing key.
        :param api_base: API base URL.
        :param file_endpoint: File upload endpoint.
        :param transcription_endpoint: Transcription API endpoint.
        :param model: ASR model name.
        :param default_sample_rate: Default audio sample rate.
        :param default_format: Default audio format hint.
        :param default_language: Default language.
        :param default_output_dir: Output directory.
        :param timeout: Polling timeout.
        :param poll_interval: Polling interval.
        :param config_path: Optional config path.
        """
        return self.handler.save_config(
            api_key=api_key,
            api_base=api_base,
            file_endpoint=file_endpoint,
            transcription_endpoint=transcription_endpoint,
            model=model,
            default_sample_rate=default_sample_rate,
            default_format=default_format,
            default_language=default_language,
            default_output_dir=default_output_dir,
            timeout=timeout,
            poll_interval=poll_interval,
            config_path=config_path,
        )

    def get_config_status(self, config_path: str = "") -> Dict[str, Any]:
        """Return ASR configuration status with the API key redacted.

        :param config_path: Optional config JSON path.
        """
        return self.handler.get_config_status(config_path=config_path)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({
            "success": False,
            "error": "usage: python asr_handler.py <action> [json_args]",
        }, ensure_ascii=False))
        sys.exit(1)

    action = sys.argv[1]
    args: Dict[str, Any] = {}
    if len(sys.argv) > 2:
        args = json.loads(sys.argv[2])
    elif not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw:
            args = json.loads(raw)

    manager = ASRToolManager()
    action_map = {
        "transcribe_audio": manager.transcribe_audio,
        "transcribe_video": manager.transcribe_video,
        "generate_subtitles": manager.generate_subtitles,
        "save_config": manager.save_config,
        "status": manager.get_config_status,
        "get_config_status": manager.get_config_status,
    }

    handler = action_map.get(action)
    if handler:
        result = handler(**args)
    else:
        result = {"success": False, "error": f"Unknown action: {action}"}

    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
