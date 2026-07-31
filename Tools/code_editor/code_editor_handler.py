#!/usr/bin/env python3
"""
智能体专用代码编辑器 - 增强版 v2.1
特性：精准替换、多级撤销、文件搜索、批量操作
对标 Trae IDE 的原子化操作能力

v2.1 修复：
- search/find_files/list_dir 输出强约束，防止 token 暴涨
- 移除返回值中的原始 results 冗余字段
- 增加二进制文件嗅探、扩展排除规则
- 增加单文件/全局匹配数量上限、扫描文件数上限
- context_lines 强制限制
- view 默认 limit 调小
"""

import os
import re
import shutil
import hashlib
import json
import fnmatch
from pathlib import Path
from typing import List, Dict, Optional, Union, Tuple, Set
from datetime import datetime, timedelta
from collections import deque, Counter, defaultdict
from dataclasses import dataclass, field
import ast
import time


class FileHistoryStore:
    """
    多级历史记录管理器
    支持撤销多次操作，类似 Git 的轻量级版本控制
    """
    
    def __init__(self, max_history: int = 10, retention_days: int = 7):
        self.max_history = max_history
        self.retention_days = retention_days
        self._history: Dict[str, deque] = {}  # file_path -> deque of snapshots
    
    def _get_history_dir(self, workspace: Path) -> Path:
        """获取历史记录目录"""
        history_dir = workspace / ".agent_history"
        history_dir.mkdir(parents=True, exist_ok=True)
        return history_dir
    
    def _cleanup_old_snapshots(self, workspace: Path) -> int:
        """删除超过 retention_days 的旧快照文件"""
        if self.retention_days <= 0:
            return 0
        
        history_dir = self._get_history_dir(workspace)
        if not history_dir.exists():
            return 0
        
        cutoff = datetime.now() - timedelta(days=self.retention_days)
        cleaned = 0
        
        for bak_file in history_dir.glob("*.bak"):
            try:
                mtime = datetime.fromtimestamp(bak_file.stat().st_mtime)
                if mtime < cutoff:
                    bak_file.unlink()
                    cleaned += 1
            except Exception:
                continue
        
        # 同步清理内存中已删除文件对应的记录
        for file_key in list(self._history.keys()):
            self._history[file_key] = deque(
                (s for s in self._history[file_key] if Path(s["snapshot_path"]).exists()),
                maxlen=self.max_history
            )
        
        return cleaned
    
    def save_snapshot(self, file_path: Path, workspace: Path) -> bool:
        """保存文件快照到历史记录"""
        if not file_path.exists():
            return False
        
        file_key = hashlib.md5(str(file_path.resolve()).encode()).hexdigest()
        if file_key not in self._history:
            self._history[file_key] = deque(maxlen=self.max_history)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        snapshot_name = f"{timestamp}_{hashlib.md5(file_key.encode()).hexdigest()[:8]}.bak"
        snapshot_path = self._get_history_dir(workspace) / snapshot_name
        
        try:
            shutil.copy2(file_path, snapshot_path)
            self._history[file_key].append({
                "snapshot_path": str(snapshot_path),
                "timestamp": timestamp,
                "file_key": file_key
            })
            # 保存成功后触发自动清理旧快照
            self._cleanup_old_snapshots(workspace)
            return True
        except Exception:
            return False
    
    def undo(self, file_path: Path, workspace: Path, steps: int = 1) -> Dict:
        """撤销到历史版本"""
        if steps <= 0:
            return {"success": False, "error": "steps 必须大于 0"}

        file_key = hashlib.md5(str(file_path.resolve()).encode()).hexdigest()
        
        if file_key not in self._history or len(self._history[file_key]) == 0:
            return {"success": False, "error": "没有可撤销的历史记录"}
        
        history_deque = self._history[file_key]
        
        if steps > len(history_deque):
            steps = len(history_deque)
        
        try:
            snapshot = None
            for _ in range(steps):
                if history_deque:
                    snapshot = history_deque.pop()
            
            if snapshot:
                snapshot_path = Path(snapshot["snapshot_path"])
                if snapshot_path.exists():
                    shutil.copy2(snapshot_path, file_path)
                    return {
                        "success": True,
                        "message": f"已撤销 {steps} 步，恢复到 {snapshot['timestamp']}",
                        "remaining_history": len(history_deque)
                    }
            
            return {"success": False, "error": "快照文件不存在"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def get_history(self, file_path: Path, workspace: Path) -> List[Dict]:
        """获取文件的历史记录列表"""
        file_key = hashlib.md5(str(file_path.resolve()).encode()).hexdigest()
        if file_key not in self._history:
            return []
        return list(self._history[file_key])
    
    def clear_history(self, file_path: Optional[Path] = None, workspace: Path = None):
        """清理历史记录"""
        if file_path:
            file_key = hashlib.md5(str(file_path.resolve()).encode()).hexdigest()
            if file_key in self._history:
                for snapshot in self._history[file_key]:
                    try:
                        Path(snapshot["snapshot_path"]).unlink(missing_ok=True)
                    except:
                        pass
                del self._history[file_key]
        else:
            # 清理所有内存记录
            self._history.clear()
            # 同时清理磁盘上的 .bak 文件
            if workspace:
                history_dir = self._get_history_dir(workspace)
                if history_dir.exists():
                    for bak_file in history_dir.glob("*.bak"):
                        try:
                            bak_file.unlink()
                        except Exception:
                            pass


class CodeEditorManager:
    """
    面向智能体的代码管理器 - 增强版
    提供类似 Trae IDE 的原子化操作能力
    """
    
    def __init__(self, workspace_dir: str = ".", enable_checksum: bool = True, max_history: int = 10, retention_days: int = 7):
        self.workspace_dir = Path(workspace_dir).resolve()
        self.enable_checksum = enable_checksum
        self.history_manager = FileHistoryStore(max_history=max_history, retention_days=retention_days)
        
        # ========== search 函数的安全限制（防止 token 暴涨） ==========
        self.SEARCH_MAX_FILE_SIZE = 200 * 1024          # 跳过超过 200KB 的文件
        self.SEARCH_MAX_LINE_LENGTH = 500               # 单行最大字符数，超出截断
        self.SEARCH_MAX_OUTPUT_SIZE = 50 * 1024         # content 输出总量上限 50KB
        self.SEARCH_MAX_MATCHES_PER_FILE = 50           # 单文件最多返回 50 条匹配
        self.SEARCH_MAX_TOTAL_MATCHES = 200             # 全局最多 200 条匹配
        self.SEARCH_MAX_FILES_SCANNED = 1000            # 最多扫描 1000 个文件
        self.SEARCH_MAX_CONTEXT_LINES = 5               # context_lines 上限
        
        # 自动排除的目录名
        self.SEARCH_EXCLUDE_DIRS = {
            'venv', '.venv', 'env', 'node_modules', '.git', '__pycache__',
            '.agent_history', 'dist', 'build', '.next', '.nuxt',
            '.idea', '.vscode', 'target', '.pytest_cache', '.mypy_cache',
            'coverage', '.tox', 'site-packages', '.cache', '.gradle',
        }
        # 排除的文件模式
        self.SEARCH_EXCLUDE_FILE_PATTERNS = [
            '*.min.*', '*.map',
            # 图片
            '*.png', '*.jpg', '*.jpeg', '*.gif', '*.bmp', '*.ico', '*.webp', '*.svg',
            # 文档/压缩
            '*.pdf', '*.zip', '*.tar', '*.gz', '*.7z', '*.rar', '*.bz2', '*.xz',
            # 二进制
            '*.exe', '*.dll', '*.so', '*.dylib', '*.bin', '*.o', '*.a',
            '*.pyc', '*.pyo', '*.class', '*.jar', '*.war',
            # 媒体
            '*.mp3', '*.mp4', '*.avi', '*.mov', '*.wav', '*.flac', '*.mkv',
            # 数据库
            '*.db', '*.sqlite', '*.sqlite3',
            # 锁文件 / 巨型生成文件
            '*.lock', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml',
            # 字体
            '*.ttf', '*.otf', '*.woff', '*.woff2', '*.eot',
        ]
        
        # find_files / list_dir 的限制
        self.FIND_MAX_RESULTS = 500
        self.LIST_DIR_MAX_ITEMS = 500
        
    def _resolve_path(self, file_path: str) -> Path:
        """
        解析路径，支持绝对路径和相对路径
        """
        path = Path(file_path)
        resolved = path.resolve() if path.is_absolute() else (self.workspace_dir / file_path).resolve()
        return resolved

    def _resolve_path_or_error(self, file_path: str) -> Union[Path, Dict]:
        """解析路径，失败时返回标准错误字典。"""
        try:
            return self._resolve_path(file_path)
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _is_binary_file(self, fp: Path) -> bool:
        """通过嗅探前 1KB 是否含 NULL 字节判断是否为二进制文件"""
        try:
            with open(fp, 'rb') as f:
                chunk = f.read(1024)
                return b'\x00' in chunk
        except Exception:
            return True  # 读不到当成二进制跳过

    def view(self, file_path: str, view_range: Optional[List[int]] = None, 
             limit: int = 500, offset: int = 0, show_invisible: bool = False) -> Dict:
        """
        查看文件内容（带行号），支持分页查看。
        """
        path = self._resolve_path_or_error(file_path)
        if isinstance(path, dict):
            return path
        if not path.exists():
            return {"success": False, "error": f"文件不存在: {file_path}"}
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
                lines = content.splitlines(keepends=True)
            
            total_lines = len(lines)
            start_line = 1
            end_line = total_lines
            
            if view_range:
                if len(view_range) != 2:
                    return {"success": False, "error": "view_range 必须包含两个整数 [start, end]"}
                
                start_line = max(1, view_range[0])
                end_line = min(total_lines, view_range[1])
                
                if start_line > end_line:
                    return {"success": False, "error": f"无效的范围: {start_line} > {end_line}"}
            else:
                start_line = max(1, offset + 1)
                end_line = min(total_lines, start_line + limit - 1)
            
            indent_info = self._analyze_indentation(content)
            
            output_lines = []
            for i in range(start_line - 1, min(end_line, total_lines)):
                line = lines[i].rstrip('\r\n')
                if show_invisible:
                    line = self._show_invisible_chars(line)
                output_lines.append(f"{i+1:>6}\t{line}")
            
            content_str = "\n".join(output_lines)
            
            msg = f"文件: {file_path} [行 {start_line}-{min(end_line, total_lines)} / 共 {total_lines} 行]"
            if total_lines > end_line:
                msg += f" (还有 {total_lines - end_line} 行未显示，可使用 view_range 或 offset 查看)"
            msg += f"\n缩进类型: {indent_info['indent_type']} (共{indent_info['tab_lines']}行用Tab, {indent_info['space_lines']}行用空格)"
            
            return {
                "success": True,
                "content": content_str,
                "metadata": {
                    "total_lines": total_lines,
                    "shown_range": [start_line, min(end_line, total_lines)],
                    "file_path": str(path),
                    "indent_info": indent_info
                },
                "message": msg
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}

    def create(self, file_path: str, content: str) -> Dict:
        """创建新文件。如果文件已存在，则会报错，防止意外覆盖。"""
        path = self._resolve_path_or_error(file_path)
        if isinstance(path, dict):
            return path
        if path.exists():
            return {"success": False, "error": f"文件已存在: {file_path}。如需覆盖请使用 'write'。"}
        
        return self._write_file(path, content)

    def write(self, file_path: str, content: str) -> Dict:
        """直接写入/覆盖文件。智能体常用的操作，会自动备份历史。"""
        path = self._resolve_path_or_error(file_path)
        if isinstance(path, dict):
            return path
        
        if path.exists():
            self.history_manager.save_snapshot(path, self.workspace_dir)
        
        return self._write_file(path, content)

    def str_replace(self, file_path: str, old_str: str, new_str: str, 
                    replace_all: bool = False, auto_indent: bool = True) -> Dict:
        """精准字符串替换。"""
        path = self._resolve_path_or_error(file_path)
        if isinstance(path, dict):
            return path
        if not path.exists():
            return {"success": False, "error": f"文件不存在: {file_path}"}
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            occurrences = content.count(old_str)
            indent_diff_msg = None
            indent_adjusted = False
            
            if occurrences == 0 and auto_indent:
                result = self._try_indent_match(content, old_str)
                if result:
                    actual_old_str, indent_diff_msg = result
                    occurrences = content.count(actual_old_str)
                    if occurrences > 0:
                        old_str = actual_old_str
                        indent_adjusted = True
                    else:
                        indent_diff_msg = None
            
            if occurrences == 0:
                suggestions = self._find_similar(content, old_str)
                indent_info = self._analyze_indentation(content)
                error_msg = "未找到要替换的旧文本。请确保 old_str 与文件内容完全一致。"
                error_msg += f"\n\n📄 文件缩进信息: {indent_info['indent_type']}"
                error_msg += f"\n💡 提示: {indent_info['hint']}"
                if suggestions:
                    error_msg += f"\n\n可能的相似内容:\n{suggestions}"
                error_msg += self._show_char_diff(old_str, content)
                return {"success": False, "error": error_msg}
            
            if occurrences > 1 and not replace_all:
                line_numbers = self._find_all_occurrence_lines(content, old_str)
                return {
                    "success": False,
                    "error": f"发现 {occurrences} 处匹配，位于行: {line_numbers}。\n请扩大上下文确保唯一性，或设置 replace_all=True 替换所有。"
                }
            
            self.history_manager.save_snapshot(path, self.workspace_dir)
            
            if indent_diff_msg and indent_adjusted:
                indent_info = self._analyze_indentation(content)
                new_str = self._adjust_new_str_indent(new_str, indent_info)
            
            if replace_all:
                new_content = content.replace(old_str, new_str)
                replaced_count = occurrences
            else:
                new_content = content.replace(old_str, new_str, 1)
                replaced_count = 1
            
            self._write_file(path, new_content)
            
            diff_preview = f"--- 原始 ---\n{self._escape_for_display(old_str)}\n\n--- 替换为 ---\n{self._escape_for_display(new_str)}"
            
            msg = f"成功替换 {replaced_count} 处内容。"
            if indent_diff_msg:
                msg += f"\n⚠️ {indent_diff_msg}"
            
            return {
                "success": True,
                "message": msg,
                "diff_preview": diff_preview,
                "replaced_count": replaced_count,
                "indent_adjusted": indent_adjusted
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _try_indent_match(self, content: str, old_str: str) -> Optional[tuple]:
        """尝试通过忽略缩进差异来匹配"""
        old_lines = old_str.split('\n')
        if len(old_lines) < 1:
            return None
        
        content_lines = content.split('\n')
        old_normalized = '\n'.join(line.lstrip() for line in old_lines)
        
        for i in range(len(content_lines) - len(old_lines) + 1):
            candidate_lines = content_lines[i:i + len(old_lines)]
            candidate_normalized = '\n'.join(line.lstrip() for line in candidate_lines)
            
            if candidate_normalized == old_normalized:
                actual_old_str = '\n'.join(candidate_lines)
                indent_diff_msg = f"检测到缩进差异，已自动适配（第{i+1}行）"
                return (actual_old_str, indent_diff_msg)
        
        return None

    def _adjust_new_str_indent(self, new_str: str, indent_info: Dict) -> str:
        """根据文件缩进风格调整新内容的缩进"""
        if indent_info['indent_type'] == "Tab":
            return self._normalize_indentation(new_str, "tab")
        elif indent_info['indent_type'] == "空格":
            size = indent_info.get('common_indent_size', 4)
            if size == 2:
                return self._normalize_indentation(new_str, "space2")
            else:
                return self._normalize_indentation(new_str, "space4")
        return new_str

    def _find_all_occurrence_lines(self, content: str, pattern: str) -> List[int]:
        """找到所有匹配的行号"""
        lines = content.split('\n')
        pattern_lines = pattern.split('\n')
        first_line = pattern_lines[0] if pattern_lines else pattern
        
        result = []
        for i, line in enumerate(lines):
            if first_line in line:
                if len(pattern_lines) == 1:
                    result.append(i + 1)
                elif i + len(pattern_lines) <= len(lines):
                    if '\n'.join(lines[i:i+len(pattern_lines)]) == pattern:
                        result.append(i + 1)
        return result

    def _show_char_diff(self, old_str: str, content: str) -> str:
        """显示字符级别的差异提示"""
        old_first_line = old_str.split('\n')[0] if old_str else ""
        
        lines = content.split('\n')
        best_match_line = 0
        best_match_score = 0
        
        for i, line in enumerate(lines):
            score = self._similarity_score(old_first_line, line)
            if score > best_match_score:
                best_match_score = score
                best_match_line = i
        
        if best_match_score > 0.3:
            actual_line = lines[best_match_line]
            msg = f"\n\n🔍 最相似的实际内容（第{best_match_line+1}行）:"
            msg += f"\n期望: {self._show_invisible_chars(old_first_line)}"
            msg += f"\n实际: {self._show_invisible_chars(actual_line)}"
            return msg
        return ""

    def _similarity_score(self, s1: str, s2: str) -> float:
        """计算两个字符串的相似度"""
        s1_clean = s1.strip().lower()
        s2_clean = s2.strip().lower()
        
        if not s1_clean or not s2_clean:
            return 0.0
        
        words1 = set(s1_clean.split())
        words2 = set(s2_clean.split())
        
        if not words1 or not words2:
            return 0.0
        
        intersection = words1 & words2
        union = words1 | words2
        
        return len(intersection) / len(union)

    def _escape_for_display(self, text: str) -> str:
        """转义特殊字符用于显示"""
        return text

    def insert(self, file_path: str, insert_line: int, content: str) -> Dict:
        """在指定行号后插入新内容。"""
        if content == "":
            return {"success": False, "error": "插入内容不能为空"}

        path = self._resolve_path_or_error(file_path)
        if isinstance(path, dict):
            return path
        if not path.exists():
            return {"success": False, "error": f"文件不存在: {file_path}"}

        try:
            with open(path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            total_lines = len(lines)
            if insert_line < 0 or insert_line > total_lines:
                return {"success": False, "error": f"行号 {insert_line} 超出范围 (0-{total_lines})"}
            
            new_lines = content.split('\n')
            formatted_new_lines = [line + '\n' for line in new_lines[:-1]]
            if new_lines:
                if insert_line < total_lines:
                    formatted_new_lines.append(new_lines[-1] + '\n')
                else:
                    if lines and not lines[-1].endswith('\n'):
                        lines[-1] += '\n'
                    formatted_new_lines.append(new_lines[-1])

            lines[insert_line:insert_line] = formatted_new_lines
            
            self.history_manager.save_snapshot(path, self.workspace_dir)
            self._write_file(path, "".join(lines))
            
            return {
                "success": True,
                "message": f"已在第 {insert_line} 行后插入 {len(new_lines)} 行新代码。",
                "new_line_count": len(lines)
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}

    def append(self, file_path: str, content: str, ensure_newline: bool = True) -> Dict:
        """追加内容到文件末尾。"""
        path = self._resolve_path_or_error(file_path)
        if isinstance(path, dict):
            return path
        
        if not path.exists():
            return self.create(file_path, content)
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                original = f.read()
            
            self.history_manager.save_snapshot(path, self.workspace_dir)
            
            if ensure_newline and original and not original.endswith('\n'):
                content = '\n' + content
            
            new_content = original + content
            self._write_file(path, new_content)
            
            return {
                "success": True,
                "message": f"已追加 {len(content.splitlines())} 行内容到文件末尾。",
                "new_total_lines": len(new_content.splitlines())
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete(self, file_path: str) -> Dict:
        """删除文件。"""
        path = self._resolve_path_or_error(file_path)
        if isinstance(path, dict):
            return path
        
        if not path.exists():
            return {"success": False, "error": f"文件不存在: {file_path}"}
        
        if path.is_dir():
            return {"success": False, "error": f"这是一个目录，请使用专门的目录删除功能。"}
        
        try:
            self.history_manager.save_snapshot(path, self.workspace_dir)
            path.unlink()
            
            return {
                "success": True,
                "message": f"已删除文件: {file_path}",
                "hint": "可使用 undo 恢复此文件"
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}

    def undo(self, file_path: str, steps: int = 1) -> Dict:
        """撤销操作"""
        if steps <= 0:
            return {"success": False, "error": "steps 必须大于 0"}

        path = self._resolve_path_or_error(file_path)
        if isinstance(path, dict):
            return path
        return self.history_manager.undo(path, self.workspace_dir, steps)

    def search(self, pattern: str, file_path: Optional[str] = None, 
           dir_path: Optional[str] = None, 
           file_pattern: str = "*",
           ignore_case: bool = False,
           context_lines: int = 0) -> Dict:
        """
        在文件中搜索内容（类似 grep）
        强约束输出量，防止 token 暴涨。
        """
        if not file_path and not dir_path:
            dir_path = "."
        
        # 强制限制 context_lines，防止智能体传入过大值
        context_lines = max(0, min(context_lines, self.SEARCH_MAX_CONTEXT_LINES))
        
        results = []
        flags = re.IGNORECASE if ignore_case else 0
        
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return {"success": False, "error": f"无效的正则表达式: {e}"}
        
        # 用于跨文件控制的可变状态
        stats = {
            "total_matches": 0,
            "files_scanned": 0,
            "files_skipped_binary": 0,
            "files_skipped_size": 0,
            "hit_global_limit": False,
        }
        
        def search_in_file(fp: Path) -> List[Dict]:
            matches = []
            try:
                file_size = fp.stat().st_size
                if file_size > self.SEARCH_MAX_FILE_SIZE:
                    stats["files_skipped_size"] += 1
                    return []
                
                # 二进制嗅探
                if self._is_binary_file(fp):
                    stats["files_skipped_binary"] += 1
                    return []
                
                with open(fp, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()
                
                file_match_count = 0
                for i, line in enumerate(lines):
                    # 单文件匹配数上限
                    if file_match_count >= self.SEARCH_MAX_MATCHES_PER_FILE:
                        matches.append({
                            "line_number": -1,
                            "line_content": f"[已达单文件匹配上限 {self.SEARCH_MAX_MATCHES_PER_FILE}，省略后续匹配]",
                            "context": f"      -\t[已达单文件匹配上限 {self.SEARCH_MAX_MATCHES_PER_FILE}，省略后续匹配]"
                        })
                        break
                    
                    # 全局匹配数上限
                    if stats["total_matches"] + file_match_count >= self.SEARCH_MAX_TOTAL_MATCHES:
                        stats["hit_global_limit"] = True
                        break
                    
                    if regex.search(line):
                        start = max(0, i - context_lines)
                        end = min(len(lines), i + context_lines + 1)
                        context = []
                        for j in range(start, end):
                            prefix = ">>>" if j == i else "   "
                            line_text = lines[j].rstrip()
                            if len(line_text) > self.SEARCH_MAX_LINE_LENGTH:
                                original_len = len(line_text)
                                line_text = line_text[:self.SEARCH_MAX_LINE_LENGTH] + f"...[截断,原{original_len}字符]"
                            context.append(f"{prefix} {j+1:>5}\t{line_text}")
                        
                        line_content = line.rstrip()
                        if len(line_content) > self.SEARCH_MAX_LINE_LENGTH:
                            original_len = len(line_content)
                            line_content = line_content[:self.SEARCH_MAX_LINE_LENGTH] + f"...[截断,原{original_len}字符]"
                        
                        matches.append({
                            "line_number": i + 1,
                            "line_content": line_content,
                            "context": "\n".join(context)
                        })
                        file_match_count += 1
            except Exception:
                pass
            return matches
        
        try:
            if file_path:
                path = self._resolve_path_or_error(file_path)
                if isinstance(path, dict):
                    return path
                stats["files_scanned"] += 1
                matches = search_in_file(path)
                if matches:
                    results.append({"file": file_path, "matches": matches})
                    stats["total_matches"] += sum(1 for m in matches if m["line_number"] > 0)
            else:
                search_dir = self._resolve_path_or_error(dir_path)
                if isinstance(search_dir, dict):
                    return search_dir
                
                # 使用 walk() 替代 rglob，在遍历时直接裁剪噪声目录
                for root, dirs, files in search_dir.walk():
                    # 在遍历时直接裁剪噪声目录，避免进入 venv 等大型目录
                    dirs[:] = [d for d in dirs if d not in self.SEARCH_EXCLUDE_DIRS]
                    
                    for file in files:
                        # 文件数上限
                        if stats["files_scanned"] >= self.SEARCH_MAX_FILES_SCANNED:
                            break
                        # 全局匹配上限
                        if stats["hit_global_limit"] or stats["total_matches"] >= self.SEARCH_MAX_TOTAL_MATCHES:
                            stats["hit_global_limit"] = True
                            break
                        # 文件名模式匹配
                        if not fnmatch.fnmatch(file, file_pattern):
                            continue
                        # 排除文件名模式
                        if any(fnmatch.fnmatch(file, pat) for pat in self.SEARCH_EXCLUDE_FILE_PATTERNS):
                            continue
                        
                        fp = root / file
                        if not fp.is_file():
                            continue
                        
                        stats["files_scanned"] += 1
                        matches = search_in_file(fp)
                        if matches:
                            results.append({"file": str(fp), "matches": matches})
                            stats["total_matches"] += sum(1 for m in matches if m["line_number"] > 0)
                    
                    if stats["files_scanned"] >= self.SEARCH_MAX_FILES_SCANNED:
                        break
            
            # 构建输出文本
            output_lines = []
            for r in results:
                output_lines.append(f"\n📄 {r['file']}")
                for m in r["matches"]:
                    output_lines.append(m["context"])
            
            content = "\n".join(output_lines) if output_lines else "未找到匹配内容"
            
            # 总输出大小截断
            output_truncated = False
            if len(content) > self.SEARCH_MAX_OUTPUT_SIZE:
                content = content[:self.SEARCH_MAX_OUTPUT_SIZE]
                content += f"\n\n⚠️ [输出截断: 超出 {self.SEARCH_MAX_OUTPUT_SIZE // 1024}KB 上限，已省略后续内容]"
                output_truncated = True
            
            # 在末尾追加搜索统计与提示
            tips = []
            if stats["hit_global_limit"]:
                tips.append(f"⚠️ 已达全局匹配上限 {self.SEARCH_MAX_TOTAL_MATCHES}，建议缩小搜索范围（指定 file_pattern 或更精确的 pattern）")
            if stats["files_scanned"] >= self.SEARCH_MAX_FILES_SCANNED:
                tips.append(f"⚠️ 已达扫描文件数上限 {self.SEARCH_MAX_FILES_SCANNED}，请缩小 dir_path 或指定 file_pattern")
            if stats["files_skipped_binary"] > 0:
                tips.append(f"ℹ️ 跳过 {stats['files_skipped_binary']} 个二进制文件")
            if stats["files_skipped_size"] > 0:
                tips.append(f"ℹ️ 跳过 {stats['files_skipped_size']} 个超过 {self.SEARCH_MAX_FILE_SIZE // 1024}KB 的大文件")
            if output_truncated:
                tips.append("⚠️ 输出文本已截断")
            
            if tips:
                content += "\n\n" + "\n".join(tips)
            
            # ⚠️ 注意：不再返回原始 results 字段，避免双倍输出
            return {
                "success": True,
                "content": content,
                "metadata": {
                    "total_files_with_match": len(results),
                    "total_matches": stats["total_matches"],
                    "files_scanned": stats["files_scanned"],
                    "files_skipped_binary": stats["files_skipped_binary"],
                    "files_skipped_size": stats["files_skipped_size"],
                    "pattern": pattern,
                    "output_truncated": output_truncated,
                    "hit_global_limit": stats["hit_global_limit"],
                }
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}

    def find_files(self, pattern: str, dir_path: str = ".") -> Dict:
        """
        查找匹配模式的文件（类似 glob）
        强约束输出，自动排除噪声目录。
        使用 walk() 遍历，在遍历时裁剪噪声目录，防止进入 venv 等大型目录。
        """
        search_dir = self._resolve_path_or_error(dir_path)
        if isinstance(search_dir, dict):
            return search_dir
        
        if not search_dir.is_dir():
            return {"success": False, "error": f"不是目录: {dir_path}"}
        
        try:
            matches = []
            truncated = False
            
            # 使用 walk() 替代 glob/rglob，可在遍历时排除噪声目录
            for root, dirs, files in search_dir.walk():
                # 在遍历时直接裁剪噪声目录，避免进入
                dirs[:] = [d for d in dirs if d not in self.SEARCH_EXCLUDE_DIRS]
                
                for file in files:
                    if fnmatch.fnmatch(file, pattern):
                        if len(matches) >= self.FIND_MAX_RESULTS:
                            truncated = True
                            break
                        
                        fp = root / file
                        file_type = "DIR" if fp.is_dir() else "FILE"
                        try:
                            size = fp.stat().st_size if fp.is_file() else "-"
                        except Exception:
                            size = "-"
                        matches.append({
                            "path": str(fp),
                            "type": file_type,
                            "size": size
                        })
                
                if truncated:
                    break
            
            matches.sort(key=lambda x: x["path"])
            
            output_lines = [f"[{m['type']:4}]\t{m['path']}\t{m['size']}" for m in matches]
            content = "\n".join(output_lines) if output_lines else "未找到匹配文件"
            
            if truncated:
                content += f"\n\n⚠️ [结果截断: 超过 {self.FIND_MAX_RESULTS} 个，请缩小搜索范围或使用更精确的 pattern]"
            
            # ⚠️ 不再返回 files 原始字段
            return {
                "success": True,
                "content": content,
                "metadata": {
                    "pattern": pattern,
                    "total_count": len(matches),
                    "truncated": truncated
                }
            }
            
        except Exception as e:
            return {"success": False, "error": str(e)}

    def list_dir(self, dir_path: str = ".", recursive: bool = False, max_depth: int = 3) -> Dict:
        """
        列出目录结构。带数量上限和噪声目录排除。
        """
        path = self._resolve_path_or_error(dir_path)
        if isinstance(path, dict):
            return path
        if not path.is_dir():
            return {"success": False, "error": f"不是目录: {dir_path}"}
        
        # 限制递归深度，防止无限展开
        max_depth = max(1, min(max_depth, 5))
        
        try:
            items = []
            truncated = {"flag": False}
            
            def list_recursive(p: Path, depth: int = 0, prefix: str = ""):
                if depth > max_depth:
                    return
                if len(items) >= self.LIST_DIR_MAX_ITEMS:
                    truncated["flag"] = True
                    return
                
                try:
                    sorted_items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
                except PermissionError:
                    return
                
                for i, item in enumerate(sorted_items):
                    if len(items) >= self.LIST_DIR_MAX_ITEMS:
                        truncated["flag"] = True
                        return
                    if item.name.startswith('.'):
                        continue
                    # 排除噪声目录
                    if item.is_dir() and item.name in self.SEARCH_EXCLUDE_DIRS:
                        items.append(f"{prefix}├── 📁 {item.name}/  [已折叠]")
                        continue
                    
                    is_last = i == len(sorted_items) - 1
                    connector = "└── " if is_last else "├── "
                    item_type = "📁" if item.is_dir() else "📄"
                    
                    items.append(f"{prefix}{connector}{item_type} {item.name}")
                    
                    if item.is_dir() and recursive and depth < max_depth:
                        new_prefix = prefix + ("    " if is_last else "│   ")
                        list_recursive(item, depth + 1, new_prefix)
            
            if recursive:
                items.append(f"📁 {path.name}/")
                list_recursive(path, 0, "")
            else:
                for item in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                    if len(items) >= self.LIST_DIR_MAX_ITEMS:
                        truncated["flag"] = True
                        break
                    if item.name.startswith('.'):
                        continue
                    item_type = "📁" if item.is_dir() else "📄"
                    try:
                        size = f"({item.stat().st_size} bytes)" if item.is_file() else ""
                    except Exception:
                        size = ""
                    items.append(f"{item_type} {item.name} {size}")
            
            content = "\n".join(items)
            if truncated["flag"]:
                content += f"\n\n⚠️ [结果截断: 超过 {self.LIST_DIR_MAX_ITEMS} 项，请缩小范围或减少 max_depth]"
            
            return {
                "success": True,
                "content": content,
                "message": f"目录: {dir_path}" + (" (递归)" if recursive else "")
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_file_info(self, file_path: str) -> Dict:
        """获取文件详细信息"""
        path = self._resolve_path_or_error(file_path)
        if isinstance(path, dict):
            return path
        
        if not path.exists():
            return {"success": False, "error": f"文件不存在: {file_path}"}
        
        try:
            stat = path.stat()
            
            lines = 0
            checksum = None
            if path.is_file():
                # 大文件不读取全文计算 checksum
                if stat.st_size <= 5 * 1024 * 1024:  # 5MB 以下才计算
                    try:
                        with open(path, 'r', encoding='utf-8', errors='replace') as f:
                            content = f.read()
                            lines = content.count('\n') + (1 if content and not content.endswith('\n') else 0)
                            if self.enable_checksum:
                                checksum = hashlib.md5(content.encode('utf-8', errors='replace')).hexdigest()
                    except Exception:
                        lines = -1
                else:
                    lines = -1  # 表示文件过大未统计
            
            return {
                "success": True,
                "info": {
                    "path": str(path),
                    "size_bytes": stat.st_size,
                    "lines": lines,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "created": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                    "is_file": path.is_file(),
                    "is_dir": path.is_dir(),
                    "extension": path.suffix,
                    "checksum": checksum
                }
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


    # ============== 文件管理操作（从 file_manager 移植） ==============

    def copy_file(self, source_path: str, destination_path: str, overwrite: bool = False) -> Dict:
        """复制文件。"""
        source = self._resolve_path_or_error(source_path)
        if isinstance(source, dict):
            return source
        destination = self._resolve_path_or_error(destination_path)
        if isinstance(destination, dict):
            return destination

        if not source.exists() or not source.is_file():
            return {"success": False, "error": f"源文件不存在: {source_path}"}

        if destination.exists() and not overwrite:
            return {"success": False, "error": "目标文件已存在，请使用 overwrite=True 覆盖"}

        try:
            self.history_manager.save_snapshot(destination, self.workspace_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            return {
                "success": True,
                "message": f"已复制: {source_path} → {destination_path}",
                "source": str(source),
                "destination": str(destination),
                "hint": "可使用 undo 撤销此操作"
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def move_file(self, source_path: str, destination_path: str, overwrite: bool = False) -> Dict:
        """移动/重命名文件。"""
        source = self._resolve_path_or_error(source_path)
        if isinstance(source, dict):
            return source
        destination = self._resolve_path_or_error(destination_path)
        if isinstance(destination, dict):
            return destination

        if not source.exists() or not source.is_file():
            return {"success": False, "error": f"源文件不存在: {source_path}"}

        if source == destination:
            return {"success": False, "error": "源文件和目标文件相同"}

        if destination.exists() and not overwrite:
            return {"success": False, "error": "目标文件已存在，请使用 overwrite=True 覆盖"}

        try:
            self.history_manager.save_snapshot(source, self.workspace_dir)
            if destination.exists():
                self.history_manager.save_snapshot(destination, self.workspace_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, destination)
            return {
                "success": True,
                "message": f"已移动: {source_path} → {destination_path}",
                "source": str(source),
                "destination": str(destination),
                "hint": "可使用 undo 撤销此操作"
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def create_directory(self, dir_path: str) -> Dict:
        """创建目录（类似 mkdir -p）。"""
        path = self._resolve_path_or_error(dir_path)
        if isinstance(path, dict):
            return path
        try:
            path.mkdir(parents=True, exist_ok=True)
            return {
                "success": True,
                "message": f"目录创建成功: {dir_path}",
                "path": str(path)
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_tree_view(self, dir_path: str = "", max_depth: int = 2,
                      max_items: int = 100, show_size: bool = True,
                      show_emojis: bool = True) -> Dict:
        """生成可视化目录树（树形字符画格式）。"""
        path = self._resolve_path_or_error(dir_path) if dir_path else self.workspace_dir
        if isinstance(path, dict):
            return path
        if not path.is_dir():
            return {"success": False, "error": f"不是目录: {dir_path}"}

        max_depth = max(1, min(max_depth, 5))
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
                try:
                    if item.is_dir():
                        dir_count += 1
                        icon = "📂 " if show_emojis else ""
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
                except OSError:
                    continue

        root_icon = "📂 " if show_emojis else ""
        lines.append(f"{root_icon}{path.name}/")
        _build_tree(path, "", 1)

        tree_text = "\n".join(lines)

        return {
            "success": True,
            "path": str(path),
            "tree": tree_text,
            "summary": {"directories": dir_count, "files": file_count, "total_items": total_items},
            "meta": {
                "max_depth": max_depth,
                "truncated": truncated,
            },
            "message": f"生成目录树成功：{dir_count} 个目录, {file_count} 个文件",
        }

    def quick_scan(self, dir_path: str = "", max_depth: int = 3) -> Dict:
        """快速扫描目录结构（轻量级，适合了解目录布局）。"""
        path = self._resolve_path_or_error(dir_path) if dir_path else self.workspace_dir
        if isinstance(path, dict):
            return path
        if not path.is_dir():
            return {"success": False, "error": f"不是目录: {dir_path}"}

        max_depth = max(1, min(max_depth, 10))
        dirs_list = []
        file_types = {}
        total_files, total_dirs = 0, 0

        try:
            for root, dirs, files in path.walk():
                rel = root.relative_to(path)
                depth_here = len(rel.parts) if path != root else 0
                if depth_here > max_depth:
                    dirs[:] = []
                    continue
                dirs[:] = [d for d in dirs if d not in self.SEARCH_EXCLUDE_DIRS]

                for d in dirs:
                    dir_path_obj = root / d
                    rel_path = dir_path_obj.relative_to(path)
                    dirs_list.append({"path": str(rel_path), "name": d, "depth": len(rel_path.parts)})
                    total_dirs += 1

                for f in files:
                    total_files += 1
                    ext = Path(f).suffix.lower() or "(no ext)"
                    file_types[ext] = file_types.get(ext, 0) + 1

            by_depth = {}
            for d in dirs_list:
                lvl = d["depth"]
                by_depth.setdefault(lvl, []).append(d["name"])

            return {
                "success": True,
                "path": str(path),
                "summary": {
                    "total_directories": total_dirs,
                    "total_files": total_files,
                    "file_types": dict(sorted(file_types.items(), key=lambda x: -x[1])[:10]),
                },
                "directory_tree": by_depth,
                "all_directories": [d["path"] for d in dirs_list],
                "message": f"扫描完成: {total_dirs} 个目录, {total_files} 个文件",
            }
        except Exception as e:
            return {"success": False, "error": f"扫描失败: {str(e)}"}

    def read_file_chunked(self, file_path: str, chunk_index: int = 0,
                          chunk_size: int = None, encoding: str = 'utf-8') -> Dict:
        """顺序分块读取文本文件，每次返回一个完整且不重叠的块。"""
        path = self._resolve_path_or_error(file_path)
        if isinstance(path, dict):
            return path
        if not path.exists() or not path.is_file():
            return {"success": False, "error": f"文件不存在: {file_path}"}
        if chunk_index < 0:
            return {"success": False, "error": "块索引不能小于 0"}

        MAX_CHUNK_SIZE = 20000
        requested = chunk_size if chunk_size is not None else MAX_CHUNK_SIZE
        if requested <= 0:
            return {"success": False, "error": "块大小必须大于 0"}
        chunk_size_val = min(requested, MAX_CHUNK_SIZE)

        try:
            content = None
            used_enc = encoding
            for enc in [encoding, 'utf-8', 'gbk', 'latin-1']:
                try:
                    with open(path, 'r', encoding=enc) as f:
                        content = f.read()
                    used_enc = enc
                    break
                except UnicodeDecodeError:
                    continue
                except Exception:
                    continue
            if content is None:
                return {"success": False, "error": "无法解码文件（尝试了多种编码）"}

            total_chars = len(content)
            total_chunks = max(1, (total_chars + chunk_size_val - 1) // chunk_size_val)

            if chunk_index >= total_chunks:
                return {"success": False, "error": f"块索引 {chunk_index} 超出范围（共 {total_chunks} 块）"}

            start = chunk_index * chunk_size_val
            end = min(start + chunk_size_val, total_chars)
            chunk_content = content[start:end]
            has_more = chunk_index + 1 < total_chunks

            return {
                "success": True,
                "path": str(path),
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
                "total_chars": total_chars,
                "chunk_size": chunk_size_val,
                "start_char": start,
                "end_char": end,
                "content": chunk_content,
                "has_more": has_more,
                "is_last_chunk": not has_more,
                "next_chunk_index": chunk_index + 1 if has_more else None,
                "remaining_chunks": total_chunks - chunk_index - 1,
                "encoding": used_enc,
                "message": f"第 {chunk_index + 1}/{total_chunks} 块读取完成" +
                          (f"；继续请使用 next_chunk_index={chunk_index + 1}" if has_more else "；已到末尾"),
            }
        except Exception as e:
            return {"success": False, "error": f"分块读取失败: {str(e)}"}

    def list_history(self, file_path: str = "", limit: int = 20) -> Dict:
        """列出文件的操作历史记录（支持撤销历史查询）。"""
        try:
            limit = max(1, min(limit, 200))
            if file_path:
                path = self._resolve_path_or_error(file_path)
                if isinstance(path, dict):
                    return path
                history = self.history_manager.get_history(path, self.workspace_dir)
            else:
                history = []
                history_dir = self.workspace_dir / ".agent_history"
                if history_dir.exists():
                    for bak_file in sorted(history_dir.glob("*.bak"), reverse=True)[:limit]:
                        history.append({
                            "file": bak_file.stem,
                            "timestamp": bak_file.stat().st_mtime,
                            "snapshot": bak_file.name
                        })

            return {
                "success": True,
                "history": history[:limit],
                "count": min(len(history), limit),
                "message": f"找到 {min(len(history), limit)} 条历史记录",
            }
        except Exception as e:
            return {"success": False, "error": f"读取历史失败: {str(e)}"}

    def find_directory(self, dir_name: str, start_dir: str = "",
                       exact_match: bool = False, limit: int = 20) -> Dict:
        """快速查找目录（专门用于定位文件夹位置）。"""
        search_path = self._resolve_path_or_error(start_dir) if start_dir else self.workspace_dir
        if isinstance(search_path, dict):
            return search_path
        if not search_path.is_dir():
            return {"success": False, "error": f"起始目录不存在: {start_dir}"}

        limit = max(1, min(limit, 100))
        matches = []
        dir_name_lower = dir_name.lower()

        try:
            for root, dirs, files in search_path.walk():
                dirs[:] = [d for d in dirs if d not in self.SEARCH_EXCLUDE_DIRS]
                for d in dirs:
                    if len(matches) >= limit:
                        break
                    name_lower = d.lower()
                    if exact_match and name_lower == dir_name_lower:
                        matches.append(root / d)
                    elif not exact_match and dir_name_lower in name_lower:
                        matches.append(root / d)
                if len(matches) >= limit:
                    break

            result_list = []
            for m in matches:
                try:
                    result_list.append({
                        "name": m.name,
                        "path": str(m),
                        "relative_path": str(m.relative_to(self.workspace_dir)),
                        "depth": len(m.relative_to(self.workspace_dir).parts),
                    })
                except ValueError:
                    result_list.append({
                        "name": m.name,
                        "path": str(m),
                        "relative_path": str(m),
                        "depth": len(m.parts),
                    })

            return {
                "success": True,
                "search_term": dir_name,
                "exact_match": exact_match,
                "start_dir": str(search_path),
                "found": len(result_list),
                "directories": result_list,
                "message": f"找到 {len(result_list)} 个匹配的目录",
            }
        except Exception as e:
            return {"success": False, "error": f"查找目录失败: {str(e)}"}

    def _find_similar(self, content: str, target: str, max_suggestions: int = 3) -> str:
        """查找相似内容（简单的模糊匹配建议）"""
        lines = content.split('\n')
        suggestions = []
        
        target_lower = target.lower().strip()
        target_words = set(target_lower.split())
        
        for i, line in enumerate(lines):
            line_lower = line.lower().strip()
            if target_lower in line_lower:
                suggestions.append(f"第 {i+1} 行: {line.strip()[:100]}")
            elif target_words & set(line_lower.split()):
                suggestions.append(f"第 {i+1} 行: {line.strip()[:100]}")
            
            if len(suggestions) >= max_suggestions:
                break
        
        return "\n".join(suggestions) if suggestions else ""

    def _analyze_indentation(self, content: str) -> Dict:
        """分析文件的缩进类型和统计"""
        lines = content.split('\n')
        tab_lines = 0
        space_lines = 0
        space_indent_sizes = []
        
        for line in lines:
            if not line.strip():
                continue
            
            leading = line[:len(line) - len(line.lstrip())]
            if '\t' in leading:
                tab_lines += 1
            elif leading and leading[0] == ' ':
                space_lines += 1
                space_indent_sizes.append(len(leading))
        
        if tab_lines > space_lines:
            indent_type = "Tab"
        elif space_lines > tab_lines:
            indent_type = "空格"
        elif tab_lines > 0 and space_lines > 0:
            indent_type = "混合(注意!)"
        else:
            indent_type = "无缩进"
        
        common_indent = 4
        if space_indent_sizes:
            size_counts = Counter(space_indent_sizes)
            common_indent = size_counts.most_common(1)[0][0]
        
        return {
            "indent_type": indent_type,
            "tab_lines": tab_lines,
            "space_lines": space_lines,
            "common_indent_size": common_indent,
            "hint": f"建议使用{'Tab' if tab_lines > space_lines else str(common_indent) + '空格'}作为缩进"
        }

    def _show_invisible_chars(self, line: str) -> str:
        """将不可见字符转换为可见符号"""
        result = []
        for char in line:
            if char == '\t':
                result.append('→')
            elif char == ' ':
                result.append('·')
            else:
                result.append(char)
        return ''.join(result)

    def _normalize_indentation(self, text: str, target_style: str = "preserve") -> str:
        """规范化缩进"""
        if target_style == "preserve":
            return text
        
        lines = text.split('\n')
        result = []
        
        for line in lines:
            if not line.strip():
                result.append(line)
                continue
            
            leading = line[:len(line) - len(line.lstrip())]
            content = line.lstrip()
            
            tab_count = leading.count('\t')
            space_count = len(leading) - tab_count
            
            total_indent = tab_count * 4 + space_count
            
            if target_style == "tab":
                new_leading = '\t' * (total_indent // 4) + ' ' * (total_indent % 4)
            elif target_style == "space4":
                new_leading = ' ' * total_indent
            elif target_style == "space2":
                new_leading = ' ' * (total_indent // 2 * 2)
            else:
                new_leading = leading
            
            result.append(new_leading + content)
        
        return '\n'.join(result)

    def _write_file(self, path: Path, content: str) -> Dict:
        """原子写入文件"""
        path.parent.mkdir(parents=True, exist_ok=True)
        
        checksum = hashlib.md5(content.encode('utf-8')).hexdigest() if self.enable_checksum else None
        
        temp_path = path.with_suffix(path.suffix + '.tmp')
        try:
            with open(temp_path, 'w', encoding='utf-8') as f:
                f.write(content)
            
            os.replace(temp_path, path)
            
            return {
                "success": True, 
                "path": str(path), 
                "size": len(content),
                "checksum": checksum
            }
        except Exception as e:
            if temp_path.exists():
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            raise e


def get_tool_definitions():
    """返回给智能体的工具定义"""
    return [
        {
            "type": "function",
            "function": {
                "name": "code_editor_view",
                "description": "【修改前必做】查看代码文件内容（带行号）。修改代码前必须先查看目标区域。\n\n建议：\n- 默认只显示 500 行，使用 view_range 精确指定范围（如 [10, 60]）\n- 大文件请配合 offset 分页查看\n- 注意文件缩进类型（Tab/空格），替换时会自动适配\n\n返回内容包括：代码行、缩进类型、总行数等。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "文件路径"},
                        "view_range": {"type": "array", "items": {"type": "integer"}, "description": "查看的行号范围 [start, end]，例如 [10, 30]"},
                        "limit": {"type": "integer", "description": "最大返回行数，默认 500"},
                        "offset": {"type": "integer", "description": "跳过的行数，用于分页"},
                        "show_invisible": {"type": "boolean", "description": "显示不可见字符（Tab→, 空格·），用于调试缩进问题"}
                    },
                    "required": ["file_path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_str_replace",
                "description": "【修改代码首选】精准替换代码片段，只改需要改的部分。\n\n使用流程：\n1. 先用 view 查看目标代码\n2. 从查看结果中复制要修改的内容作为 old_str\n3. 修改后的内容作为 new_str\n\n优点：只改目标代码；自动备份可 undo；支持缩进自动适配。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "old_str": {"type": "string", "description": "要替换的旧代码（从view结果复制，确保准确）"},
                        "new_str": {"type": "string", "description": "修改后的新代码"},
                        "replace_all": {"type": "boolean", "description": "是否替换所有匹配项，默认 False"},
                        "auto_indent": {"type": "boolean", "description": "是否自动适配缩进，默认 True"}
                    },
                    "required": ["file_path", "old_str", "new_str"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_write",
                "description": "【谨慎使用】直接写入/覆盖整个文件。会覆盖文件全部内容！仅适用于新建后完整写入或完全重构。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "content": {"type": "string", "description": "要写入的完整内容（会覆盖整个文件）"}
                    },
                    "required": ["file_path", "content"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_create",
                "description": "创建新文件。如果文件已存在则会失败。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "content": {"type": "string"}
                    },
                    "required": ["file_path", "content"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_append",
                "description": "追加内容到文件末尾。比 insert 更方便，无需计算行号。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "content": {"type": "string", "description": "要追加的内容"},
                        "ensure_newline": {"type": "boolean", "description": "是否确保追加前有换行符，默认 True"}
                    },
                    "required": ["file_path", "content"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_insert",
                "description": "在指定行号后插入代码。常用于文件中间插入新函数。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "insert_line": {"type": "integer", "description": "在第几行后面插入，0表示插在最前面"},
                        "content": {"type": "string"}
                    },
                    "required": ["file_path", "insert_line", "content"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_delete",
                "description": "删除文件。删除前会自动备份，支持撤销恢复。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "要删除的文件路径"}
                    },
                    "required": ["file_path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_undo",
                "description": "撤销操作，支持多级撤销。可恢复被修改或删除的文件。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "要撤销的文件路径"},
                        "steps": {"type": "integer", "description": "撤销步数，默认 1"}
                    },
                    "required": ["file_path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_search",
                "description": "在文件中搜索内容（类似 grep）。支持正则表达式和多文件搜索。\n\n⚠️ 注意：\n- 自动跳过二进制文件、大文件（>200KB）、噪声目录（node_modules/venv/.git 等）\n- 单文件最多返回 50 条匹配，全局最多 200 条\n- context_lines 上限为 5\n- 建议尽量缩小范围：使用具体的 file_path、file_pattern（如 *.py）、精确的 pattern",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "搜索模式（正则表达式或普通文本）"},
                        "file_path": {"type": "string", "description": "单个文件路径"},
                        "dir_path": {"type": "string", "description": "目录路径（递归搜索）"},
                        "file_pattern": {"type": "string", "description": "文件名模式，如 *.py，默认 *。强烈建议指定以缩小范围"},
                        "ignore_case": {"type": "boolean", "description": "是否忽略大小写"},
                        "context_lines": {"type": "integer", "description": "显示匹配行前后多少行上下文（最多 5 行）"}
                    },
                    "required": ["pattern"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_find_files",
                "description": "查找匹配模式的文件（类似 glob）。最多返回 500 个结果，自动排除噪声目录。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "文件名模式，如 *.py, **/*.js"},
                        "dir_path": {"type": "string", "description": "搜索起始目录，默认当前目录"}
                    },
                    "required": ["pattern"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_list_dir",
                "description": "列出目录结构。支持递归显示。最多显示 500 项，自动折叠 node_modules 等噪声目录。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dir_path": {"type": "string", "description": "目录路径，默认当前目录"},
                        "recursive": {"type": "boolean", "description": "是否递归列出子目录"},
                        "max_depth": {"type": "integer", "description": "递归最大深度（1-5），默认 3"}
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_get_file_info",
                "description": "获取文件详细信息（大小、行数、修改时间等）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "文件路径"}
                    },
                    "required": ["file_path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_copy_file",
                "description": "复制文件。支持跨目录复制，自动创建目标目录。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source_path": {"type": "string", "description": "源文件路径"},
                        "destination_path": {"type": "string", "description": "目标文件路径"},
                        "overwrite": {"type": "boolean", "description": "是否覆盖已存在的目标文件，默认 false"}
                    },
                    "required": ["source_path", "destination_path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_move_file",
                "description": "移动或重命名文件。支持跨目录移动，自动创建目标目录。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source_path": {"type": "string", "description": "源文件路径"},
                        "destination_path": {"type": "string", "description": "目标文件路径"},
                        "overwrite": {"type": "boolean", "description": "是否覆盖已存在的目标文件，默认 false"}
                    },
                    "required": ["source_path", "destination_path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_create_directory",
                "description": "创建目录（类似 mkdir -p）。支持创建多级目录。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dir_path": {"type": "string", "description": "要创建的目录路径"}
                    },
                    "required": ["dir_path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_get_tree_view",
                "description": "生成可视化目录树（树形字符画格式）。快速了解目录结构布局。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dir_path": {"type": "string", "description": "目标目录路径，默认为当前目录"},
                        "max_depth": {"type": "integer", "description": "最大递归深度（1-5），默认 2"},
                        "max_items": {"type": "integer", "description": "最大显示项目数，默认 100"},
                        "show_size": {"type": "boolean", "description": "是否显示文件大小，默认 true"},
                        "show_emojis": {"type": "boolean", "description": "是否使用 emoji 图标，默认 true"}
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_quick_scan",
                "description": "快速扫描目录结构（轻量级）。返回文件类型统计、目录分层等摘要信息，适合快速了解项目布局。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dir_path": {"type": "string", "description": "目标目录，默认为当前目录"},
                        "max_depth": {"type": "integer", "description": "最大扫描深度，默认 3"}
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_read_file_chunked",
                "description": "顺序分块读取文本文件。用于读取大文件，每次返回一个不重叠的块，支持编码自动回退。\n\n使用方式：\n1. 第一次调用传入 file_path 和可选的 chunk_size\n2. 后续调用使用返回的 next_chunk_index 继续读取\n3. 当 has_more=false 时表示已到文件末尾",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "要读取的文件路径"},
                        "chunk_index": {"type": "integer", "description": "块索引，从 0 开始。继续读取时使用返回的 next_chunk_index"},
                        "chunk_size": {"type": "integer", "description": "每块字符数，最大 20000，默认自动选择"},
                        "encoding": {"type": "string", "description": "编码，默认 utf-8，失败自动回退"}
                    },
                    "required": ["file_path"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_list_history",
                "description": "列出文件的操作历史记录。不传 file_path 时返回所有历史文件列表。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string", "description": "文件路径，可选。传了则只返回该文件的历史"},
                        "limit": {"type": "integer", "description": "最大返回条数，默认 20，最大 200"}
                    },
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "code_editor_find_directory",
                "description": "快速查找目录（专门用于定位文件夹位置）。支持模糊匹配和精确匹配。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "dir_name": {"type": "string", "description": "目录名称（或名称的一部分）"},
                        "start_dir": {"type": "string", "description": "起始搜索目录，默认为当前目录"},
                        "exact_match": {"type": "boolean", "description": "是否精确匹配，false 表示模糊匹配"},
                        "limit": {"type": "integer", "description": "最大返回数量，默认 20"}
                    },
                    "required": ["dir_name"]
                }
            },
        },
    ]

# ============================================================================
# 代码导航器（CodeNavigator） — 合并自 Tools/code_navigator.py
# ============================================================================

@dataclass
class CodeLocation:
    """代码位置"""
    file_path: str
    start_line: int
    end_line: int

    def __str__(self):
        return f"{self.file_path}:{self.start_line}-{self.end_line}"


@dataclass
class ClassInfo:
    """类信息"""
    name: str
    start_line: int
    end_line: int
    docstring: Optional[str] = None
    methods: List[str] = field(default_factory=list)
    base_classes: List[str] = field(default_factory=list)

    def to_summary(self) -> str:
        """生成简要描述"""
        base = f"类 {self.name}"
        if self.base_classes:
            base += f"({', '.join(self.base_classes)})"
        base += f" (行{self.start_line}-{self.end_line})"
        if self.docstring:
            first_line = self.docstring.split('\n')[0][:50]
            base += f"\n  📝 {first_line}"
        if self.methods:
            base += f"\n  🔧 方法: {', '.join(self.methods[:5])}"
            if len(self.methods) > 5:
                base += f" ... (+{len(self.methods)-5})"
        return base


@dataclass
class FunctionInfo:
    """函数信息"""
    name: str
    start_line: int
    end_line: int
    parameters: List[str] = field(default_factory=list)
    docstring: Optional[str] = None
    is_async: bool = False
    is_method: bool = False
    class_name: Optional[str] = None

    def to_summary(self) -> str:
        """生成简要描述"""
        prefix = "async " if self.is_async else ""
        func_type = "方法" if self.is_method else "函数"
        params = ", ".join(self.parameters) if self.parameters else ""
        summary = f"{prefix}{func_type} {self.name}({params}) (行{self.start_line}-{self.end_line})"
        if self.docstring:
            first_line = self.docstring.split('\n')[0][:50]
            summary += f"\n  📝 {first_line}"
        return summary


@dataclass
class ImportInfo:
    """导入信息"""
    module: str
    names: List[str] = field(default_factory=list)
    alias: Optional[str] = None
    line: int = 0


@dataclass
class CodeStructureCard:
    """代码结构卡片"""
    file_path: str
    file_size: int
    total_lines: int
    language: str
    classes: List[ClassInfo] = field(default_factory=list)
    functions: List[FunctionInfo] = field(default_factory=list)
    imports: List[ImportInfo] = field(default_factory=list)
    entry_points: List[str] = field(default_factory=list)
    constants: Dict[str, int] = field(default_factory=dict)
    scan_time: float = 0.0

    def to_map(self) -> str:
        """生成结构地图（< 1KB）"""
        lines = []
        lines.append(f"📁 文件: {Path(self.file_path).name}")
        lines.append(f"   大小: {self.file_size/1024:.1f}KB | 行数: {self.total_lines}")
        lines.append(f"   语言: {self.language}")
        lines.append(f"⏱️  扫描耗时: {self.scan_time*1000:.1f}ms")
        lines.append("")

        if self.entry_points:
            lines.append("🎯 入口点:")
            for ep in self.entry_points:
                lines.append(f"   • {ep}")
            lines.append("")

        if self.classes:
            lines.append("🏗️  类结构:")
            for cls in self.classes:
                lines.append(f"   ├── {cls.name} (行{cls.start_line})")
                for method in cls.methods[:3]:
                    lines.append(f"   │   • {method}")
                if len(cls.methods) > 3:
                    lines.append(f"   │   ... (+{len(cls.methods)-3})")
            lines.append("")

        standalone_funcs = [f for f in self.functions if not f.is_method]
        if standalone_funcs:
            lines.append("🔧 关键函数:")
            for func in standalone_funcs[:5]:
                async_mark = "async " if func.is_async else ""
                lines.append(f"   • {async_mark}{func.name}() (行{func.start_line})")
            if len(standalone_funcs) > 5:
                lines.append(f"   ... (+{len(standalone_funcs)-5})")
            lines.append("")

        if self.imports:
            lines.append(f"📦 导入模块: {len(self.imports)}个")

        read_time = len(self.functions) * 0.5 + len(self.classes) * 1.0
        lines.append(f"\n📖 预计阅读: {read_time:.1f}分钟 → 使用导航器: 30秒")

        return '\n'.join(lines)

    def find_function(self, name: str) -> Optional[FunctionInfo]:
        """查找函数"""
        for func in self.functions:
            if func.name == name:
                return func
        return None

    def find_class(self, name: str) -> Optional[ClassInfo]:
        """查找类"""
        for cls in self.classes:
            if cls.name == name:
                return cls
        return None


@dataclass
class FlowNode:
    """信息流节点"""
    id: str
    name: str
    kind: str
    file_path: str
    start_line: int = 0
    end_line: int = 0
    signature: str = ""
    doc_summary: str = ""


@dataclass
class FlowEdge:
    """信息流边"""
    source: str
    target: str
    relation: str
    label: str = ""


class CodeNavigator:
    """代码导航器"""

    def __init__(self):
        self.cache: Dict[str, CodeStructureCard] = {}
        self.file_contents: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------------
    # 核心功能1：闪电扫描
    # ------------------------------------------------------------------------

    def scan(self, file_path: str, use_cache: bool = True) -> CodeStructureCard:
        """闪电扫描 - 5秒内理解文件结构"""
        if use_cache and file_path in self.cache:
            return self.cache[file_path]

        start_time = time.time()

        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            lines = content.split('\n')

        self.file_contents[file_path] = lines
        language = self._detect_language(file_path)

        card = CodeStructureCard(
            file_path=file_path,
            file_size=os.path.getsize(file_path),
            total_lines=len(lines),
            language=language
        )

        if language == 'python':
            self._scan_python(content, lines, card)
        else:
            self._scan_generic(content, lines, card)

        card.entry_points = self._find_entry_points(lines)
        card.scan_time = time.time() - start_time
        self.cache[file_path] = card
        return card

    def _detect_language(self, file_path: str) -> str:
        ext = Path(file_path).suffix.lower()
        lang_map = {
            '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
            '.java': 'java', '.cpp': 'cpp', '.c': 'c', '.go': 'go', '.rs': 'rust',
        }
        return lang_map.get(ext, 'unknown')

    def _scan_python(self, content: str, lines: List[str], card: CodeStructureCard):
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        card.imports.append(ImportInfo(module=alias.name, alias=alias.asname, line=node.lineno))
                elif isinstance(node, ast.ImportFrom):
                    names = [alias.name for alias in node.names]
                    card.imports.append(ImportInfo(module=node.module or '', names=names, line=node.lineno))

            for node in ast.iter_child_nodes(tree):
                if isinstance(node, ast.ClassDef):
                    bases = [base.id for base in node.bases if isinstance(base, ast.Name)]
                    methods = [item.name for item in node.body if isinstance(item, ast.FunctionDef)]
                    docstring = ast.get_docstring(node)
                    card.classes.append(ClassInfo(name=node.name, start_line=node.lineno, end_line=node.end_lineno or node.lineno, docstring=docstring, methods=methods, base_classes=bases))
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            is_async = isinstance(item, ast.AsyncFunctionDef)
                            params = [arg.arg for arg in item.args.args]
                            method_docstring = ast.get_docstring(item)
                            card.functions.append(FunctionInfo(name=item.name, start_line=item.lineno, end_line=item.end_lineno or item.lineno, parameters=params, docstring=method_docstring, is_async=is_async, is_method=True, class_name=node.name))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    is_async = isinstance(node, ast.AsyncFunctionDef)
                    params = [arg.arg for arg in node.args.args]
                    docstring = ast.get_docstring(node)
                    card.functions.append(FunctionInfo(name=node.name, start_line=node.lineno, end_line=node.end_lineno or node.lineno, parameters=params, docstring=docstring, is_async=is_async, is_method=False))
        except SyntaxError:
            self._scan_generic(content, lines, card)

    def _scan_generic(self, content: str, lines: List[str], card: CodeStructureCard):
        class_pattern = r'^\s*class\s+(\w+).*?:'
        for i, line in enumerate(lines, 1):
            match = re.match(class_pattern, line)
            if match:
                card.classes.append(ClassInfo(name=match.group(1), start_line=i, end_line=self._find_block_end(lines, i)))
        func_pattern = r'^\s*(async\s+)?def\s+(\w+)\s*\((.*?)\):'
        for i, line in enumerate(lines, 1):
            match = re.match(func_pattern, line)
            if match:
                is_async = match.group(1) is not None
                name = match.group(2)
                params = [p.strip() for p in match.group(3).split(',') if p.strip()]
                card.functions.append(FunctionInfo(name=name, start_line=i, end_line=self._find_block_end(lines, i), parameters=params, is_async=is_async))

    def _find_block_end(self, lines: List[str], start_line: int) -> int:
        if start_line >= len(lines):
            return start_line
        start_indent = len(lines[start_line - 1]) - len(lines[start_line - 1].lstrip())
        for i in range(start_line, len(lines)):
            line = lines[i]
            if line.strip():
                current_indent = len(line) - len(line.lstrip())
                if current_indent <= start_indent:
                    return i
        return len(lines)

    def _find_entry_points(self, lines: List[str]) -> List[str]:
        entry_points = []
        for i, line in enumerate(lines, 1):
            if '__name__' in line and '__main__' in line:
                entry_points.append(f"主入口 (行{i})")
            if re.match(r'^\s*def\s+main\s*\(', line):
                entry_points.append(f"main() (行{i})")
            if re.match(r'^\s*async\s+def\s+main\s*\(', line):
                entry_points.append(f"async main() (行{i})")
        return entry_points

    # ------------------------------------------------------------------------
    # 核心功能2：精准导航
    # ------------------------------------------------------------------------

    def search(self, pattern: str, file_path: Optional[str] = None, search_type: str = "name") -> List[Tuple[str, int, str]]:
        results = []
        files = [file_path] if file_path else list(self.cache.keys())
        for fp in files:
            if fp not in self.file_contents:
                continue
            lines = self.file_contents[fp]
            if search_type == "name":
                results.extend(self._search_by_name(fp, lines, pattern))
            elif search_type == "regex":
                results.extend(self._search_by_regex(fp, lines, pattern))
            elif search_type == "functionality":
                results.extend(self._search_by_functionality(fp, lines, pattern))
            elif search_type == "text":
                results.extend(self._search_by_text(fp, lines, pattern))
            else:
                results.extend(self._search_by_functionality(fp, lines, pattern))
        return results

    def _search_by_name(self, file_path: str, lines: List[str], name: str) -> List[Tuple[str, int, str]]:
        results = []
        func_pattern = rf'^\s*(async\s+)?def\s+{re.escape(name)}\s*\('
        for i, line in enumerate(lines, 1):
            if re.match(func_pattern, line):
                results.append((file_path, i, f"函数定义: {line.strip()[:60]}"))
        class_pattern = rf'^\s*class\s+{re.escape(name)}\s*[:\(]'
        for i, line in enumerate(lines, 1):
            if re.match(class_pattern, line):
                results.append((file_path, i, f"类定义: {line.strip()[:60]}"))
        for i, line in enumerate(lines, 1):
            if name in line and not re.match(func_pattern, line) and not re.match(class_pattern, line):
                if not line.strip().startswith('#'):
                    results.append((file_path, i, f"调用: {line.strip()[:60]}"))
        return results

    def _search_by_regex(self, file_path: str, lines: List[str], pattern: str) -> List[Tuple[str, int, str]]:
        results = []
        try:
            regex = re.compile(pattern)
            for i, line in enumerate(lines, 1):
                if regex.search(line):
                    results.append((file_path, i, line.strip()[:80]))
        except re.error:
            pass
        return results

    def _search_by_functionality(self, file_path: str, lines: List[str], keyword: str) -> List[Tuple[str, int, str]]:
        results = []
        keyword_lower = keyword.lower()
        for i, line in enumerate(lines, 1):
            if line.strip().startswith('#') and keyword_lower in line.lower():
                results.append((file_path, i, f"注释: {line.strip()[:60]}"))
            if '"""' in line or "'''" in line:
                if keyword_lower in line.lower():
                    results.append((file_path, i, f"文档: {line.strip()[:60]}"))
            if keyword_lower in line.lower():
                stripped = line.strip()
                if not stripped.startswith('#'):
                    results.append((file_path, i, f"代码: {stripped[:60]}"))
        return results

    def _search_by_text(self, file_path: str, lines: List[str], pattern: str) -> List[Tuple[str, int, str]]:
        results = []
        pattern_lower = pattern.lower()
        for i, line in enumerate(lines, 1):
            if pattern_lower in line.lower():
                results.append((file_path, i, f"匹配: {line.strip()[:80]}"))
        return results

    def jump_to(self, file_path: str, location: str) -> Tuple[int, int]:
        range_match = re.match(r'(\d+)-(\d+)', location)
        if range_match:
            return int(range_match.group(1)), int(range_match.group(2))
        if location.isdigit():
            line_num = int(location)
            return line_num, line_num
        card = self.cache.get(file_path)
        if not card:
            card = self.scan(file_path)
        func = card.find_function(location)
        if func:
            return func.start_line, func.end_line
        cls = card.find_class(location)
        if cls:
            return cls.start_line, cls.end_line
        raise ValueError(f"未找到位置: {location}")

    def view(self, file_path: str, start_line: int, end_line: int, context_lines: int = 5) -> str:
        if file_path not in self.file_contents:
            with open(file_path, 'r', encoding='utf-8') as f:
                self.file_contents[file_path] = f.read().split('\n')
        lines = self.file_contents[file_path]
        actual_start = max(1, start_line - context_lines)
        actual_end = min(len(lines), end_line + context_lines)
        result = [f"📄 {Path(file_path).name}:{start_line}-{end_line}", ""]
        for i in range(actual_start - 1, actual_end):
            line_num = i + 1
            prefix = "→" if start_line <= line_num <= end_line else " "
            result.append(f"{prefix} {line_num:4d} | {lines[i]}")
        return '\n'.join(result)

    # ------------------------------------------------------------------------
    # 核心功能3：代码分析
    # ------------------------------------------------------------------------

    def analyze_dependencies(self, file_path: str) -> Dict[str, List[str]]:
        card = self.cache.get(file_path)
        if not card:
            card = self.scan(file_path)
        imports = [imp.module for imp in card.imports]
        imported_by = []
        for other_path in self.cache:
            if other_path == file_path:
                continue
            other_card = self.cache[other_path]
            module_name = Path(file_path).stem
            for imp in other_card.imports:
                if module_name in imp.module or imp.module.endswith(module_name):
                    imported_by.append(other_path)
        return {"imports": imports, "imported_by": list(set(imported_by))}

    def find_callers(self, file_path: str, function_name: str) -> List[Tuple[str, int]]:
        callers = []
        for other_path in self.cache:
            if other_path == file_path:
                continue
            if other_path not in self.file_contents:
                with open(other_path, 'r', encoding='utf-8') as f:
                    self.file_contents[other_path] = f.read().split('\n')
            lines = self.file_contents[other_path]
            for i, line in enumerate(lines, 1):
                if function_name in line:
                    if not re.match(rf'^\s*(async\s+)?def\s+{function_name}\s*\(', line):
                        callers.append((other_path, i))
        return callers

    def find_callees(self, file_path: str, start_line: int, end_line: int) -> List[str]:
        if file_path not in self.file_contents:
            with open(file_path, 'r', encoding='utf-8') as f:
                self.file_contents[file_path] = f.read().split('\n')
        lines = self.file_contents[file_path]
        code_block = '\n'.join(lines[start_line - 1:end_line])
        callees = set()
        call_pattern = r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\('
        for match in re.finditer(call_pattern, code_block):
            func_name = match.group(1)
            if func_name not in ['if', 'for', 'while', 'with', 'print', 'len', 'str', 'int', 'list', 'dict']:
                callees.add(func_name)
        return sorted(list(callees))

    def estimate_complexity(self, file_path: str, start_line: int, end_line: int) -> Dict[str, int]:
        if file_path not in self.file_contents:
            with open(file_path, 'r', encoding='utf-8') as f:
                self.file_contents[file_path] = f.read().split('\n')
        lines = self.file_contents[file_path]
        code_block = '\n'.join(lines[start_line - 1:end_line])
        return {
            "lines": end_line - start_line + 1,
            "if_statements": len(re.findall(r'\bif\b', code_block)),
            "for_loops": len(re.findall(r'\bfor\b', code_block)),
            "while_loops": len(re.findall(r'\bwhile\b', code_block)),
            "try_blocks": len(re.findall(r'\btry\b', code_block)),
            "nesting_depth": self._estimate_nesting(lines, start_line, end_line),
        }

    def _estimate_nesting(self, lines: List[str], start_line: int, end_line: int) -> int:
        max_indent = 0
        for i in range(start_line - 1, end_line):
            if lines[i].strip():
                indent = len(lines[i]) - len(lines[i].lstrip())
                max_indent = max(max_indent, indent)
        return max_indent // 4

    # ------------------------------------------------------------------------
    # 核心功能4：项目级导航
    # ------------------------------------------------------------------------

    _NOISE_DIRS_EXACT = {
        '.git', '__pycache__', 'node_modules', '.venv', 'venv', 'env',
        '.tox', '.eggs', 'build', 'dist', '.mypy_cache', '.pytest_cache',
        '.ruff_cache', '__MACOSX', '.claude', '.idea', '.vscode',
        'egg-info', 'logs', '.cache', 'site-packages', '.ipynb_checkpoints',
        'wheelhouse', 'htmlcov', '.coverage', '.pixi',
    }

    def _is_noise_dir(self, dirname: str) -> bool:
        if dirname in self._NOISE_DIRS_EXACT:
            return True
        if dirname.startswith('._'):
            return True
        d_lower = dirname.lower()
        if d_lower.startswith('venv') or d_lower.startswith('.venv'):
            return True
        if d_lower.startswith('__pycache__'):
            return True
        return False

    def scan_project(self, project_path: str, max_depth: int = 5) -> Dict[str, CodeStructureCard]:
        project_cards = {}
        for root, dirs, files in os.walk(project_path):
            if max_depth > 0:
                rel_root = os.path.relpath(root, project_path)
                depth = 0 if rel_root == '.' else rel_root.count(os.sep) + 1
                if depth > max_depth:
                    dirs[:] = []
                    continue
            dirs[:] = [d for d in dirs if not self._is_noise_dir(d)]
            for file in files:
                if file.endswith('.py'):
                    file_path = os.path.join(root, file)
                    if file.startswith('._') or file.endswith('.pyc'):
                        continue
                    try:
                        card = self.scan(file_path)
                        project_cards[file_path] = card
                    except Exception as e:
                        print(f"⚠️  扫描失败 {file_path}: {e}")
        return project_cards

    def generate_project_map(self, project_path: str, max_depth: int = 4) -> str:
        project_path = os.path.abspath(project_path)
        project_name = os.path.basename(project_path) or project_path
        tree_map: Dict[str, dict] = {}
        file_sizes: Dict[str, int] = {}
        stats = {'total_dirs': 0, 'total_files': 0, 'py_files': 0, 'py_lines': 0, 'py_classes': 0, 'py_funcs': 0, 'skipped': 0, 'errors': 0}

        for root, dirs, files in os.walk(project_path):
            rel_root = os.path.relpath(root, project_path)
            if rel_root == '.':
                rel_root = ''
            depth = 0 if not rel_root else rel_root.count(os.sep) + 1
            if depth > max_depth:
                dirs[:] = []
                continue
            stats['total_dirs'] += 1
            kept_dirs = [d for d in dirs if not self._is_noise_dir(d)]
            stats['skipped'] += len(dirs) - len(kept_dirs)
            dirs[:] = kept_dirs
            kept_files = []
            for f in sorted(files):
                if f.startswith('._') or f == '.DS_Store':
                    stats['skipped'] += 1
                    continue
                if f.endswith(('.pyc', '.pyo', '.pyd')):
                    stats['skipped'] += 1
                    continue
                kept_files.append(f)
                stats['total_files'] += 1
                full_path = os.path.join(root, f)
                try:
                    file_sizes[full_path] = os.path.getsize(full_path)
                except OSError:
                    file_sizes[full_path] = 0
                if f.endswith('.py'):
                    try:
                        fsize = file_sizes.get(full_path, 0)
                        if fsize < 500_000:
                            with open(full_path, 'r', encoding='utf-8', errors='replace') as fh:
                                content = fh.read()
                            stats['py_files'] += 1
                            stats['py_lines'] += content.count('\n') + (0 if content.endswith('\n') else 1)
                            stats['py_classes'] += len(re.findall(r'^\s*class\s+(\w+)', content, re.MULTILINE))
                            stats['py_funcs'] += len(re.findall(r'^\s*(?:async\s+)?def\s+(\w+)', content, re.MULTILINE))
                    except Exception:
                        stats['errors'] += 1
            tree_map[rel_root] = {'dirs': sorted(kept_dirs), 'files': kept_files}

        lines = []
        lines.append(f"📁 {project_name}/  ({stats['total_files']} files, {stats['total_dirs']} dirs)")
        if stats['skipped']:
            lines.append(f"   ⊘ 已跳过 {stats['skipped']} 个噪声目录/文件")
        lines.append("")

        ICON_MAP = {
            '.py': '🐍', '.md': '📝', '.txt': '📝', '.rst': '📝',
            '.json': '⚙️', '.yaml': '⚙️', '.yml': '⚙️', '.toml': '⚙️',
            '.ini': '⚙️', '.cfg': '⚙️', '.html': '🌐', '.htm': '🌐',
            '.css': '🎨', '.scss': '🎨', '.less': '🎨',
            '.js': '📜', '.ts': '📜', '.jsx': '📜', '.tsx': '📜',
            '.png': '🖼️', '.jpg': '🖼️', '.jpeg': '🖼️', '.gif': '🖼️', '.svg': '🖼️',
            '.mp3': '🎵', '.wav': '🎵', '.ogg': '🎵',
            '.mp4': '🎬', '.avi': '🎬', '.mov': '🎬',
            '.zip': '📦', '.tar': '📦', '.gz': '📦', '.7z': '📦',
            '.log': '📋', '.sh': '⚡', '.bat': '⚡', '.ps1': '⚡',
            '.pdf': '📕', '.doc': '📘', '.docx': '📘',
        }

        def _icon(ext: str) -> str:
            return ICON_MAP.get(ext, '📄')

        def _render(rel_dir: str, prefix: str):
            node = tree_map.get(rel_dir)
            if not node:
                return
            items = node['dirs'] + node['files']
            for i, item in enumerate(items):
                is_last = (i == len(items) - 1)
                connector = '└── ' if is_last else '├── '
                child_prefix_line = '    ' if is_last else '│   '
                new_prefix = prefix + child_prefix_line
                child_rel = os.path.join(rel_dir, item) if rel_dir else item
                if item in node['dirs']:
                    sub_node = tree_map.get(child_rel, {})
                    sub_file_count = len(sub_node.get('files', []))
                    lines.append(f"{prefix}{connector}📁 {item}/  ({sub_file_count} files)")
                    child_has_kids = any(k.startswith(child_rel + os.sep) for k in tree_map)
                    current_depth = child_rel.count(os.sep) + 1
                    if current_depth < max_depth and child_has_kids:
                        _render(child_rel, new_prefix)
                    elif child_has_kids:
                        lines.append(f"{new_prefix}└── ... (max depth)")
                else:
                    ext = os.path.splitext(item)[1].lower()
                    full_path = os.path.join(project_path, child_rel)
                    fsize = file_sizes.get(full_path, 0) / 1024
                    line = f"{prefix}{connector}{_icon(ext)} {item}  ({fsize:.1f} KB)"
                    if ext == '.py' and file_sizes.get(full_path, 0) < 500_000:
                        try:
                            with open(full_path, 'r', encoding='utf-8', errors='replace') as fh:
                                content = fh.read()
                            classes = re.findall(r'^\s*class\s+(\w+)', content, re.MULTILINE)
                            funcs = re.findall(r'^\s*(?:async\s+)?def\s+(\w+)', content, re.MULTILINE)
                            parts = []
                            if classes:
                                parts.append(f"{len(classes)} classes")
                            if funcs:
                                parts.append(f"{len(funcs)} funcs")
                            if parts:
                                line += f"  [{', '.join(parts)}]"
                        except Exception:
                            pass
                    lines.append(line)

        _render('', '')

        lines.append("")
        if stats['py_files']:
            lines.append(f"📊 Python: {stats['py_files']} files, {stats['py_lines']} lines, {stats['py_classes']} classes, {stats['py_funcs']} functions")
        if stats['errors']:
            lines.append(f"⚠️  {stats['errors']} files skipped due to read errors")

        return '\n'.join(lines)


class CodeFlowAnalyzer:
    """代码信息流分析引擎"""

    PYTHON_BUILTINS = {
        'print', 'len', 'str', 'int', 'float', 'list', 'dict', 'set',
        'tuple', 'bool', 'type', 'range', 'enumerate', 'zip', 'map',
        'filter', 'sorted', 'reversed', 'min', 'max', 'sum', 'abs',
        'round', 'open', 'isinstance', 'hasattr', 'getattr', 'setattr',
        'super', 'any', 'all', 'iter', 'next', 'input',
        'issubclass', 'callable', 'id', 'hex', 'oct', 'bin', 'chr',
        'ord', 'repr', 'format', 'vars', 'dir', 'locals', 'globals',
        'staticmethod', 'classmethod', 'property', '__import__',
        'True', 'False', 'None', 'self', 'cls',
        'append', 'extend', 'pop', 'get', 'keys', 'values', 'items',
        'update', 'join', 'split', 'strip', 'replace', 'startswith',
        'endswith', 'encode', 'decode', 'read', 'write', 'close',
        'add', 'remove', 'clear', 'copy', 'sort', 'reverse',
    }

    def __init__(self, navigator: CodeNavigator):
        self.nav = navigator

    def _ensure_scanned(self, file_path: str):
        if file_path not in self.nav.cache:
            self.nav.scan(file_path)

    def _get_internal_functions(self, file_path: str) -> Set[str]:
        self._ensure_scanned(file_path)
        card = self.nav.cache[file_path]
        return {f.name for f in card.functions}

    def _get_internal_classes(self, file_path: str) -> Set[str]:
        self._ensure_scanned(file_path)
        card = self.nav.cache[file_path]
        return {c.name for c in card.classes}

    def _find_file_entries(self, file_path: str) -> List[str]:
        self._ensure_scanned(file_path)
        card = self.nav.cache[file_path]
        entries = []
        if file_path in self.nav.file_contents:
            lines = self.nav.file_contents[file_path]
            in_main = False
            main_indent = 0
            for i, line in enumerate(lines):
                if '__name__' in line and '__main__' in line:
                    in_main = True
                    main_indent = len(line) - len(line.lstrip())
                    continue
                if in_main:
                    if line.strip() and (len(line) - len(line.lstrip())) <= main_indent:
                        in_main = False
                        continue
                    call_match = re.search(r'\b([a-zA-Z_]\w*)\s*\(', line)
                    if call_match:
                        func_name = call_match.group(1)
                        if func_name in self._get_internal_functions(file_path) and func_name not in entries:
                            entries.append(func_name)
        if 'main' in self._get_internal_functions(file_path) and 'main' not in entries:
            entries.append('main')
        if not entries:
            top_funcs = [f for f in card.functions if not f.is_method]
            if top_funcs:
                for keyword in ['run', 'execute', 'build', 'main', 'process', 'handle']:
                    for f in top_funcs:
                        if keyword in f.name.lower():
                            entries.append(f.name)
                            break
                    if entries:
                        break
                if not entries:
                    longest = max(top_funcs, key=lambda f: f.end_line - f.start_line)
                    entries.append(longest.name)
        return entries

    def _render_call_tree(self, func_name: str, file_path: str, internal_funcs: Set[str], depth: int, max_depth: int, visited: Set[str], prefix: str) -> str:
        if depth >= max_depth:
            return f"{prefix}... (达到最大深度)\n"
        node_key = f"{file_path}:{func_name}"
        if node_key in visited:
            return f"{prefix}↻ {func_name}() (递归/已访问)\n"
        visited.add(node_key)
        result = ""
        card = self.nav.cache.get(file_path)
        func = card.find_function(func_name) if card else None
        if depth > 0:
            sig = f"({', '.join(func.parameters)})" if func and func.parameters else "()"
            doc = ""
            if func and func.docstring:
                doc = f" — {func.docstring.split(chr(10))[0][:40]}"
            result += f"{prefix}{func_name}{sig}{doc}\n"
        if func:
            callees = self.nav.find_callees(file_path, func.start_line, func.end_line)
            children = [c for c in callees if c in internal_funcs and c != func_name]
            children = list(dict.fromkeys(children))
            for i, child in enumerate(children):
                is_last = (i == len(children) - 1)
                child_prefix = prefix + ("└── " if is_last else "├── ")
                next_prefix = prefix + ("    " if is_last else "│   ")
                result += self._render_call_tree(child, file_path, internal_funcs, depth + 1, max_depth, visited, next_prefix)
        return result

    def flow_diagram(self, file_path: str, max_depth: int = 5) -> str:
        self._ensure_scanned(file_path)
        card = self.nav.cache[file_path]
        internal_funcs = self._get_internal_functions(file_path)
        lines = []
        lines.append(f"📊 信息流图: {Path(file_path).name}")
        lines.append(f"   {card.total_lines}行 | {len(card.classes)}个类 | {len(card.functions)}个函数")
        lines.append("")
        entries = self._find_file_entries(file_path)
        if not entries:
            lines.append("⚠️  未找到入口点或顶层函数")
            return '\n'.join(lines)
        for entry in entries:
            visited: Set[str] = set()
            func = card.find_function(entry)
            sig = f"({', '.join(func.parameters)})" if func and func.parameters else "()"
            lines.append(f"🎯 入口: {entry}{sig}")
            if func and func.docstring:
                lines.append(f"   📝 {func.docstring.split(chr(10))[0][:60]}")
            tree = self._render_call_tree(entry, file_path, internal_funcs, 0, max_depth, visited, "   ")
            if tree.strip():
                lines.append(tree)
            lines.append("")
        external_calls = self._collect_external_calls(file_path)
        if external_calls:
            lines.append("📦 外部依赖:")
            for ext in sorted(external_calls)[:15]:
                lines.append(f"   • {ext}()")
            if len(external_calls) > 15:
                lines.append(f"   ... 还有 {len(external_calls)-15} 个")
        return '\n'.join(lines)

    def _collect_external_calls(self, file_path: str) -> Set[str]:
        self._ensure_scanned(file_path)
        card = self.nav.cache[file_path]
        internal_funcs = self._get_internal_functions(file_path)
        external: Set[str] = set()
        for func in card.functions:
            callees = self.nav.find_callees(file_path, func.start_line, func.end_line)
            for c in callees:
                if c not in internal_funcs and c not in self.PYTHON_BUILTINS:
                    external.add(c)
        return external

    def project_flow(self, project_path: str, max_files: int = 20) -> str:
        cards = self.nav.scan_project(project_path)
        if not cards:
            return f"⚠️  未在 {project_path} 中找到 Python 文件"
        lines = []
        lines.append(f"📊 项目信息流: {Path(project_path).name}")
        lines.append(f"   {len(cards)}个Python文件")
        lines.append("")
        entry_files = [fp for fp, card in cards.items() if card.entry_points]
        if entry_files:
            lines.append("🎯 入口文件:")
            for ef in entry_files:
                rel = os.path.relpath(ef, project_path)
                eps = cards[ef].entry_points
                lines.append(f"   📄 {rel}")
                for ep in eps:
                    lines.append(f"      └── {ep}")
            lines.append("")
        lines.append("🔗 模块依赖关系:")
        dep_graph: Dict[str, List[str]] = {}
        for fp, card in cards.items():
            rel = os.path.relpath(fp, project_path)
            deps = []
            for imp in card.imports:
                imp_parts = imp.module.split('.')
                for other_fp in cards:
                    if other_fp == fp:
                        continue
                    other_stem = Path(other_fp).stem
                    if other_stem in imp_parts or imp.module.endswith(other_stem):
                        if other_rel := os.path.relpath(other_fp, project_path):
                            if other_rel not in deps:
                                deps.append(other_rel)
            if deps:
                dep_graph[rel] = deps
        if dep_graph:
            for file_rel, deps in sorted(dep_graph.items())[:max_files]:
                lines.append(f"   📄 {file_rel}")
                for dep in deps[:5]:
                    lines.append(f"      ├── import → {dep}")
                if len(deps) > 5:
                    lines.append(f"      └── ... +{len(deps)-5} more")
        else:
            lines.append("   (无项目内模块依赖)")
        lines.append("")
        lines.append("📞 跨文件调用热点 (Top 10):")
        call_counts: Dict[str, int] = defaultdict(int)
        for fp in cards:
            card = cards[fp]
            for func in card.functions:
                callees = self.nav.find_callees(fp, func.start_line, func.end_line)
                for callee in callees:
                    if callee not in self.PYTHON_BUILTINS:
                        call_counts[callee] += 1
        top_calls = sorted(call_counts.items(), key=lambda x: x[1], reverse=True)[:10]
        if top_calls:
            for func_name, count in top_calls:
                lines.append(f"   • {func_name}() — 被调用 {count} 次")
        else:
            lines.append("   (未检测到显著调用关系)")
        return '\n'.join(lines)

    def _trace_call_chain_down(self, file_path: str, func_name: str, max_depth: int, visited: Set[str], depth: int) -> str:
        if depth >= max_depth:
            return "   " * (depth + 1) + "... (达到最大深度)\n"
        key = f"{file_path}:{func_name}"
        if key in visited:
            return "   " * (depth + 1) + f"↻ {func_name}() (递归)\n"
        visited.add(key)
        result = ""
        self._ensure_scanned(file_path)
        card = self.nav.cache[file_path]
        func = card.find_function(func_name)
        if not func:
            return result
        if depth > 0:
            result += "   " * (depth + 1) + f"├── {func_name}()\n"
        callees = self.nav.find_callees(file_path, func.start_line, func.end_line)
        internal_funcs = self._get_internal_functions(file_path)
        children = [c for c in callees if c in internal_funcs and c != func_name]
        children = list(dict.fromkeys(children))
        for child in children:
            result += self._trace_call_chain_down(file_path, child, max_depth, visited, depth + 1)
        return result

    def _trace_call_chain_up(self, file_path: str, func_name: str, max_depth: int) -> str:
        self._ensure_scanned(file_path)
        card = self.nav.cache[file_path]
        callers = []
        for func in card.functions:
            if func.name == func_name:
                continue
            callees = self.nav.find_callees(file_path, func.start_line, func.end_line)
            if func_name in callees:
                callers.append((func.name, func.start_line))
        result = ""
        for caller, line in callers[:max_depth]:
            result += f"   ├── {caller}() — 行{line}\n"
        ext_callers = self.nav.find_callers(file_path, func_name)
        if ext_callers:
            for fp, line in ext_callers[:max_depth]:
                result += f"   ├── {Path(fp).name}:{line}\n"
        return result

    def _extract_return_info(self, file_path: str, start_line: int, end_line: int) -> List[str]:
        if file_path not in self.nav.file_contents:
            return []
        lines = self.nav.file_contents[file_path]
        returns = []
        for i in range(start_line - 1, min(end_line, len(lines))):
            line = lines[i].strip()
            match = re.match(r'return\s+(.+)', line)
            if match:
                ret_val = match.group(1).strip()
                if len(ret_val) > 60:
                    ret_val = ret_val[:57] + "..."
                returns.append(ret_val)
        return returns

    def call_chain(self, file_path: str, func_name: str, direction: str = "down", max_depth: int = 8) -> str:
        self._ensure_scanned(file_path)
        card = self.nav.cache[file_path]
        func = card.find_function(func_name)
        if not func:
            return f"⚠️  未找到函数: {func_name} (在 {Path(file_path).name} 中)"
        lines = []
        lines.append(f"🔗 调用链: {func_name}()")
        lines.append(f"   📄 {Path(file_path).name}:{func.start_line}-{func.end_line}")
        if func.docstring:
            lines.append(f"   📝 {func.docstring.split(chr(10))[0][:60]}")
        lines.append("")
        if direction in ("down", "both"):
            lines.append("⬇️  向下追踪（调用了谁）:")
            visited: Set[str] = set()
            chain = self._trace_call_chain_down(file_path, func_name, max_depth, visited, 0)
            lines.append(chain if chain.strip() else "   (无内部调用)")
        if direction in ("up", "both"):
            lines.append("")
            lines.append("⬆️  向上追踪（被谁调用）:")
            chain_up = self._trace_call_chain_up(file_path, func_name, max_depth)
            lines.append(chain_up if chain_up.strip() else "   (未找到调用者 — 可能是入口函数)")
        return '\n'.join(lines)

    def data_flow(self, file_path: str, func_name: str) -> str:
        self._ensure_scanned(file_path)
        card = self.nav.cache[file_path]
        func = card.find_function(func_name)
        if not func:
            return f"⚠️  未找到函数: {func_name}"
        lines = []
        lines.append(f"🌊 数据流分析: {func_name}()")
        lines.append(f"   📄 {Path(file_path).name}:{func.start_line}-{func.end_line}")
        lines.append("")
        lines.append("📥 输入参数:")
        if func.parameters:
            for p in func.parameters:
                lines.append(f"   • {p}")
        else:
            lines.append("   (无参数)")
        lines.append("")
        lines.append("⚙️  内部处理链:")
        callees = self.nav.find_callees(file_path, func.start_line, func.end_line)
        internal_funcs = self._get_internal_functions(file_path)
        internal_calls = [c for c in callees if c in internal_funcs and c != func_name]
        external_calls = [c for c in callees if c not in internal_funcs and c not in self.PYTHON_BUILTINS]
        if internal_calls:
            for c in internal_calls:
                c_func = card.find_function(c)
                if c_func and c_func.docstring:
                    lines.append(f"   ├── {c}() — {c_func.docstring.split(chr(10))[0][:50]}")
                else:
                    lines.append(f"   ├── {c}()")
        if external_calls:
            for c in external_calls[:8]:
                lines.append(f"   ├── {c}() [外部]")
        if not internal_calls and not external_calls:
            lines.append("   (纯计算函数，无内部调用)")
        lines.append("")
        lines.append("📤 返回值:")
        returns = self._extract_return_info(file_path, func.start_line, func.end_line)
        if returns:
            for r in returns[:5]:
                lines.append(f"   • return {r}")
        else:
            lines.append("   (无显式 return 或返回 None)")
        complexity = self.nav.estimate_complexity(file_path, func.start_line, func.end_line)
        lines.append("")
        lines.append("📊 复杂度快照:")
        lines.append(f"   代码行: {complexity['lines']} | 嵌套: {complexity['nesting_depth']} | 分支: {complexity['if_statements'] + complexity['for_loops'] + complexity['while_loops']}")
        return '\n'.join(lines)


class ASTSemanticSearcher:
    """基于 Python AST 的规则式语义搜索引擎。

    不需要模型，只用 Python 内置 ast 模块解析代码结构，
    支持结构化的代码查询：继承关系、装饰器、调用链、代码质量等。
    """

    def __init__(self, navigator: 'CodeNavigator'):
        self.navigator = navigator
        self._ast_cache: Dict[str, ast.AST] = {}

    def _get_ast(self, file_path: str) -> Optional[ast.AST]:
        """获取文件的 AST，带缓存"""
        if file_path in self._ast_cache:
            return self._ast_cache[file_path]
        if not file_path.endswith('.py'):
            return None
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            tree = ast.parse(content)
            self._ast_cache[file_path] = tree
            return tree
        except (SyntaxError, UnicodeDecodeError, FileNotFoundError):
            return None

    def _resolve_import_aliases(self, tree: ast.AST) -> Dict[str, str]:
        """解析导入别名: {'BM': 'BaseModel', 'np': 'numpy'}"""
        aliases: Dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    key = alias.asname or alias.name
                    aliases[key] = alias.name
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    key = alias.asname or alias.name
                    full_name = f"{node.module}.{alias.name}" if node.module else alias.name
                    aliases[key] = full_name
                    aliases[alias.name] = full_name
        return aliases

    def _collect_py_files(self, dir_path: str) -> List[str]:
        """收集目录下所有 .py 文件"""
        result = []
        base = Path(dir_path)
        if not base.is_dir():
            return result
        for p in base.rglob("*.py"):
            if any(skip in p.parts for skip in ('__pycache__', 'venv', '.git', 'node_modules', '.pytest-tmp')):
                continue
            result.append(str(p))
        return result

    # ------------------------------------------------------------------
    # 查询类型实现
    # ------------------------------------------------------------------

    def _query_inherits(self, base_class: str, file_path: Optional[str],
                        dir_path: Optional[str]) -> List[Dict]:
        """查找所有继承自 base_class 的类"""
        results = []
        files = self._resolve_files(file_path, dir_path)
        for fp in files:
            tree = self._get_ast(fp)
            if tree is None:
                continue
            aliases = self._resolve_import_aliases(tree)
            resolved_target = aliases.get(base_class, base_class.split('.')[-1])
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for base in node.bases:
                        base_name = self._get_name_str(base)
                        resolved_base = aliases.get(base_name, base_name)
                        # 匹配：精确匹配 或 别名解析后匹配 或 短名匹配
                        if (base_name == base_class
                                or resolved_base == resolved_target
                                or base_name == base_class.split('.')[-1]
                                or resolved_base.endswith('.' + base_class.split('.')[-1])):
                            results.append({
                                'file': fp, 'line': node.lineno,
                                'name': node.name,
                                'bases': [self._get_name_str(b) for b in node.bases],
                                'detail': f"class {node.name}({', '.join(self._get_name_str(b) for b in node.bases)})"
                            })
        return results

    def _query_decorated_with(self, decorator: str, file_path: Optional[str],
                              dir_path: Optional[str]) -> List[Dict]:
        """查找被特定装饰器修饰的函数/类"""
        results = []
        files = self._resolve_files(file_path, dir_path)
        for fp in files:
            tree = self._get_ast(fp)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    for dec in node.decorator_list:
                        dec_name = self._get_decorator_name(dec)
                        if decorator in dec_name or dec_name == decorator:
                            kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else \
                                   "class" if isinstance(node, ast.ClassDef) else "def"
                            results.append({
                                'file': fp, 'line': node.lineno,
                                'name': node.name, 'kind': kind,
                                'detail': f"{kind} {node.name} (装饰器: {dec_name})"
                            })
        return results

    def _query_calls(self, func_name: str, file_path: Optional[str],
                     dir_path: Optional[str]) -> List[Dict]:
        """查找所有调用 func_name 的位置"""
        results = []
        files = self._resolve_files(file_path, dir_path)
        for fp in files:
            tree = self._get_ast(fp)
            if tree is None:
                continue
            # 找出所有 Call 节点，用 ast.unparse 获取函数名
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    call_name = self._get_call_func_name(node)
                    if call_name == func_name or call_name.endswith('.' + func_name):
                        results.append({
                            'file': fp, 'line': node.lineno,
                            'name': func_name,
                            'detail': f"{call_name}(...) 行 {node.lineno}"
                        })
        return results

    def _query_imports_module(self, module: str, file_path: Optional[str],
                              dir_path: Optional[str]) -> List[Dict]:
        """查找导入了指定模块的文件"""
        results = []
        files = self._resolve_files(file_path, dir_path)
        for fp in files:
            tree = self._get_ast(fp)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if module in alias.name:
                            results.append({
                                'file': fp, 'line': node.lineno,
                                'name': module,
                                'detail': f"import {alias.name} (行 {node.lineno})"
                            })
                elif isinstance(node, ast.ImportFrom):
                    if node.module and module in node.module:
                        names = [a.name for a in node.names]
                        results.append({
                            'file': fp, 'line': node.lineno,
                            'name': module,
                            'detail': f"from {node.module} import {', '.join(names)} (行 {node.lineno})"
                        })
        return results

    def _query_bare_except(self, file_path: Optional[str],
                           dir_path: Optional[str]) -> List[Dict]:
        """查找裸 except: 子句"""
        results = []
        files = self._resolve_files(file_path, dir_path)
        for fp in files:
            tree = self._get_ast(fp)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    if node.type is None:
                        results.append({
                            'file': fp, 'line': node.lineno,
                            'name': 'bare-except',
                            'detail': f"裸 except: (行 {node.lineno}) — 可能吞掉所有异常"
                        })
        return results

    def _query_long_function(self, threshold: int, file_path: Optional[str],
                             dir_path: Optional[str]) -> List[Dict]:
        """查找代码行数超过 threshold 的函数"""
        results = []
        files = self._resolve_files(file_path, dir_path)
        for fp in files:
            tree = self._get_ast(fp)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    end_line = node.end_lineno or node.lineno
                    lines = end_line - node.lineno + 1
                    if lines > threshold:
                        kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                        results.append({
                            'file': fp, 'line': node.lineno,
                            'name': node.name,
                            'detail': f"{kind} {node.name} — {lines} 行 (阈值 {threshold})"
                        })
        return results

    def _query_many_params(self, threshold: int, file_path: Optional[str],
                           dir_path: Optional[str]) -> List[Dict]:
        """查找参数超过 threshold 的函数"""
        results = []
        files = self._resolve_files(file_path, dir_path)
        for fp in files:
            tree = self._get_ast(fp)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    params = [arg.arg for arg in node.args.args
                              if arg.arg != 'self' and arg.arg != 'cls']
                    if len(params) > threshold:
                        kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                        results.append({
                            'file': fp, 'line': node.lineno,
                            'name': node.name,
                            'detail': f"{kind} {node.name}({', '.join(params)}) — {len(params)} 参数 (阈值 {threshold})"
                        })
        return results

    def _query_async_without_await(self, file_path: Optional[str],
                                   dir_path: Optional[str]) -> List[Dict]:
        """查找没有 await 的 async 函数"""
        results = []
        files = self._resolve_files(file_path, dir_path)
        for fp in files:
            tree = self._get_ast(fp)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef):
                    has_await = any(isinstance(n, ast.Await) for n in ast.walk(node))
                    if not has_await:
                        results.append({
                            'file': fp, 'line': node.lineno,
                            'name': node.name,
                            'detail': f"async def {node.name} — 无 await（可能不需要 async）"
                        })
        return results

    def _query_no_return_type(self, file_path: Optional[str],
                              dir_path: Optional[str]) -> List[Dict]:
        """查找缺少返回类型注解的函数"""
        results = []
        files = self._resolve_files(file_path, dir_path)
        for fp in files:
            tree = self._get_ast(fp)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if node.returns is None:
                        kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                        results.append({
                            'file': fp, 'line': node.lineno,
                            'name': node.name,
                            'detail': f"{kind} {node.name} — 缺少返回类型 -> "
                        })
        return results

    def _query_no_param_types(self, file_path: Optional[str],
                               dir_path: Optional[str]) -> List[Dict]:
        """查找缺少参数类型注解的函数"""
        results = []
        files = self._resolve_files(file_path, dir_path)
        for fp in files:
            tree = self._get_ast(fp)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    untyped = []
                    for arg in node.args.args:
                        if arg.arg in ('self', 'cls'):
                            continue
                        if arg.annotation is None:
                            untyped.append(arg.arg)
                    if untyped:
                        kind = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                        results.append({
                            'file': fp, 'line': node.lineno,
                            'name': node.name,
                            'detail': f"{kind} {node.name} — 缺少类型: {', '.join(untyped)}"
                        })
        return results

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _resolve_files(self, file_path: Optional[str],
                       dir_path: Optional[str]) -> List[str]:
        """解析搜索范围"""
        if file_path:
            return [file_path]
        if dir_path:
            return self._collect_py_files(dir_path)
        # 默认：使用已缓存的所有文件
        return list(self.navigator.cache.keys())

    def _get_name_str(self, node: ast.expr) -> str:
        """从 AST 节点提取名称字符串"""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{self._get_name_str(node.value)}.{node.attr}"
        if isinstance(node, ast.Subscript):
            return self._get_name_str(node.value)
        return ast.unparse(node) if hasattr(ast, 'unparse') else str(node)

    def _get_decorator_name(self, node: ast.expr) -> str:
        """从装饰器节点提取名称"""
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return f"{self._get_name_str(node.value)}.{node.attr}"
        if isinstance(node, ast.Call):
            return self._get_decorator_name(node.func)
        return self._get_name_str(node)

    def _get_call_func_name(self, node: ast.Call) -> str:
        """从 Call 节点提取被调用的函数名"""
        return self._get_name_str(node.func)

    def search(self, query_type: str, target: str = "",
               threshold: int = 0, file_path: str = None,
               dir_path: str = None) -> str:
        """AST 语义搜索主入口"""
        query_map = {
            'inherits': lambda: self._query_inherits(target, file_path, dir_path),
            'decorated_with': lambda: self._query_decorated_with(target, file_path, dir_path),
            'calls': lambda: self._query_calls(target, file_path, dir_path),
            'imports_module': lambda: self._query_imports_module(target, file_path, dir_path),
            'bare_except': lambda: self._query_bare_except(file_path, dir_path),
            'long_function': lambda: self._query_long_function(max(threshold, 1), file_path, dir_path),
            'many_params': lambda: self._query_many_params(max(threshold, 0), file_path, dir_path),
            'async_without_await': lambda: self._query_async_without_await(file_path, dir_path),
            'no_return_type': lambda: self._query_no_return_type(file_path, dir_path),
            'no_param_types': lambda: self._query_no_param_types(file_path, dir_path),
        }

        if query_type not in query_map:
            available = ', '.join(query_map.keys())
            return f"未知查询类型: {query_type}\n可用类型: {available}"

        try:
            results = query_map[query_type]()
        except Exception as e:
            return f"AST 搜索异常: {str(e)}"

        if not results:
            return f"未找到匹配结果 (query_type={query_type}, target={target or '(无)'})"

        # 格式化输出
        lines = [f"🔍 AST 语义搜索: {query_type} | target='{target}' | 找到 {len(results)} 个结果\n"]
        by_file: Dict[str, List[Dict]] = {}
        for r in results:
            by_file.setdefault(Path(r['file']).name, []).append(r)

        for fname, items in sorted(by_file.items()):
            lines.append(f"📄 {fname} ({len(items)} 个匹配):")
            for item in items[:30]:
                lines.append(f"   • 行 {item['line']:4d}: {item['detail']}")
            if len(items) > 30:
                lines.append(f"   ... 还有 {len(items) - 30} 个结果未显示")
            lines.append("")

        return '\n'.join(lines)


class CodeNavigatorToolManager:
    """为智能体提供的工具接口（导航器）"""

    def __init__(self):
        self.navigator = CodeNavigator()
        self.flow = CodeFlowAnalyzer(self.navigator)
        self.ast_searcher = ASTSemanticSearcher(self.navigator)

    def scan_file(self, file_path: str) -> str:
        """工具：扫描文件"""
        card = self.navigator.scan(file_path)
        return card.to_map()

    def search_code(self, pattern: str, file_path: Optional[str] = None, search_type: str = "name") -> str:
        """工具：搜索代码"""
        results = self.navigator.search(pattern, file_path, search_type)
        if not results:
            return f"未找到匹配: {pattern}"
        lines = [f"找到 {len(results)} 个匹配:\n"]
        for fp, line_num, context in results[:20]:
            lines.append(f"  • {Path(fp).name}:{line_num} - {context}")
        if len(results) > 20:
            lines.append(f"\n... 还有 {len(results)-20} 个结果")
        return '\n'.join(lines)

    def view_code(self, file_path: str, location: str, context_lines: int = 5) -> str:
        """工具：查看代码"""
        try:
            start, end = self.navigator.jump_to(file_path, location)
            return self.navigator.view(file_path, start, end, context_lines)
        except ValueError as e:
            return str(e)

    def analyze_function(self, file_path: str, function_name: str) -> str:
        """工具：分析函数"""
        card = self.navigator.scan(file_path)
        func = card.find_function(function_name)
        if not func:
            return f"未找到函数: {function_name}"
        lines = [f"🔍 函数分析: {function_name}", ""]
        lines.append(func.to_summary())
        lines.append("")
        complexity = self.navigator.estimate_complexity(file_path, func.start_line, func.end_line)
        lines.append("📊 复杂度:")
        lines.append(f"  代码行数: {complexity['lines']}")
        lines.append(f"  嵌套深度: {complexity['nesting_depth']}")
        lines.append(f"  分支数: {complexity['if_statements'] + complexity['for_loops'] + complexity['while_loops']}")
        lines.append("")
        callees = self.navigator.find_callees(file_path, func.start_line, func.end_line)
        if callees:
            lines.append(f"📞 调用的函数: {', '.join(callees[:10])}")
        return '\n'.join(lines)

    def flow_diagram(self, file_path: str, max_depth: int = 5) -> str:
        return self.flow.flow_diagram(file_path, max_depth)

    def project_flow(self, project_path: str, max_files: int = 20) -> str:
        return self.flow.project_flow(project_path, max_files)

    def call_chain(self, file_path: str, func_name: str, direction: str = "down", max_depth: int = 8) -> str:
        return self.flow.call_chain(file_path, func_name, direction, max_depth)

    def data_flow(self, file_path: str, func_name: str) -> str:
        return self.flow.data_flow(file_path, func_name)

    def project_map(self, project_path: str) -> str:
        return self.navigator.generate_project_map(project_path)

    def ast_search(self, query_type: str, target: str = "",
                   threshold: int = 0, file_path: str = None,
                   dir_path: str = None) -> str:
        """工具：AST 语义搜索（规则式，无需模型）

        基于 Python AST 的结构化代码搜索。支持：

        query_type (必填):
          'inherits'          — 查找继承自 target 的所有类（自动解析 import 别名）
          'decorated_with'    — 查找被 target 装饰器修饰的函数/类
          'calls'             — 查找所有调用 target 函数的位置
          'imports_module'    — 查找导入了 target 模块的文件
          'bare_except'       — 查找所有裸 except: 子句
          'long_function'     — 查找超过 threshold 行的函数（默认 50）
          'many_params'       — 查找参数超过 threshold 个的函数（默认 5）
          'async_without_await' — 查找没有 await 的 async 函数
          'no_return_type'    — 查找缺少返回类型注解的函数
          'no_param_types'    — 查找缺少参数类型注解的函数

        Args:
            query_type: 查询类型（见上）
            target: 搜索目标（类名/装饰器名/函数名/模块名），部分类型不需要
            threshold: 数值阈值（用于 long_function/many_params）
            file_path: 限定单个文件
            dir_path: 限定目录（递归搜索 .py 文件）
        """
        return self.ast_searcher.search(
            query_type=query_type,
            target=target,
            threshold=threshold,
            file_path=file_path,
            dir_path=dir_path
        )



# 基于文件位置自动计算项目根目录，不依赖当前工作目录
_default_workspace = str(Path(__file__).resolve().parent.parent.parent)
_manager = CodeEditorManager(workspace_dir=_default_workspace)
_navigator = CodeNavigatorToolManager()


def handle_tool_call(function_name, arguments):
    """统一的调用入口"""
    try:
        # ==================== 导航器工具（合并自 code_navigator） ====================
        if function_name == "code_editor_handler_CodeNavigator_scan_file":
            return {"success": True, "content": _navigator.scan_file(arguments['file_path'])}
        elif function_name == "code_editor_handler_CodeNavigator_search_code":
            return {"success": True, "content": _navigator.search_code(
                arguments['pattern'],
                arguments.get('file_path'),
                arguments.get('search_type', 'name')
            )}
        elif function_name == "code_editor_handler_CodeNavigator_view_code":
            return {"success": True, "content": _navigator.view_code(
                arguments['file_path'],
                arguments['location'],
                arguments.get('context_lines', 5)
            )}
        elif function_name == "code_editor_handler_CodeNavigator_analyze_function":
            return {"success": True, "content": _navigator.analyze_function(
                arguments['file_path'],
                arguments['function_name']
            )}
        elif function_name == "code_editor_handler_CodeNavigator_call_chain":
            return {"success": True, "content": _navigator.call_chain(
                arguments['file_path'],
                arguments['func_name'],
                arguments.get('direction', 'down'),
                arguments.get('max_depth', 8)
            )}
        elif function_name == "code_editor_handler_CodeNavigator_data_flow":
            return {"success": True, "content": _navigator.data_flow(
                arguments['file_path'],
                arguments['func_name']
            )}
        elif function_name == "code_editor_handler_CodeNavigator_flow_diagram":
            return {"success": True, "content": _navigator.flow_diagram(
                arguments['file_path'],
                arguments.get('max_depth', 5)
            )}
        elif function_name == "code_editor_handler_CodeNavigator_project_flow":
            return {"success": True, "content": _navigator.project_flow(
                arguments['project_path'],
                arguments.get('max_files', 20)
            )}
        elif function_name == "code_editor_handler_CodeNavigator_project_map":
            return {"success": True, "content": _navigator.project_map(arguments['project_path'])}
        elif function_name == "code_editor_handler_CodeNavigator_ast_search":
            return {"success": True, "content": _navigator.ast_search(
                arguments['query_type'],
                arguments.get('target', ''),
                arguments.get('threshold', 0),
                arguments.get('file_path'),
                arguments.get('dir_path')
            )}
        # ==================== 编辑器工具 ====================
        elif function_name == "code_editor_view":
            return _manager.view(
                arguments['file_path'], 
                arguments.get('view_range'),
                arguments.get('limit', 500),
                arguments.get('offset', 0),
                arguments.get('show_invisible', False)
            )
        elif function_name == "code_editor_str_replace":
            return _manager.str_replace(
                arguments['file_path'], 
                arguments['old_str'], 
                arguments['new_str'],
                arguments.get('replace_all', False),
                arguments.get('auto_indent', True)
            )
        elif function_name == "code_editor_write":
            return _manager.write(arguments['file_path'], arguments['content'])
        elif function_name == "code_editor_create":
            return _manager.create(arguments['file_path'], arguments['content'])
        elif function_name == "code_editor_append":
            return _manager.append(
                arguments['file_path'], 
                arguments['content'],
                arguments.get('ensure_newline', True)
            )
        elif function_name == "code_editor_insert":
            return _manager.insert(
                arguments['file_path'], 
                arguments['insert_line'], 
                arguments['content']
            )
        elif function_name == "code_editor_delete":
            return _manager.delete(arguments['file_path'])
        elif function_name == "code_editor_undo":
            return _manager.undo(arguments['file_path'], arguments.get('steps', 1))
        elif function_name == "code_editor_search":
            return _manager.search(
                arguments['pattern'],
                arguments.get('file_path'),
                arguments.get('dir_path'),
                arguments.get('file_pattern', '*'),
                arguments.get('ignore_case', False),
                arguments.get('context_lines', 0)
            )
        elif function_name == "code_editor_find_files":
            return _manager.find_files(
                arguments['pattern'],
                arguments.get('dir_path', '.')
            )
        elif function_name == "code_editor_list_dir":
            return _manager.list_dir(
                arguments.get('dir_path', '.'),
                arguments.get('recursive', False),
                arguments.get('max_depth', 3)
            )
        elif function_name == "code_editor_get_file_info":
            return _manager.get_file_info(arguments['file_path'])
        elif function_name == "code_editor_copy_file":
            return _manager.copy_file(
                arguments['source_path'],
                arguments['destination_path'],
                arguments.get('overwrite', False)
            )
        elif function_name == "code_editor_move_file":
            return _manager.move_file(
                arguments['source_path'],
                arguments['destination_path'],
                arguments.get('overwrite', False)
            )
        elif function_name == "code_editor_create_directory":
            return _manager.create_directory(arguments['dir_path'])
        elif function_name == "code_editor_get_tree_view":
            return _manager.get_tree_view(
                arguments.get('dir_path', ''),
                arguments.get('max_depth', 2),
                arguments.get('max_items', 100),
                arguments.get('show_size', True),
                arguments.get('show_emojis', True)
            )
        elif function_name == "code_editor_quick_scan":
            return _manager.quick_scan(
                arguments.get('dir_path', ''),
                arguments.get('max_depth', 3)
            )
        elif function_name == "code_editor_read_file_chunked":
            return _manager.read_file_chunked(
                arguments['file_path'],
                arguments.get('chunk_index', 0),
                arguments.get('chunk_size'),
                arguments.get('encoding', 'utf-8')
            )
        elif function_name == "code_editor_list_history":
            return _manager.list_history(
                arguments.get('file_path', ''),
                arguments.get('limit', 20)
            )
        elif function_name == "code_editor_find_directory":
            return _manager.find_directory(
                arguments['dir_name'],
                arguments.get('start_dir', ''),
                arguments.get('exact_match', False),
                arguments.get('limit', 20)
            )
        else:
            return {"success": False, "error": f"未知工具: {function_name}"}
    except Exception as e:
        return {"success": False, "error": f"工具执行异常: {str(e)}"}


if __name__ == "__main__":
    print("=== 智能体编辑器测试 v2.1 ===")
    
    print("\n1. 创建文件")
    res = handle_tool_call("code_editor_create", {
        "file_path": "test_v2.py",
        "content": "def hello():\n    print('world')\n"
    })
    print(res)
    
    print("\n2. 追加内容")
    res = handle_tool_call("code_editor_append", {
        "file_path": "test_v2.py",
        "content": "def goodbye():\n    print('bye')\n"
    })
    print(res)
    
    print("\n3. 搜索内容（单文件）")
    res = handle_tool_call("code_editor_search", {
        "pattern": "def",
        "file_path": "test_v2.py"
    })
    print("content:")
    print(res.get('content'))
    print("metadata:", res.get('metadata'))
    
    print("\n4. 搜索内容（目录递归 + 文件模式）")
    res = handle_tool_call("code_editor_search", {
        "pattern": "def ",
        "dir_path": ".",
        "file_pattern": "*.py",
        "context_lines": 1
    })
    print("metadata:", res.get('metadata'))
    print("content 前500字符:")
    print(res.get('content', '')[:500])
    
    print("\n5. 查找文件")
    res = handle_tool_call("code_editor_find_files", {
        "pattern": "*.py"
    })
    print("metadata:", res.get('metadata'))
    print("content 前500字符:")
    print(res.get('content', '')[:500])
    
    print("\n6. 列出目录")
    res = handle_tool_call("code_editor_list_dir", {
        "dir_path": ".",
        "recursive": False
    })
    print(res.get('content', '')[:500])
    
    print("\n7. 获取文件信息")
    res = handle_tool_call("code_editor_get_file_info", {
        "file_path": "test_v2.py"
    })
    print(res)
    
    print("\n8. 测试撤销")
    res = handle_tool_call("code_editor_undo", {
        "file_path": "test_v2.py"
    })
    print(res)
    
    print("\n9. 删除文件")
    res = handle_tool_call("code_editor_delete", {
        "file_path": "test_v2.py"
    })
    print(res)
    
    # 清理
    for f in ["test_v2.py", "test_v2.py.tmp"]:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass