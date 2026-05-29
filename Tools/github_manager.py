#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""GitHub repository management tools for Xenon agents.

The tool prefers the local git CLI for repository operations and uses the
GitHub REST API only for tasks that git cannot do, such as creating a repository
or opening a pull request. Authentication is read from environment variables by
default (`GITHUB_TOKEN` or `GH_TOKEN`) so secrets do not need to be passed in
tool arguments.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

try:
    import requests
except Exception:  # pragma: no cover - handled at runtime for lightweight loads
    requests = None  # type: ignore[assignment]


DEFAULT_REMOTE = "origin"
DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_HOST = "github.com"
TOKEN_ENV_CANDIDATES = ("GITHUB_TOKEN", "GH_TOKEN")


class GitResult:
    def __init__(self, command: List[str], returncode: int, stdout: str, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": _redact_command(self.command),
            "exit_code": self.returncode,
            "stdout": self.stdout.strip(),
            "stderr": self.stderr.strip(),
            "success": self.ok,
        }


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "gbk", sys.getdefaultencoding()):
        try:
            return data.decode(encoding)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def _redact_command(command: Sequence[str]) -> List[str]:
    redacted: List[str] = []
    for item in command:
        cleaned = re.sub(r"(https://)([^/@:\s]+):([^/@\s]+)@", r"\1***:***@", str(item))
        cleaned = re.sub(r"(token\s+)[A-Za-z0-9_\-\.]+", r"\1***", cleaned, flags=re.IGNORECASE)
        redacted.append(cleaned)
    return redacted


def _redact_url(url: str) -> str:
    return re.sub(r"(https://)([^/@:\s]+):([^/@\s]+)@", r"\1***:***@", url)


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


class GitHubService:
    def __init__(self, api_base: str = DEFAULT_API_BASE, token_env: Optional[str] = None) -> None:
        self.api_base = api_base.rstrip("/")
        self.token_env = token_env

    def get_token(self) -> Optional[str]:
        if self.token_env:
            return os.environ.get(self.token_env)
        for env_name in TOKEN_ENV_CANDIDATES:
            token = os.environ.get(env_name)
            if token:
                return token
        return None

    def headers(self) -> Dict[str, str]:
        token = self.get_token()
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Xenon-GitHub-Manager",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: int = 30,
    ) -> Dict[str, Any]:
        if requests is None:
            return {
                "success": False,
                "error": "requests is not installed; install project requirements first.",
            }
        if not self.get_token():
            return {
                "success": False,
                "error": "GitHub token not found. Set GITHUB_TOKEN or GH_TOKEN in the environment.",
            }

        url = f"{self.api_base}{path}"
        response = requests.request(
            method,
            url,
            headers=self.headers(),
            json=payload,
            timeout=timeout,
        )
        try:
            data: Any = response.json()
        except Exception:
            data = response.text

        result = {
            "success": 200 <= response.status_code < 300,
            "status_code": response.status_code,
            "url": url,
            "data": data,
        }
        if not result["success"]:
            result["error"] = self._extract_error(data, response.status_code)
        return result

    @staticmethod
    def _extract_error(data: Any, status_code: int) -> str:
        if isinstance(data, dict):
            message = data.get("message")
            if message:
                return f"GitHub API {status_code}: {message}"
        if isinstance(data, str) and data:
            return f"GitHub API {status_code}: {data[:300]}"
        return f"GitHub API request failed with status {status_code}"


