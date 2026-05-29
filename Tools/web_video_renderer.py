#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Render a web page to a video file for Xenon.

The tool uses Playwright to open a URL or local HTML file, records the page,
and uses FFmpeg when conversion to MP4/MOV/MKV is needed. It can also fall
back to frame-by-frame screenshots for deterministic captures.
"""

from __future__ import annotations

import json
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
from urllib.parse import urlparse

try:
    from playwright.sync_api import Page, sync_playwright

    PLAYWRIGHT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on missing dependency
    Page = Any  # type: ignore
    sync_playwright = None  # type: ignore
    PLAYWRIGHT_AVAILABLE = False


DEFAULT_OUTPUT_DIR = Path("work") / "web_video_outputs"
SUPPORTED_OUTPUT_FORMATS = {"mp4", "webm", "mov", "mkv"}
VALID_WAIT_UNTIL = {"commit", "domcontentloaded", "load", "networkidle"}


class WebVideoRenderer:
    """Internal Playwright and FFmpeg renderer."""

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
    def _first_error_line(error: BaseException) -> str:
        text = str(error).strip()
        if not text:
            return error.__class__.__name__
        return text.splitlines()[0][:500]

    @staticmethod
    def _source_to_url(source: str) -> str:
        if not source or not str(source).strip():
            raise ValueError("source is required")

        value = str(source).strip()
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https", "file", "data", "about"}:
            return value

        if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith(("/", "\\")):
            path = Path(value).expanduser().resolve()
            if not path.exists():
                raise FileNotFoundError(f"local page not found: {path}")
            return path.as_uri()

        path = Path(value).expanduser()
        if path.exists() or path.suffix.lower() in {".html", ".htm", ".xhtml"}:
            resolved = path.resolve()
            if not resolved.exists():
                raise FileNotFoundError(f"local page not found: {resolved}")
            return resolved.as_uri()

        if parsed.scheme:
            raise ValueError(f"unsupported source scheme: {parsed.scheme}")

        return "https://" + value

    @staticmethod
    def _resolve_output(output_path: str, output_format: str, prefix: str = "web_render") -> Tuple[Path, str]:
        fmt = (output_format or "mp4").lower().strip().lstrip(".")
        if fmt not in SUPPORTED_OUTPUT_FORMATS:
            raise ValueError(f"unsupported output format: {output_format}. Use one of {sorted(SUPPORTED_OUTPUT_FORMATS)}")

        if output_path:
            target = Path(output_path).expanduser()
            if target.suffix:
                fmt = target.suffix.lower().lstrip(".")
                if fmt not in SUPPORTED_OUTPUT_FORMATS:
                    raise ValueError(f"unsupported output extension: {target.suffix}")
            else:
                target = target.with_suffix("." + fmt)
        else:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            target = DEFAULT_OUTPUT_DIR / f"{prefix}_{stamp}.{fmt}"

        if not target.is_absolute():
            target = Path.cwd() / target
        target = target.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        return target, fmt

    @staticmethod
    def _validate_positive_number(name: str, value: Any, default: float, minimum: float, maximum: float) -> float:
        if value in (None, ""):
            number = default
        else:
            number = float(value)
        if number < minimum or number > maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return number

    @staticmethod
    def _validate_int(name: str, value: Any, default: int, minimum: int, maximum: int) -> int:
        if value in (None, ""):
            number = default
        else:
            number = int(value)
        if number < minimum or number > maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        return number

    @staticmethod
    def _browser_candidates(browser_channel: str = "", browser_executable_path: str = "") -> List[Dict[str, Optional[str]]]:
        if browser_executable_path:
            return [{"label": "custom executable", "channel": None, "executable_path": browser_executable_path}]

        channel = (browser_channel or "auto").strip().lower()
        if channel in {"", "auto"}:
            return [
                {"label": "playwright chromium", "channel": None, "executable_path": None},
                {"label": "chrome", "channel": "chrome", "executable_path": None},
                {"label": "msedge", "channel": "msedge", "executable_path": None},
            ]
        if channel in {"chromium", "bundled", "playwright"}:
            return [{"label": "playwright chromium", "channel": None, "executable_path": None}]
        if channel in {"edge", "msedge", "microsoft-edge"}:
            return [{"label": "msedge", "channel": "msedge", "executable_path": None}]
        return [{"label": channel, "channel": channel, "executable_path": None}]

    def _launch_browser(
        self,
        playwright: Any,
        *,
        browser_channel: str,
        browser_executable_path: str,
        headless: bool,
    ) -> Tuple[Any, Dict[str, Any]]:
        failures: List[Dict[str, str]] = []
        for candidate in self._browser_candidates(browser_channel, browser_executable_path):
            label = str(candidate["label"])
            try:
                launch_kwargs: Dict[str, Any] = {"headless": headless}
                if candidate.get("channel"):
                    launch_kwargs["channel"] = candidate["channel"]
                if candidate.get("executable_path"):
                    launch_kwargs["executable_path"] = candidate["executable_path"]
                browser = playwright.chromium.launch(**launch_kwargs)
                return browser, {"browser": label, "headless": headless}
            except Exception as exc:  # pragma: no cover - depends on host browsers
                failures.append({"browser": label, "error": self._first_error_line(exc)})

        raise RuntimeError(
            "No usable Chromium browser found. Run 'playwright install chromium', "
            "set browser_channel to 'chrome' or 'msedge', or pass browser_executable_path. "
            f"Failures: {failures}"
        )

    def _run(self, args: Sequence[Union[str, Path]], timeout: int = 1800, check_output: Optional[Path] = None) -> Dict[str, Any]:
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
                result.update(self._output_file_info(check_output))
            if not success:
                result["error"] = stderr or stdout or "command failed"
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

    @staticmethod
    def _overwrite_args(overwrite: bool) -> List[str]:
        return ["-y"] if overwrite else ["-n"]

    @staticmethod
    def _output_file_info(path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {"output_file": str(path), "file_exists": False, "file_size": 0}
        return {"output_file": str(path), "file_exists": True, "file_size": path.stat().st_size}

    @staticmethod
    def _page_options(
        *,
        width: int,
        height: int,
        device_scale_factor: float,
        user_agent: str,
        locale: str,
        timezone_id: str,
        ignore_https_errors: bool,
        extra_http_headers: Optional[Dict[str, str]],
    ) -> Dict[str, Any]:
        options: Dict[str, Any] = {
            "viewport": {"width": width, "height": height},
            "device_scale_factor": device_scale_factor,
            "ignore_https_errors": ignore_https_errors,
        }
        if user_agent:
            options["user_agent"] = user_agent
        if locale:
            options["locale"] = locale
        if timezone_id:
            options["timezone_id"] = timezone_id
        if extra_http_headers:
            options["extra_http_headers"] = extra_http_headers
        return options

    @staticmethod
    def _wait_for_page(
        page: Page,
        url: str,
        *,
        wait_until: str,
        navigation_timeout: float,
        initial_wait: float,
    ) -> Dict[str, Any]:
        response = page.goto(url, wait_until=wait_until, timeout=int(navigation_timeout * 1000))
        if initial_wait > 0:
            page.wait_for_timeout(int(initial_wait * 1000))

        return {
            "final_url": page.url,
            "http_status": response.status if response is not None else None,
            "page_title": page.title(),
        }

    @staticmethod
    def _scroll_page(page: Page, duration: float) -> None:
        page.evaluate(
            """
            async ({ durationMs }) => {
                const root = document.scrollingElement || document.documentElement || document.body;
                const maxY = Math.max(0, root.scrollHeight - window.innerHeight);
                const startY = window.scrollY || 0;
                const start = performance.now();
                await new Promise(resolve => {
                    const step = now => {
                        const progress = durationMs <= 0 ? 1 : Math.min(1, (now - start) / durationMs);
                        window.scrollTo(0, startY + (maxY - startY) * progress);
                        if (progress < 1) {
                            requestAnimationFrame(step);
                        } else {
                            resolve();
                        }
                    };
                    requestAnimationFrame(step);
                });
            }
            """,
            {"durationMs": int(duration * 1000)},
        )

    def _convert_video(
        self,
        input_video: Path,
        output_path: Path,
        *,
        output_format: str,
        fps: int,
        crf: int,
        preset: str,
        overwrite: bool,
        timeout: int,
    ) -> Dict[str, Any]:
        if output_format == "webm" and input_video.suffix.lower() == ".webm":
            if output_path.exists() and not overwrite:
                return self._format_response(False, output_file=str(output_path), error=f"output file already exists: {output_path}")
            shutil.copy2(input_video, output_path)
            return self._format_response(True, command="copy webm", **self._output_file_info(output_path))

        if not shutil.which(str(self.ffmpeg_path)) and not Path(str(self.ffmpeg_path)).exists():
            return self._format_response(False, error="ffmpeg is required for non-webm output")

        filters = [f"fps={fps}", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "format=yuv420p"]
        args: List[Union[str, Path]] = [
            self.ffmpeg_path,
            *self._overwrite_args(overwrite),
            "-i",
            input_video,
            "-an",
            "-vf",
            ",".join(filters),
        ]

        if output_format in {"mp4", "mov", "mkv"}:
            args.extend(["-c:v", "libx264", "-crf", str(crf), "-preset", preset])
            if output_format in {"mp4", "mov"}:
                args.extend(["-movflags", "+faststart"])
        else:
            args.extend(["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(max(crf, 30))])

        args.append(output_path)
        return self._run(args, timeout=timeout, check_output=output_path)

    def _encode_frames(
        self,
        frame_pattern: Path,
        output_path: Path,
        *,
        output_format: str,
        fps: int,
        crf: int,
        preset: str,
        overwrite: bool,
        timeout: int,
    ) -> Dict[str, Any]:
        if not shutil.which(str(self.ffmpeg_path)) and not Path(str(self.ffmpeg_path)).exists():
            return self._format_response(False, error="ffmpeg is required for frame capture output")

        args: List[Union[str, Path]] = [
            self.ffmpeg_path,
            *self._overwrite_args(overwrite),
            "-framerate",
            str(fps),
            "-i",
            frame_pattern,
            "-an",
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
        ]

        if output_format == "webm":
            args.extend(["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(max(crf, 30))])
        else:
            args.extend(["-c:v", "libx264", "-crf", str(crf), "-preset", preset])
            if output_format in {"mp4", "mov"}:
                args.extend(["-movflags", "+faststart"])

        args.append(output_path)
        return self._run(args, timeout=timeout, check_output=output_path)

    def check_dependencies(self, browser_channel: str = "auto", browser_executable_path: str = "", headless: bool = True) -> Dict[str, Any]:
        """Check Playwright, browser, FFmpeg, and FFprobe availability."""
        ffmpeg_path = shutil.which(str(self.ffmpeg_path)) or (str(self.ffmpeg_path) if Path(str(self.ffmpeg_path)).exists() else "")
        ffprobe_path = shutil.which(str(self.ffprobe_path)) or (str(self.ffprobe_path) if Path(str(self.ffprobe_path)).exists() else "")

        browser_results: List[Dict[str, Any]] = []
        usable_browser: Optional[Dict[str, Any]] = None
        if PLAYWRIGHT_AVAILABLE:
            with sync_playwright() as playwright:
                for candidate in self._browser_candidates(browser_channel, browser_executable_path):
                    label = str(candidate["label"])
                    try:
                        kwargs: Dict[str, Any] = {"headless": headless}
                        if candidate.get("channel"):
                            kwargs["channel"] = candidate["channel"]
                        if candidate.get("executable_path"):
                            kwargs["executable_path"] = candidate["executable_path"]
                        browser = playwright.chromium.launch(**kwargs)
                        browser.close()
                        item = {"browser": label, "available": True}
                        browser_results.append(item)
                        usable_browser = item
                        break
                    except Exception as exc:  # pragma: no cover - depends on host browsers
                        browser_results.append({"browser": label, "available": False, "error": self._first_error_line(exc)})

        success = PLAYWRIGHT_AVAILABLE and usable_browser is not None and bool(ffmpeg_path)
        return self._format_response(
            success,
            playwright_available=PLAYWRIGHT_AVAILABLE,
            browser_available=usable_browser is not None,
            usable_browser=usable_browser,
            browser_results=browser_results,
            ffmpeg_available=bool(ffmpeg_path),
            ffmpeg_path=ffmpeg_path or self.ffmpeg_path,
            ffprobe_available=bool(ffprobe_path),
            ffprobe_path=ffprobe_path or self.ffprobe_path,
        )

    def render_page_to_video(
        self,
        source: str,
        output_path: str = "",
        duration: Any = 5,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        output_format: str = "mp4",
        capture_mode: str = "auto",
        wait_until: str = "networkidle",
        initial_wait: Any = 0.5,
        scroll: bool = False,
        browser_channel: str = "auto",
        browser_executable_path: str = "",
        headless: bool = True,
        device_scale_factor: Any = 1,
        user_agent: str = "",
        locale: str = "",
        timezone_id: str = "",
        ignore_https_errors: bool = False,
        extra_http_headers: Optional[Dict[str, str]] = None,
        crf: int = 23,
        preset: str = "medium",
        overwrite: bool = True,
        navigation_timeout: Any = 60,
        timeout: int = 1800,
        keep_intermediate: bool = False,
    ) -> Dict[str, Any]:
        """Render a URL or local HTML file to video.

        :param source: URL, domain, file:// URL, or local HTML path.
        :param output_path: Output path. Defaults to work/web_video_outputs.
        :param duration: Capture duration in seconds after navigation and initial wait.
        :param width: Browser viewport width.
        :param height: Browser viewport height.
        :param fps: Output frame rate, 1 to 60.
        :param output_format: mp4, webm, mov, or mkv. Output extension wins when output_path has one.
        :param capture_mode: auto, native, or frames. Native uses browser video recording; frames uses screenshots.
        :param wait_until: Playwright navigation state: commit, domcontentloaded, load, or networkidle.
        :param initial_wait: Extra wait before the capture duration begins.
        :param scroll: Slowly scroll from the current position to the bottom during capture.
        :param browser_channel: auto, chromium, chrome, or msedge.
        :param browser_executable_path: Optional explicit Chromium executable path.
        :param headless: Run the browser headlessly.
        :param device_scale_factor: Browser device scale factor.
        :param user_agent: Optional user agent override.
        :param locale: Optional browser locale.
        :param timezone_id: Optional browser timezone id.
        :param ignore_https_errors: Ignore HTTPS certificate errors.
        :param extra_http_headers: Optional HTTP headers.
        :param crf: FFmpeg quality value for encoders. Lower is higher quality.
        :param preset: FFmpeg x264 preset.
        :param overwrite: Overwrite existing output file.
        :param navigation_timeout: Page navigation timeout in seconds.
        :param timeout: FFmpeg timeout in seconds.
        :param keep_intermediate: Keep raw webm or screenshot frames beside the output.
        """
        if not PLAYWRIGHT_AVAILABLE:
            return self._format_response(False, error="playwright is not installed")

        try:
            url = self._source_to_url(source)
            duration_seconds = self._validate_positive_number("duration", duration, 5.0, 0.1, 3600.0)
            initial_wait_seconds = self._validate_positive_number("initial_wait", initial_wait, 0.5, 0.0, 3600.0)
            navigation_timeout_seconds = self._validate_positive_number("navigation_timeout", navigation_timeout, 60.0, 1.0, 3600.0)
            viewport_width = self._validate_int("width", width, 1280, 64, 7680)
            viewport_height = self._validate_int("height", height, 720, 64, 4320)
            output_fps = self._validate_int("fps", fps, 30, 1, 60)
            quality_crf = self._validate_int("crf", crf, 23, 0, 63)
            scale_factor = self._validate_positive_number("device_scale_factor", device_scale_factor, 1.0, 0.1, 5.0)
            mode = (capture_mode or "auto").strip().lower()
            if mode not in {"auto", "native", "frames"}:
                raise ValueError("capture_mode must be auto, native, or frames")
            wait_state = (wait_until or "networkidle").strip().lower()
            if wait_state not in VALID_WAIT_UNTIL:
                raise ValueError(f"wait_until must be one of {sorted(VALID_WAIT_UNTIL)}")
            output_file, fmt = self._resolve_output(output_path, output_format)
        except Exception as exc:
            return self._format_response(False, error=str(exc))

        errors: List[Dict[str, str]] = []
        if mode in {"auto", "native"}:
            native_result = self._render_native(
                url,
                output_file,
                output_format=fmt,
                duration=duration_seconds,
                width=viewport_width,
                height=viewport_height,
                fps=output_fps,
                wait_until=wait_state,
                initial_wait=initial_wait_seconds,
                scroll=scroll,
                browser_channel=browser_channel,
                browser_executable_path=browser_executable_path,
                headless=headless,
                device_scale_factor=scale_factor,
                user_agent=user_agent,
                locale=locale,
                timezone_id=timezone_id,
                ignore_https_errors=ignore_https_errors,
                extra_http_headers=extra_http_headers,
                crf=quality_crf,
                preset=preset,
                overwrite=overwrite,
                navigation_timeout=navigation_timeout_seconds,
                timeout=timeout,
                keep_intermediate=keep_intermediate,
            )
            if native_result.get("success") or mode == "native":
                return native_result
            errors.append({"capture_mode": "native", "error": str(native_result.get("error", "native capture failed"))})

        frame_result = self._render_frames(
            url,
            output_file,
            output_format=fmt,
            duration=duration_seconds,
            width=viewport_width,
            height=viewport_height,
            fps=output_fps,
            wait_until=wait_state,
            initial_wait=initial_wait_seconds,
            scroll=scroll,
            browser_channel=browser_channel,
            browser_executable_path=browser_executable_path,
            headless=headless,
            device_scale_factor=scale_factor,
            user_agent=user_agent,
            locale=locale,
            timezone_id=timezone_id,
            ignore_https_errors=ignore_https_errors,
            extra_http_headers=extra_http_headers,
            crf=quality_crf,
            preset=preset,
            overwrite=overwrite,
            navigation_timeout=navigation_timeout_seconds,
            timeout=timeout,
            keep_intermediate=keep_intermediate,
        )
        if errors and not frame_result.get("success"):
            frame_result["previous_errors"] = errors
        return frame_result

    def _render_native(
        self,
        url: str,
        output_file: Path,
        *,
        output_format: str,
        duration: float,
        width: int,
        height: int,
        fps: int,
        wait_until: str,
        initial_wait: float,
        scroll: bool,
        browser_channel: str,
        browser_executable_path: str,
        headless: bool,
        device_scale_factor: float,
        user_agent: str,
        locale: str,
        timezone_id: str,
        ignore_https_errors: bool,
        extra_http_headers: Optional[Dict[str, str]],
        crf: int,
        preset: str,
        overwrite: bool,
        navigation_timeout: float,
        timeout: int,
        keep_intermediate: bool,
    ) -> Dict[str, Any]:
        start = time.perf_counter()
        temp_dir_obj = tempfile.TemporaryDirectory(prefix="xenon_web_video_")
        temp_dir = Path(temp_dir_obj.name)
        browser = None
        context = None
        raw_video: Optional[Path] = None
        page_info: Dict[str, Any] = {}
        launch_info: Dict[str, Any] = {}
        try:
            with sync_playwright() as playwright:
                browser, launch_info = self._launch_browser(
                    playwright,
                    browser_channel=browser_channel,
                    browser_executable_path=browser_executable_path,
                    headless=headless,
                )
                options = self._page_options(
                    width=width,
                    height=height,
                    device_scale_factor=device_scale_factor,
                    user_agent=user_agent,
                    locale=locale,
                    timezone_id=timezone_id,
                    ignore_https_errors=ignore_https_errors,
                    extra_http_headers=extra_http_headers,
                )
                options.update({"record_video_dir": str(temp_dir), "record_video_size": {"width": width, "height": height}})
                context = browser.new_context(**options)
                page = context.new_page()
                page_info = self._wait_for_page(
                    page,
                    url,
                    wait_until=wait_until,
                    navigation_timeout=navigation_timeout,
                    initial_wait=initial_wait,
                )
                if scroll:
                    self._scroll_page(page, duration)
                else:
                    page.wait_for_timeout(int(duration * 1000))
                video = page.video
                context.close()
                context = None
                if video is None:
                    raise RuntimeError("Playwright did not create a page video")
                raw_video = Path(video.path()).resolve()
                browser.close()
                browser = None

            if raw_video is None or not raw_video.exists():
                raise RuntimeError("native recording did not create a video file")

            intermediate_file = ""
            input_for_conversion = raw_video
            if keep_intermediate:
                intermediate_file = str(output_file.with_suffix(".raw.webm"))
                shutil.copy2(raw_video, intermediate_file)
                input_for_conversion = Path(intermediate_file)

            converted = self._convert_video(
                input_for_conversion,
                output_file,
                output_format=output_format,
                fps=fps,
                crf=crf,
                preset=preset,
                overwrite=overwrite,
                timeout=timeout,
            )
            converted.update(
                {
                    "capture_mode": "native",
                    "source": url,
                    "duration": duration,
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "render_seconds": round(time.perf_counter() - start, 3),
                    "launch": launch_info,
                    "page": page_info,
                }
            )
            if intermediate_file:
                converted["intermediate_file"] = intermediate_file
            return converted
        except Exception as exc:
            return self._format_response(False, capture_mode="native", output_file=str(output_file), source=url, error=str(exc))
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            temp_dir_obj.cleanup()

    def _render_frames(
        self,
        url: str,
        output_file: Path,
        *,
        output_format: str,
        duration: float,
        width: int,
        height: int,
        fps: int,
        wait_until: str,
        initial_wait: float,
        scroll: bool,
        browser_channel: str,
        browser_executable_path: str,
        headless: bool,
        device_scale_factor: float,
        user_agent: str,
        locale: str,
        timezone_id: str,
        ignore_https_errors: bool,
        extra_http_headers: Optional[Dict[str, str]],
        crf: int,
        preset: str,
        overwrite: bool,
        navigation_timeout: float,
        timeout: int,
        keep_intermediate: bool,
    ) -> Dict[str, Any]:
        start = time.perf_counter()
        temp_dir_obj = tempfile.TemporaryDirectory(prefix="xenon_web_frames_")
        temp_dir = Path(temp_dir_obj.name)
        browser = None
        context = None
        page_info: Dict[str, Any] = {}
        launch_info: Dict[str, Any] = {}
        frame_count = max(1, int(round(duration * fps)))
        try:
            with sync_playwright() as playwright:
                browser, launch_info = self._launch_browser(
                    playwright,
                    browser_channel=browser_channel,
                    browser_executable_path=browser_executable_path,
                    headless=headless,
                )
                context = browser.new_context(
                    **self._page_options(
                        width=width,
                        height=height,
                        device_scale_factor=device_scale_factor,
                        user_agent=user_agent,
                        locale=locale,
                        timezone_id=timezone_id,
                        ignore_https_errors=ignore_https_errors,
                        extra_http_headers=extra_http_headers,
                    )
                )
                page = context.new_page()
                page_info = self._wait_for_page(
                    page,
                    url,
                    wait_until=wait_until,
                    navigation_timeout=navigation_timeout,
                    initial_wait=initial_wait,
                )
                scroll_height = 0
                if scroll:
                    scroll_height = int(
                        page.evaluate(
                            "() => Math.max(0, (document.scrollingElement || document.documentElement || document.body).scrollHeight - window.innerHeight)"
                        )
                    )

                capture_start = time.perf_counter()
                for index in range(frame_count):
                    if scroll and frame_count > 1:
                        progress = index / max(1, frame_count - 1)
                        page.evaluate("(y) => window.scrollTo(0, y)", scroll_height * progress)
                    frame_path = temp_dir / f"frame_{index + 1:06d}.png"
                    page.screenshot(path=str(frame_path), full_page=False)
                    next_time = capture_start + ((index + 1) / fps)
                    wait_seconds = next_time - time.perf_counter()
                    if wait_seconds > 0 and index < frame_count - 1:
                        page.wait_for_timeout(int(wait_seconds * 1000))

                context.close()
                context = None
                browser.close()
                browser = None

            frame_pattern = temp_dir / "frame_%06d.png"
            encoded = self._encode_frames(
                frame_pattern,
                output_file,
                output_format=output_format,
                fps=fps,
                crf=crf,
                preset=preset,
                overwrite=overwrite,
                timeout=timeout,
            )
            encoded.update(
                {
                    "capture_mode": "frames",
                    "source": url,
                    "duration": duration,
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "frame_count": frame_count,
                    "render_seconds": round(time.perf_counter() - start, 3),
                    "launch": launch_info,
                    "page": page_info,
                }
            )

            if keep_intermediate:
                frames_dir = output_file.with_suffix(".frames")
                if frames_dir.exists() and overwrite:
                    shutil.rmtree(frames_dir)
                if not frames_dir.exists():
                    shutil.copytree(temp_dir, frames_dir)
                encoded["frames_dir"] = str(frames_dir)
            return encoded
        except Exception as exc:
            return self._format_response(False, capture_mode="frames", output_file=str(output_file), source=url, error=str(exc))
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            temp_dir_obj.cleanup()


class WebVideoRendererToolManager:
    """Tool manager exposed to Xenon."""

    def __init__(self):
        self.renderer = WebVideoRenderer()

    def check_dependencies(self, browser_channel: str = "auto", browser_executable_path: str = "", headless: bool = True) -> Dict[str, Any]:
        """Check whether Playwright, a Chromium browser, and FFmpeg are usable.

        :param browser_channel: auto, chromium, chrome, or msedge.
        :param browser_executable_path: Optional explicit Chromium executable path.
        :param headless: Run the browser headlessly during the check.
        """
        return self.renderer.check_dependencies(
            browser_channel=browser_channel,
            browser_executable_path=browser_executable_path,
            headless=headless,
        )

    def render_page_to_video(
        self,
        source: str,
        output_path: str = "",
        duration: Any = 5,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        output_format: str = "mp4",
        capture_mode: str = "auto",
        wait_until: str = "networkidle",
        initial_wait: Any = 0.5,
        scroll: bool = False,
        browser_channel: str = "auto",
        browser_executable_path: str = "",
        headless: bool = True,
        device_scale_factor: Any = 1,
        user_agent: str = "",
        locale: str = "",
        timezone_id: str = "",
        ignore_https_errors: bool = False,
        extra_http_headers: Optional[Dict[str, str]] = None,
        crf: int = 23,
        preset: str = "medium",
        overwrite: bool = True,
        navigation_timeout: Any = 60,
        timeout: int = 1800,
        keep_intermediate: bool = False,
    ) -> Dict[str, Any]:
        """Render a URL or local HTML file to a video file.

        :param source: URL, domain, file:// URL, or local HTML path.
        :param output_path: Output path. Defaults to work/web_video_outputs.
        :param duration: Capture duration in seconds after navigation and initial wait.
        :param width: Browser viewport width.
        :param height: Browser viewport height.
        :param fps: Output frame rate, 1 to 60.
        :param output_format: mp4, webm, mov, or mkv. Output extension wins when output_path has one.
        :param capture_mode: auto, native, or frames.
        :param wait_until: commit, domcontentloaded, load, or networkidle.
        :param initial_wait: Extra wait before capture.
        :param scroll: Slowly scroll to the page bottom during capture.
        :param browser_channel: auto, chromium, chrome, or msedge.
        :param browser_executable_path: Optional explicit Chromium executable path.
        :param headless: Run the browser headlessly.
        :param device_scale_factor: Browser device scale factor.
        :param user_agent: Optional user agent override.
        :param locale: Optional browser locale.
        :param timezone_id: Optional browser timezone id.
        :param ignore_https_errors: Ignore HTTPS certificate errors.
        :param extra_http_headers: Optional HTTP headers.
        :param crf: FFmpeg quality value. Lower is higher quality.
        :param preset: FFmpeg x264 preset.
        :param overwrite: Overwrite existing output file.
        :param navigation_timeout: Page navigation timeout in seconds.
        :param timeout: FFmpeg timeout in seconds.
        :param keep_intermediate: Keep raw webm or screenshot frames beside the output.
        """
        return self.renderer.render_page_to_video(
            source=source,
            output_path=output_path,
            duration=duration,
            width=width,
            height=height,
            fps=fps,
            output_format=output_format,
            capture_mode=capture_mode,
            wait_until=wait_until,
            initial_wait=initial_wait,
            scroll=scroll,
            browser_channel=browser_channel,
            browser_executable_path=browser_executable_path,
            headless=headless,
            device_scale_factor=device_scale_factor,
            user_agent=user_agent,
            locale=locale,
            timezone_id=timezone_id,
            ignore_https_errors=ignore_https_errors,
            extra_http_headers=extra_http_headers,
            crf=crf,
            preset=preset,
            overwrite=overwrite,
            navigation_timeout=navigation_timeout,
            timeout=timeout,
            keep_intermediate=keep_intermediate,
        )


def create_web_video_renderer_tool_manager() -> WebVideoRendererToolManager:
    return WebVideoRendererToolManager()


def main() -> None:
    if len(os.sys.argv) > 1 and os.sys.argv[1] in {"-h", "--help"}:
        print(
            "Usage: python Tools/web_video_renderer.py '{\"action\":\"render_page_to_video\","
            "\"source\":\"https://example.com\",\"output_path\":\"out.mp4\"}'"
        )
        return

    if len(os.sys.argv) > 1:
        payload = json.loads(os.sys.argv[1])
    else:
        raw = os.sys.stdin.read().strip()
        payload = json.loads(raw) if raw else {}

    action = payload.pop("action", "check_dependencies")
    manager = WebVideoRendererToolManager()
    if not hasattr(manager, action):
        print(json.dumps({"success": False, "error": f"Unknown action: {action}"}, ensure_ascii=False))
        return
    result = getattr(manager, action)(**payload)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
