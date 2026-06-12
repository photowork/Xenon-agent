#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Localhost JSON bridge for controlling the active FreeCAD GUI document."""

from __future__ import annotations

import importlib.util
import json
import queue
import socket
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore


class BridgeRequest:
    def __init__(self, payload):
        self.payload = payload
        self.response = None
        self.done = threading.Event()


class XenonLiveBridge:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self.config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.log_path = self.config_path.with_suffix(".log")
        self.host = "127.0.0.1"
        self.port = int(self.config["port"])
        self.token = str(self.config["token"])
        self.requests = queue.Queue()
        self.stopping = threading.Event()
        self.worker = self._load_worker()
        self.server_thread = threading.Thread(target=self._serve, name="XenonFreeCADBridge", daemon=True)
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self._process_requests)
        self._log("Bridge object initialized")

    def _log(self, message):
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")

    def _load_worker(self):
        worker_path = Path(self.config["worker_path"]).resolve()
        spec = importlib.util.spec_from_file_location("xenon_freecad_worker", worker_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def start(self):
        source_path = str(self.config.get("source_path") or "").strip()
        if source_path:
            App.openDocument(source_path)
        elif App.ActiveDocument is None:
            App.newDocument("XenonLive")
        self.server_thread.start()
        self.timer.start(50)
        Gui.getMainWindow().statusBar().showMessage(f"Xenon live bridge listening on 127.0.0.1:{self.port}")
        self._log(f"Bridge start requested on {self.host}:{self.port}")

    def _serve(self):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind((self.host, self.port))
                server.listen(8)
                server.settimeout(0.5)
                self._log(f"Socket listening on {self.host}:{self.port}")
                while not self.stopping.is_set():
                    try:
                        connection, _ = server.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        file = connection.makefile("rwb")
                        try:
                            payload = json.loads(file.readline().decode("utf-8"))
                            if payload.get("token") != self.token:
                                response = {"success": False, "error": "Invalid live bridge token"}
                            else:
                                request = BridgeRequest(payload)
                                self.requests.put(request)
                                if not request.done.wait(float(payload.get("bridge_timeout", 300))):
                                    response = {"success": False, "error": "Live bridge request timed out"}
                                else:
                                    response = request.response
                        except Exception as exc:
                            response = {"success": False, "error": str(exc), "traceback": traceback.format_exc()}
                        file.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
                        file.flush()
        except Exception:
            self._log("Socket server failed:\n" + traceback.format_exc())

    def _process_requests(self):
        for _ in range(10):
            try:
                request = self.requests.get_nowait()
            except queue.Empty:
                return
            try:
                command = str(request.payload.get("command") or "scenario").lower()
                if command == "stop_bridge":
                    self.stopping.set()
                    self.timer.stop()
                    request.response = {"success": True, "message": "FreeCAD live bridge stopped; GUI remains open"}
                else:
                    request.response = self.worker.execute_active_request(request.payload)
            except Exception as exc:
                request.response = {"success": False, "error": str(exc), "traceback": traceback.format_exc()}
            finally:
                request.done.set()


def start_bridge(config_path):
    global XENON_LIVE_BRIDGE
    existing = globals().get("XENON_LIVE_BRIDGE")
    if existing is not None and not existing.stopping.is_set():
        return existing
    XENON_LIVE_BRIDGE = XenonLiveBridge(config_path)
    XENON_LIVE_BRIDGE.start()
    return XENON_LIVE_BRIDGE


def _config_from_arguments():
    args = [Path(str(item).strip().strip('"')) for item in sys.argv[1:] if item != "--pass"]
    configs = [item for item in args if item.suffix.lower() == ".json" and item.is_file()]
    return configs[-1] if configs else None


def main():
    config = _config_from_arguments()
    if config is None:
        raise RuntimeError("Usage: freecad_live_bridge.py --pass <bridge-config.json>")
    start_bridge(config)


_automatic_config = _config_from_arguments()
if _automatic_config is not None:
    try:
        start_bridge(_automatic_config)
    except Exception:
        Path(__file__).with_name("freecad_live_bridge_startup.log").write_text(
            traceback.format_exc(),
            encoding="utf-8",
        )
        raise