class GitHubToolManager:
    """Manage GitHub repositories from an agent-safe tool facade."""

    def __init__(self) -> None:
        self.default_timeout = 120

    def status(
        self,
        repo_path: str = ".",
        remote: str = DEFAULT_REMOTE,
        fetch: bool = False,
    ) -> Dict[str, Any]:
        """Inspect local git and GitHub connection status for a repository.

        :param repo_path: Local repository path to inspect.
        :param remote: Remote name to inspect, normally origin.
        :param fetch: Whether to run git fetch before reporting ahead/behind.
        """
        repo = self._resolve_path(repo_path)
        if not repo.exists():
            return {"success": False, "error": f"Path does not exist: {repo}", "repo_path": str(repo)}

        git_root = self._git_root(repo)
        if not git_root:
            return {
                "success": True,
                "is_git_repo": False,
                "repo_path": str(repo),
                "message": "Path is not a git repository yet.",
            }

        if fetch:
            fetch_result = self._git(["fetch", "--prune", remote], git_root, check=False)
        else:
            fetch_result = None

        branch = self._git_output(["branch", "--show-current"], git_root)
        upstream = self._git_output(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], git_root)
        remote_url = self._git_output(["remote", "get-url", remote], git_root)
        porcelain = self._git_raw_output(["status", "--porcelain=v1"], git_root, default="")
        changes = self._parse_porcelain(porcelain)
        ahead, behind = self._ahead_behind(git_root, upstream)
        remote_info = self._parse_github_remote(remote_url)

        return {
            "success": True,
            "is_git_repo": True,
            "repo_path": str(git_root),
            "branch": branch,
            "remote": remote,
            "remote_url": _redact_url(remote_url) if remote_url else "",
            "github": remote_info,
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "dirty": bool(changes),
            "changes": changes,
            "fetch": fetch_result.to_dict() if fetch_result else None,
        }

    def initialize_or_connect(
        self,
        repo_path: str = ".",
        owner: str = "",
        repo: str = "",
        remote_url: str = "",
        remote: str = DEFAULT_REMOTE,
        branch: str = "main",
        private: bool = True,
        description: str = "",
        create_github_repo: bool = False,
        token_env: str = "",
        api_base: str = DEFAULT_API_BASE,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Initialize git, optionally create a GitHub repo, and configure remote.

        :param repo_path: Local project path.
        :param owner: GitHub owner or organization. Required when creating a repo.
        :param repo: GitHub repository name. Required when creating or deriving remote_url.
        :param remote_url: Existing GitHub clone URL. If empty, owner/repo is used.
        :param remote: Git remote name to add or update.
        :param branch: Default branch to create or rename to.
        :param private: Create GitHub repository as private when create_github_repo is true.
        :param description: Repository description used when creating the GitHub repo.
        :param create_github_repo: Whether to create the GitHub repository through the API.
        :param token_env: Optional token environment variable name, default checks GITHUB_TOKEN and GH_TOKEN.
        :param api_base: GitHub API base URL, useful for GitHub Enterprise.
        :param dry_run: Preview operations without modifying local git or GitHub.
        """
        repo_dir = self._resolve_path(repo_path)
        if not repo_dir.exists():
            if dry_run:
                return {
                    "success": True,
                    "dry_run": True,
                    "planned": [f"create directory {repo_dir}", "git init", "configure remote"],
                }
            repo_dir.mkdir(parents=True, exist_ok=True)

        planned: List[str] = []
        results: List[Dict[str, Any]] = []
        github_result: Optional[Dict[str, Any]] = None

        if not remote_url:
            if not owner or not repo:
                return {
                    "success": False,
                    "error": "Provide remote_url or both owner and repo.",
                }
            remote_url = f"https://{DEFAULT_HOST}/{owner}/{repo}.git"

        if create_github_repo:
            if not owner or not repo:
                return {
                    "success": False,
                    "error": "owner and repo are required to create a GitHub repository.",
                }
            planned.append(f"create GitHub repository {owner}/{repo}")
            if not dry_run:
                github_result = self._create_repository(
                    owner=owner,
                    repo=repo,
                    private=private,
                    description=description,
                    token_env=token_env or None,
                    api_base=api_base,
                )
                if not github_result.get("success") and github_result.get("status_code") == 422:
                    github_result = {
                        **github_result,
                        "success": True,
                        "already_exists": True,
                        "message": "GitHub repository already exists; continuing.",
                    }
                elif not github_result.get("success"):
                    return github_result

        git_root = self._git_root(repo_dir)
        if not git_root:
            planned.append("git init")
            if not dry_run:
                results.append(self._git(["init"], repo_dir).to_dict())
            git_root = repo_dir

        if branch:
            current_branch = self._git_output(["branch", "--show-current"], git_root, default="")
            if current_branch != branch:
                planned.append(f"set branch to {branch}")
                if not dry_run:
                    results.append(self._git(["branch", "-M", branch], git_root, check=False).to_dict())

        existing_remote = self._git_output(["remote", "get-url", remote], git_root, default="")
        if existing_remote:
            if existing_remote != remote_url:
                planned.append(f"update remote {remote}")
                if not dry_run:
                    results.append(self._git(["remote", "set-url", remote, remote_url], git_root).to_dict())
        else:
            planned.append(f"add remote {remote}")
            if not dry_run:
                results.append(self._git(["remote", "add", remote, remote_url], git_root).to_dict())

        return {
            "success": all(item.get("success") for item in results) if results else True,
            "dry_run": dry_run,
            "repo_path": str(git_root),
            "remote": remote,
            "remote_url": _redact_url(remote_url),
            "branch": branch,
            "planned": planned,
            "github": github_result,
            "git_results": results,
            "message": "Repository initialized/connected.",
        }

    def upload_code(
        self,
        repo_path: str = ".",
        message: str = "chore: update repository",
        remote: str = DEFAULT_REMOTE,
        branch: str = "",
        include: Optional[List[str]] = None,
        all_changes: bool = True,
        pull_first: bool = True,
        rebase: bool = True,
        set_upstream: bool = True,
        allow_empty: bool = False,
        run_checks: bool = False,
        checks_command: str = "",
        push: bool = True,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Stage, commit, and push code to GitHub.

        :param repo_path: Local repository path.
        :param message: Commit message.
        :param remote: Git remote name.
        :param branch: Branch to push. Empty means current branch.
        :param include: Optional pathspecs to stage when all_changes is false.
        :param all_changes: Stage all tracked/untracked changes with git add -A.
        :param pull_first: Pull remote changes before pushing.
        :param rebase: Use pull --rebase when pull_first is true.
        :param set_upstream: Push with -u when no upstream is configured.
        :param allow_empty: Allow an empty commit.
        :param run_checks: Run checks_command before committing.
        :param checks_command: Command to run before commit, for example python -m pytest.
        :param push: Push the commit to the remote after committing.
        :param dry_run: Preview without staging, committing, or pushing.
        """
        repo = self._require_git_repo(repo_path)
        if not repo["success"]:
            return repo
        git_root = Path(repo["repo_path"])
        branch = branch or self._git_output(["branch", "--show-current"], git_root, default="")
        if not branch:
            return {"success": False, "error": "Could not determine current branch.", "repo_path": str(git_root)}

        planned: List[str] = []
        results: List[Dict[str, Any]] = []

        if run_checks:
            if not checks_command:
                return {"success": False, "error": "checks_command is required when run_checks is true."}
            planned.append(f"run checks: {checks_command}")
            if not dry_run:
                check_result = self._run_shell(checks_command, git_root, timeout=600)
                results.append(check_result)
                if not check_result.get("success"):
                    return {
                        "success": False,
                        "stage": "checks",
                        "repo_path": str(git_root),
                        "planned": planned,
                        "results": results,
                    }

        if pull_first and push:
            pull_command = ["pull", "--rebase", remote, branch] if rebase else ["pull", remote, branch]
            planned.append(" ".join(["git", *pull_command]))
            if not dry_run:
                pull_result = self._git(pull_command, git_root, check=False).to_dict()
                results.append(pull_result)
                if not pull_result.get("success"):
                    return {
                        "success": False,
                        "stage": "pull",
                        "repo_path": str(git_root),
                        "planned": planned,
                        "results": results,
                        "hint": "Resolve conflicts or disable pull_first if this is the first push.",
                    }

        stage_args = ["add", "-A"] if all_changes else ["add", "--", *(include or [])]
        if not all_changes and not include:
            return {"success": False, "error": "include must be provided when all_changes is false."}

        planned.append(" ".join(["git", *stage_args]))
        if not dry_run:
            results.append(self._git(stage_args, git_root).to_dict())

        if dry_run:
            staged = self._git_raw_output(["status", "--porcelain=v1"], git_root, default="")
        else:
            staged = self._git_output(["diff", "--cached", "--name-status"], git_root, default="")
        if not staged and not allow_empty:
            return {
                "success": True,
                "stage": "no_changes",
                "repo_path": str(git_root),
                "branch": branch,
                "planned": planned,
                "results": results,
                "message": "No staged changes to commit.",
            }

        commit_args = ["commit", "-m", message]
        if allow_empty:
            commit_args.insert(1, "--allow-empty")
        planned.append(" ".join(["git", *commit_args]))
        if not dry_run:
            commit_result = self._git(commit_args, git_root, check=False).to_dict()
            results.append(commit_result)
            if not commit_result.get("success"):
                return {
                    "success": False,
                    "stage": "commit",
                    "repo_path": str(git_root),
                    "planned": planned,
                    "results": results,
                }

        if push:
            upstream = self._git_output(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], git_root, default="")
            push_args = ["push", remote, branch]
            if set_upstream and not upstream:
                push_args = ["push", "-u", remote, branch]
            planned.append(" ".join(["git", *push_args]))
            if not dry_run:
                push_result = self._git(push_args, git_root, check=False).to_dict()
                results.append(push_result)
                if not push_result.get("success"):
                    return {
                        "success": False,
                        "stage": "push",
                        "repo_path": str(git_root),
                        "planned": planned,
                        "results": results,
                    }

        return {
            "success": True,
            "dry_run": dry_run,
            "repo_path": str(git_root),
            "branch": branch,
            "remote": remote,
            "pushed": push and not dry_run,
            "staged_changes": staged.splitlines() if staged else [],
            "planned": planned,
            "results": results,
            "message": (
                "Code uploaded to GitHub."
                if push and not dry_run
                else "Dry run complete." if dry_run else "Commit workflow complete."
            ),
        }

    def sync_repository(
        self,
        repo_path: str = ".",
        remote: str = DEFAULT_REMOTE,
        branch: str = "",
        rebase: bool = True,
        prune: bool = True,
        push: bool = False,
    ) -> Dict[str, Any]:
        """Fetch and pull a repository, optionally pushing local commits.

        :param repo_path: Local repository path.
        :param remote: Git remote name.
        :param branch: Branch to sync. Empty means current branch.
        :param rebase: Use pull --rebase instead of merge.
        :param prune: Prune deleted remote refs during fetch.
        :param push: Push after pulling.
        """
        repo = self._require_git_repo(repo_path)
        if not repo["success"]:
            return repo
        git_root = Path(repo["repo_path"])
        branch = branch or self._git_output(["branch", "--show-current"], git_root, default="")

        results: List[Dict[str, Any]] = []
        fetch_args = ["fetch", remote]
        if prune:
            fetch_args.append("--prune")
        results.append(self._git(fetch_args, git_root, check=False).to_dict())
        if not results[-1]["success"]:
            return {"success": False, "stage": "fetch", "repo_path": str(git_root), "results": results}

        pull_args = ["pull", "--rebase", remote, branch] if rebase else ["pull", remote, branch]
        results.append(self._git(pull_args, git_root, check=False).to_dict())
        if not results[-1]["success"]:
            return {"success": False, "stage": "pull", "repo_path": str(git_root), "results": results}

        if push:
            results.append(self._git(["push", remote, branch], git_root, check=False).to_dict())
            if not results[-1]["success"]:
                return {"success": False, "stage": "push", "repo_path": str(git_root), "results": results}

        return {
            "success": True,
            "repo_path": str(git_root),
            "branch": branch,
            "remote": remote,
            "results": results,
            "status": self.status(str(git_root), remote=remote, fetch=False),
        }

    def maintain_repository(
        self,
        repo_path: str = ".",
        message: str = "chore: automated repository maintenance",
        remote: str = DEFAULT_REMOTE,
        branch: str = "",
        run_checks: bool = False,
        checks_command: str = "",
        update_gitignore: bool = True,
        push: bool = True,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Run a conservative maintenance pass and optionally upload changes.

        :param repo_path: Local repository path.
        :param message: Commit message for maintenance changes.
        :param remote: Git remote name.
        :param branch: Branch to push. Empty means current branch.
        :param run_checks: Run checks_command before committing.
        :param checks_command: Test or lint command to run before commit.
        :param update_gitignore: Ensure common local/cache paths are ignored.
        :param push: Push committed changes to the remote.
        :param dry_run: Preview without editing, committing, or pushing.
        """
        repo = self._require_git_repo(repo_path)
        if not repo["success"]:
            return repo
        git_root = Path(repo["repo_path"])
        maintenance: List[Dict[str, Any]] = []

        if update_gitignore:
            maintenance.append(self._ensure_gitignore(git_root, dry_run=dry_run))

        upload = self.upload_code(
            repo_path=str(git_root),
            message=message,
            remote=remote,
            branch=branch,
            all_changes=True,
            pull_first=push,
            rebase=True,
            set_upstream=True,
            allow_empty=False,
            run_checks=run_checks,
            checks_command=checks_command,
            push=push,
            dry_run=dry_run,
        )

        return {
            "success": bool(upload.get("success")) and all(item.get("success") for item in maintenance),
            "repo_path": str(git_root),
            "dry_run": dry_run,
            "maintenance": maintenance,
            "upload": upload,
            "message": "Maintenance pass complete.",
        }

    def create_pull_request(
        self,
        repo_path: str = ".",
        title: str = "",
        body: str = "",
        head: str = "",
        base: str = "main",
        draft: bool = False,
        remote: str = DEFAULT_REMOTE,
        token_env: str = "",
        api_base: str = DEFAULT_API_BASE,
    ) -> Dict[str, Any]:
        """Create a GitHub pull request for the current repository.

        :param repo_path: Local repository path.
        :param title: Pull request title.
        :param body: Pull request body.
        :param head: Head branch, empty means current branch.
        :param base: Base branch.
        :param draft: Whether to create a draft pull request.
        :param remote: Remote used to discover owner/repo.
        :param token_env: Optional token environment variable name.
        :param api_base: GitHub API base URL.
        """
        repo = self._require_git_repo(repo_path)
        if not repo["success"]:
            return repo
        git_root = Path(repo["repo_path"])
        remote_url = self._git_output(["remote", "get-url", remote], git_root, default="")
        remote_info = self._parse_github_remote(remote_url)
        if not remote_info.get("success"):
            return remote_info

        head_branch = head or self._git_output(["branch", "--show-current"], git_root, default="")
        if not title:
            title = f"Update {head_branch}"
        payload = {
            "title": title,
            "body": body,
            "head": head_branch,
            "base": base,
            "draft": draft,
        }

        service = GitHubService(api_base=api_base, token_env=token_env or None)
        result = service.request(
            "POST",
            f"/repos/{remote_info['owner']}/{remote_info['repo']}/pulls",
            payload=payload,
        )
        if result.get("success") and isinstance(result.get("data"), dict):
            data = result["data"]
            result["pull_request"] = {
                "number": data.get("number"),
                "url": data.get("html_url"),
                "state": data.get("state"),
                "draft": data.get("draft"),
            }
        return result

    def repository_health(self, repo_path: str = ".", remote: str = DEFAULT_REMOTE) -> Dict[str, Any]:
        """Collect a compact repository health report for autonomous maintenance.

        :param repo_path: Local repository path.
        :param remote: Git remote name.
        """
        repo = self._require_git_repo(repo_path)
        if not repo["success"]:
            return repo
        git_root = Path(repo["repo_path"])
        status = self.status(str(git_root), remote=remote, fetch=False)
        tracked_files = self._git_output(["ls-files"], git_root, default="").splitlines()
        large_files = []
        for rel_path in tracked_files:
            path = git_root / rel_path
            if path.is_file():
                size = path.stat().st_size
                if size >= 5 * 1024 * 1024:
                    large_files.append({"path": rel_path, "size_bytes": size})

        gitignore_path = git_root / ".gitignore"
        has_gitignore = gitignore_path.exists()
        remote_url = self._git_output(["remote", "get-url", remote], git_root, default="")

        recommendations: List[str] = []
        if not remote_url:
            recommendations.append("Configure a GitHub remote before uploading code.")
        if status.get("dirty"):
            recommendations.append("Commit or discard local changes before long-running maintenance.")
        if status.get("behind", 0) > 0:
            recommendations.append("Pull or rebase remote changes.")
        if large_files:
            recommendations.append("Review large tracked files; use Git LFS for assets when appropriate.")
        if not has_gitignore:
            recommendations.append("Add a .gitignore for local caches and secrets.")

        return {
            "success": True,
            "repo_path": str(git_root),
            "status": status,
            "tracked_file_count": len(tracked_files),
            "large_files": large_files,
            "has_gitignore": has_gitignore,
            "recommendations": recommendations,
        }

    def _resolve_path(self, repo_path: str) -> Path:
        return Path(repo_path or ".").expanduser().resolve()

    def _require_git_repo(self, repo_path: str) -> Dict[str, Any]:
        repo = self._resolve_path(repo_path)
        if not repo.exists():
            return {"success": False, "error": f"Path does not exist: {repo}", "repo_path": str(repo)}
        git_root = self._git_root(repo)
        if not git_root:
            return {"success": False, "error": "Path is not a git repository.", "repo_path": str(repo)}
        return {"success": True, "repo_path": str(git_root)}

    def _git_root(self, repo_path: Path) -> Optional[Path]:
        result = self._git(["rev-parse", "--show-toplevel"], repo_path, check=False)
        if result.ok and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
        return None

    def _git_output(self, args: Sequence[str], cwd: Path, default: str = "") -> str:
        result = self._git(args, cwd, check=False)
        if not result.ok:
            return default
        return result.stdout.strip()

    def _git_raw_output(self, args: Sequence[str], cwd: Path, default: str = "") -> str:
        result = self._git(args, cwd, check=False)
        if not result.ok:
            return default
        return result.stdout.rstrip("\r\n")

    def _git(self, args: Sequence[str], cwd: Path, check: bool = True, timeout: Optional[int] = None) -> GitResult:
        command = ["git", *list(args)]
        try:
            proc = subprocess.run(
                command,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout or self.default_timeout,
                shell=False,
            )
            result = GitResult(command, proc.returncode, _decode(proc.stdout), _decode(proc.stderr))
        except FileNotFoundError as exc:
            missing = exc.filename or "git"
            message = "git executable not found" if missing == "git" else f"path not found: {missing}"
            result = GitResult(command, 127, "", message)
        except subprocess.TimeoutExpired as exc:
            stdout = _decode(exc.stdout or b"") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
            stderr = _decode(exc.stderr or b"") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            result = GitResult(command, 124, stdout, stderr or "git command timed out")
        if check and not result.ok:
            raise RuntimeError(f"Git command failed: {_redact_command(command)}\n{result.stderr}")
        return result

    def _run_shell(self, command: str, cwd: Path, timeout: int = 600) -> Dict[str, Any]:
        try:
            proc = subprocess.run(
                command,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                shell=True,
            )
            return {
                "success": proc.returncode == 0,
                "command": command,
                "exit_code": proc.returncode,
                "stdout": _decode(proc.stdout).strip(),
                "stderr": _decode(proc.stderr).strip(),
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "command": command,
                "exit_code": 124,
                "error": "command timed out",
            }

    def _parse_porcelain(self, porcelain: str) -> List[Dict[str, str]]:
        changes: List[Dict[str, str]] = []
        for line in porcelain.splitlines():
            if not line:
                continue
            status = line[:2]
            path = line[3:] if len(line) > 3 else ""
            changes.append({"status": status.strip() or "modified", "path": path})
        return changes

    def _ahead_behind(self, git_root: Path, upstream: str) -> Tuple[int, int]:
        if not upstream:
            return 0, 0
        output = self._git_output(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], git_root)
        parts = output.split()
        if len(parts) != 2:
            return 0, 0
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return 0, 0

    def _parse_github_remote(self, remote_url: str) -> Dict[str, Any]:
        if not remote_url:
            return {"success": False, "error": "No remote URL configured."}

        owner = ""
        repo = ""
        host = DEFAULT_HOST

        if remote_url.startswith("git@"):
            match = re.match(r"git@([^:]+):([^/]+)/(.+?)(?:\.git)?$", remote_url)
            if match:
                host, owner, repo = match.groups()
        else:
            parsed = urlparse(remote_url)
            if parsed.netloc:
                host = parsed.netloc.split("@")[-1]
                parts = parsed.path.strip("/").split("/")
                if len(parts) >= 2:
                    owner = parts[-2]
                    repo = parts[-1]

        repo = repo[:-4] if repo.endswith(".git") else repo
        if not owner or not repo:
            return {
                "success": False,
                "error": "Remote URL is not a recognizable GitHub URL.",
                "remote_url": _redact_url(remote_url),
            }
        return {
            "success": True,
            "host": host,
            "owner": owner,
            "repo": repo,
            "full_name": f"{owner}/{repo}",
        }

    def _create_repository(
        self,
        owner: str,
        repo: str,
        private: bool,
        description: str,
        token_env: Optional[str],
        api_base: str,
    ) -> Dict[str, Any]:
        service = GitHubService(api_base=api_base, token_env=token_env)
        payload = {
            "name": repo,
            "private": private,
            "description": description,
            "auto_init": False,
        }

        user_result = service.request("GET", "/user")
        if not user_result.get("success"):
            return user_result
        login = ""
        if isinstance(user_result.get("data"), dict):
            login = str(user_result["data"].get("login") or "")

        if owner and login and owner.lower() != login.lower():
            return service.request("POST", f"/orgs/{owner}/repos", payload)
        return service.request("POST", "/user/repos", payload)

    def _ensure_gitignore(self, git_root: Path, dry_run: bool = False) -> Dict[str, Any]:
        gitignore_path = git_root / ".gitignore"
        desired = [
            "__pycache__/",
            "*.py[cod]",
            ".pytest_cache/",
            ".mypy_cache/",
            ".ruff_cache/",
            ".env",
            ".env.*",
            "!.env.example",
            "node_modules/",
            "dist/",
            "build/",
        ]
        existing = ""
        if gitignore_path.exists():
            existing = gitignore_path.read_text(encoding="utf-8", errors="ignore")
        missing = [item for item in desired if item not in existing.splitlines()]
        if not missing:
            return {"success": True, "changed": False, "path": str(gitignore_path), "missing": []}
        if not dry_run:
            prefix = "" if not existing or existing.endswith("\n") else "\n"
            with gitignore_path.open("a", encoding="utf-8") as handle:
                handle.write(prefix)
                handle.write("\n".join(missing))
                handle.write("\n")
        return {
            "success": True,
            "changed": bool(missing),
            "dry_run": dry_run,
            "path": str(gitignore_path),
            "added": missing,
        }


def create_github_tool_manager() -> GitHubToolManager:
    return GitHubToolManager()


def main() -> None:
    payload: Dict[str, Any]
    if len(sys.argv) > 1:
        payload = json.loads(sys.argv[1])
    else:
        raw = sys.stdin.read().strip()
        payload = json.loads(raw) if raw else {}

    action = payload.pop("action", "status")
    manager = GitHubToolManager()
    if not hasattr(manager, action):
        print(json.dumps({"success": False, "error": f"Unknown action: {action}"}, ensure_ascii=False))
        return
    result = getattr(manager, action)(**payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
