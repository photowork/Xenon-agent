# [file name]: file_manager.py
"""
文件管理工具 v3 — 性能优化版 (合并版)
基于 file_manager_v3 的优化引擎，替换 V2 的慢速 pathlib.glob。

主要改进：
1. ripgrep 子进程加速内容搜索（原生 Python 慢 50-100 倍）
2. os.scandir() 手动遍历代替 pathlib.glob（快 3-5 倍）
3. 自动跳过 vendor/缓存/隐藏目录（.git, venv, node_modules, __pycache__ 等）
4. 二进制文件自动跳过（内容搜索时）
5. 延迟 stat() — 名称不匹配则不碰磁盘
6. Windows / Ubuntu 双平台兼容

所有对外接口（方法名、参数、返回值格式）保持完全向后兼容。
"""
import os
import json
import sys
import time
import logging
import hashlib
import subprocess
import shutil
import mimetypes
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path


# ============== 需要跳过的目录 ==============
SKIP_DIRS = {
    ".git", "venv", ".venv", "venv39", "venv310", "venv311",
    "__pycache__", "node_modules", ".claude", "build", "dist",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".idea", ".vscode", ".DS_Store", "target",  # IDE / 构建产物
    ".agent_history", "webui",  # 额外的项目级跳过
}


def _find_rg() -> Optional[str]:
    """查找 ripgrep 可执行文件路径，返回 None 表示不可用"""
    rg_name = "rg.exe" if sys.platform == "win32" else "rg"
    # 先检查 PATH
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(path_dir, rg_name)
        if os.path.isfile(candidate):
            return candidate
    # Windows 常见安装位置
    if sys.platform == "win32":
        common_dirs = [
            os.path.expandvars(r"%ProgramFiles%\\ripgrep"),
            os.path.expandvars(r"%ChocolateyInstall%\\bin"),
            os.path.expandvars(r"%USERPROFILE%\\scoop\\shims"),
            os.path.expandvars(r"%LOCALAPPDATA%\\Microsoft\\WindowsApps"),
        ]
        for d in common_dirs:
            candidate = os.path.join(d, "rg.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


_RG_PATH: Optional[str] = None  # 缓存


def _rg_available() -> bool:
    global _RG_PATH
    if _RG_PATH is None:
        _RG_PATH = _find_rg()
    return _RG_PATH is not None


def _should_skip_dir(name: str) -> bool:
    """判断目录是否应该跳过"""
    if name.startswith(".") and name != ".":
        return True
    return name in SKIP_DIRS


def _is_binary_or_large(path: Path) -> bool:
    """快速判断文件是否为二进制或太大不适合内容搜索"""
    # 先检查后缀
    suffix = path.suffix.lower()
    if suffix in {
        ".pyc", ".pyo", ".so", ".dll", ".exe", ".pyd", ".bin",
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".svg",
        ".mp3", ".mp4", ".wav", ".avi", ".mov", ".mkv",
        ".zip", ".tar", ".gz", ".7z", ".rar", ".whl",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".pptx",
        ".db", ".sqlite", ".sqlite3",
        ".ttf", ".woff", ".woff2", ".eot",
        ".pyc", ".pkl", ".npy", ".npz", ".h5",
    }:
        return True
    # 文件太大也不搜索内容
    try:
        if path.stat().st_size > 2 * 1024 * 1024:  # > 2MB
            return True
    except OSError:
        return True
    return False


class FileManager:
    """
    文件管理工具，提供文件列表、信息获取、搜索、复制、移动、删除等操作。
    v3 优化版：内部使用 os.scandir() 和 ripgrep 加速。
    """

    # 默认分页大小
    DEFAULT_PAGE_SIZE = 50
    # 最大允许返回条目数
    MAX_HARD_LIMIT = 1000

    def __init__(self, base_path: str = "."):
        """
        初始化文件管理器
        :param base_path: 基础工作目录，默认为当前目录 "."
        """
        self.base_path = Path(base_path).resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(__name__)

    def _resolve_path(self, file_path: str) -> Path:
        """辅助方法：解析路径，处理相对路径和绝对路径。"""
        input_path = Path(file_path)
        if input_path.is_absolute():
            resolved = input_path.resolve()
            # 沙箱隔离检查
            sandbox_dir = getattr(self, '_sandbox_dir_override', None)
            if sandbox_dir is not None:
                try:
                    resolved.relative_to(sandbox_dir)
                except ValueError:
                    self.logger.warning(
                        "沙箱警告: 绝对路径 %s 超出了沙箱范围 %s",
                        resolved, sandbox_dir,
                    )
            return resolved
        relative_parts = input_path.parts
        if len(relative_parts) > 0 and relative_parts[0] == self.base_path.name:
            return self.base_path.joinpath(*relative_parts[1:]).resolve()
        else:
            return (self.base_path / input_path).resolve()

    def _display_path(self, target_path: Path) -> str:
        """返回适合展示的路径，优先使用相对 base_path 的相对路径。"""
        try:
            return str(target_path.relative_to(self.base_path))
        except ValueError:
            return str(target_path)

    # ============== 核心：fast_walk（替代 glob("**/*")） ==============

    def _fast_walk(self, root: Path, yield_dirs: bool = True):
        """
        基于 os.scandir() 的快速递归遍历。
        比 pathlib.glob("**/*") 快 3-5 倍，且自动跳过垃圾目录。
        """
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        name = entry.name
                        is_dir = entry.is_dir()
                        path = Path(entry.path)

                        if is_dir:
                            if _should_skip_dir(name):
                                continue
                            stack.append(path)
                            if yield_dirs:
                                yield path, True  # (path, is_dir)

                        yield path, is_dir

            except (PermissionError, OSError):
                continue

    # ============== ripgrep 内容搜索 ==============

    def _search_with_rg(
        self,
        pattern: str,
        target: Path,
        limit: int = 100,
        extensions: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """用 ripgrep 子进程搜索内容，50-100 倍速度提升"""
        global _RG_PATH
        if not _RG_PATH:
            return []

        cmd = [
            _RG_PATH,
            "-l",              # 只输出文件名
            "-i",              # 不区分大小写
            "--no-heading",
            "--with-filename",
        ]

        # 扩展名过滤
        if extensions:
            for ext in extensions:
                clean_ext = ext.lstrip(".")
                cmd.extend(["-g", f"*.{clean_ext}"])
        else:
            # rg 自动跳过二进制文件（检测 NUL 字节），无需手动过滤类型
            pass

        # 限制匹配数
        cmd.extend(["-m", str(limit)])

        # 搜索模式和目录
        cmd.extend([pattern, str(target)])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                encoding="utf-8",
                errors="replace",
            )
            results = []
            # rg -l 输出格式：每行一个匹配文件路径
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                matched_path = Path(line)
                if not matched_path.exists():
                    continue
                try:
                    stat = matched_path.stat()
                    results.append({
                        "name": matched_path.name,
                        "path": self._display_path(matched_path),
                        "type": "file",
                        "is_dir": False,
                        "size": stat.st_size,
                        "modified_time": stat.st_mtime,
                    })
                except OSError:
                    continue

            return results

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

    # ============== 纯 Python 搜索（无 rg 时回退） ==============

    def _search_python_fast(
        self,
        pattern: str,
        target: Path,
        search_in: str,
        extensions: Optional[List[str]],
        limit: int,
        search_content: bool,
    ) -> List[Dict[str, Any]]:
        """
        优化版纯 Python 搜索：
        - 用 _fast_walk() 代替 glob("**/*")
        - 延迟 stat()
        - 二进制文件跳过
        """
        results = []
        pattern_lower = pattern.lower()
        clean_exts = set(ext.lstrip(".").lower() for ext in extensions) if extensions else None

        for entry_path, is_dir in self._fast_walk(target):

            if len(results) >= limit:
                break

            # 类型筛选
            if search_in == "files" and is_dir:
                continue
            if search_in == "dirs" and not is_dir:
                continue

            # 名称匹配（O(1) 操作，不做 stat）
            name_lower = entry_path.name.lower()
            name_matched = pattern_lower in name_lower

            if not name_matched and not search_content:
                continue

            # 扩展名过滤（文件）
            if clean_exts and not is_dir:
                ext = entry_path.suffix.lstrip(".").lower()
                if ext not in clean_exts:
                    continue

            # 内容搜索（仅当名称不匹配时）
            if not name_matched and search_content and not is_dir:
                if _is_binary_or_large(entry_path):
                    continue
                try:
                    with open(entry_path, "r", encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if pattern_lower in line.lower():
                                name_matched = True  # 标记为匹配
                                break
                except Exception:
                    continue

            if not name_matched:
                continue

            # 名称匹配后，才做 stat（延迟）
            try:
                st = entry_path.stat() if not is_dir else None

                item = {
                    "name": entry_path.name,
                    "path": self._display_path(entry_path),
                    "type": "directory" if is_dir else "file",
                    "is_dir": is_dir,
                }
                if st:
                    item["size"] = st.st_size
                    item["modified_time"] = st.st_mtime

                results.append(item)
            except OSError:
                continue

        return results

    # ============== search() 优化版 ==============

    def search(self,
               pattern: str,
               search_in: str = "all",
               extensions: Optional[List[str]] = None,
               start_dir: str = "",
               recursive: bool = True,
               limit: int = 100,
               search_content: bool = False) -> Dict[str, Any]:
        """
        搜索文件和目录（支持目录名和文件内容搜索！）

        :param pattern: 搜索模式（名称中包含的字符串，不区分大小写）
        :param search_in: 搜索范围 - "all"(全部), "files"(仅文件), "dirs"(仅目录)
        :param extensions: 扩展名筛选列表，仅对文件有效
        :param start_dir: 起始搜索目录，空字符串表示 base_path
        :param recursive: 是否递归搜索
        :param limit: 最大返回数量
        :param search_content: 是否同时搜索文件内容（类 grep）
        :return: 包含匹配结果的字典
        """
        start_time = time.time()

        try:
            target = self._resolve_path(start_dir) if start_dir else self.base_path
            if not target.exists():
                return {"success": False, "error": f"目录 '{target}' 不存在"}

            limit = max(1, min(limit, self.MAX_HARD_LIMIT))

            # 决策：ripgrep 还是 Python
            if search_content and _rg_available() and search_in != "dirs":
                results = self._search_with_rg(pattern, target, limit, extensions)
                method = "ripgrep"
            else:
                results = self._search_python_fast(
                    pattern, target, search_in, extensions, limit, search_content,
                )
                method = "python"

            execution_time = time.time() - start_time

            dirs_found = sum(1 for r in results if r["is_dir"])
            files_found = len(results) - dirs_found

            return {
                "success": True,
                "pattern": pattern,
                "path": str(target),
                "search_method": method,
                "summary": {
                    "directories": dirs_found,
                    "files": files_found,
                },
                "results": results,
                "truncated": len(results) >= limit,
                "execution_time": round(execution_time, 2),
                "message": f"找到 {dirs_found} 个目录, {files_found} 个文件匹配 '{pattern}' ({method})",
            }

        except Exception as e:
            return {"success": False, "error": f"搜索失败: {str(e)}"}

    # ============== find_directory 优化版 ==============

    def find_directory(self,
                       dir_name: str,
                       start_dir: str = "",
                       exact_match: bool = False,
                       limit: int = 20) -> Dict[str, Any]:
        """
        快速查找目录（专门用于定位文件夹位置）

        :param dir_name: 目录名称（或名称的一部分）
        :param start_dir: 起始搜索目录
        :param exact_match: 是否精确匹配，False 时为模糊匹配（包含即可）
        :param limit: 最大返回数量
        :return: 匹配的目录列表
        """
        start_time = time.time()
        try:
            target = self._resolve_path(start_dir) if start_dir else self.base_path
            if not target.exists():
                return {"success": False, "error": f"起始目录 '{target}' 不存在"}

            matches = []
            dir_name_lower = dir_name.lower()

            for entry_path, is_dir in self._fast_walk(target):
                if not is_dir:
                    continue
                name_lower = entry_path.name.lower()
                if exact_match and name_lower == dir_name_lower:
                    matches.append(entry_path)
                elif not exact_match and dir_name_lower in name_lower:
                    matches.append(entry_path)

            matches.sort(key=lambda p: (len(p.relative_to(target).parts), p.name.lower()))
            matches = matches[:limit]

            result_list = []
            for m in matches:
                try:
                    result_list.append({
                        "name": m.name,
                        "path": str(m),
                        "relative_path": str(m.relative_to(self.base_path)),
                        "depth": len(m.relative_to(self.base_path).parts),
                    })
                except ValueError:
                    result_list.append({
                        "name": m.name,
                        "path": str(m),
                        "relative_path": str(m),
                        "depth": len(m.parts),
                    })

            execution_time = time.time() - start_time
            return {
                "success": True,
                "search_term": dir_name,
                "exact_match": exact_match,
                "start_dir": str(target),
                "found": len(result_list),
                "directories": result_list,
                "execution_time": round(execution_time, 2),
                "message": f"找到 {len(result_list)} 个匹配的目录",
            }

        except Exception as e:
            return {"success": False, "error": f"查找目录失败: {str(e)}"}

    # ============== list_items 优化版 ==============

    def list_items(self,
                   dir_path: str = "",
                   item_type: str = "all",
                   extensions: Optional[List[str]] = None,
                   recursive: bool = False,
                   page: int = 1,
                   page_size: int = DEFAULT_PAGE_SIZE) -> Dict[str, Any]:
        """
        列出目录内容（同时返回文件和目录）

        :param dir_path: 目标目录路径，空字符串表示 base_path
        :param item_type: 筛选类型 - "all"(全部), "files"(仅文件), "dirs"(仅目录)
        :param extensions: 文件扩展名筛选列表，如 ["py", "txt"]，仅对文件有效
        :param recursive: 是否递归查找子目录
        :param page: 页码，从 1 开始
        :param page_size: 每页数量
        :return: 包含文件/目录列表和分页信息的字典
        """
        start_time = time.time()

        try:
            target = self._resolve_path(dir_path) if dir_path else self.base_path
            if not target.exists() or not target.is_dir():
                return {"success": False, "error": "目录不存在或不是目录"}

            page_size = max(1, min(page_size, self.MAX_HARD_LIMIT))
            clean_exts = set(ext.lstrip(".").lower() for ext in extensions) if extensions else None

            items = []

            if recursive:
                for entry_path, is_dir in self._fast_walk(target):
                    if item_type == "files" and is_dir:
                        continue
                    if item_type == "dirs" and not is_dir:
                        continue
                    if clean_exts and not is_dir:
                        ext = entry_path.suffix.lstrip(".").lower()
                        if ext not in clean_exts:
                            continue
                    items.append((entry_path, is_dir))
            else:
                try:
                    with os.scandir(target) as it:
                        for entry in it:
                            is_dir = entry.is_dir()
                            if item_type == "files" and is_dir:
                                continue
                            if item_type == "dirs" and not is_dir:
                                continue
                            path = Path(entry.path)
                            if clean_exts and not is_dir:
                                ext = path.suffix.lstrip(".").lower()
                                if ext not in clean_exts:
                                    continue
                            items.append((path, is_dir))
                except PermissionError:
                    pass

            # 排序：目录优先，然后按名称
            items.sort(key=lambda x: (0 if x[1] else 1, x[0].name.lower()))

            total = len(items)
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            page_items = items[start_idx:end_idx]

            result_list = []
            skipped = 0
            for p, is_dir in page_items:
                try:
                    st = p.stat() if not is_dir else None
                    info = {
                        "name": p.name,
                        "path": self._display_path(p),
                        "type": "directory" if is_dir else "file",
                        "is_dir": is_dir,
                    }
                    if st:
                        info["size"] = st.st_size
                        info["modified_time"] = st.st_mtime
                    result_list.append(info)
                except OSError:
                    skipped += 1

            dirs = sum(1 for i in result_list if i["is_dir"])
            files = len(result_list) - dirs
            execution_time = time.time() - start_time

            return {
                "success": True,
                "path": str(target),
                "pagination": {
                    "current_page": page,
                    "page_size": page_size,
                    "total_items": total,
                    "total_pages": (total + page_size - 1) // page_size,
                    "has_more": end_idx < total,
                },
                "summary": {"directories": dirs, "files": files},
                "items": result_list,
                "skipped_count": skipped,
                "execution_time": round(execution_time, 2),
                "message": f"找到 {dirs} 个目录, {files} 个文件 | 第 {page} 页",
            }

        except Exception as e:
            return {"success": False, "error": f"列出目录失败: {str(e)}"}

    # ============== quick_scan 优化版 ==============

    def quick_scan(self, dir_path: str = "", max_depth: int = 3) -> Dict[str, Any]:
        """
        快速扫描目录结构（轻量级，适合智能体快速了解目录布局）

        :param dir_path: 目标目录
        :param max_depth: 最大深度
        :return: 扁平化的目录结构摘要
        """
        start_time = time.time()
        try:
            target = self._resolve_path(dir_path) if dir_path else self.base_path
            if not target.exists() or not target.is_dir():
                return {"success": False, "error": "目录不存在"}

            dirs_list = []
            file_types = {}
            total_files, total_dirs = 0, 0

            for entry_path, is_dir in self._fast_walk(target):
                rel = entry_path.relative_to(target)
                depth = len(rel.parts)
                if depth > max_depth:
                    continue
                if is_dir:
                    dirs_list.append({"path": str(rel), "name": entry_path.name, "depth": depth})
                    total_dirs += 1
                else:
                    total_files += 1
                    ext = entry_path.suffix.lower() or "(no ext)"
                    file_types[ext] = file_types.get(ext, 0) + 1

            by_depth = {}
            for d in dirs_list:
                lvl = d["depth"]
                by_depth.setdefault(lvl, []).append(d["name"])

            execution_time = time.time() - start_time

            return {
                "success": True,
                "path": str(target),
                "summary": {
                    "total_directories": total_dirs,
                    "total_files": total_files,
                    "file_types": dict(sorted(file_types.items(), key=lambda x: -x[1])[:10]),
                },
                "directory_tree": by_depth,
                "all_directories": [d["path"] for d in dirs_list],
                "execution_time": round(execution_time, 2),
                "message": f"扫描完成: {total_dirs} 个目录, {total_files} 个文件",
            }

        except Exception as e:
            return {"success": False, "error": f"扫描失败: {str(e)}"}

    # ============== get_tree_view 优化版（os.scandir 替代 iterdir） ==============

    def get_tree_view(self,
                      dir_path: str = "",
                      max_depth: int = 2,
                      max_items: int = 100,
                      show_size: bool = True,
                      show_emojis: bool = True) -> Dict[str, Any]:
        """
        生成可视化目录树（树形字符画格式）

        :param dir_path: 目标目录路径
        :param max_depth: 最大递归深度（默认2层，避免上下文溢出）
        :param max_items: 最大显示项目数（默认100）
        :param show_size: 是否显示文件大小
        :param show_emojis: 是否使用emoji图标
        :return: 包含树形文本的字典
        """
        start_time = time.time()
        try:
            target = self._resolve_path(dir_path) if dir_path else self.base_path
            if not target.exists() or not target.is_dir():
                return {"success": False, "error": "目录不存在"}

            lines = []
            total_items, dir_count, file_count, truncated = 0, 0, 0, False

            def _fmt_size(sz):
                if sz < 1024: return f"{sz}B"
                if sz < 1024 * 1024: return f"{sz/1024:.1f}KB"
                if sz < 1024 * 1024 * 1024: return f"{sz/(1024*1024):.1f}MB"
                return f"{sz/(1024*1024*1024):.1f}GB"

            def _build_tree(cur_dir, prefix, depth):
                nonlocal total_items, dir_count, file_count, truncated
                if depth > max_depth or total_items >= max_items:
                    truncated = True
                    return
                try:
                    items = sorted(
                        os.scandir(cur_dir),
                        key=lambda x: (0 if x.is_dir() else 1, x.name.lower())
                    )
                except (PermissionError, OSError):
                    lines.append(f"{prefix}└── [权限不足]")
                    return
                for i, item in enumerate(items):
                    if total_items >= max_items:
                        truncated = True
                        lines.append(f"{prefix}└── ... (还有更多)")
                        return
                    total_items += 1
                    is_last = (i == len(items) - 1)
                    conn = "└── " if is_last else "├── "
                    new_prefix = prefix + ("    " if is_last else "│   ")
                    icon = "📂 " if show_emojis else ""
                    if item.is_dir():
                        dir_count += 1
                        lines.append(f"{prefix}{conn}{icon}{item.name}/")
                        if depth < max_depth:
                            _build_tree(Path(item.path), new_prefix, depth + 1)
                    else:
                        file_count += 1
                        icon2 = "📄 " if show_emojis else ""
                        sz_str = ""
                        if show_size:
                            try:
                                sz_str = f" ({_fmt_size(item.stat().st_size)})"
                            except OSError:
                                pass
                        lines.append(f"{prefix}{conn}{icon2}{item.name}{sz_str}")

            root_icon = "📂 " if show_emojis else ""
            lines.append(f"{root_icon}{target.name}/")
            _build_tree(target, "", 1)

            tree_text = "\n".join(lines)
            execution_time = time.time() - start_time

            return {
                "success": True,
                "path": str(target),
                "tree": tree_text,
                "summary": {"directories": dir_count, "files": file_count, "total_items": total_items},
                "meta": {
                    "max_depth": max_depth,
                    "truncated": truncated,
                    "execution_time": round(execution_time, 2),
                },
                "message": f"生成目录树成功：{dir_count} 个目录, {file_count} 个文件",
            }

        except Exception as e:
            return {"success": False, "error": f"生成目录树失败: {str(e)}"}

    # ============== 兼容旧方法（保持向后兼容） ==============

    def list_files(self,
                   recursive: bool = False,
                   extensions: Optional[List[str]] = None,
                   page: int = 1,
                   page_size: int = DEFAULT_PAGE_SIZE) -> Dict[str, Any]:
        """列出文件（兼容旧版本，建议使用 list_items）"""
        result = self.list_items(
            dir_path="", item_type="files",
            extensions=extensions, recursive=recursive,
            page=page, page_size=page_size,
        )
        if result["success"]:
            result["files"] = result.pop("items")
        return result

    def search_files(self,
                     pattern: str,
                     extensions: Optional[List[str]] = None,
                     recursive: bool = True,
                     limit: int = 100) -> Dict[str, Any]:
        """搜索文件（兼容旧版本，建议使用 search）"""
        result = self.search(
            pattern=pattern, search_in="files",
            extensions=extensions, recursive=recursive, limit=limit,
        )
        if result["success"]:
            result["files"] = result.pop("results")
        return result

    # ============== 其他方法（保持 V2 完整兼容） ==============

    def get_file_info(self, file_path: str) -> Dict[str, Any]:
        """获取文件的详细信息"""
        try:
            full_path = self._resolve_path(file_path)
            if not full_path.exists():
                return {"success": False, "error": f"文件 '{file_path}' 不存在"}
            if not full_path.is_file():
                return {"success": False, "error": f"'{file_path}' 不是一个文件"}
            stat = full_path.stat()
            mime_type, _ = mimetypes.guess_type(full_path)
            try:
                relative_path = str(full_path.relative_to(self.base_path))
            except ValueError:
                relative_path = str(full_path)
            return {
                "success": True,
                "name": full_path.name,
                "path": relative_path,
                "full_path": str(full_path.absolute()),
                "size": stat.st_size,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified_time": time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(stat.st_mtime)),
                "mime_type": mime_type or "unknown",
                "message": f"获取文件信息成功: {full_path.name}",
            }
        except Exception as e:
            return {"success": False, "error": f"获取文件信息失败: {str(e)}"}

    def read_file_content(self,
                          file_path: str,
                          encoding: str = "utf-8",
                          max_size: int = 50 * 1024,
                          preview_only: bool = False) -> Dict[str, Any]:
        """读取文件内容"""
        try:
            target = self._resolve_path(file_path)
            if not target.exists() or not target.is_file():
                return {"success": False, "error": "文件不存在或不是文件"}

            file_size = target.stat().st_size
            should_preview = preview_only

            if should_preview:
                content_lines = []
                with open(target, 'r', encoding=encoding, errors='replace') as f:
                    for i, line in enumerate(f):
                        if i >= 50:
                            break
                        content_lines.append(line)
                content = "".join(content_lines)
                return {
                    "success": True,
                    "path": str(target),
                    "size": file_size,
                    "line_count": len(content_lines),
                    "content": content,
                    "truncated": True,
                    "message": "文件预览 (前50行)",
                }

            if file_size > max_size:
                return {
                    "success": False,
                    "error": f"文件过大 ({file_size} 字节)，超过限制 {max_size} 字节。",
                    "suggestion": "请使用 preview_only=True 查看摘要。",
                }

            with open(target, 'r', encoding=encoding, errors='replace') as f:
                content = f.read()

            lines = content.splitlines()

            return {
                "success": True,
                "path": str(target),
                "size": file_size,
                "line_count": len(lines),
                "content": content,
                "message": "文件读取成功",
            }

        except UnicodeDecodeError:
            return {"success": False, "error": "无法解码文件 (非文本文件?)"}
        except Exception as e:
            return {"success": False, "error": f"读取失败: {str(e)}"}

    def read_file_chunked(self,
                          file_path: str,
                          chunk_index: int = 0,
                          chunk_size: int = None,
                          encoding: str = 'utf-8') -> Dict[str, Any]:
        """分块读取大文件，避免一次性加载到内存"""
        try:
            target = self._resolve_path(file_path)
            if not target.exists() or not target.is_file():
                return {"success": False, "error": "文件不存在或不是文件"}

            chunk_size = chunk_size or 15000
            max_safe = 5000

            content, used_enc = self._read_with_fallback(target, encoding)
            total_chars = len(content)
            total_chunks = max(1, (total_chars + chunk_size - 1) // chunk_size)

            if chunk_index >= total_chunks:
                return {"success": False, "error": f"块索引 {chunk_index} 超出范围（共 {total_chunks} 块）"}

            start = chunk_index * chunk_size
            end = min(start + chunk_size, total_chars)
            chunk_content = content[start:end]

            is_truncated = len(chunk_content) > max_safe
            display = chunk_content[:max_safe] if is_truncated else chunk_content

            return {
                "success": True,
                "path": str(target),
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
                "total_chars": total_chars,
                "chunk_size": chunk_size,
                "content": display,
                "encoding": used_enc,
                "truncated": is_truncated,
                "message": f"第 {chunk_index + 1}/{total_chunks} 块",
            }
        except Exception as e:
            return {"success": False, "error": f"分块读取失败: {str(e)}"}

    def _try_decode(self, file_path: Path, encoding: str = 'utf-8') -> Tuple[bool, str, str]:
        """尝试用指定编码读取文件"""
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                return True, f.read(), encoding
        except UnicodeDecodeError:
            return False, "", encoding
        except Exception:
            return False, "", encoding

    def _read_with_fallback(self, file_path: Path, encoding: str = 'utf-8') -> Tuple[str, str]:
        """智能读取文件，自动处理编码回退"""
        preferred = (encoding or 'utf-8').lower()
        candidates = []
        for enc in [preferred, 'utf-8', 'gbk', 'latin-1']:
            if enc not in candidates:
                candidates.append(enc)
        for enc in candidates:
            success, content, used = self._try_decode(file_path, enc)
            if success:
                return content, used
        return "", "unknown"

    def write_file(self,
                   file_path: str,
                   content: str,
                   encoding: str = 'utf-8',
                   append: bool = False) -> Dict[str, Any]:
        """写入文件内容，支持追加模式"""
        try:
            target = self._resolve_path(file_path)

            if append and target.exists():
                existing, _ = self._read_with_fallback(target, encoding)
                content = existing + content

            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, 'w', encoding=encoding) as f:
                f.write(content)

            checksum = hashlib.md5(content.encode('utf-8')).hexdigest()
            return {
                "success": True,
                "path": str(target),
                "size": len(content),
                "checksum": checksum,
                "message": "内容已写入" + ("（追加模式）" if append else ""),
            }
        except Exception as e:
            return {"success": False, "error": f"写入失败: {str(e)}"}

    def append_file(self,
                    file_path: str,
                    content: str,
                    encoding: str = 'utf-8') -> Dict[str, Any]:
        """追加内容到文件末尾"""
        return self.write_file(file_path, content, encoding, append=True)

    def search_content(self,
                       file_path: str,
                       keyword: str,
                       case_sensitive: bool = False,
                       encoding: str = 'utf-8') -> Dict[str, Any]:
        """在文件内容中搜索关键词（类 grep）"""
        try:
            target = self._resolve_path(file_path)
            if not target.exists() or not target.is_file():
                return {"success": False, "error": "文件不存在或不是文件"}

            content, used_enc = self._read_with_fallback(target, encoding)

            if not case_sensitive:
                search_in = content.lower()
                keyword_search = keyword.lower()
            else:
                search_in = content
                keyword_search = keyword

            lines = content.splitlines()
            matches = []
            for i, line in enumerate(lines, 1):
                if (case_sensitive and keyword in line) or \
                   (not case_sensitive and keyword.lower() in line.lower()):
                    matches.append({
                        "line_number": i,
                        "content": line.strip(),
                    })

            return {
                "success": True,
                "path": str(target),
                "keyword": keyword,
                "case_sensitive": case_sensitive,
                "total_matches": len(matches),
                "matches": matches[:100],
                "truncated": len(matches) > 100,
                "encoding": used_enc,
            }
        except Exception as e:
            return {"success": False, "error": f"搜索失败: {str(e)}"}

    def create_directory(self, dir_path: str) -> Dict[str, Any]:
        """创建目录"""
        try:
            target = self._resolve_path(dir_path)
            target.mkdir(parents=True, exist_ok=True)
            return {"success": True, "message": f"目录创建成功: {target}", "path": str(target)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def copy_file(self,
                  source_path: str,
                  destination_path: str,
                  overwrite: bool = False) -> Dict[str, Any]:
        """复制文件"""
        try:
            source = self._resolve_path(source_path)
            destination = self._resolve_path(destination_path)

            if not source.exists() or not source.is_file():
                return {"success": False, "error": "源文件不存在"}

            if destination.exists() and not overwrite:
                return {"success": False, "error": "目标已存在"}

            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

            return {
                "success": True,
                "message": "复制成功",
                "source": str(source),
                "destination": str(destination),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def move_file(self,
                  source_path: str,
                  destination_path: str,
                  overwrite: bool = False) -> Dict[str, Any]:
        """移动文件"""
        try:
            source = self._resolve_path(source_path)
            destination = self._resolve_path(destination_path)

            if not source.exists() or not source.is_file():
                return {"success": False, "error": "源文件不存在"}

            if destination.exists():
                if overwrite:
                    destination.unlink()
                else:
                    return {"success": False, "error": "目标已存在"}

            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, destination)

            return {
                "success": True,
                "message": "移动成功",
                "source": str(source),
                "destination": str(destination),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_file(self, file_path: str) -> Dict[str, Any]:
        """删除文件"""
        try:
            target = self._resolve_path(file_path)
            if not target.exists() or not target.is_file():
                return {"success": False, "error": "文件不存在"}
            target.unlink()
            return {"success": True, "message": "删除成功", "path": str(target)}
        except Exception as e:
            return {"success": False, "error": str(e)}


def main():
    """命令行入口"""
    if len(sys.argv) < 2:
        print(json.dumps({"success": False, "error": "缺少操作参数"}, ensure_ascii=False))
        sys.exit(1)

    action = sys.argv[1]
    base_path = sys.argv[2] if len(sys.argv) > 2 else "."

    manager = FileManager(base_path)

    if action == "list":
        item_type = sys.argv[3] if len(sys.argv) > 3 else "all"
        recursive = sys.argv[4].lower() == "true" if len(sys.argv) > 4 else False
        ext_json = sys.argv[5] if len(sys.argv) > 5 else "[]"
        page = int(sys.argv[6]) if len(sys.argv) > 6 else 1
        page_size = int(sys.argv[7]) if len(sys.argv) > 7 else 50
        result = manager.list_items(
            dir_path="", item_type=item_type,
            extensions=json.loads(ext_json),
            recursive=recursive, page=page, page_size=page_size,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif action == "search":
        pattern = sys.argv[3] if len(sys.argv) > 3 else ""
        search_in = sys.argv[4] if len(sys.argv) > 4 else "all"
        recursive = sys.argv[5].lower() == "true" if len(sys.argv) > 5 else True
        limit = int(sys.argv[6]) if len(sys.argv) > 6 else 100
        search_content = sys.argv[7].lower() == "true" if len(sys.argv) > 7 else False
        result = manager.search(
            pattern=pattern, search_in=search_in,
            recursive=recursive, limit=limit, search_content=search_content,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif action == "find_dir":
        dir_name = sys.argv[3] if len(sys.argv) > 3 else ""
        exact_match = sys.argv[4].lower() == "true" if len(sys.argv) > 4 else False
        limit = int(sys.argv[5]) if len(sys.argv) > 5 else 20
        result = manager.find_directory(dir_name=dir_name, exact_match=exact_match, limit=limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif action == "scan":
        max_depth = int(sys.argv[3]) if len(sys.argv) > 3 else 3
        result = manager.quick_scan(max_depth=max_depth)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif action == "tree":
        dir_path = sys.argv[3] if len(sys.argv) > 3 else ""
        max_depth = int(sys.argv[4]) if len(sys.argv) > 4 else 2
        max_items = int(sys.argv[5]) if len(sys.argv) > 5 else 100
        show_size = sys.argv[6].lower() == "true" if len(sys.argv) > 6 else True
        show_emojis = sys.argv[7].lower() == "true" if len(sys.argv) > 7 else True
        result = manager.get_tree_view(dir_path, max_depth, max_items, show_size, show_emojis)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif action == "read":
        path = sys.argv[3] if len(sys.argv) > 3 else ""
        result = manager.read_file_content(path)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif action == "info":
        path = sys.argv[3] if len(sys.argv) > 3 else ""
        result = manager.get_file_info(path)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    else:
        print(json.dumps({"success": False, "error": f"未知操作: {action}"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
