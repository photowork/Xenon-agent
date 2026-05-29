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
from typing import List, Dict, Optional, Union
from datetime import datetime
from collections import deque, Counter


class FileHistoryStore:
    """
    多级历史记录管理器
    支持撤销多次操作，类似 Git 的轻量级版本控制
    """
    
    def __init__(self, max_history: int = 10):
        self.max_history = max_history
        self._history: Dict[str, deque] = {}  # file_path -> deque of snapshots
    
    def _get_history_dir(self, workspace: Path) -> Path:
        """获取历史记录目录"""
        history_dir = workspace / ".agent_history"
        history_dir.mkdir(parents=True, exist_ok=True)
        return history_dir
    
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
            self._history.clear()


class CodeEditorManager:
    """
    面向智能体的代码管理器 - 增强版
    提供类似 Trae IDE 的原子化操作能力
    """
    
    def __init__(self, workspace_dir: str = ".", enable_checksum: bool = True, max_history: int = 10):
        self.workspace_dir = Path(workspace_dir).resolve()
        self.enable_checksum = enable_checksum
        self.history_manager = FileHistoryStore(max_history=max_history)
        
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
                
                for fp in search_dir.rglob(file_pattern):
                    # 文件数上限
                    if stats["files_scanned"] >= self.SEARCH_MAX_FILES_SCANNED:
                        break
                    # 全局匹配上限
                    if stats["hit_global_limit"] or stats["total_matches"] >= self.SEARCH_MAX_TOTAL_MATCHES:
                        stats["hit_global_limit"] = True
                        break
                    if not fp.is_file():
                        continue
                    # 排除目录
                    if any(excluded in fp.parts for excluded in self.SEARCH_EXCLUDE_DIRS):
                        continue
                    # 排除文件名模式
                    if any(fnmatch.fnmatch(fp.name, pat) for pat in self.SEARCH_EXCLUDE_FILE_PATTERNS):
                        continue
                    
                    stats["files_scanned"] += 1
                    matches = search_in_file(fp)
                    if matches:
                        results.append({"file": str(fp), "matches": matches})
                        stats["total_matches"] += sum(1 for m in matches if m["line_number"] > 0)
            
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
        """
        search_dir = self._resolve_path_or_error(dir_path)
        if isinstance(search_dir, dict):
            return search_dir
        
        if not search_dir.is_dir():
            return {"success": False, "error": f"不是目录: {dir_path}"}
        
        try:
            matches = []
            truncated = False
            
            for fp in search_dir.glob(pattern):
                # 排除噪声目录
                if any(excluded in fp.parts for excluded in self.SEARCH_EXCLUDE_DIRS):
                    continue
                
                if len(matches) >= self.FIND_MAX_RESULTS:
                    truncated = True
                    break
                
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
        }
    ]


# 基于文件位置自动计算项目根目录，不依赖当前工作目录
_default_workspace = str(Path(__file__).resolve().parent.parent.parent)
_manager = CodeEditorManager(workspace_dir=_default_workspace)


def handle_tool_call(function_name, arguments):
    """统一的调用入口"""
    try:
        if function_name == "code_editor_view":
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