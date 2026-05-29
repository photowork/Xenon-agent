#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
FFmpeg based video editing tool for Xenon.

It provides practical editing operations while keeping the implementation
thin: ffmpeg/ffprobe do the heavy lifting, this module validates inputs,
builds commands, and returns structured results.
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


DEFAULT_OUTPUT_DIR = Path("work") / "video_outputs"
VIDEO_EXTENSIONS = [".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv"]
AUDIO_EXTENSIONS = [".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"]


class VideoHandler:
    """Internal FFmpeg wrapper."""

    def __init__(self, ffmpeg_path: str = "", ffprobe_path: str = ""):
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg") or "ffmpeg"
        self.ffprobe_path = ffprobe_path or shutil.which("ffprobe") or "ffprobe"

    @staticmethod
    def _format_response(success: bool, **kwargs: Any) -> Dict[str, Any]:
        result = {"success": success}
        result.update(kwargs)
        return result

    @staticmethod
    def _decode(data: bytes) -> str:
        if not data:
            return ""
        for encoding in ("utf-8", "gbk", "latin1"):
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def _command_preview(args: Sequence[Union[str, Path]]) -> str:
        values = [str(arg) for arg in args]
        if platform.system().lower() == "windows":
            return subprocess.list2cmdline(values)
        return " ".join(shlex.quote(value) for value in values)

    @staticmethod
    def _resolve_input(path: str) -> Path:
        if not path:
            raise ValueError("input path is required")
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = Path.cwd() / target
        target = target.resolve()
        if not target.exists():
            raise FileNotFoundError(f"input file not found: {target}")
        if not target.is_file():
            raise ValueError(f"input path is not a file: {target}")
        return target

    @staticmethod
    def _resolve_output(path: str, suffix: str, prefix: str) -> Path:
        if path:
            target = Path(path).expanduser()
            if not target.suffix and suffix:
                target = target.with_suffix(suffix)
        else:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            target = DEFAULT_OUTPUT_DIR / f"{prefix}_{stamp}{suffix}"
        if not target.is_absolute():
            target = Path.cwd() / target
        target = target.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def _overwrite_args(overwrite: bool) -> List[str]:
        return ["-y"] if overwrite else ["-n"]

    @staticmethod
    def _ensure_output_file(path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {"output_file": str(path), "file_exists": False, "file_size": 0}
        return {"output_file": str(path), "file_exists": True, "file_size": path.stat().st_size}

    @staticmethod
    def _parse_time(value: Any, *, allow_empty: bool = True) -> Optional[float]:
        if value is None:
            return None if allow_empty else 0.0
        if isinstance(value, (int, float)):
            seconds = float(value)
        else:
            text = str(value).strip()
            if not text:
                return None if allow_empty else 0.0
            if re.fullmatch(r"\d+(?:\.\d+)?", text):
                seconds = float(text)
            else:
                parts = text.split(":")
                if len(parts) not in (2, 3):
                    raise ValueError(f"invalid time value: {value}")
                try:
                    numeric = [float(part) for part in parts]
                except ValueError as exc:
                    raise ValueError(f"invalid time value: {value}") from exc
                if len(parts) == 2:
                    minutes, secs = numeric
                    seconds = minutes * 60 + secs
                else:
                    hours, minutes, secs = numeric
                    seconds = hours * 3600 + minutes * 60 + secs
        if seconds < 0:
            raise ValueError("time value cannot be negative")
        return seconds

    @staticmethod
    def _ff_time(seconds: Optional[float]) -> str:
        if seconds is None:
            return "0"
        return f"{seconds:.6f}".rstrip("0").rstrip(".")

    @staticmethod
    def _ratio_to_float(value: str) -> Optional[float]:
        if not value or value == "0/0":
            return None
        if "/" in value:
            num, den = value.split("/", 1)
            try:
                denominator = float(den)
                if denominator == 0:
                    return None
                return float(num) / denominator
            except ValueError:
                return None
        try:
            return float(value)
        except ValueError:
            return None

    @staticmethod
    def _concat_file_line(path: Path) -> str:
        normalized = str(path.resolve()).replace("\\", "/").replace("'", r"'\''")
        return f"file '{normalized}'\n"

    @staticmethod
    def _video_filter(width: int = 0, height: int = 0, fps: Any = 0) -> str:
        filters: List[str] = []
        if width or height:
            filters.append(f"scale={width if width else -2}:{height if height else -2}")
        if fps:
            filters.append(f"fps={fps}")
        filters.append("format=yuv420p")
        return ",".join(filters)

    def _run(
        self,
        args: Sequence[Union[str, Path]],
        timeout: int = 1800,
        check_output: Optional[Path] = None,
    ) -> Dict[str, Any]:
        command = [str(arg) for arg in args]
        preview = self._command_preview(command)
        try:
            proc = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                shell=False,
            )
            stdout = self._decode(proc.stdout).strip()
            stderr = self._decode(proc.stderr).strip()
            success = proc.returncode == 0 and (check_output is None or check_output.exists())
            result = self._format_response(
                success,
                command=preview,
                exit_code=proc.returncode,
                stdout=stdout,
                stderr=stderr,
            )
            if check_output is not None:
                result.update(self._ensure_output_file(check_output))
            if not success:
                result["error"] = stderr or stdout or "ffmpeg command failed"
            return result
        except FileNotFoundError as exc:
            return self._format_response(False, command=preview, error=f"executable not found: {exc.filename}")
        except subprocess.TimeoutExpired as exc:
            stdout = self._decode(exc.stdout or b"").strip() if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
            stderr = self._decode(exc.stderr or b"").strip() if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            return self._format_response(
                False,
                command=preview,
                exit_code=124,
                stdout=stdout,
                stderr=stderr,
                error=f"command timed out after {timeout} seconds",
            )

    def _probe_json(self, input_path: Path) -> Dict[str, Any]:
        args = [
            self.ffprobe_path,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(input_path),
        ]
        result = self._run(args, timeout=120)
        if not result.get("success"):
            raise RuntimeError(result.get("error") or result.get("stderr") or "ffprobe failed")
        try:
            return json.loads(result.get("stdout") or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("ffprobe returned invalid JSON") from exc

    def _summarize_probe(self, payload: Dict[str, Any], input_path: Path) -> Dict[str, Any]:
        streams = payload.get("streams") or []
        fmt = payload.get("format") or {}
        try:
            duration = float(fmt.get("duration")) if fmt.get("duration") not in (None, "N/A") else None
        except (TypeError, ValueError):
            duration = None

        video_streams = []
        audio_streams = []
        other_streams = []
        for stream in streams:
            codec_type = stream.get("codec_type")
            base = {
                "index": stream.get("index"),
                "codec_type": codec_type,
                "codec_name": stream.get("codec_name"),
                "duration": stream.get("duration"),
                "bit_rate": stream.get("bit_rate"),
            }
            if codec_type == "video":
                base.update(
                    {
                        "width": stream.get("width"),
                        "height": stream.get("height"),
                        "pix_fmt": stream.get("pix_fmt"),
                        "avg_frame_rate": stream.get("avg_frame_rate"),
                        "fps": self._ratio_to_float(str(stream.get("avg_frame_rate") or "")),
                    }
                )
                video_streams.append(base)
            elif codec_type == "audio":
                base.update(
                    {
                        "sample_rate": stream.get("sample_rate"),
                        "channels": stream.get("channels"),
                        "channel_layout": stream.get("channel_layout"),
                    }
                )
                audio_streams.append(base)
            else:
                other_streams.append(base)

        return {
            "path": str(input_path),
            "filename": input_path.name,
            "size": input_path.stat().st_size if input_path.exists() else None,
            "format_name": fmt.get("format_name"),
            "duration_seconds": duration,
            "bit_rate": fmt.get("bit_rate"),
            "video_streams": video_streams,
            "audio_streams": audio_streams,
            "other_streams": other_streams,
            "has_video": bool(video_streams),
            "has_audio": bool(audio_streams),
            "raw_format": fmt,
        }

    def _media_summary(self, input_path: Path) -> Dict[str, Any]:
        return self._summarize_probe(self._probe_json(input_path), input_path)

    def _duration(self, input_path: Path) -> float:
        summary = self._media_summary(input_path)
        duration = summary.get("duration_seconds")
        if duration is None or duration <= 0:
            raise ValueError(f"could not determine media duration: {input_path}")
        return float(duration)

    def _write_concat_list(self, paths: List[Path]) -> Path:
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False)
        with handle:
            for path in paths:
                handle.write(self._concat_file_line(path))
        return Path(handle.name)

    def check_dependencies(self) -> Dict[str, Any]:
        """Check ffmpeg and ffprobe availability."""
        ffmpeg = shutil.which(self.ffmpeg_path) if self.ffmpeg_path == "ffmpeg" else self.ffmpeg_path
        ffprobe = shutil.which(self.ffprobe_path) if self.ffprobe_path == "ffprobe" else self.ffprobe_path
        result = {
            "ffmpeg_path": ffmpeg or self.ffmpeg_path,
            "ffprobe_path": ffprobe or self.ffprobe_path,
            "ffmpeg_available": bool(ffmpeg or Path(self.ffmpeg_path).exists()),
            "ffprobe_available": bool(ffprobe or Path(self.ffprobe_path).exists()),
        }
        if result["ffmpeg_available"]:
            version_result = self._run([self.ffmpeg_path, "-version"], timeout=30)
            first_line = (version_result.get("stdout") or "").splitlines()
            result["ffmpeg_version"] = first_line[0] if first_line else ""
        return self._format_response(result["ffmpeg_available"] and result["ffprobe_available"], **result)

    def get_media_info(self, input_path: str, include_raw: bool = False) -> Dict[str, Any]:
        """Return media metadata from ffprobe."""
        try:
            path = self._resolve_input(input_path)
            raw = self._probe_json(path)
            summary = self._summarize_probe(raw, path)
            if include_raw:
                summary["raw"] = raw
            return self._format_response(True, **summary)
        except Exception as exc:
            return self._format_response(False, error=str(exc))

    def trim_video(
        self,
        input_path: str,
        output_path: str = "",
        start_time: Any = 0,
        end_time: Any = None,
        duration: Any = None,
        reencode: bool = False,
        video_codec: str = "libx264",
        audio_codec: str = "aac",
        crf: int = 18,
        preset: str = "medium",
        overwrite: bool = True,
        timeout: int = 1800,
    ) -> Dict[str, Any]:
        """Cut one segment from a video."""
        try:
            source = self._resolve_input(input_path)
            target = self._resolve_output(output_path, source.suffix or ".mp4", f"{source.stem}_trim")
            start = self._parse_time(start_time, allow_empty=False) or 0.0
            clip_duration = self._parse_time(duration)
            if clip_duration is None:
                end = self._parse_time(end_time)
                if end is None:
                    raise ValueError("duration or end_time is required")
                clip_duration = end - start
            if clip_duration <= 0:
                raise ValueError("trim duration must be greater than zero")

            args: List[Union[str, Path]] = [
                self.ffmpeg_path,
                *self._overwrite_args(overwrite),
                "-ss",
                self._ff_time(start),
                "-i",
                source,
                "-t",
                self._ff_time(clip_duration),
                "-map",
                "0:v:0?",
                "-map",
                "0:a?",
            ]
            if reencode:
                args.extend(
                    [
                        "-c:v",
                        video_codec,
                        "-preset",
                        preset,
                        "-crf",
                        str(crf),
                        "-c:a",
                        audio_codec,
                        "-movflags",
                        "+faststart",
                    ]
                )
            else:
                args.extend(["-c", "copy", "-avoid_negative_ts", "make_zero"])
            args.append(target)

            result = self._run(args, timeout=timeout, check_output=target)
            result.update({"start_time": start, "duration": clip_duration, "reencoded": reencode})
            return result
        except Exception as exc:
            return self._format_response(False, error=str(exc))

    def split_video(
        self,
        input_path: str,
        output_dir: str = "",
        segment_duration: Any = None,
        cut_points: Optional[List[Any]] = None,
        output_pattern: str = "part_{index:03d}.mp4",
        reencode: bool = False,
        max_segments: int = 500,
        overwrite: bool = True,
        timeout: int = 1800,
    ) -> Dict[str, Any]:
        """Split a video by fixed segment duration or explicit cut points."""
        try:
            source = self._resolve_input(input_path)
            duration_total = self._duration(source)
            if output_dir:
                target_dir = Path(output_dir).expanduser()
                if not target_dir.is_absolute():
                    target_dir = Path.cwd() / target_dir
            else:
                target_dir = DEFAULT_OUTPUT_DIR / f"{source.stem}_parts"
            target_dir = target_dir.resolve()
            target_dir.mkdir(parents=True, exist_ok=True)

            if cut_points:
                parsed = sorted({point for point in (self._parse_time(item) for item in cut_points) if point is not None})
                parsed = [point for point in parsed if 0 < point < duration_total]
                boundaries = [0.0, *parsed, duration_total]
            else:
                seg = self._parse_time(segment_duration)
                if seg is None or seg <= 0:
                    raise ValueError("segment_duration or cut_points is required")
                count = int(math.ceil(duration_total / seg))
                boundaries = [min(index * seg, duration_total) for index in range(count)]
                boundaries.append(duration_total)

            intervals: List[Tuple[float, float]] = []
            for start, end in zip(boundaries, boundaries[1:]):
                if end - start > 0.001:
                    intervals.append((start, end))
            if len(intervals) > max_segments:
                raise ValueError(f"refusing to create {len(intervals)} segments; max_segments={max_segments}")

            output_files: List[str] = []
            failures: List[Dict[str, Any]] = []
            for index, (start, end) in enumerate(intervals, 1):
                filename = output_pattern.format(index=index, start=self._ff_time(start), end=self._ff_time(end))
                target = (target_dir / filename).resolve()
                result = self.trim_video(
                    input_path=str(source),
                    output_path=str(target),
                    start_time=start,
                    duration=end - start,
                    reencode=reencode,
                    overwrite=overwrite,
                    timeout=timeout,
                )
                if result.get("success"):
                    output_files.append(str(target))
                else:
                    failures.append(
                        {
                            "index": index,
                            "start": start,
                            "end": end,
                            "error": result.get("error"),
                            "stderr": result.get("stderr"),
                        }
                    )
                    break

            return self._format_response(
                not failures,
                input_file=str(source),
                output_dir=str(target_dir),
                total_duration=duration_total,
                segment_count=len(output_files),
                output_files=output_files,
                failures=failures,
                reencoded=reencode,
            )
        except Exception as exc:
            return self._format_response(False, error=str(exc))

    def merge_videos(
        self,
        input_paths: List[str],
        output_path: str = "",
        reencode: bool = False,
        video_codec: str = "libx264",
        audio_codec: str = "aac",
        crf: int = 20,
        preset: str = "medium",
        overwrite: bool = True,
        timeout: int = 3600,
    ) -> Dict[str, Any]:
        """Concatenate multiple videos in order."""
        concat_list: Optional[Path] = None
        try:
            if not input_paths or len(input_paths) < 2:
                raise ValueError("at least two input videos are required")
            sources = [self._resolve_input(path) for path in input_paths]
            target = self._resolve_output(output_path, ".mp4", "merged_video")
            concat_list = self._write_concat_list(sources)
            args: List[Union[str, Path]] = [
                self.ffmpeg_path,
                *self._overwrite_args(overwrite),
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_list,
            ]
            if reencode:
                args.extend(
                    [
                        "-c:v",
                        video_codec,
                        "-preset",
                        preset,
                        "-crf",
                        str(crf),
                        "-c:a",
                        audio_codec,
                        "-movflags",
                        "+faststart",
                    ]
                )
            else:
                args.extend(["-c", "copy"])
            args.append(target)

            result = self._run(args, timeout=timeout, check_output=target)
            result.update(
                {
                    "input_files": [str(path) for path in sources],
                    "input_count": len(sources),
                    "reencoded": reencode,
                    "note": "For mixed codecs/resolutions, convert clips first, then merge.",
                }
            )
            return result
        except Exception as exc:
            return self._format_response(False, error=str(exc))
        finally:
            if concat_list and concat_list.exists():
                try:
                    concat_list.unlink()
                except OSError:
                    pass

    def add_audio_to_video(
        self,
        video_path: str,
        audio_path: str,
        output_path: str = "",
        mode: str = "replace",
        original_volume: float = 1.0,
        added_volume: float = 1.0,
        audio_start: Any = 0,
        loop_audio: bool = False,
        match_video_duration: bool = True,
        audio_codec: str = "aac",
        overwrite: bool = True,
        timeout: int = 1800,
    ) -> Dict[str, Any]:
        """Add, replace, or mix an audio file into a video."""
        try:
            source_video = self._resolve_input(video_path)
            source_audio = self._resolve_input(audio_path)
            target = self._resolve_output(output_path, source_video.suffix or ".mp4", f"{source_video.stem}_audio")
            mode = str(mode or "replace").strip().lower()
            if mode not in {"replace", "mix", "keep_original"}:
                raise ValueError("mode must be replace, mix, or keep_original")
            delay = self._parse_time(audio_start, allow_empty=False) or 0.0
            delay_ms = int(round(delay * 1000))
            video_info = self._media_summary(source_video)
            if mode == "mix" and not video_info.get("has_audio"):
                mode = "replace"

            args: List[Union[str, Path]] = [self.ffmpeg_path, *self._overwrite_args(overwrite), "-i", source_video]
            if loop_audio:
                args.extend(["-stream_loop", "-1"])
            args.extend(["-i", source_audio])

            if mode == "mix":
                added_chain = f"[1:a]volume={added_volume}"
                if delay_ms:
                    added_chain += f",adelay={delay_ms}:all=1"
                added_chain += "[a1]"
                filter_complex = (
                    f"[0:a]volume={original_volume}[a0];"
                    f"{added_chain};"
                    "[a0][a1]amix=inputs=2:duration=first:dropout_transition=2[aout]"
                )
                args.extend(["-filter_complex", filter_complex, "-map", "0:v:0", "-map", "[aout]", "-c:v", "copy", "-c:a", audio_codec])
            elif mode == "keep_original":
                if delay_ms or added_volume != 1.0:
                    chain = f"[1:a]volume={added_volume}"
                    if delay_ms:
                        chain += f",adelay={delay_ms}:all=1"
                    chain += "[a1]"
                    args.extend(["-filter_complex", chain, "-map", "0:v:0", "-map", "0:a?", "-map", "[a1]", "-c:v", "copy", "-c:a", audio_codec])
                else:
                    args.extend(["-map", "0:v:0", "-map", "0:a?", "-map", "1:a:0", "-c:v", "copy", "-c:a", audio_codec])
            else:
                if delay_ms or added_volume != 1.0:
                    chain = f"[1:a]volume={added_volume}"
                    if delay_ms:
                        chain += f",adelay={delay_ms}:all=1"
                    chain += "[a1]"
                    args.extend(["-filter_complex", chain, "-map", "0:v:0", "-map", "[a1]", "-c:v", "copy", "-c:a", audio_codec])
                else:
                    args.extend(["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", audio_codec])

            if match_video_duration and video_info.get("duration_seconds"):
                args.extend(["-t", self._ff_time(float(video_info["duration_seconds"]))])
            args.extend(["-movflags", "+faststart", target])

            result = self._run(args, timeout=timeout, check_output=target)
            result.update(
                {
                    "video_file": str(source_video),
                    "audio_file": str(source_audio),
                    "mode": mode,
                    "loop_audio": loop_audio,
                    "audio_start": delay,
                    "match_video_duration": match_video_duration,
                }
            )
            return result
        except Exception as exc:
            return self._format_response(False, error=str(exc))

    def extract_audio(
        self,
        input_path: str,
        output_path: str = "",
        audio_codec: str = "",
        bitrate: str = "192k",
        overwrite: bool = True,
        timeout: int = 1800,
    ) -> Dict[str, Any]:
        """Extract audio from a video or audio container."""
        try:
            source = self._resolve_input(input_path)
            target = self._resolve_output(output_path, ".mp3", f"{source.stem}_audio")
            suffix = target.suffix.lower()
            if not audio_codec:
                audio_codec = {
                    ".mp3": "libmp3lame",
                    ".wav": "pcm_s16le",
                    ".m4a": "aac",
                    ".aac": "aac",
                    ".flac": "flac",
                    ".ogg": "libvorbis",
                    ".opus": "libopus",
                }.get(suffix, "aac")
            args: List[Union[str, Path]] = [
                self.ffmpeg_path,
                *self._overwrite_args(overwrite),
                "-i",
                source,
                "-vn",
                "-map",
                "0:a:0",
                "-c:a",
                audio_codec,
            ]
            if audio_codec != "copy" and bitrate and audio_codec not in {"pcm_s16le", "flac"}:
                args.extend(["-b:a", bitrate])
            args.append(target)
            result = self._run(args, timeout=timeout, check_output=target)
            result.update({"input_file": str(source), "audio_codec": audio_codec, "bitrate": bitrate})
            return result
        except Exception as exc:
            return self._format_response(False, error=str(exc))

    def remove_audio(self, input_path: str, output_path: str = "", overwrite: bool = True, timeout: int = 1800) -> Dict[str, Any]:
        """Create a video-only copy with all audio tracks removed."""
        try:
            source = self._resolve_input(input_path)
            target = self._resolve_output(output_path, source.suffix or ".mp4", f"{source.stem}_silent")
            args: List[Union[str, Path]] = [
                self.ffmpeg_path,
                *self._overwrite_args(overwrite),
                "-i",
                source,
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-an",
                target,
            ]
            result = self._run(args, timeout=timeout, check_output=target)
            result.update({"input_file": str(source)})
            return result
        except Exception as exc:
            return self._format_response(False, error=str(exc))

    def convert_video(
        self,
        input_path: str,
        output_path: str = "",
        video_codec: str = "libx264",
        audio_codec: str = "aac",
        width: int = 0,
        height: int = 0,
        fps: Any = 0,
        crf: int = 23,
        preset: str = "medium",
        keep_audio: bool = True,
        overwrite: bool = True,
        timeout: int = 3600,
    ) -> Dict[str, Any]:
        """Convert, resize, or compress a video."""
        try:
            source = self._resolve_input(input_path)
            target = self._resolve_output(output_path, ".mp4", f"{source.stem}_converted")
            args: List[Union[str, Path]] = [
                self.ffmpeg_path,
                *self._overwrite_args(overwrite),
                "-i",
                source,
                "-map",
                "0:v:0",
            ]
            if keep_audio:
                args.extend(["-map", "0:a?"])
            args.extend(["-vf", self._video_filter(width=width, height=height, fps=fps), "-c:v", video_codec, "-preset", preset, "-crf", str(crf)])
            if keep_audio:
                args.extend(["-c:a", audio_codec])
            else:
                args.append("-an")
            args.extend(["-movflags", "+faststart", target])
            result = self._run(args, timeout=timeout, check_output=target)
            result.update(
                {
                    "input_file": str(source),
                    "video_codec": video_codec,
                    "audio_codec": audio_codec if keep_audio else "",
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "crf": crf,
                }
            )
            return result
        except Exception as exc:
            return self._format_response(False, error=str(exc))

    def create_thumbnail(
        self,
        input_path: str,
        output_path: str = "",
        timestamp: Any = 0,
        width: int = 0,
        height: int = 0,
        overwrite: bool = True,
        timeout: int = 600,
    ) -> Dict[str, Any]:
        """Export a still image from a video."""
        try:
            source = self._resolve_input(input_path)
            target = self._resolve_output(output_path, ".png", f"{source.stem}_thumb")
            ts = self._parse_time(timestamp, allow_empty=False) or 0.0
            args: List[Union[str, Path]] = [
                self.ffmpeg_path,
                *self._overwrite_args(overwrite),
                "-ss",
                self._ff_time(ts),
                "-i",
                source,
                "-frames:v",
                "1",
            ]
            if width or height:
                args.extend(["-vf", f"scale={width if width else -2}:{height if height else -2}"])
            args.extend(["-q:v", "2", target])
            result = self._run(args, timeout=timeout, check_output=target)
            result.update({"input_file": str(source), "timestamp": ts})
            return result
        except Exception as exc:
            return self._format_response(False, error=str(exc))

    def create_gif(
        self,
        input_path: str,
        output_path: str = "",
        start_time: Any = 0,
        duration: Any = 3,
        width: int = 480,
        fps: int = 12,
        overwrite: bool = True,
        timeout: int = 1800,
    ) -> Dict[str, Any]:
        """Create a GIF preview from a video segment."""
        try:
            source = self._resolve_input(input_path)
            target = self._resolve_output(output_path, ".gif", f"{source.stem}_preview")
            start = self._parse_time(start_time, allow_empty=False) or 0.0
            clip_duration = self._parse_time(duration, allow_empty=False) or 3.0
            if clip_duration <= 0:
                raise ValueError("duration must be greater than zero")
            filter_complex = (
                f"fps={fps},scale={width}:-1:flags=lanczos,split[s0][s1];"
                "[s0]palettegen[p];[s1][p]paletteuse"
            )
            args: List[Union[str, Path]] = [
                self.ffmpeg_path,
                *self._overwrite_args(overwrite),
                "-ss",
                self._ff_time(start),
                "-t",
                self._ff_time(clip_duration),
                "-i",
                source,
                "-filter_complex",
                filter_complex,
                target,
            ]
            result = self._run(args, timeout=timeout, check_output=target)
            result.update({"input_file": str(source), "start_time": start, "duration": clip_duration, "width": width, "fps": fps})
            return result
        except Exception as exc:
            return self._format_response(False, error=str(exc))

    def list_media_files(self, base_path: str = ".", recursive: bool = False, media_type: str = "video", limit: int = 200) -> Dict[str, Any]:
        """List video, audio, or all media files under a directory."""
        try:
            root = Path(base_path).expanduser()
            if not root.is_absolute():
                root = Path.cwd() / root
            root = root.resolve()
            if not root.exists() or not root.is_dir():
                raise ValueError(f"directory not found: {root}")
            media_type = str(media_type or "video").lower()
            if media_type == "video":
                suffixes = set(VIDEO_EXTENSIONS)
            elif media_type == "audio":
                suffixes = set(AUDIO_EXTENSIONS)
            elif media_type == "all":
                suffixes = set(VIDEO_EXTENSIONS + AUDIO_EXTENSIONS)
            else:
                raise ValueError("media_type must be video, audio, or all")
            iterator = root.rglob("*") if recursive else root.glob("*")
            files = []
            for path in iterator:
                if path.is_file() and path.suffix.lower() in suffixes:
                    files.append({"path": str(path), "name": path.name, "size": path.stat().st_size})
                    if len(files) >= limit:
                        break
            return self._format_response(True, base_path=str(root), media_type=media_type, recursive=recursive, total=len(files), files=files)
        except Exception as exc:
            return self._format_response(False, error=str(exc))


