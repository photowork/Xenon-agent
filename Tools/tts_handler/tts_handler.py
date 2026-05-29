#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
DashScope Qwen TTS tool for Xenon.

The tool uses the non-streaming Qwen-TTS HTTP API and optionally downloads
the returned 24-hour audio URL to a local wav file for downstream tools.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "tts_config.json"
DEFAULT_API_BASE = "https://dashscope.aliyuncs.com/api/v1"
DEFAULT_ENDPOINT = f"{DEFAULT_API_BASE}/services/aigc/multimodal-generation/generation"
DEFAULT_OUTPUT_DIR = Path("work") / "tts_outputs"
SUPPORTED_LANGUAGE_TYPES = [
    "Auto",
    "Chinese",
    "English",
    "German",
    "Italian",
    "Portuguese",
    "Spanish",
    "Japanese",
    "Korean",
    "French",
    "Russian",
]
SUPPORTED_VOICES = [
    {"voice": "Cherry", "name": "Qianyue", "gender": "female", "style": "bright and friendly"},
    {"voice": "Serena", "name": "Suyao", "gender": "female", "style": "gentle"},
    {"voice": "Ethan", "name": "Chenxu", "gender": "male", "style": "warm and energetic"},
    {"voice": "Chelsie", "name": "Qianxue", "gender": "female", "style": "anime character"},
    {"voice": "Momo", "name": "Motu", "gender": "female", "style": "playful"},
    {"voice": "Vivian", "name": "Shisan", "gender": "female", "style": "cute and assertive"},
    {"voice": "Moon", "name": "Yuebai", "gender": "male", "style": "cool and relaxed"},
    {"voice": "Maia", "name": "Siyue", "gender": "female", "style": "intellectual and gentle"},
    {"voice": "Kai", "name": "Kai", "gender": "male", "style": "soft and immersive"},
    {"voice": "Nofish", "name": "Buchiyu", "gender": "male", "style": "non-retroflex accent"},
    {"voice": "Bella", "name": "Mengbao", "gender": "female", "style": "childlike"},
    {"voice": "Jennifer", "name": "Jennifer", "gender": "female", "style": "cinematic US English"},
    {"voice": "Ryan", "name": "Ryan", "gender": "male", "style": "dramatic"},
    {"voice": "Katerina", "name": "Katerina", "gender": "female", "style": "mature and rhythmic"},
    {"voice": "Aiden", "name": "Aiden", "gender": "male", "style": "US English young male"},
    {"voice": "Eldric Sage", "name": "Cangmingzi", "gender": "male", "style": "wise elder"},
    {"voice": "Mia", "name": "Mia", "gender": "female", "style": "gentle and obedient"},
    {"voice": "Mochi", "name": "Mochi", "gender": "male", "style": "smart child"},
    {"voice": "Bellona", "name": "Bellona", "gender": "female", "style": "loud and clear"},
    {"voice": "Vincent", "name": "Vincent", "gender": "male", "style": "husky"},
    {"voice": "Bunny", "name": "Bunny", "gender": "female", "style": "cute childlike"},
]


