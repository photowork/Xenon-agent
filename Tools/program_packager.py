#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Program packaging helper for Xenon.

This tool keeps packaging pragmatic: it detects common project types, builds a
portable plan, and only executes commands that are possible on the current host.
For unsupported cross-builds it returns clear CI/runner guidance instead of
pretending one machine can produce every native binary.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_EXCLUDES = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "dist",
    "build",
    "target",
    ".idea",
    ".vscode",
}

TARGET_ALIASES = {
    "win": "windows-x64",
    "windows": "windows-x64",
    "windows-x64": "windows-x64",
    "linux": "linux-x64",
    "linux-x64": "linux-x64",
    "mac": "macos-x64",
    "macos": "macos-x64",
    "darwin": "macos-x64",
    "macos-x64": "macos-x64",
    "macos-arm64": "macos-arm64",
}

GO_TARGETS = {
    "windows-x64": ("windows", "amd64", ".exe"),
    "linux-x64": ("linux", "amd64", ""),
    "macos-x64": ("darwin", "amd64", ""),
    "macos-arm64": ("darwin", "arm64", ""),
}

DOTNET_RIDS = {
    "windows-x64": "win-x64",
    "linux-x64": "linux-x64",
    "macos-x64": "osx-x64",
    "macos-arm64": "osx-arm64",
}

RUST_TARGETS = {
    "windows-x64": "x86_64-pc-windows-msvc",
    "linux-x64": "x86_64-unknown-linux-gnu",
    "macos-x64": "x86_64-apple-darwin",
    "macos-arm64": "aarch64-apple-darwin",
}


def _now_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _host_target() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "windows-x64"
    if system == "linux":
        return "linux-x64"
    if system == "darwin":
        return "macos-arm64" if machine in {"arm64", "aarch64"} else "macos-x64"
    return system or "unknown"