class VideoToolManager:
    def __init__(self):
        self.handler = VideoHandler()

    def check_dependencies(self) -> Dict[str, Any]:
        """Check whether ffmpeg and ffprobe are available."""
        return self.handler.check_dependencies()

    def get_media_info(self, input_path: str, include_raw: bool = False) -> Dict[str, Any]:
        """Return media metadata from ffprobe.

        :param input_path: Video or audio file path.
        :param include_raw: Include the full ffprobe JSON payload when true.
        """
        return self.handler.get_media_info(input_path=input_path, include_raw=include_raw)

    def trim_video(
        self,
        input_path: str,
        output_path: str = "",
        start_time: Any = 0,
        end_time: Any = None,
        duration: Any = None,
        reencode: bool = False,
        video_codec: str = "libx264",
        audio_codec: str = "aac",
        crf: int = 18,
        preset: str = "medium",
        overwrite: bool = True,
        timeout: int = 1800,
    ) -> Dict[str, Any]:
        """Cut one segment from a video.

        :param input_path: Source video file.
        :param output_path: Output video path. Defaults to work/video_outputs.
        :param start_time: Start time in seconds, MM:SS, or HH:MM:SS.
        :param end_time: End time in seconds, MM:SS, or HH:MM:SS. Optional if duration is set.
        :param duration: Segment duration. Takes priority over end_time when provided.
        :param reencode: Re-encode for frame-accurate cuts. False uses fast stream copy.
        :param video_codec: Video codec when reencode is true.
        :param audio_codec: Audio codec when reencode is true.
        :param crf: H.264/H.265 quality value when reencoding. Lower is higher quality.
        :param preset: Encoder preset when reencoding.
        :param overwrite: Overwrite output file when true.
        :param timeout: Command timeout in seconds.
        """
        return self.handler.trim_video(
            input_path=input_path,
            output_path=output_path,
            start_time=start_time,
            end_time=end_time,
            duration=duration,
            reencode=reencode,
            video_codec=video_codec,
            audio_codec=audio_codec,
            crf=crf,
            preset=preset,
            overwrite=overwrite,
            timeout=timeout,
        )

    def split_video(
        self,
        input_path: str,
        output_dir: str = "",
        segment_duration: Any = None,
        cut_points: Optional[List[Any]] = None,
        output_pattern: str = "part_{index:03d}.mp4",
        reencode: bool = False,
        max_segments: int = 500,
        overwrite: bool = True,
        timeout: int = 1800,
    ) -> Dict[str, Any]:
        """Split a video by fixed segment duration or explicit cut points.

        :param input_path: Source video file.
        :param output_dir: Directory for generated clips. Defaults to work/video_outputs/<name>_parts.
        :param segment_duration: Fixed segment length, for example 10 or 00:00:10.
        :param cut_points: Explicit cut boundaries. Example: [10, 25.5, "00:01:00"].
        :param output_pattern: Filename pattern with {index}, {start}, and {end} placeholders.
        :param reencode: Re-encode each segment for frame-accurate cuts.
        :param max_segments: Safety limit for generated segments.
        :param overwrite: Overwrite existing segment files.
        :param timeout: Timeout per generated segment.
        """
        return self.handler.split_video(
            input_path=input_path,
            output_dir=output_dir,
            segment_duration=segment_duration,
            cut_points=cut_points,
            output_pattern=output_pattern,
            reencode=reencode,
            max_segments=max_segments,
            overwrite=overwrite,
            timeout=timeout,
        )

    def merge_videos(
        self,
        input_paths: List[str],
        output_path: str = "",
        reencode: bool = False,
        video_codec: str = "libx264",
        audio_codec: str = "aac",
        crf: int = 20,
        preset: str = "medium",
        overwrite: bool = True,
        timeout: int = 3600,
    ) -> Dict[str, Any]:
        """Concatenate multiple videos in order.

        :param input_paths: Ordered list of source video files.
        :param output_path: Output video path. Defaults to work/video_outputs.
        :param reencode: Re-encode final concat output. False uses fast stream copy.
        :param video_codec: Video codec for reencoding.
        :param audio_codec: Audio codec for reencoding.
        :param crf: Quality value for reencoding.
        :param preset: Encoder preset.
        :param overwrite: Overwrite output file.
        :param timeout: Command timeout in seconds.
        """
        return self.handler.merge_videos(
            input_paths=input_paths,
            output_path=output_path,
            reencode=reencode,
            video_codec=video_codec,
            audio_codec=audio_codec,
            crf=crf,
            preset=preset,
            overwrite=overwrite,
            timeout=timeout,
        )

    def add_audio_to_video(
        self,
        video_path: str,
        audio_path: str,
        output_path: str = "",
        mode: str = "replace",
        original_volume: float = 1.0,
        added_volume: float = 1.0,
        audio_start: Any = 0,
        loop_audio: bool = False,
        match_video_duration: bool = True,
        audio_codec: str = "aac",
        overwrite: bool = True,
        timeout: int = 1800,
    ) -> Dict[str, Any]:
        """Add, replace, or mix an audio file into a video.

        :param video_path: Source video file.
        :param audio_path: Audio file to add.
        :param output_path: Output video path. Defaults to work/video_outputs.
        :param mode: replace replaces video audio, mix blends with existing audio, keep_original adds a second audio track.
        :param original_volume: Existing video audio volume for mix mode.
        :param added_volume: Added audio volume for replace or mix mode.
        :param audio_start: Delay for added audio, in seconds/MM:SS/HH:MM:SS.
        :param loop_audio: Loop added audio until the video ends.
        :param match_video_duration: Limit output duration to source video duration.
        :param audio_codec: Output audio codec.
        :param overwrite: Overwrite output file.
        :param timeout: Command timeout in seconds.
        """
        return self.handler.add_audio_to_video(
            video_path=video_path,
            audio_path=audio_path,
            output_path=output_path,
            mode=mode,
            original_volume=original_volume,
            added_volume=added_volume,
            audio_start=audio_start,
            loop_audio=loop_audio,
            match_video_duration=match_video_duration,
            audio_codec=audio_codec,
            overwrite=overwrite,
            timeout=timeout,
        )

    def extract_audio(
        self,
        input_path: str,
        output_path: str = "",
        audio_codec: str = "",
        bitrate: str = "192k",
        overwrite: bool = True,
        timeout: int = 1800,
    ) -> Dict[str, Any]:
        """Extract audio from a video or audio container.

        :param input_path: Source media file.
        :param output_path: Output audio path. Extension selects a default codec.
        :param audio_codec: Optional codec override. Use copy to avoid reencoding.
        :param bitrate: Audio bitrate for lossy codecs.
        :param overwrite: Overwrite output file.
        :param timeout: Command timeout in seconds.
        """
        return self.handler.extract_audio(
            input_path=input_path,
            output_path=output_path,
            audio_codec=audio_codec,
            bitrate=bitrate,
            overwrite=overwrite,
            timeout=timeout,
        )

    def remove_audio(self, input_path: str, output_path: str = "", overwrite: bool = True, timeout: int = 1800) -> Dict[str, Any]:
        """Create a video-only copy with all audio tracks removed.

        :param input_path: Source video file.
        :param output_path: Output video path.
        :param overwrite: Overwrite output file.
        :param timeout: Command timeout in seconds.
        """
        return self.handler.remove_audio(input_path=input_path, output_path=output_path, overwrite=overwrite, timeout=timeout)

    def convert_video(
        self,
        input_path: str,
        output_path: str = "",
        video_codec: str = "libx264",
        audio_codec: str = "aac",
        width: int = 0,
        height: int = 0,
        fps: Any = 0,
        crf: int = 23,
        preset: str = "medium",
        keep_audio: bool = True,
        overwrite: bool = True,
        timeout: int = 3600,
    ) -> Dict[str, Any]:
        """Convert, resize, or compress a video.

        :param input_path: Source video file.
        :param output_path: Output video path. Extension controls the container.
        :param video_codec: Output video codec.
        :param audio_codec: Output audio codec.
        :param width: Output width. 0 preserves aspect ratio from height or source.
        :param height: Output height. 0 preserves aspect ratio from width or source.
        :param fps: Output frame rate. 0 preserves source frame rate.
        :param crf: Quality value for CRF encoders. Lower is higher quality.
        :param preset: Encoder preset.
        :param keep_audio: Keep and transcode audio when true.
        :param overwrite: Overwrite output file.
        :param timeout: Command timeout in seconds.
        """
        return self.handler.convert_video(
            input_path=input_path,
            output_path=output_path,
            video_codec=video_codec,
            audio_codec=audio_codec,
            width=width,
            height=height,
            fps=fps,
            crf=crf,
            preset=preset,
            keep_audio=keep_audio,
            overwrite=overwrite,
            timeout=timeout,
        )

    def create_thumbnail(
        self,
        input_path: str,
        output_path: str = "",
        timestamp: Any = 0,
        width: int = 0,
        height: int = 0,
        overwrite: bool = True,
        timeout: int = 600,
    ) -> Dict[str, Any]:
        """Export a still image from a video.

        :param input_path: Source video file.
        :param output_path: Output image path. Defaults to PNG in work/video_outputs.
        :param timestamp: Frame timestamp in seconds, MM:SS, or HH:MM:SS.
        :param width: Optional thumbnail width.
        :param height: Optional thumbnail height.
        :param overwrite: Overwrite output image.
        :param timeout: Command timeout in seconds.
        """
        return self.handler.create_thumbnail(
            input_path=input_path,
            output_path=output_path,
            timestamp=timestamp,
            width=width,
            height=height,
            overwrite=overwrite,
            timeout=timeout,
        )

    def create_gif(
        self,
        input_path: str,
        output_path: str = "",
        start_time: Any = 0,
        duration: Any = 3,
        width: int = 480,
        fps: int = 12,
        overwrite: bool = True,
        timeout: int = 1800,
    ) -> Dict[str, Any]:
        """Create a GIF preview from a video segment.

        :param input_path: Source video file.
        :param output_path: Output GIF path.
        :param start_time: Segment start time.
        :param duration: GIF duration.
        :param width: GIF width, preserving aspect ratio.
        :param fps: GIF frame rate.
        :param overwrite: Overwrite output GIF.
        :param timeout: Command timeout in seconds.
        """
        return self.handler.create_gif(
            input_path=input_path,
            output_path=output_path,
            start_time=start_time,
            duration=duration,
            width=width,
            fps=fps,
            overwrite=overwrite,
            timeout=timeout,
        )

    def list_media_files(self, base_path: str = ".", recursive: bool = False, media_type: str = "video", limit: int = 200) -> Dict[str, Any]:
        """List video, audio, or all media files under a directory.

        :param base_path: Directory to scan.
        :param recursive: Scan subdirectories when true.
        :param media_type: video, audio, or all.
        :param limit: Maximum number of results.
        """
        return self.handler.list_media_files(base_path=base_path, recursive=recursive, media_type=media_type, limit=limit)


def create_video_tool_manager() -> VideoToolManager:
    return VideoToolManager()


def main() -> None:
    if len(os.sys.argv) > 1:
        payload = json.loads(os.sys.argv[1])
    else:
        raw = os.sys.stdin.read().strip()
        payload = json.loads(raw) if raw else {}

    action = payload.pop("action", "check_dependencies")
    manager = VideoToolManager()
    if not hasattr(manager, action):
        print(json.dumps({"success": False, "error": f"Unknown action: {action}"}, ensure_ascii=False))
        return
    result = getattr(manager, action)(**payload)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
