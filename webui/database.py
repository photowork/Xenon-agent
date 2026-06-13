import json
import uuid
import shutil
import threading
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional
from contextlib import contextmanager


class Database:
    def __init__(self, base_path: str = None):
        if base_path is None:
            base_path = Path(__file__).parent / "sessions"
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._file_locks: Dict[str, threading.RLock] = {}
        self._init_session_index()
    
    def _get_file_lock(self, filepath: str) -> threading.RLock:
        """获取或创建文件锁"""
        with self._lock:
            if filepath not in self._file_locks:
                self._file_locks[filepath] = threading.RLock()
            return self._file_locks[filepath]
    
    @contextmanager
    def _safe_read_json(self, filepath: Path):
        """安全读取 JSON 文件的上下文管理器"""
        lock = self._get_file_lock(str(filepath))
        with lock:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                yield data
            except FileNotFoundError:
                yield None
            except json.JSONDecodeError as e:
                raise RuntimeError(f"JSON 解析错误 {filepath}: {e}")
    
    @contextmanager
    def _safe_write_json(self, filepath: Path):
        """安全写入 JSON 文件的上下文管理器"""
        lock = self._get_file_lock(str(filepath))
        with lock:
            # 先读取现有数据
            data = {}
            if filepath.exists():
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError):
                    data = {}
            
            yield data
            
            # 写入文件（使用临时文件+重命名确保原子性）
            temp_path = filepath.with_suffix('.tmp')
            try:
                with open(temp_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                
                # 原子性重命名
                temp_path.replace(filepath)
            except Exception as e:
                # 清理临时文件
                if temp_path.exists():
                    temp_path.unlink()
                raise
    
    def _init_session_index(self):
        index_path = self.base_path / "sessions_index.json"
        if not index_path.exists():
            with self._safe_write_json(index_path) as index:
                pass  # 空字典会自动创建
    
    def _load_index(self) -> Dict[str, Any]:
        index_path = self.base_path / "sessions_index.json"
        with self._safe_read_json(index_path) as index:
            return index if index is not None else {}
    
    def _save_index(self, index: Dict[str, Any]):
        index_path = self.base_path / "sessions_index.json"
        with self._safe_write_json(index_path) as data:
            data.update(index)

    @staticmethod
    def _validate_session_id(session_id: str) -> str:
        try:
            return str(uuid.UUID(str(session_id)))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("Invalid session ID format") from error
    
    def _get_session_path(self, session_id: str) -> Path:
        session_id = self._validate_session_id(session_id)
        return self.base_path / f"{session_id}.json"
    
    def create_session(
        self,
        title: Optional[str] = None,
        model: Optional[str] = None,
        theme: Optional[str] = None,
    ) -> str:
        with self._lock:  # 保护整个创建过程
            session_id = str(uuid.uuid4())
            if not title:
                title = f"新对话 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            if not theme:
                theme = title
            
            now = datetime.now().isoformat()
            
            session_data = {
                "id": session_id,
                "title": title,
                "theme": theme,
                "model": model,
                "created_at": now,
                "updated_at": now,
                "context": [],
                "full_context": []
            }
            
            session_path = self._get_session_path(session_id)
            with self._safe_write_json(session_path) as data:
                data.update(session_data)
            
            with self._safe_write_json(self.base_path / "sessions_index.json") as index:
                index[session_id] = {
                    "title": title,
                    "theme": theme,
                    "model": model,
                    "created_at": now,
                    "updated_at": now,
                    "message_count": 0
                }
            
            return session_id
    
    def get_sessions(self) -> List[Dict[str, Any]]:
        with self._lock:
            index = self._load_index()
            sessions = []
            
            for session_id, info in index.items():
                session_path = self._get_session_path(session_id)
                if not session_path.exists():
                    continue
                sessions.append({
                    "id": session_id,
                    "title": info.get("title", "未命名"),
                    "theme": info.get("theme") or info.get("title", "未命名"),
                    "model": info.get("model"),
                    "created_at": info.get("created_at", ""),
                    "updated_at": info.get("updated_at", ""),
                    "message_count": info.get("message_count", 0)
                })
            
            sessions.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
            return sessions
    
    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        session_path = self._get_session_path(session_id)
        with self._safe_read_json(session_path) as data:
            return data
    
    def update_session_title(self, session_id: str, title: str):
        session_path = self._get_session_path(session_id)
        with self._safe_write_json(session_path) as data:
            if not data:
                return
            data["title"] = title
            data["updated_at"] = datetime.now().isoformat()
        
        # 更新索引
        with self._safe_write_json(self.base_path / "sessions_index.json") as index:
            if session_id in index:
                index[session_id]["title"] = title
                index[session_id]["updated_at"] = data["updated_at"]

    def update_session_theme(self, session_id: str, theme: str):
        """更新会话主题显示名，同时将 title 也设为同一值（"主题即标题"设计）。

        注意：调用此方法会覆盖原有的 title。如需单独修改标题，请使用
        update_session_title()。
        """
        session_path = self._get_session_path(session_id)
        with self._safe_write_json(session_path) as data:
            if not data:
                return
            data["theme"] = theme
            data["title"] = theme
            data["updated_at"] = datetime.now().isoformat()

        with self._safe_write_json(self.base_path / "sessions_index.json") as index:
            if session_id in index:
                index[session_id]["theme"] = theme
                index[session_id]["title"] = theme
                index[session_id]["updated_at"] = data["updated_at"]

    def update_session_model(self, session_id: str, model: str):
        session_path = self._get_session_path(session_id)
        with self._safe_write_json(session_path) as data:
            if not data:
                return
            data["model"] = model
            data["updated_at"] = datetime.now().isoformat()

        with self._safe_write_json(self.base_path / "sessions_index.json") as index:
            if session_id in index:
                index[session_id]["model"] = model
                index[session_id]["updated_at"] = data["updated_at"]
    
    def update_session_timestamp(self, session_id: str):
        session_path = self._get_session_path(session_id)
        with self._safe_write_json(session_path) as data:
            if not data:
                return
            data["updated_at"] = datetime.now().isoformat()
        
        # 更新索引
        with self._safe_write_json(self.base_path / "sessions_index.json") as index:
            if session_id in index:
                index[session_id]["updated_at"] = data["updated_at"]
                index[session_id]["message_count"] = len(data.get("full_context") or data.get("context", []))
    
    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            session_path = self._get_session_path(session_id)

            if not session_path.exists():
                return False

            # 删除文件
            session_path.unlink()

            # 更新索引
            with self._safe_write_json(self.base_path / "sessions_index.json") as index:
                if session_id in index:
                    del index[session_id]

            # 清理文件锁
            if str(session_path) in self._file_locks:
                del self._file_locks[str(session_path)]

            return True

    def evict_oldest_sessions(self, keep: int) -> List[str]:
        """淘汰最旧的会话，保留最近 keep 个（按 created_at 排序）。

        返回被淘汰的 session_id 列表，调用方负责清理对应的内存资源。
        当会话数未超过 keep 时不执行任何操作。
        """
        with self._lock:
            index = self._load_index()
            if not index:
                return []

            # 按 created_at 升序排列（最旧的在前）
            sorted_sessions = sorted(
                index.items(),
                key=lambda item: item[1].get("created_at", ""),
            )

            excess = len(sorted_sessions) - keep
            if excess <= 0:
                return []

            evicted: List[str] = []
            for session_id, _ in sorted_sessions[:excess]:
                session_path = self._get_session_path(session_id)
                if session_path.exists():
                    try:
                        session_path.unlink()
                    except OSError:
                        continue
                del index[session_id]
                evicted.append(session_id)

            # 写回索引
            with self._safe_write_json(self.base_path / "sessions_index.json") as idx:
                idx.update(index)

            return evicted
    
    def get_context(self, session_id: str) -> List[Dict[str, Any]]:
        session = self.get_session(session_id)
        if not session:
            return []
        return session.get("context", [])

    def get_full_context(self, session_id: str) -> List[Dict[str, Any]]:
        session = self.get_session(session_id)
        if not session:
            return []
        return session.get("full_context") or session.get("context", [])
    
    def save_context(self, session_id: str, context: List[Dict[str, Any]]):
        """[已弃用] 请使用 save_session_state() 替代。

        保留以兼容旧调用，但当前 webui 中所有上下文持久化都通过
        save_session_state() 完成。
        """
        import warnings
        warnings.warn(
            "save_context is deprecated, use save_session_state instead",
            DeprecationWarning, stacklevel=2,
        )
        session_path = self._get_session_path(session_id)
        with self._safe_write_json(session_path) as data:
            if not data:
                return
            data["context"] = context
            data.setdefault("full_context", [])
            data["updated_at"] = datetime.now().isoformat()
        
        # 更新索引
        with self._safe_write_json(self.base_path / "sessions_index.json") as index:
            if session_id in index:
                index[session_id]["updated_at"] = data["updated_at"]
                index[session_id]["message_count"] = len(data.get("full_context") or data.get("context", []))

    def save_session_state(
        self,
        session_id: str,
        context: List[Dict[str, Any]],
        full_context: Optional[List[Dict[str, Any]]] = None
    ):
        """Save session context and full_context, then update index timestamp/count.

        Writes index FIRST so that if session write fails, index inconsistency
        can be repaired by rebuilding from session files (not vice versa).
        """
        session_path = self._get_session_path(session_id)
        now = datetime.now().isoformat()
        message_count = len(full_context if full_context is not None else context)

        # 先写索引（派生数据，失败时影响较小，可从 session 文件重建）
        with self._safe_write_json(self.base_path / "sessions_index.json") as index:
            if session_id in index:
                index[session_id]["updated_at"] = now
                index[session_id]["message_count"] = message_count

        # 再写 session 数据（主数据）
        with self._safe_write_json(session_path) as data:
            if not data:
                return
            data["context"] = context
            data["full_context"] = full_context if full_context is not None else data.get("full_context", [])
            data["updated_at"] = now
    
    def add_message(
        self,
        session_id: str,
        role: str,
        content: str = "",
        msg_type: str = "content",
        tool_call: Optional[Dict] = None,
        tool_call_id: Optional[str] = None,
        reasoning_content: Optional[str] = None
    ) -> int:
        session_path = self._get_session_path(session_id)
        with self._safe_write_json(session_path) as data:
            if not data:
                return -1
            
            msg = {
                "role": role,
                "content": content
            }
            
            if role == "tool":
                if tool_call_id:
                    msg["tool_call_id"] = tool_call_id
            elif role == "assistant":
                if tool_call:
                    msg["tool_calls"] = [tool_call]
                if reasoning_content:
                    msg["reasoning_content"] = reasoning_content
            
            if "context" not in data:
                data["context"] = []
            if "full_context" not in data:
                data["full_context"] = []
            
            data["context"].append(msg)
            data["full_context"].append(dict(msg))
            data["updated_at"] = datetime.now().isoformat()
            
            # 更新索引
            index_data = data
        
        with self._safe_write_json(self.base_path / "sessions_index.json") as index:
            if session_id in index:
                index[session_id]["updated_at"] = index_data["updated_at"]
                index[session_id]["message_count"] = len(index_data.get("full_context") or index_data.get("context", []))
        
        return len(data.get("full_context", data.get("context", [])))
    
    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        return self.get_full_context(session_id)
    
    def get_last_user_message(self, session_id: str) -> Optional[Dict[str, Any]]:
        context = self.get_context(session_id)
        for msg in reversed(context):
            if msg.get("role") == "user":
                return msg
        return None
    
    def clear_messages(self, session_id: str):
        self.save_session_state(session_id, [], [])
    
    def get_latest_session(self) -> Optional[Dict[str, Any]]:
        sessions = self.get_sessions()
        if sessions:
            return self.get_session(sessions[0]["id"])
        return None