def _as_list(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _normalize_targets(targets: Any) -> List[str]:
    values = _as_list(targets)
    if not values:
        return [_host_target()]
    if any(value.lower() == "all" for value in values):
        return ["windows-x64", "linux-x64", "macos-x64", "macos-arm64"]

    normalized: List[str] = []
    for value in values:
        target = TARGET_ALIASES.get(value.lower(), value.lower())
        if target not in normalized:
            normalized.append(target)
    return normalized


def _command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def _python_module_exists(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def _resolve(path: str, default: Optional[Path] = None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    if default is not None:
        return default.resolve()
    return Path.cwd().resolve()


class ProgramPackager:
    def __init__(self) -> None:
        self.host_target = _host_target()

    def inspect_project(self, project_path: str) -> Dict[str, Any]:
        root = _resolve(project_path)
        if not root.exists():
            return {"success": False, "error": f"Project path not found: {root}"}
        if root.is_file():
            root = root.parent

        files = {path.name for path in root.iterdir() if path.is_file()}
        project_types: List[str] = []
        entry_candidates: List[str] = []
        metadata: Dict[str, Any] = {}

        if "package.json" in files:
            project_types.append("node")
            package_data = self._read_package_json(root / "package.json")
            metadata["package_json"] = {
                "name": package_data.get("name"),
                "scripts": sorted((package_data.get("scripts") or {}).keys()),
                "has_electron_builder": self._has_node_dependency(package_data, "electron-builder"),
            }

        if {"pyproject.toml", "setup.py", "requirements.txt"} & files:
            project_types.append("python")
            entry_candidates.extend(self._python_entry_candidates(root))

        if "go.mod" in files:
            project_types.append("go")

        if "Cargo.toml" in files:
            project_types.append("rust")

        if any(path.suffix.lower() in {".csproj", ".fsproj", ".vbproj", ".sln"} for path in root.iterdir() if path.is_file()):
            project_types.append("dotnet")

        if "pom.xml" in files:
            project_types.append("maven")

        if "build.gradle" in files or "build.gradle.kts" in files:
            project_types.append("gradle")

        if "CMakeLists.txt" in files:
            project_types.append("cmake")

        if "Makefile" in files or "makefile" in files:
            project_types.append("make")

        if not project_types:
            project_types.append("generic")

        return {
            "success": True,
            "project_path": str(root),
            "project_types": project_types,
            "entry_candidates": entry_candidates,
            "host_target": self.host_target,
            "metadata": metadata,
        }

    def check_dependencies(self) -> Dict[str, Any]:
        commands = [
            "python",
            "pyinstaller",
            "node",
            "npm",
            "npx",
            "go",
            "cargo",
            "dotnet",
            "mvn",
            "gradle",
            "cmake",
            "make",
            "zip",
        ]
        return {
            "success": True,
            "host_target": self.host_target,
            "commands": {
                command: {"available": _command_exists(command), "path": shutil.which(command)}
                for command in commands
            },
        }

    def create_plan(
        self,
        project_path: str,
        output_dir: str = "",
        targets: Any = None,
        project_type: str = "auto",
        app_name: str = "",
        entry: str = "",
        archive: bool = True,
    ) -> Dict[str, Any]:
        inspection = self.inspect_project(project_path)
        if not inspection.get("success"):
            return inspection

        root = Path(inspection["project_path"])
        resolved_type = self._resolve_project_type(inspection["project_types"], project_type)
        package_name = app_name or self._default_app_name(root, inspection)
        out_root = _resolve(output_dir, root / "dist" / "packages")
        target_list = _normalize_targets(targets)

        target_plans = [
            self._target_plan(
                root=root,
                out_root=out_root,
                target=target,
                project_type=resolved_type,
                app_name=package_name,
                entry=entry,
                archive=archive,
                inspection=inspection,
            )
            for target in target_list
        ]

        return {
            "success": True,
            "dry_run": True,
            "project_path": str(root),
            "project_type": resolved_type,
            "detected_project_types": inspection["project_types"],
            "app_name": package_name,
            "output_dir": str(out_root),
            "host_target": self.host_target,
            "targets": target_plans,
        }

    def package_project(
        self,
        project_path: str,
        output_dir: str = "",
        targets: Any = None,
        project_type: str = "auto",
        app_name: str = "",
        entry: str = "",
        dry_run: bool = True,
        archive: bool = True,
        timeout: int = 3600,
    ) -> Dict[str, Any]:
        plan = self.create_plan(
            project_path=project_path,
            output_dir=output_dir,
            targets=targets,
            project_type=project_type,
            app_name=app_name,
            entry=entry,
            archive=archive,
        )
        if not plan.get("success") or dry_run:
            plan["dry_run"] = True
            return plan

        results: List[Dict[str, Any]] = []
        for target_plan in plan["targets"]:
            target_result = {
                "target": target_plan["target"],
                "status": target_plan["status"],
                "commands": [],
                "artifacts": list(target_plan.get("artifacts", [])),
                "messages": list(target_plan.get("messages", [])),
            }
            if target_plan["status"] != "ready":
                target_result["success"] = False
                results.append(target_result)
                continue

            for command_spec in target_plan.get("commands", []):
                command_result = self._run_command(command_spec, timeout=timeout)
                target_result["commands"].append(command_result)
                if not command_result["success"]:
                    target_result["success"] = False
                    break
            else:
                target_result["success"] = True

            results.append(target_result)

        plan["dry_run"] = False
        plan["results"] = results
        plan["success"] = all(item.get("success") for item in results)
        return plan

    def create_archive(
        self,
        source_path: str,
        output_path: str = "",
        format: str = "zip",
        include_patterns: Any = None,
        exclude_patterns: Any = None,
    ) -> Dict[str, Any]:
        source = _resolve(source_path)
        if not source.exists():
            return {"success": False, "error": f"Source path not found: {source}"}

        archive_format = format.lower().lstrip(".")
        if archive_format not in {"zip", "tar", "tar.gz", "tgz"}:
            return {"success": False, "error": "format must be zip, tar, tar.gz, or tgz"}

        if output_path:
            output = _resolve(output_path)
        else:
            suffix = ".tar.gz" if archive_format in {"tar.gz", "tgz"} else f".{archive_format}"
            output = source.parent / f"{source.name}-{_now_id()}{suffix}"
        output.parent.mkdir(parents=True, exist_ok=True)

        includes = _as_list(include_patterns) or ["*"]
        excludes = set(_as_list(exclude_patterns)) | DEFAULT_EXCLUDES
        files = list(self._iter_archive_files(source, includes, excludes))

        if archive_format == "zip":
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive_file:
                for file_path, arcname in files:
                    archive_file.write(file_path, arcname)
        else:
            mode = "w:gz" if archive_format in {"tar.gz", "tgz"} else "w"
            with tarfile.open(output, mode) as archive_file:
                for file_path, arcname in files:
                    archive_file.add(file_path, arcname)

        return {
            "success": True,
            "archive": str(output),
            "format": archive_format,
            "file_count": len(files),
            "size_bytes": output.stat().st_size,
        }

    def _target_plan(
        self,
        root: Path,
        out_root: Path,
        target: str,
        project_type: str,
        app_name: str,
        entry: str,
        archive: bool,
        inspection: Dict[str, Any],
    ) -> Dict[str, Any]:
        target_dir = out_root / target
        messages: List[str] = []
        commands: List[Dict[str, Any]] = []
        artifacts: List[str] = []
        status = "ready"

        if project_type == "python":
            entry_path = self._resolve_python_entry(root, entry, inspection)
            if not entry_path:
                status = "blocked"
                messages.append("Python packaging needs an entry file. Pass entry='app.py' or add a common entry point.")
            elif target != self.host_target:
                status = "blocked"
                messages.append("PyInstaller builds native binaries for the current OS only. Use a runner for this target.")
            elif not _command_exists("python"):
                status = "blocked"
                messages.append("Python is not available.")
            elif not (_command_exists("pyinstaller") or _python_module_exists("PyInstaller")):
                status = "blocked"
                messages.append("PyInstaller is not available. Install it in the active Python environment.")
            else:
                command = ["python", "-m", "PyInstaller", "--noconfirm", "--onefile", "--name", app_name]
                command.extend(["--distpath", str(target_dir), "--workpath", str(out_root / "_build" / target)])
                command.append(str(entry_path))
                commands.append({"command": command, "cwd": str(root), "env": {}})
                exe_suffix = ".exe" if target.startswith("windows") else ""
                artifacts.append(str(target_dir / f"{app_name}{exe_suffix}"))

        elif project_type == "go":
            go_target = GO_TARGETS.get(target)
            if not go_target:
                status = "blocked"
                messages.append(f"Unsupported Go target: {target}")
            elif not _command_exists("go"):
                status = "blocked"
                messages.append("Go toolchain is not available.")
            else:
                goos, goarch, suffix = go_target
                artifact = target_dir / f"{app_name}{suffix}"
                commands.append(
                    {
                        "command": ["go", "build", "-trimpath", "-ldflags", "-s -w", "-o", str(artifact), entry or "."],
                        "cwd": str(root),
                        "env": {"GOOS": goos, "GOARCH": goarch, "CGO_ENABLED": "0"},
                    }
                )
                artifacts.append(str(artifact))

        elif project_type == "dotnet":
            rid = DOTNET_RIDS.get(target)
            if not rid:
                status = "blocked"
                messages.append(f"Unsupported .NET target: {target}")
            elif not _command_exists("dotnet"):
                status = "blocked"
                messages.append(".NET SDK is not available.")
            else:
                commands.append(
                    {
                        "command": ["dotnet", "publish", "-c", "Release", "-r", rid, "--self-contained", "false", "-o", str(target_dir)],
                        "cwd": str(root),
                        "env": {},
                    }
                )
                artifacts.append(str(target_dir))

        elif project_type == "rust":
            triple = RUST_TARGETS.get(target)
            if not triple:
                status = "blocked"
                messages.append(f"Unsupported Rust target: {target}")
            elif not _command_exists("cargo"):
                status = "blocked"
                messages.append("Cargo is not available.")
            else:
                commands.append({"command": ["cargo", "build", "--release", "--target", triple], "cwd": str(root), "env": {}})
                artifacts.append(str(root / "target" / triple / "release"))
                if target != self.host_target:
                    messages.append("Rust cross-build requires the target toolchain and linker to be installed.")

        elif project_type == "node":
            package_data = self._read_package_json(root / "package.json")
            scripts = package_data.get("scripts") or {}
            has_builder = self._has_node_dependency(package_data, "electron-builder")
            if not _command_exists("npm"):
                status = "blocked"
                messages.append("npm is not available.")
            elif has_builder and _command_exists("npx"):
                flag = {"windows-x64": "--win", "linux-x64": "--linux", "macos-x64": "--mac", "macos-arm64": "--mac"}.get(target)
                if not flag:
                    status = "blocked"
                    messages.append(f"Unsupported electron-builder target: {target}")
                else:
                    commands.append({"command": ["npx", "electron-builder", flag, "--x64" if not target.endswith("arm64") else "--arm64"], "cwd": str(root), "env": {}})
                    artifacts.append(str(root / "dist"))
            elif "build" in scripts:
                commands.append({"command": ["npm", "run", "build"], "cwd": str(root), "env": {}})
                artifacts.append(str(root / "dist"))
                messages.append("Generic Node build is platform-neutral; archive or deploy the produced dist directory.")
            else:
                status = "blocked"
                messages.append("No supported npm build script or electron-builder dependency was found.")

        elif project_type in {"maven", "gradle", "cmake", "make"}:
            command = self._simple_build_command(project_type, root)
            if not command:
                status = "blocked"
                messages.append(f"{project_type} command is not available.")
            else:
                commands.append({"command": command, "cwd": str(root), "env": {}})
                artifacts.append(str(root / ("target" if project_type == "maven" else "build")))
                if target != self.host_target:
                    messages.append(f"{project_type} builds are usually host-native unless the project config adds cross-build support.")

        else:
            if archive:
                artifacts.append(str(target_dir / f"{app_name}-{target}.zip"))
                messages.append("Generic project: create an archive package of the source tree.")
            else:
                messages.append("Generic project: no build command detected.")

        if status == "ready" and archive and project_type == "generic":
            commands.append(
                {
                    "command": [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "archive",
                        json.dumps({"source_path": str(root), "output_path": str(target_dir / f"{app_name}-{target}.zip")}, ensure_ascii=False),
                    ],
                    "cwd": str(root),
                    "env": {},
                }
            )

        return {
            "target": target,
            "status": status,
            "commands": commands,
            "artifacts": artifacts,
            "messages": messages,
        }

    def _run_command(self, command_spec: Dict[str, Any], timeout: int) -> Dict[str, Any]:
        env = os.environ.copy()
        env.update(command_spec.get("env") or {})
        command = command_spec["command"]
        start = time.time()
        try:
            completed = subprocess.run(
                command,
                cwd=command_spec.get("cwd") or None,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            return {
                "success": completed.returncode == 0,
                "command": command,
                "exit_code": completed.returncode,
                "stdout": completed.stdout[-12000:],
                "stderr": completed.stderr[-12000:],
                "elapsed_seconds": round(time.time() - start, 2),
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "success": False,
                "command": command,
                "exit_code": -1,
                "stdout": (exc.stdout or "")[-12000:] if isinstance(exc.stdout, str) else "",
                "stderr": f"Command timed out after {timeout} seconds.",
                "elapsed_seconds": round(time.time() - start, 2),
            }

    def _iter_archive_files(self, source: Path, includes: List[str], excludes: Iterable[str]) -> Iterable[Tuple[Path, str]]:
        exclude_values = set(excludes)
        if source.is_file():
            yield source, source.name
            return

        for path in source.rglob("*"):
            if path.is_dir():
                continue
            relative = path.relative_to(source)
            parts = set(relative.parts)
            if parts & exclude_values:
                continue
            relative_text = relative.as_posix()
            if any(fnmatch.fnmatch(relative_text, pattern) for pattern in exclude_values):
                continue
            if not any(fnmatch.fnmatch(relative_text, pattern) for pattern in includes):
                continue
            yield path, relative_text

    def _resolve_project_type(self, detected: List[str], requested: str) -> str:
        if requested and requested != "auto":
            return requested.lower()
        priority = ["python", "go", "rust", "dotnet", "node", "maven", "gradle", "cmake", "make", "generic"]
        for item in priority:
            if item in detected:
                return item
        return detected[0] if detected else "generic"

    def _default_app_name(self, root: Path, inspection: Dict[str, Any]) -> str:
        package_json = inspection.get("metadata", {}).get("package_json", {})
        return package_json.get("name") or root.name

    def _read_package_json(self, path: Path) -> Dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _has_node_dependency(self, package_data: Dict[str, Any], dependency: str) -> bool:
        for key in ("dependencies", "devDependencies", "optionalDependencies"):
            if dependency in (package_data.get(key) or {}):
                return True
        return False

    def _python_entry_candidates(self, root: Path) -> List[str]:
        candidates = []
        for name in ("main.py", "app.py", "run.py", "__main__.py"):
            if (root / name).exists():
                candidates.append(name)
        return candidates

    def _resolve_python_entry(self, root: Path, entry: str, inspection: Dict[str, Any]) -> Optional[Path]:
        if entry:
            path = (root / entry).resolve()
            return path if path.exists() else None
        candidates = inspection.get("entry_candidates") or []
        if candidates:
            return (root / candidates[0]).resolve()
        return None

    def _simple_build_command(self, project_type: str, root: Path) -> List[str]:
        if project_type == "maven" and _command_exists("mvn"):
            return ["mvn", "-DskipTests", "package"]
        if project_type == "gradle":
            wrapper = root / ("gradlew.bat" if platform.system().lower() == "windows" else "gradlew")
            if wrapper.exists():
                return [str(wrapper), "build"]
            if _command_exists("gradle"):
                return ["gradle", "build"]
        if project_type == "cmake" and _command_exists("cmake"):
            return ["cmake", "--build", "build", "--config", "Release"]
        if project_type == "make" and _command_exists("make"):
            return ["make"]
        return []


class ProgramPackagerToolManager:
    def __init__(self) -> None:
        self.handler = ProgramPackager()

    def inspect_project(self, project_path: str = ".") -> Dict[str, Any]:
        """Detect project type, likely entry points, and packaging metadata.

        :param project_path: Project directory or file path to inspect.
        """
        return self.handler.inspect_project(project_path)

    def check_dependencies(self) -> Dict[str, Any]:
        """Check local packaging toolchains such as Python, npm, Go, Cargo, dotnet, Maven, Gradle, and CMake."""
        return self.handler.check_dependencies()

    def create_plan(
        self,
        project_path: str = ".",
        output_dir: str = "",
        targets: Any = None,
        project_type: str = "auto",
        app_name: str = "",
        entry: str = "",
        archive: bool = True,
    ) -> Dict[str, Any]:
        """Create a cross-platform packaging plan without executing build commands.

        :param project_path: Project directory.
        :param output_dir: Package output directory. Defaults to dist/packages under the project.
        :param targets: Target platform list or comma string. Use all, windows, linux, macos, macos-arm64.
        :param project_type: auto, python, node, go, rust, dotnet, maven, gradle, cmake, make, or generic.
        :param app_name: Output application name. Defaults to package name or directory name.
        :param entry: Entry file/path for Python or package path for Go.
        :param archive: Add archive packaging when supported.
        """
        return self.handler.create_plan(project_path, output_dir, targets, project_type, app_name, entry, archive)

    def package_project(
        self,
        project_path: str = ".",
        output_dir: str = "",
        targets: Any = None,
        project_type: str = "auto",
        app_name: str = "",
        entry: str = "",
        dry_run: bool = True,
        archive: bool = True,
        timeout: int = 3600,
    ) -> Dict[str, Any]:
        """Package a project, or return the dry-run plan by default.

        :param project_path: Project directory.
        :param output_dir: Package output directory.
        :param targets: Target platform list or comma string. Use all for common desktop targets.
        :param project_type: auto, python, node, go, rust, dotnet, maven, gradle, cmake, make, or generic.
        :param app_name: Output application name.
        :param entry: Entry file/path for Python or package path for Go.
        :param dry_run: When true, only return the plan and do not execute commands.
        :param archive: Add archive packaging when supported.
        :param timeout: Per-command timeout in seconds.
        """
        return self.handler.package_project(project_path, output_dir, targets, project_type, app_name, entry, dry_run, archive, timeout)

    def create_archive(
        self,
        source_path: str,
        output_path: str = "",
        format: str = "zip",
        include_patterns: Any = None,
        exclude_patterns: Any = None,
    ) -> Dict[str, Any]:
        """Create a zip/tar/tar.gz archive with common build and cache folders excluded.

        :param source_path: File or directory to archive.
        :param output_path: Archive output path. Defaults beside the source.
        :param format: zip, tar, tar.gz, or tgz.
        :param include_patterns: Optional glob pattern list.
        :param exclude_patterns: Optional glob pattern list or names to exclude.
        """
        return self.handler.create_archive(source_path, output_path, format, include_patterns, exclude_patterns)


def create_program_packager_tool_manager() -> ProgramPackagerToolManager:
    return ProgramPackagerToolManager()


def main() -> None:
    manager = ProgramPackagerToolManager()
    if len(sys.argv) >= 2 and sys.argv[1] == "archive":
        payload = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        result = manager.create_archive(**payload)
    else:
        if len(sys.argv) > 1:
            payload = json.loads(sys.argv[1])
        else:
            raw = sys.stdin.read().strip()
            payload = json.loads(raw) if raw else {}
        action = payload.pop("action", "create_plan")
        if not hasattr(manager, action):
            result = {"success": False, "error": f"Unknown action: {action}"}
        else:
            result = getattr(manager, action)(**payload)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