class TTSHandler:
    """Internal DashScope Qwen TTS client."""

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = Path(config_path).expanduser().resolve() if config_path else DEFAULT_CONFIG_PATH

    @staticmethod
    def _format_response(success: bool, **kwargs: Any) -> Dict[str, Any]:
        result = {"success": success}
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
        endpoint = str(config.get("endpoint") or f"{api_base}/services/aigc/multimodal-generation/generation")

        return {
            "path": str(path),
            "api_key": api_key,
            "api_key_env": env_key_name,
            "api_base": api_base,
            "endpoint": endpoint,
            "model": str(config.get("model") or "qwen3-tts-flash"),
            "default_voice": str(config.get("default_voice") or "Cherry"),
            "default_language_type": str(config.get("default_language_type") or "Chinese"),
            "default_output_dir": str(config.get("default_output_dir") or DEFAULT_OUTPUT_DIR),
            "download_audio": bool(config.get("download_audio", True)),
            "timeout": int(config.get("timeout", 120)),
        }

    def _build_output_path(self, text: str, output_path: str, output_dir: str) -> Path:
        if output_path:
            target = Path(output_path).expanduser()
            if target.suffix.lower() != ".wav":
                target = target.with_suffix(".wav")
        else:
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
            filename = f"tts_{time.strftime('%Y%m%d_%H%M%S')}_{digest}.wav"
            target = Path(output_dir).expanduser() / filename

        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def _download_file(url: str, target: Path, timeout: int) -> int:
        with requests.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            with open(target, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        handle.write(chunk)
        return target.stat().st_size

    @staticmethod
    def _write_base64_audio(data: str, target: Path) -> int:
        audio_bytes = base64.b64decode(data)
        with open(target, "wb") as handle:
            handle.write(audio_bytes)
        return target.stat().st_size

    def get_config_status(self, config_path: str = "") -> Dict[str, Any]:
        """Return TTS configuration status with the API key redacted.

        :param config_path: Optional config JSON path. Defaults to Tools/tts_handler/tts_config.json.
        """
        try:
            config = self._load_config(config_path or None)
            return self._format_response(
                True,
                config_path=config["path"],
                api_key_set=bool(config["api_key"]),
                api_key_preview=self._redact_secret(config["api_key"]),
                api_key_env=config["api_key_env"],
                api_base=config["api_base"],
                endpoint=config["endpoint"],
                model=config["model"],
                default_voice=config["default_voice"],
                default_language_type=config["default_language_type"],
                default_output_dir=config["default_output_dir"],
                download_audio=config["download_audio"],
                timeout=config["timeout"],
            )
        except Exception as exc:
            return self._format_response(False, error=str(exc), message="Failed to read TTS config")

    def save_config(
        self,
        api_key: str,
        api_base: str = DEFAULT_API_BASE,
        model: str = "qwen3-tts-flash",
        default_voice: str = "Cherry",
        default_language_type: str = "Chinese",
        default_output_dir: str = str(DEFAULT_OUTPUT_DIR),
        download_audio: bool = True,
        timeout: int = 120,
        config_path: str = "",
    ) -> Dict[str, Any]:
        """Save a DashScope TTS config JSON file.

        :param api_key: DashScope or Bailian API key.
        :param api_base: API base URL. Beijing default is https://dashscope.aliyuncs.com/api/v1.
        :param model: Qwen TTS model, for example qwen3-tts-flash.
        :param default_voice: Default voice parameter, for example Cherry.
        :param default_language_type: Default language_type, for example Chinese or Auto.
        :param default_output_dir: Directory for downloaded audio files.
        :param download_audio: Whether synthesize_speech downloads audio by default.
        :param timeout: Request timeout in seconds.
        :param config_path: Optional config path. Defaults to Tools/tts_handler/tts_config.json.
        """
        target = Path(config_path).expanduser().resolve() if config_path else self.config_path
        target.parent.mkdir(parents=True, exist_ok=True)
        api_base_clean = api_base.rstrip("/")
        payload = {
            "api_key": api_key.strip(),
            "api_key_env": "DASHSCOPE_API_KEY",
            "api_base": api_base_clean,
            "endpoint": f"{api_base_clean}/services/aigc/multimodal-generation/generation",
            "model": model,
            "default_voice": default_voice,
            "default_language_type": default_language_type,
            "default_output_dir": default_output_dir,
            "download_audio": download_audio,
            "timeout": timeout,
        }
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return self._format_response(
            True,
            config_path=str(target),
            api_key_set=bool(payload["api_key"]),
            api_key_preview=self._redact_secret(payload["api_key"]),
            message="TTS config saved",
        )

    def list_supported_voices(self) -> Dict[str, Any]:
        """List common Qwen-TTS system voices and supported language_type values."""
        return self._format_response(
            True,
            voices=SUPPORTED_VOICES,
            language_types=SUPPORTED_LANGUAGE_TYPES,
            default_voice="Cherry",
            default_language_type="Chinese",
            note="Voice support can vary by model snapshot. See the Aliyun Qwen-TTS voice list for the latest table.",
        )

    def synthesize_speech(
        self,
        text: str,
        output_path: str = "",
        voice: str = "",
        language_type: str = "",
        model: str = "",
        instructions: str = "",
        optimize_instructions: bool = False,
        download_audio: Optional[bool] = None,
        config_path: str = "",
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Synthesize text into speech using DashScope Qwen-TTS.

        :param text: Text to synthesize.
        :param output_path: Optional local wav file path. If omitted, a file is created under the configured output directory.
        :param voice: Qwen-TTS voice, for example Cherry. Defaults to config default_voice.
        :param language_type: Qwen-TTS language_type, for example Chinese, English, or Auto. Defaults to config default_language_type.
        :param model: Qwen TTS model. Defaults to config model.
        :param instructions: Optional voice-control prompt for instruct-capable Qwen3 TTS models.
        :param optimize_instructions: Whether to ask the service to optimize instructions.
        :param download_audio: Whether to download the returned audio URL. Defaults to config download_audio.
        :param config_path: Optional config JSON path. Defaults to Tools/tts_handler/tts_config.json.
        :param timeout: Request timeout in seconds. Defaults to config timeout.
        """
        clean_text = (text or "").strip()
        if not clean_text:
            return self._format_response(False, error="text cannot be empty", message="Invalid parameter")

        try:
            config = self._load_config(config_path or None)
        except Exception as exc:
            return self._format_response(False, error=str(exc), message="Failed to load TTS config")

        if not config["api_key"]:
            return self._format_response(
                False,
                error="Missing API key. Set api_key in tts_config.json or DASHSCOPE_API_KEY.",
                config_path=config["path"],
                message="TTS config is incomplete",
            )

        request_timeout = int(timeout or config["timeout"])
        selected_model = model or config["model"]
        selected_voice = voice or config["default_voice"]
        selected_language = language_type or config["default_language_type"]
        should_download = config["download_audio"] if download_audio is None else bool(download_audio)

        input_payload: Dict[str, Any] = {
            "text": clean_text,
            "voice": selected_voice,
            "language_type": selected_language,
        }
        if instructions:
            input_payload["instructions"] = instructions
            input_payload["optimize_instructions"] = bool(optimize_instructions)

        payload = {"model": selected_model, "input": input_payload}
        headers = {
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(
                config["endpoint"],
                headers=headers,
                json=payload,
                timeout=request_timeout,
            )
            response_text = response.text
            try:
                data = response.json()
            except ValueError:
                data = {}

            if response.status_code >= 400:
                return self._format_response(
                    False,
                    status_code=response.status_code,
                    request_id=data.get("request_id"),
                    code=data.get("code", ""),
                    error=data.get("message") or response_text[:500],
                    message="TTS API request failed",
                )
        except requests.RequestException as exc:
            return self._format_response(False, error=str(exc), message="TTS API request error")

        output = data.get("output") or {}
        audio = output.get("audio") or {}
        audio_url = audio.get("url") or ""
        audio_data = audio.get("data") or ""

        result: Dict[str, Any] = {
            "request_id": data.get("request_id"),
            "status_code": data.get("status_code", response.status_code),
            "model": selected_model,
            "voice": selected_voice,
            "language_type": selected_language,
            "text_length": len(clean_text),
            "finish_reason": output.get("finish_reason"),
            "audio_url": audio_url,
            "audio_id": audio.get("id"),
            "expires_at": audio.get("expires_at"),
            "usage": data.get("usage", {}),
        }

        if not audio_url and not audio_data:
            return self._format_response(
                False,
                **result,
                error=data.get("message") or "No audio URL or audio data returned",
                message="TTS API returned no audio",
            )

        if should_download:
            target = self._build_output_path(clean_text, output_path, config["default_output_dir"])
            try:
                if audio_url:
                    file_size = self._download_file(audio_url, target, request_timeout)
                else:
                    file_size = self._write_base64_audio(audio_data, target)
            except requests.RequestException as exc:
                return self._format_response(
                    False,
                    **result,
                    error=str(exc),
                    message="Audio was synthesized, but download failed",
                )
            except Exception as exc:
                return self._format_response(
                    False,
                    **result,
                    error=str(exc),
                    message="Audio was synthesized, but local save failed",
                )

            result.update(
                {
                    "output_path": str(target),
                    "absolute_path": str(target.resolve()),
                    "file_size": file_size,
                    "message": f"Audio synthesized and saved: {target}",
                }
            )
        else:
            result["message"] = "Audio synthesized. Download was disabled; use audio_url before it expires."

        return self._format_response(True, **result)


class TTSToolManager:
    """Xenon auto-discovery entry point for TTS tools."""

    def __init__(self):
        self.handler = TTSHandler()

    def synthesize_speech(
        self,
        text: str,
        output_path: str = "",
        voice: str = "",
        language_type: str = "",
        model: str = "",
        instructions: str = "",
        optimize_instructions: bool = False,
        download_audio: Optional[bool] = None,
        config_path: str = "",
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Synthesize text into speech using DashScope Qwen-TTS.

        :param text: Text to synthesize.
        :param output_path: Optional local wav file path. If omitted, a file is created under the configured output directory.
        :param voice: Qwen-TTS voice, for example Cherry.
        :param language_type: Qwen-TTS language_type, for example Chinese, English, or Auto.
        :param model: Qwen TTS model. Defaults to config model.
        :param instructions: Optional voice-control prompt for instruct-capable Qwen3 TTS models.
        :param optimize_instructions: Whether to ask the service to optimize instructions.
        :param download_audio: Whether to download the returned audio URL. Defaults to config download_audio.
        :param config_path: Optional config JSON path.
        :param timeout: Request timeout in seconds.
        """
        return self.handler.synthesize_speech(
            text=text,
            output_path=output_path,
            voice=voice,
            language_type=language_type,
            model=model,
            instructions=instructions,
            optimize_instructions=optimize_instructions,
            download_audio=download_audio,
            config_path=config_path,
            timeout=timeout,
        )

    def get_config_status(self, config_path: str = "") -> Dict[str, Any]:
        """Return TTS configuration status with the API key redacted.

        :param config_path: Optional config JSON path.
        """
        return self.handler.get_config_status(config_path=config_path)

    def save_config(
        self,
        api_key: str,
        api_base: str = DEFAULT_API_BASE,
        model: str = "qwen3-tts-flash",
        default_voice: str = "Cherry",
        default_language_type: str = "Chinese",
        default_output_dir: str = str(DEFAULT_OUTPUT_DIR),
        download_audio: bool = True,
        timeout: int = 120,
        config_path: str = "",
    ) -> Dict[str, Any]:
        """Save a DashScope TTS config JSON file.

        :param api_key: DashScope or Bailian API key.
        :param api_base: API base URL.
        :param model: Qwen TTS model, for example qwen3-tts-flash.
        :param default_voice: Default voice parameter, for example Cherry.
        :param default_language_type: Default language_type, for example Chinese or Auto.
        :param default_output_dir: Directory for downloaded audio files.
        :param download_audio: Whether synthesize_speech downloads audio by default.
        :param timeout: Request timeout in seconds.
        :param config_path: Optional config path.
        """
        return self.handler.save_config(
            api_key=api_key,
            api_base=api_base,
            model=model,
            default_voice=default_voice,
            default_language_type=default_language_type,
            default_output_dir=default_output_dir,
            download_audio=download_audio,
            timeout=timeout,
            config_path=config_path,
        )

    def list_supported_voices(self) -> Dict[str, Any]:
        """List common Qwen-TTS system voices and supported language_type values."""
        return self.handler.list_supported_voices()


def main() -> None:
    if len(os.sys.argv) < 2:
        print(json.dumps({"success": False, "error": "usage: python tts_handler.py <action> [json_args]"}, ensure_ascii=False))
        raise SystemExit(1)

    action = os.sys.argv[1]
    args: Dict[str, Any] = {}
    if len(os.sys.argv) > 2:
        args = json.loads(os.sys.argv[2])
    elif not os.sys.stdin.isatty():
        raw = os.sys.stdin.read().strip()
        if raw:
            args = json.loads(raw)

    manager = TTSToolManager()
    if action == "synthesize":
        result = manager.synthesize_speech(**args)
    elif action == "status":
        result = manager.get_config_status(**args)
    elif action == "save_config":
        result = manager.save_config(**args)
    elif action == "voices":
        result = manager.list_supported_voices()
    else:
        result = {"success": False, "error": f"unknown action: {action}"}

    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
